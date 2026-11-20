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
    """从本地 profile 的 Cookies 库里读 cookie 名（不联网，最快）。"""
    import sqlite3
    prof = "dy" if plat == "douyin" else "ks"
    db = _PROFILE_DIR / prof / "Default" / "Cookies"
    names: dict[str, str] = {}
    if not db.exists():
        return names
    try:
        tmp = db.with_suffix(".copy")
        tmp.write_bytes(db.read_bytes())
        con = sqlite3.connect(str(tmp))
        for row in con.execute("SELECT name, value FROM cookies"):
            names[row[0]] = row[1]
        con.close()
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return names


def has_local_auth(plat: str) -> bool:
    """本地 profile 里有没有登录特征 cookie（不判断是否过期）。"""
    marks = AUTH_MARK.get(plat, ())
    got = _cookies_of(plat)
    return any(m in got and got[m] for m in marks)


async def probe_remote(plat: str, keyword: str = "测试") -> tuple[bool, str]:
    """真发一次搜索请求，判断当前登录态/验证态还有效没。

    返回 (是否可用, 说明)
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as hc:
            if plat == "douyin":
                url = ("https://www.douyin.com/aweme/v1/web/search/item/"
                       f"?device_platform=webapp&aid=6383&keyword={quote(keyword)}&count=5")
                r = await hc.get(url, headers={
                    "User-Agent": UA_DESKTOP,
                    "Referer": "https://www.douyin.com/search/" + quote(keyword),
                    "Cookie": _cookie_header(plat),
                })
                j = r.json()
                code = j.get("status_code")
                if code == 2483 or "请先登录" in str(j.get("status_msg", "")):
                    return False, "抖音返回 2483「请先登录」"
                lst = j.get("data", {}).get("aweme_list") or []
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
                    "Cookie": _cookie_header(plat),
                })
                j = r.json()
                errs = j.get("errors")
                if errs:
                    msg = (errs[0] or {}).get("message", "")[:70]
                    return False, f"快手接口报错：{msg}"
                res = (j.get("data") or {}).get("result")
                if res == 400002:
                    return False, "快手要求过人机验证（result=400002，滑块）"
                feeds = (j.get("data") or {}).get("feeds") or []
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

async def prepare(plat: str, timeout: int = 300, open_image: bool = True) -> bool:
    """打开有头浏览器，让用户扫码登录 / 过人机验证，成功后存进本地 profile。

    * 抖音：页面右上角点登录 → 手机抖音 App 扫二维码
    * 快手：搜索页会弹滑块验证，手动拖一下即可（不必登录）
    """
    from playwright.async_api import async_playwright
    prof = _pick_profile("dy" if plat == "douyin" else "ks")
    url = ("https://www.douyin.com/" if plat == "douyin"
           else "https://www.kuaishou.com/search/video?searchKey=%E7%8B%AC%E6%A3%8B")
    shot = Path(__file__).resolve().parent.parent / "profiles" / f"{plat}_login.png"
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
            print(f"  （也可以直接看这个截图文件：{shot}）")
            t0 = time.time()
            shot_done = False
            while time.time() - t0 < timeout:
                await asyncio.sleep(2)
                if has_local_auth(plat):
                    print(f"  ✅ 检测到登录/验证已通过")
                    ok, why = await probe_remote(plat)
                    if ok:
                        return True
                    print(f"  … 还没生效：{why}")
                if not shot_done and open_image:
                    try:
                        await page.screenshot(path=str(shot), full_page=False)
                        shot_done = True
                        if hasattr(shot, "exists"):
                            import os
                            os.startfile(str(shot))   # Windows 打开图片
                            print(f"  📷 已截图并打开：{shot}")
                    except Exception:
                        pass
            print(f"  ⏰ 超时（{timeout}s）未完成")
            return False
        finally:
            await ctx.close()
            _release_profile(prof)
