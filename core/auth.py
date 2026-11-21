"""登录 / 人机验证状态管理。

两平台的门槛不一样（2026-11 实测）：

* **抖音**：搜索接口硬性要求登录
  → `aweme/v1/web/search/item/` 返回 `{"status_code":2483,"status_msg":"请先登录，再继续搜索吧"}`
  → 只能扫码登录，登录态在 cookie `sessionid` / `sessionid_ss`
* **快手**：搜索接口要过人机验证，**不一定要登录**
  → GraphQL `visionSearchPhoto` 返回 `{"result":400002,"url":"https://captcha.zt.kuaishou.com/..."}`
  → 在浏览器里过一次滑块，把 cookie 存下来就行

本模块负责：
1. 判断当前本地 profile 里有没有有效登录态
2. 真的发一次请求探测「是否过期」
3. 过期时弹出浏览器让用户重新扫码 / 过验证
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from engine import (UA_DESKTOP, UA_MOBILE, _PROFILE_DIR, _pick_profile,
                    _release_profile, acquire_profile)

# 判定「已登录」的特征 cookie
AUTH_MARK = {
    "douyin": ("sessionid", "sessionid_ss", "sid_tt", "passport_csrf_token"),
    "kuaishou": ("kuaishou.web_st", "passToken", "kuaishou.server.web_st",
                 "kuaishou.token"),
}

PLAT_NAME = {"douyin": "抖音", "kuaishou": "快手"}


class AuthExpired(Exception):
    """登录态/验证态失效，需要用户重新处理。"""

    def __init__(self, plat: str, detail: str = ""):
        super().__init__(f"{PLAT_NAME.get(plat, plat)} 需要重新登录/验证：{detail}")
        self.plat = plat
        self.detail = detail


# ------------------------------------------------------------------ 状态探测

def _cookies_of(plat: str) -> dict:
    """从本地 profile 的 Cookies 库里读 cookie 名（不联网，最快）。

    ⚠️ 新版 Chrome 的 Cookies 库在 `Default/Network/Cookies`，
       旧版在 `Default/Cookies` —— 两个路径都要试，
       否则永远读到空、判定永远失败。
    """
    import sqlite3
    prof = "dy" if plat == "douyin" else "ks"
    base = _PROFILE_DIR / prof / "Default"
    names: dict[str, str] = {}
    for db in (base / "Network" / "Cookies", base / "Cookies"):
        if not db.exists():
            continue
        try:
            tmp = db.with_suffix(".readcopy")
            tmp.write_bytes(db.read_bytes())
            con = sqlite3.connect(str(tmp))
            for row in con.execute("SELECT name, value FROM cookies"):
                names[row[0]] = row[1]
            con.close()
            tmp.unlink(missing_ok=True)
            if names:
                break
        except Exception:
            pass
    return names


def has_local_auth(plat: str) -> bool:
    """本地 profile 里有没有登录特征 cookie（只判断「存不存在」，不判断是否过期）。

    ⚠️ 只看 cookie【名】不看值：Chrome 在磁盘上把 cookie 值加密了，
       直接读 SQLite 拿不到明文（读出来是空串），
       所以「有没有值」这个判断永远为假 → 状态栏会一直显示「未就绪」。
    真能不能搜，交给浏览器实时 cookie 去探测（prepare / search 里做）。
    """
    marks = AUTH_MARK.get(plat, ())
    got = _cookies_of(plat)
    return any(m in got for m in marks)


def _extract_awemes(j) -> list:
    """从抖音的搜索响应里把视频列表抠出来。

    ⚠️ 抖音的响应形状有三层坑：
       ① `data` 可能是 dict 也可能是 list（登录态下会变）
       ② `aweme_list` 常常是 null，真正的数据在 `data[]` 里
       ③ `data[]` 的每一项是 `{"type":1,"aweme_info":{...}}` 包装
    所以这里：到处找「像 aweme 列表的数组」→ 再把 aweme_info 包装解开。
    """
    def unwrap(lst):
        out = []
        for x in lst:
            if isinstance(x, dict):
                ai = x.get("aweme_info")
                if isinstance(ai, dict):
                    out.append(ai)
                    continue
            out.append(x)
        return out

    def looks(x):
        if not (isinstance(x, list) and x):
            return False
        for i in x[:3]:
            if not isinstance(i, dict):
                return False
            if not ("aweme_id" in i or "aweme_info" in i or "video" in i):
                return False
        return True

    if looks(j):
        return unwrap(j)
    if not isinstance(j, dict):
        return []
    for path in (("data",), ("data", "aweme_list"), ("aweme_list",),
                 ("data", "aweme_details"), ("aweme_details",),
                 ("result", "aweme_list")):
        node = j
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if looks(node):
            return unwrap(node)
    # 兜底：全树扫一遍，找第一个像 aweme 列表的数组
    def scan(x):
        if looks(x):
            return unwrap(x)
        if isinstance(x, dict):
            for v in x.values():
                r = scan(v)
                if r:
                    return r
        elif isinstance(x, list):
            for v in x:
                r = scan(v)
                if r:
                    return r
        return None
    return scan(j) or []


def _extract_feeds(j) -> list:
    """从快手的搜索响应里把视频列表抠出来。

    ★ 快手的形状（实测 2026-11）：
      顶层 {"result":1,"feeds":[{"type":1,"tags":[...],"photo":{...},"author":{...}}]}
      → 真正的视频对象在 feeds[].photo 里（又是包装！）
      → author 在【外层】，得一起带出来
      → 清晰度在 photo.photoUrls[]（各档不同 CDN，体积相同 = 同一档）
         photo.photoH265Urls[] 是 H265 压缩版，【更小】，不要用
    """
    def unwrap(lst):
        out = []
        for x in lst:
            if isinstance(x, dict):
                ph = x.get("photo")
                if isinstance(ph, dict):
                    ph = dict(ph)
                    a = x.get("author")
                    if isinstance(a, dict):
                        ph.setdefault("_author", a)
                    # 评论数可能在外层的 comment 里
                    if x.get("comment") is not None:
                        ph.setdefault("_comment", x.get("comment"))
                    out.append(ph)
                    continue
            out.append(x)
        return out

    def looks(x):
        if not (isinstance(x, list) and x):
            return False
        for i in x[:3]:
            if not isinstance(i, dict):
                return False
            if isinstance(i.get("photo"), dict):
                continue                      # {"type":1,"photo":{...}} 包装
            if not any(k in i for k in ("photoId", "id", "caption", "srcNoMark",
                                        "photoUrls")):
                return False
        return True

    if looks(j):
        return unwrap(j)
    if not isinstance(j, dict):
        return []
    for path in (("data", "feeds"), ("data", "searchPhoto", "feeds"),
                 ("data", "visionSearchPhoto", "feeds"), ("feeds",),
                 ("data", "photo", "feeds")):
        node = j
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if looks(node):
            return unwrap(node)

    def scan(x):
        if looks(x):
            return unwrap(x)
        if isinstance(x, dict):
            for v in x.values():
                r = scan(v)
                if r:
                    return r
        elif isinstance(x, list):
            for v in x:
                r = scan(v)
                if r:
                    return r
        return None
    return scan(j) or []


async def check_auth(plat: str, cookie_header: str = "") -> tuple[bool, str]:
    """只看【登录/验证状态】，不看有没有搜索结果。

    ★ 这是判定登录成功的正确标准：
      抖音：status_code 不再是 2483 = 已登录
            （已登录但搜索无结果 ≠ 没登录！）
      快手：result 不再是 400002 = 过了验证
    """
    import httpx
    ck = cookie_header or _cookie_header(plat)
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as hc:
            if plat == "douyin":
                url = ("https://www.douyin.com/aweme/v1/web/search/item/"
                       f"?device_platform=webapp&aid=6383&keyword=test&count=5")
                r = await hc.get(url, headers={
                    "User-Agent": UA_DESKTOP,
                    "Referer": "https://www.douyin.com/search/test",
                    "Cookie": ck,
                })
                try:
                    j = r.json()
                except Exception:
                    return False, f"响应不是 JSON (HTTP {r.status_code})"
                code = j.get("status_code") if isinstance(j, dict) else None
                msg = str(j.get("status_msg", "")) if isinstance(j, dict) else ""
                if code == 2483 or "请先登录" in msg:
                    return False, "抖音返回 2483「请先登录」"
                return True, f"已登录 (status_code={code})"
            else:
                body = {
                    "operationName": "visionSearchPhoto",
                    "variables": {"keyword": "test", "page": "search", "cursor": ""},
                    "query": ("query visionSearchPhoto($keyword:String,$page:String,$cursor:String){"
                              "visionSearchPhoto(keyword:$keyword,page:$page,cursor:$cursor){"
                              "feeds{photoId caption srcNoMark}}}"),
                }
                r = await hc.post("https://www.kuaishou.com/graphql", json=body, headers={
                    "User-Agent": UA_DESKTOP,
                    "Content-Type": "application/json",
                    "Referer": "https://www.kuaishou.com/search/video?searchKey=test",
                    "Cookie": ck,
                })
                try:
                    j = r.json()
                except Exception:
                    return False, f"响应不是 JSON (HTTP {r.status_code})"
                d = j.get("data") if isinstance(j, dict) else None
                res = d.get("result") if isinstance(d, dict) else None
                if res == 400002:
                    return False, "快手要求过人机验证（result=400002，滑块）"
                return True, f"已过验证 (result={res})"
    except Exception as e:
        return False, f"探测异常：{str(e)[:80]}"


async def probe_remote(plat: str, keyword: str = "测试",
                       cookie_header: str = "") -> tuple[bool, str]:
    """真发一次搜索请求，判断当前登录态/验证态还有效没。

    cookie_header: 传了就用它（浏览器里的实时 cookie），没传就读本地 profile 里的。

    返回 (是否可用, 说明)
    """
    import httpx
    ck = cookie_header or _cookie_header(plat)
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as hc:
            if plat == "douyin":
                url = ("https://www.douyin.com/aweme/v1/web/search/item/"
                       f"?device_platform=webapp&aid=6383&keyword={quote(keyword)}&count=5")
                r = await hc.get(url, headers={
                    "User-Agent": UA_DESKTOP,
                    "Referer": "https://www.douyin.com/search/" + quote(keyword),
                    "Cookie": ck,
                })
                try:
                    j = r.json()
                except Exception:
                    return False, f"抖音响应不是 JSON (HTTP {r.status_code})"
                code = j.get("status_code") if isinstance(j, dict) else None
                if code == 2483 or "请先登录" in str((j or {}).get("status_msg", "")
                                                    if isinstance(j, dict) else ""):
                    return False, "抖音返回 2483「请先登录」"
                lst = _extract_awemes(j)
                if lst:
                    return True, "ok"
                return False, f"抖音搜索无结果 (status_code={code})"
            else:
                body = {
                    "operationName": "visionSearchPhoto",
                    "variables": {"keyword": keyword, "page": "search", "cursor": ""},
                    "query": ("query visionSearchPhoto($keyword:String,$page:String,$cursor:String){"
                              "visionSearchPhoto(keyword:$keyword,page:$page,cursor:$cursor){"
                              "feeds{photoId caption duration photoUrl{url size width height} "
                              "srcNoMark width height author{name}}}}"),
                }
                r = await hc.post("https://www.kuaishou.com/graphql", json=body, headers={
                    "User-Agent": UA_DESKTOP,
                    "Content-Type": "application/json",
                    "Referer": f"https://www.kuaishou.com/search/video?searchKey={quote(keyword)}",
                    "Cookie": ck,
                })
                try:
                    j = r.json()
                except Exception:
                    return False, f"快手响应不是 JSON (HTTP {r.status_code})"
                errs = j.get("errors") if isinstance(j, dict) else None
                if errs:
                    msg = (errs[0] or {}).get("message", "")[:70]
                    return False, f"快手接口报错：{msg}"
                res = (j.get("data") or {}).get("result") if isinstance(j, dict) else None
                if res == 400002:
                    return False, "快手要求过人机验证（result=400002，滑块）"
                feeds = _extract_feeds(j)
                if feeds:
                    return True, "ok"
                return False, f"快手搜索无结果（result={res}）"
    except Exception as e:
        return False, f"探测异常：{str(e)[:80]}"


def _cookie_header(plat: str) -> str:
    """把本地 profile 的 cookie 拼成请求头。

    快手的接口必须带 `did`（设备 id），没有就随机造一个 ——
    这是快手风控的最低门槛，有它才会返回验证码而不是直接报错。
    """
    ck = _cookies_of(plat)
    if plat == "kuaishou" and not ck.get("did"):
        import secrets
        ck["did"] = secrets.token_hex(16)
    if plat == "kuaishou":
        ck.setdefault("kpf", "PC_WEB")
    return "; ".join(f"{k}={v}" for k, v in ck.items() if v)


# ------------------------------------------------------------------ 交互准备

def _home_url(plat: str) -> str:
    """登录页地址。★ 快手用【网页版】（www.kuaishou.com），不是手机版页面。"""
    if plat == "douyin":
        return "https://www.douyin.com/"
    return "https://www.kuaishou.com/"


def _login_url(plat: str) -> str:
    """要弹给人看的页面。

    ★ 快手直接开【搜索页】—— 滑块验证是在发起搜索时才弹的，
      光开首页用户看不到要验证什么。
    """
    if plat == "douyin":
        return "https://www.douyin.com/"
    return "https://www.kuaishou.com/search/video?searchKey=test"


# ------------------------------------------------------------------ 浏览器复用
# ★ 用户诉求：登录/检测都用【他自己正在用的那个浏览器】，
#   而不是程序另开一个（那样登录态两套，用户要重复登录）。
# 优先级：
#   ① CDP 连接【已运行的 Chrome】（有调试端口时）—— 完全复用，零污染
#   ② 用用户 Chrome 的 profile 目录启动（要求 Chrome 没在运行）
#   ③ 兜底：程序自己的独立 profile（跟现在的行为一致）

_CDP_PORTS = (9222, 9223, 9333, 9515)


def _cdp_url() -> str | None:
    """探测本机已运行的 Chrome 调试端口（用户带 --remote-debugging-port 启动的）。"""
    import urllib.request
    for port in _CDP_PORTS:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                if r.status == 200:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return None


# 网页端上报的「用户正在用的浏览器」（前端 navigator.userAgent 解析出来的）
PREFER_BROWSER: dict = {"kind": "", "ua": ""}
# 浏览器使用情况（给界面显示提示用）
BROWSER_HINT: dict = {"kind": "", "profile": "", "cdp": ""}

_BROWSER_PROFILE_DIRS = {
    "chrome": ("Google", "Chrome", "User Data"),
    "edge": ("Microsoft", "Edge", "User Data"),
}


def _user_browser_profile(kind: str = "") -> Path | None:
    """用户自己的 Chrome / Edge 用户数据目录（用它就等于用他日常的浏览器登录态）。

    kind: "chrome" / "edge" / ""（自动挑一个存在的）
    """
    local = os.environ.get("LOCALAPPDATA") or ""
    if not local:
        return None
    order = [kind] if kind in _BROWSER_PROFILE_DIRS else list(_BROWSER_PROFILE_DIRS)
    for k in order:
        parts = _BROWSER_PROFILE_DIRS[k]
        d = Path(local).joinpath(*parts)
        if d.exists():
            return d
    return None


def _chrome_running() -> bool:
    """Chrome/Edge 是否正在运行（运行中就不能直接开它的 profile）。"""
    import subprocess
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                           capture_output=True, timeout=10,
                           encoding="utf-8", errors="replace")
        if "chrome.exe" in (r.stdout or "").lower():
            return True
        r2 = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe"],
                            capture_output=True, timeout=10,
                            encoding="utf-8", errors="replace")
        return "msedge.exe" in (r2.stdout or "").lower()
    except Exception:
        return False


class BrowserSession:
    """一次浏览器会话（可能是 CDP 连上的用户浏览器，也可能是程序开的独立浏览器）。"""

    def __init__(self, ctx, browser=None, owned: bool = True, how: str = "",
                 profile: str = ""):
        self.ctx = ctx
        self.browser = browser
        self.owned = owned          # owned=True → 关闭时要真的关掉
        self.how = how              # 说明用的哪种方式
        self.profile = profile      # 实际用的 profile 名（释放时要准）
        self.opened: list = []      # 我们自己开的标签页（CDP 模式只关这些）

    async def new_page(self):
        """开一个标签页（CDP 模式下会被记下来，关闭时只关我们开的）。"""
        pg = await self.ctx.new_page()
        self.opened.append(pg)
        return pg

    async def close(self):
        try:
            if self.owned:
                await self.ctx.close()
            else:
                # CDP 接来的：只关我们自己开的标签页，不动用户的浏览器
                for pg in list(self.ctx.pages):
                    pass
                if self.browser:
                    await self.browser.close()
        except Exception:
            pass


async def open_browser(pw, plat: str, headless: bool = True,
                       prefer_user: bool = True,
                       prefer_browser: str = "",
                       profile: str = "") -> BrowserSession:
    """开一个浏览器会话。

    ★ prefer_user=True 时优先复用用户自己的浏览器（CDP → 已连接就同标签页）。
    ★ profile：指定就用指定的（登录/探测/搜索必须是【同一个】profile，
      因为登录态存在主 profile 里）。
    """
    # ★ profile 由调用方指定就用指定的；没指定才自己分配。
    #   （登录态存在【主 profile】里，所以登录/探测/搜索必须是同一个！）
    prof = profile or _pick_profile("dy" if plat == "douyin" else "ks")
    # ① CDP：连到用户正在跑的 Chrome（登录态直接共享）
    if prefer_user:
        cdp = _cdp_url()
        BROWSER_HINT["cdp"] = cdp or ""
        BROWSER_HINT["kind"] = ""
        if cdp:
            try:
                browser = await asyncio.wait_for(
                    pw.chromium.connect_over_cdp(cdp), timeout=15)
                ctx = browser.contexts[0] if browser.contexts else \
                    await browser.new_context()
                return BrowserSession(ctx, browser, owned=False,
                                      how=f"已连接你正在运行的 Chrome（{cdp}）")
            except Exception:
                pass
        # ② 用用户自己的浏览器 profile（要求那个浏览器没在运行 —— 开着会锁 profile）
        up = _user_browser_profile(prefer_browser)
        # ② ★★★ 用户自己的浏览器 profile：【已废弃，不要用】
        #   实测 2026-11-21：用用户的 Chrome profile 启动 playwright
        #   90 秒都起不来（profile 被锁 / 首次启动要迁移），
        #   会把整个界面卡在「检测中…」几十分钟。
        #   而且它返回的会话没带 profile 名 → 主 profile 永远不释放
        #   → 搜索拿不到有登录态的 profile → 一直转圈。
        #   ★ 想复用同一个浏览器，唯一可靠的办法是【CDP 连接】（上面 ①）：
        #     用 chrome.exe --remote-debugging-port=9222 启动浏览器，
        #     程序直接连进去在他窗口里开标签页。
        up = _user_browser_profile(prefer_browser)
        if up:
            BROWSER_HINT["kind"] = "profile-unavailable"
            BROWSER_HINT["profile"] = str(up)
    # ③ 兜底：程序自己的独立 profile
    try:
        ctx = await asyncio.wait_for(
            _open_ctx(pw, plat, prof, headless=headless), timeout=45)
    except asyncio.TimeoutError:
        raise RuntimeError("浏览器启动超时（45 秒）") from None
    return BrowserSession(ctx, None, owned=True, how="程序独立浏览器",
                          profile=prof)


async def _open_ctx(pw, plat: str, prof: str, headless: bool):
    """按平台建浏览器上下文（抖音桌面 UA，快手也用桌面 UA 走网页版）。"""
    if plat == "douyin":
        return await pw.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR / prof), headless=headless, channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1440, "height": 900}, locale="zh-CN", user_agent=UA_DESKTOP)
    return await pw.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR / prof), headless=headless, channel="chrome",
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        viewport={"width": 1440, "height": 900}, locale="zh-CN", user_agent=UA_DESKTOP)


# 浏览器探测的串行锁 + 短缓存
# ★ 为什么必须串行：两个浏览器同时开同一个 profile → 后启动的那个
#   因 profile 被占用而失败 → 状态栏和下载判定各开一个就会撞，
#   表现为「状态栏说可用、下载却提示待登录」。
# ★ 为什么还要缓存：状态栏刷新、下载前判定、prepare 三处都要查，
#   每次都开浏览器太慢；30 秒内共用同一份结果，三处结论必然一致。
# ★ 必须是 RLock（可重入）：下面在已持有锁的情况下还会 `with _probe_lock:`
#   读写缓存 —— 用普通 Lock 会自死锁（探测永远卡住，Chrome 进程堆积）。
_probe_lock = threading.RLock()
_probe_cache: dict = {}
PROBE_CACHE_TTL = 30.0


async def check_auth_in_browser(plat: str, use_cache: bool = True) -> tuple[bool, str]:
    """用【浏览器里的真实 cookie】检查登录/验证状态。

    ★ 为什么必须走浏览器：Chrome 在磁盘上把 cookie【值】加密了，
      直接读 SQLite 拿到的 sessionid 是空串 → 请求发出去还是 2483，
      状态栏就会误报「未就绪」（实测登录了也报没登录）。
      浏览器能自动解密，拿到的是真实会话。
    """
    if use_cache:
        with _probe_lock:
            hit = _probe_cache.get(plat)
        if hit and (time.time() - hit[0]) < PROBE_CACHE_TTL:
            return hit[1], hit[2]
    # ★ 用 acquire(timeout=) 而不是 `with` —— 后者在协程里会阻塞事件循环线程，
    #   多个线程各带自己的事件循环时会死锁。
    if not _probe_lock.acquire(timeout=90):
        return False, "另一个检测还在跑，请稍候再试"
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            # ★ 用【用户自己的浏览器】（CDP 或他的 profile）→ 登录态天然一致
            sess = await open_browser(pw, plat, headless=True, prefer_user=True,
                                      prefer_browser=PREFER_BROWSER.get("kind", ""))
            ctx = sess.ctx
            try:
                # ★ 不必加载页面：cookie 就在浏览器里，启动完直接拿。
                #   （原来 goto 首页 + 等 4 秒要 15~30 秒，状态栏转圈半天）
                ck = {c["name"]: c["value"] for c in await ctx.cookies()}
                header = "; ".join(f"{k}={v}" for k, v in ck.items() if v)
                ok, why = await check_auth(plat, cookie_header=header)
                with _probe_lock:
                    _probe_cache[plat] = (time.time(), ok, why)
                return ok, why
            except Exception as e:
                # ★ 探测失败（比如别的浏览器正占着 profile）时，
                #   宁可给一个【旧的结论】，也不要误报成「未就绪」
                with _probe_lock:
                    old = _probe_cache.get(plat)
                if old:
                    return old[1], old[2]
                msg = str(e)
                if "user data directory is already in use" in msg or                         "ProcessSingleton" in msg or "SingletonLock" in msg:
                    return False, ("profile 被占用：还有残留的浏览器进程没关"
                                   "（任务管理器里结束 chrome.exe 再试）")
                return False, f"浏览器探测异常：{msg[:80]}"
            finally:
                # ★ 用【实际用的】profile 名释放。
                #   （之前误用 _pick_profile() 再取一次 —— 那是「分配」，
                #     会把主 profile 又占走 → 搜索只能拿没登录态的备用 → 一直转圈）
                prof_name = sess.profile
                await sess.close()
                if prof_name:
                    try:
                        from engine import wait_profile_free
                        wait_profile_free(prof_name, timeout=6)
                    except Exception:
                        pass
                    _release_profile(prof_name)
    finally:
        _probe_lock.release()


async def prepare(plat: str, timeout: int = 300, keyword: str = "测试") -> bool:
    """确保该平台处于【已登录 / 已过验证】状态。

    流程：
      ① 先用无头浏览器快速探一下（拿浏览器里的实时 cookie）
         → 已经登录？直接返回 True，【根本不弹浏览器】
      ② 没登录才开有头窗口让用户扫码 / 过滑块
         → 后台盯着接口，一旦通过就【自动关掉窗口】

    ★ 判定标准是 check_auth（不再返回 2483 / 400002），
      不是「必须搜到结果」—— 已登录但某关键词没结果 ≠ 没登录。
    """
    from playwright.async_api import async_playwright

    # ── ① 先快速检查（无头，不打扰用户）──
    # ★ 这里【不要】先调 _pick_profile —— 那是「分配」，会把主 profile 占走，
    #   导致探测只能用没登录态的备用 profile（登录窗口秒关的元凶）。
    ok, why = await check_auth_in_browser(plat)
    if ok:
        print(f"  ✅ {PLAT_NAME[plat]} 已登录，无需重复登录（{why}）")
        return True

    # ── ② 没登录 → 弹有头窗口 ──
    # ★ 关键：必须用【主 profile】（登录态存在那儿），
    #   所以先等它空出来，而不是随便拿个没登录态的备用 profile。
    base = "dy" if plat == "douyin" else "ks"
    prof = acquire_profile(base, wait=15)
    async with async_playwright() as pw:
        sess = await open_browser(pw, plat, headless=False, prefer_user=True,
                                   prefer_browser=PREFER_BROWSER.get("kind", ""),
                                   profile=prof)
        ctx = sess.ctx
        try:
            page = await sess.new_page()
            try:
                await page.goto(_login_url(plat), wait_until="domcontentloaded",
                                timeout=60000)
            except Exception as ge:
                # ★ 页面没打开也【不要立刻把窗口关了】—— 让用户看得见窗口，
                #   自己刷新一下就能继续（否则表现为「弹窗一瞬间就没了」）
                print(f"  ⚠ 页面加载慢/失败：{str(ge)[:80]}")
                print("    窗口保留着，你可以在窗口里手动刷新页面。")
            print(f"\n▶ 已打开【{PLAT_NAME[plat]}】网页版：{_login_url(plat)}")
            print(f"  用的浏览器：{sess.how}")
            if plat == "douyin":
                print("  点右上角「登录」→ 用手机抖音 App 扫二维码")
            else:
                print("  页面若弹出滑块验证，手动拖一下通过即可（不必登录账号）")
            print("  ★ 我在后台盯着接口，一检测到通过就【自动关掉这个窗口】。")
            t0 = time.time()
            last = ""
            while time.time() - t0 < timeout:
                await asyncio.sleep(2)
                try:
                    cookies = await ctx.cookies()
                except Exception:
                    break                      # 用户自己关了窗口
                ck = {c["name"]: c["value"] for c in cookies}
                header = "; ".join(f"{k}={v}" for k, v in ck.items() if v)
                ok, why = await check_auth(plat, cookie_header=header)
                if ok:
                    print("  ✅ 检测到登录/验证已通过，正在关闭窗口…")
                    await asyncio.sleep(0.5)
                    return True
                if why != last:
                    last = why
                    print(f"  … 还没通过：{why}")
            print(f"  ⏰ 超时（{timeout}s）未完成")
            return False
        finally:
            await sess.close()
            _release_profile(prof)
