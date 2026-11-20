"""关键词搜索（浏览器驱动 + 拦截搜索 XHR）。

为什么用浏览器而不是直接调接口：
* 抖音的搜索接口要 `a_bogus` 签名（浏览器会自己算）
* 快手的风控参数 `__NS_hxfalcon` 同理
所以最稳的做法就是：让真实浏览器去搜，我们把它的响应拦下来。

拦截到之后：
* 按【体积最大】挑清晰度（社区实测 url_list[0] 顺序不固定）
* 遇到登录墙 / 人机验证 → 抛 AuthExpired，交给上层弹浏览器让用户处理
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import quote

from auth import AuthExpired, _extract_awemes, _extract_feeds
from engine import (UA_DESKTOP, UA_MOBILE, _PROFILE_DIR, _pick_profile,
                    _release_profile)

PLAT_NAME = {"douyin": "抖音", "kuaishou": "快手"}


@dataclass
class Item:
    platform: str = ""
    video_id: str = ""
    desc: str = ""
    author: str = ""
    cover: str = ""
    width: int = 0
    height: int = 0
    duration: int = 0
    play_url: str = ""
    bytes: int = 0
    title: str = ""

    @property
    def portrait(self) -> bool:
        return self.height > self.width

    @property
    def size_mb(self) -> float:
        return self.bytes / 1048576


def clean_name(desc: str, vid: str) -> str:
    import re
    t = (desc or "").split("#")[0].strip()
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", t).strip(" ._")
    return (t or vid or "video")[:60]


def _pick_best_douyin(v: dict) -> tuple[str, int, int, int]:
    """挑【体积最大】的那一档。download_addr 是带水印的裁剪版，不参与比较。"""
    cands, seen = [], set()

    def add(pa):
        if not pa:
            return
        urls = pa.get("url_list") or []
        if not urls:
            return
        u = urls[0]
        if not u or u in seen:
            return
        seen.add(u)
        cands.append((pa.get("data_size") or 0,
                      (pa.get("width") or 0) * (pa.get("height") or 0),
                      u, pa.get("width") or 0, pa.get("height") or 0))

    add(v.get("play_addr"))
    add(v.get("play_addr_h264"))
    add(v.get("play_addr_265"))
    for b in (v.get("bit_rate") or []):
        add(b.get("play_addr"))
    if not cands:
        return "", 0, 0, 0
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    b = cands[0]
    return b[2], b[0], b[3], b[4]


def _pick_best_kuaishou(ph: dict) -> tuple[str, int]:
    best, size = "", 0
    for pu in (ph.get("photoUrl") or []):
        u, sz = pu.get("url") or "", pu.get("size") or 0
        if u and sz > size:
            best, size = u, sz
    if not best:
        best = ph.get("srcNoMark") or ph.get("photo") or ""
        size = ph.get("photoSize") or 0
    return best, size


async def _search_douyin(ctx, keyword: str, limit: int) -> list[Item]:
    items: list[Item] = []
    page = await ctx.new_page()
    buckets: list[list] = []
    need_login = False

    async def on_resp(r):
        nonlocal need_login
        u = r.url
        if "aweme/v1/web" not in u or "/search/" not in u:
            return
        try:
            j = await r.json()
        except Exception:
            return
        if j.get("status_code") == 2483 or "请先登录" in str(j.get("status_msg", "")):
            need_login = True
            return
        d = j.get("data") or {}
        lst = d.get("aweme_list") or d.get("aweme_details") or [] if isinstance(d, dict) else []
        if not lst:
            # ⚠️ 登录态下 data 可能直接是 list —— 用健壮提取器兜住
            lst = _extract_awemes(j)
        if lst:
            buckets.append(lst)

    page.on("response", lambda r: asyncio.create_task(on_resp(r)))
    await page.goto(f"https://www.douyin.com/search/{quote(keyword)}?type=video",
                    wait_until="domcontentloaded", timeout=45000)
    # ★ 抖音搜索：第一页常常 aweme_list=null，真正的数据在【翻页请求】里
    #   → 必须等够时间 + 多滚几次，让它把 offset=16/26 的分页也发出来
    await page.wait_for_timeout(9000)
    for i in range(8):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(2600)
        if sum(len(b) for b in buckets) >= limit:
            break
    if not buckets:
        # 再来一轮：有时第一下滚动没生效
        for i in range(4):
            await page.mouse.wheel(0, 3500)
            await page.wait_for_timeout(2800)
            if sum(len(b) for b in buckets) >= limit:
                break
    await page.close()
    if need_login and not buckets:
        raise AuthExpired("douyin", "抖音搜索返回 2483「请先登录」")

    seen = set()
    for lst in buckets:
        for a in lst:
            vid = str(a.get("aweme_id") or "")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            v = a.get("video") or {}
            url, size, w, h = _pick_best_douyin(v)
            if not url:
                continue
            items.append(Item(
                platform="douyin", video_id=vid,
                desc=(a.get("desc") or "").strip(),
                author=((a.get("author") or {}).get("nickname") or "").strip(),
                cover=(((v.get("cover") or {}).get("url_list") or [""])[0]),
                width=w or (v.get("width") or 0), height=h or (v.get("height") or 0),
                duration=v.get("duration") or 0,
                play_url=url, bytes=size,
                title=clean_name(a.get("desc") or "", vid)))
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return items


async def _search_kuaishou(ctx, keyword: str, limit: int) -> list[Item]:
    items: list[Item] = []
    page = await ctx.new_page()
    buckets: list[list] = []
    need_verify = False

    async def on_resp(r):
        nonlocal need_verify
        u = r.url
        if "graphql" not in u and "/search/" not in u:
            return
        try:
            j = await r.json()
        except Exception:
            return
        res = j.get("result") if isinstance(j, dict) else None
        if res is None and isinstance(j, dict):
            d = j.get("data")
            res = d.get("result") if isinstance(d, dict) else None
        if res == 400002:
            need_verify = True
            return
        # ⚠️ data 可能是 dict 也可能是 list，用健壮提取器
        feeds = _extract_feeds(j)
        if feeds:
            buckets.append(feeds)

    page.on("response", lambda r: asyncio.create_task(on_resp(r)))
    await page.goto(f"https://www.kuaishou.com/search/video?searchKey={quote(keyword)}",
                    wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(8000)
    for i in range(8):
        await page.mouse.wheel(0, 2600)
        await page.wait_for_timeout(2400)
        if sum(len(b) for b in buckets) >= limit:
            break
    await page.close()
    if need_verify and not buckets:
        raise AuthExpired("kuaishou", "快手要求过人机验证（result=400002）")

    seen = set()
    for lst in buckets:
        for ph in lst:
            pid = str(ph.get("photoId") or ph.get("id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            url, size = _pick_best_kuaishou(ph)
            if not url:
                continue
            items.append(Item(
                platform="kuaishou", video_id=pid,
                desc=(ph.get("caption") or "").strip(),
                author=str(ph.get("userName") or
                           (ph.get("author") or {}).get("name") or "").strip(),
                cover=((ph.get("coverUrls") or [{}])[0] or {}).get("url", ""),
                width=ph.get("width") or 0, height=ph.get("height") or 0,
                duration=ph.get("duration") or 0,
                play_url=url, bytes=size,
                title=clean_name(ph.get("caption") or "", pid)))
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return items


async def search(plat: str, keyword: str, limit: int = 30,
                 headless: bool = True, retries: int = 2) -> list[Item]:
    """搜索一个平台。登录态/验证态失效时抛 AuthExpired。

    ★ 会自动重试：抖音的搜索请求在无头浏览器下【触发不稳定】
      （有请求但 aweme_list=null，或干脆不发），
      重开一次浏览器 + 拉长等待往往就能拿到。
    """
    from playwright.async_api import async_playwright
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = await _search_once(plat, keyword, limit, headless)
            if r:
                return r
            if attempt < retries:
                await asyncio.sleep(4 + attempt * 3)
        except AuthExpired:
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(4 + attempt * 3)
    if last_exc:
        raise last_exc
    return []


async def _search_once(plat: str, keyword: str, limit: int,
                        headless: bool) -> list[Item]:
    from playwright.async_api import async_playwright
    prof = _pick_profile("dy" if plat == "douyin" else "ks")
    try:
        async with async_playwright() as pw:
            if plat == "douyin":
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(_PROFILE_DIR / prof), headless=headless,
                    channel="chrome",
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                    viewport={"width": 1440, "height": 900}, locale="zh-CN",
                    user_agent=UA_DESKTOP)
            else:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(_PROFILE_DIR / prof), headless=headless,
                    channel="chrome",
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                    viewport={"width": 390, "height": 844}, locale="zh-CN",
                    user_agent=UA_MOBILE, is_mobile=True, has_touch=True)
            try:
                if plat == "douyin":
                    return await _search_douyin(ctx, keyword, limit)
                return await _search_kuaishou(ctx, keyword, limit)
            finally:
                await ctx.close()
    finally:
        _release_profile(prof)
