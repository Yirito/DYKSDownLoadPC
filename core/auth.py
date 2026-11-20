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
import time
from pathlib import Path
from urllib.parse import quote

from engine import (UA_DESKTOP, UA_MOBILE, _PROFILE_DIR, _pick_profile,
                    _release_profile)

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
    """从快手的 GraphQL 响应里把 feeds 抠出来（data 可能是 dict 也可能是 list）。"""
    def looks(x):
        return (isinstance(x, list) and x and isinstance(x[0], dict)
                and ("photoId" in x[0] or "caption" in x[0] or "srcNoMark" in x[0]))

    if looks(j):
        return j
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
            return node

    def scan(x):
        if looks(x):
            return x
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

async def prepare(plat: str, timeout: int = 300, keyword: str = "测试") -> bool:
    """打开有头浏览器，让用户扫码登录 / 过人机验证。

    ★ 判定方式：**拿浏览器里的实时 cookie 去真发一次搜索请求**，
      接口返回正常才算成功 —— 不截图、不猜、不看磁盘上的旧 cookie。
      一旦检测到成功，**自动把浏览器窗口关掉**。

    * 抖音：页面右上角点登录 → 手机抖音 App 扫二维码
    * 快手：搜索页会弹滑块验证，手动拖一下通过即可（不必登录）
    """
    from playwright.async_api import async_playwright
    prof = _pick_profile("dy" if plat == "douyin" else "ks")
    url = ("https://www.douyin.com/" if plat == "douyin"
           else "https://www.kuaishou.com/search/video?searchKey=%E7%8B%AC%E6%A3%8B")
    async with async_playwright() as pw:
        if plat == "douyin":
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(_PROFILE_DIR / prof), headless=False, channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 1440, "height": 900}, locale="zh-CN", user_agent=UA_DESKTOP)
        else:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(_PROFILE_DIR / prof), headless=False, channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 390, "height": 844}, locale="zh-CN",
                user_agent=UA_MOBILE, is_mobile=True, has_touch=True)
        try:
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"\n▶ 已打开【{PLAT_NAME[plat]}】：{url}")
            if plat == "douyin":
                print("  点右上角「登录」→ 用手机抖音 App 扫二维码")
            else:
                print("  页面若弹出滑块验证，手动拖一下通过即可（不必登录）")
            print("  ★ 我在后台盯着接口，一检测到成功就【自动关掉这个窗口】，你不用管。")
            t0 = time.time()
            last = ""
            while time.time() - t0 < timeout:
                await asyncio.sleep(2)
                # ① 拿浏览器里的【实时】cookie
                try:
                    cookies = await ctx.cookies()
                except Exception:
                    break                      # 用户自己把窗口关了
                ck = {c["name"]: c["value"] for c in cookies}
                header = "; ".join(f"{k}={v}" for k, v in ck.items() if v)
                # ② 真发一次搜索请求，接口认了才算成功
                ok, why = await probe_remote(plat, keyword, cookie_header=header)
                if ok:
                    print("  ✅ 检测到登录/验证已通过，正在关闭窗口…")
                    await asyncio.sleep(0.6)
                    return True
                if why != last:
                    last = why
                    print(f"  … 还没通过：{why}")
            print(f"  ⏰ 超时（{timeout}s）未完成")
            return False
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            _release_profile(prof)
