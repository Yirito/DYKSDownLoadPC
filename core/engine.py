"""
无登录去水印引擎 —— 双平台纯 HTTP 直链解析

═══════════════════════════════════════════════════════════════════
重大发现（2026-11-19 实测）：抖音和快手【都不需要登录账号】就能拿到无水印直链
═══════════════════════════════════════════════════════════════════

抖音（桌面 UA 即可）：
  1. 打开 https://www.douyin.com/video/{id}（匿名也能打开）
  2. 页面内 fetch('/aweme/v1/web/aweme/detail/?...&aweme_id={id}')
  3. 响应 JSON 里 aweme_detail.video.play_addr.url_list[0]
     —— 这就是【无水印】地址（与 wm_play_addr 不同）
  4. 该直链可用 httpx 裸连下载（不需要 Referer）✅ 实测 3.3MB

快手（必须移动端 UA）：
  1. 打开 https://v.m.chenzhongtech.com/fw/photo/{id}
     （或 https://m.gifshow.com/fw/photo/{id}）
  2. 页面自动加载视频 → 拦截 .mp4 响应即可拿到直链
     域名形如 k0u75y28y77yfzw240ex960x200x1xxfz.djvod.ndcimgs.com/...
  3. 该直链可用 httpx 裸连下载（不需要 Referer）✅ 实测 10MB

⚠️ 为什么之前必须登录？
  ① 之前走的是【搜索】接口 —— 搜索强制登录（这是平台策略）
  ② 之前快手用【桌面 UA + 作品页】—— 桌面 UA 触发 result:2 反爬
  ③ 之前快手用 GraphQL visionVideoDetail —— 无登录态触发 result:400002 滑块验证
  ⇒ 换成「移动端 UA + 移动端分享页」，完全绕开这两道墙

⚠️ GraphQL 的正确用法（供未来参考）：
   operationName: visionVideoDetail
   query: query visionVideoDetail($photoId: String, $type: String,
         $page: String, $webPageArea: String) { visionVideoDetail(...) {
         photo { photoUrl mainMvUrls { url } } } }
   —— 但无登录态会触发验证码，优先级低于移动端页面方案

本模块的价值：让【服务器】能独立完成下载，不需要登录用户账号。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from pathlib import Path

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/16.6 Mobile/15E148 Safari/604.1")

# ★ 浏览器 profile 放在部署目录【之外】，绝不会被上传到公网沙箱
_PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"
# 正在使用的 profile 名（Chrome 的 user_data_dir 同时只能被一个实例打开）
_profiles_in_use = set()


def _profile_busy(prof: str) -> bool:
    """这个 profile 是不是正被 Chrome 占着。

    ★ Chrome 在 user_data_dir 里放 SingletonLock 锁文件，
      进程没退干净时锁文件还在 → 新的 launch 会卡死/失败。
      实测：残留 11 个 chrome.exe → 搜索拿到被占的备用 profile
      → 一直转圈 120 秒。所以必须看【真实锁状态】，不能只看内存集合。
    """
    d = _PROFILE_DIR / prof
    if not d.exists():
        return False
    return any((d / n).exists() for n in
               ("SingletonLock", "SingletonCookie", "SingletonSocket"))


def _pick_profile(base: str) -> str:
    """挑一个没被占用的 profile 名；全占了就再退一档。

    ★ 为什么要这个：Chrome 的 user_data_dir 同时只能被一个实例打开，
      被占用时 launch 会直接失败。以前的做法是【排队等】，
      结果用户的解析被拖慢十几秒（表现为「点 ✕ 后再解析变慢」）。
      现在改成【换用备用 profile】→ 永不排队。
    ★ ★ 再加一层：【真实锁文件检查】—— 残留的 chrome.exe 会留下
      SingletonLock，光看内存集合不够（残留进程是另一个进程留下的）。
    """
    for cand in (base, base + "_alt", base + "_alt2"):
        if cand not in _profiles_in_use and not _profile_busy(cand):
            _profiles_in_use.add(cand)
            return cand
    for cand in (base, base + "_alt", base + "_alt2"):
        if cand not in _profiles_in_use:
            _profiles_in_use.add(cand)
            return cand
    cand = base + "_alt2"
    _profiles_in_use.add(cand)
    return cand


def _release_profile(name: str) -> None:
    _profiles_in_use.discard(name)


def acquire_profile(base: str, wait: float = 10.0) -> str:
    """申请一个可用的 profile（优先等【主 profile】空出来）。

    ★ 为什么不能直接用 _pick_profile：它每次调用都会【占用】一个 profile，
      在候选循环里反复调用会自己把自己挤到第三档备用 profile
      （实测搜索因此拿到没登录态的 dy_alt2 → 一直搜不到东西）。
    ★ 逻辑：主 profile 只是被【别人正在用】时，等它一会儿（最多 wait 秒）；
      真的被残留进程锁住（真实锁文件）才换备用。
    """
    import time as _t
    t0 = _t.time()
    cands = [base, base + "_alt", base + "_alt2"]
    while _t.time() - t0 < wait:
        for c in cands:
            if c not in _profiles_in_use and not _profile_busy(c):
                _profiles_in_use.add(c)
                return c
        _t.sleep(0.5)
    # 等不到就按老规矩分配（_pick_profile 本身不会再多占用）
    return _pick_profile(base)


def wait_profile_free(name: str, timeout: float = 12.0) -> bool:
    """等 profile 的【真实锁】消失（Chrome 进程退干净了才算释放）。

    ★ 为什么必须等：ctx.close() 只是关窗口，Chrome 进程还要几秒才退出，
      这期间 SingletonLock 还在 → 下一个任务拿到这个 profile 会启动失败/卡死。
      实测：探测刚结束就搜索 → 搜索改用没有登录态的备用 profile
      → 反复超时重试 3 轮 = 界面「一直转圈」100 多秒。
    """
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        if not _profile_busy(name):
            return True
        _t.sleep(0.5)
    return not _profile_busy(name)

# ★ UA 池：每次解析随机换一个，降低被风控按指纹命中的概率
UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/15.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; PDSM00) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/14.8 Mobile/15E148 Safari/604.1",
]
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

# 快手 CDN 域名（实测直链常在 ndcimgs.com）
KS_CDN = ("kwaicdn", "kwai", "ndcimgs", "djvod", "kwaixcdn")


@dataclass
class ParsedVideo:
    ok: bool = False
    platform: str = ""
    video_id: str = ""
    play_url: str = ""
    desc: str = ""
    author: str = ""
    size: int = 0
    method: str = ""          # 用了哪条路
    error: str = ""
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 提取 ID


# 平台域名
DY_HOSTS = ("douyin.com", "iesdouyin.com")
KS_HOSTS = ("kuaishou.com", "gifshow.com", "chenzhongtech.com", "kwai.com")


def platform_of(url: str) -> str:
    """判断 URL 属于哪个平台。"""
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        h = ""
    if any(h == d or h.endswith("." + d) for d in DY_HOSTS):
        return "douyin"
    if any(h == d or h.endswith("." + d) for d in KS_HOSTS):
        return "kuaishou"
    return ""


def extract_urls(text: str) -> list[str]:
    """从分享文本里提取所有 URL。

    抖音/快手的分享文本形如：
      "7.98 复制打开抖音，看看【xxx】标题... https://v.douyin.com/iRNBho6G/ 复制此链接..."
    """
    raw = text or ""
    # 先按空白/中文标点切
    hits = re.findall(r"https?://[^\s，。！？、；：）】》”\'\"，,]+", raw)
    out = []
    for h in hits:
        h = h.rstrip(".,;!?)]}'\"")
        if h and h not in out:
            out.append(h)
    return out


def extract_id(url: str) -> tuple[str, str]:
    """从【单个 URL】里提取作品 ID。返回 (platform, video_id)。

    覆盖：
      抖音  www.douyin.com/video/<id>  |  /note/<id>  |  ?modal_id=<id>
            iesdouyin.com/share/video/<id>
            iesdouyin.com/?schema_type=20&object_id=<id>   ← 短链中间跳
      快手  www.kuaishou.com/short-video/<id>  |  /f/<id>
            ?photoId=<id>  |  live.kuaishou.com/u/xx/<id>
            v.m.chenzhongtech.com/fw/photo/<id>
    """
    u = (url or "").strip()
    pf = platform_of(u)

    # 抖音：纯数字 ID
    m = re.search(r"/(?:video|note)/(\d{10,})", u)
    if m:
        return "douyin", m.group(1)
    m = re.search(r"/share/video/(\d{10,})", u)
    if m:
        return "douyin", m.group(1)
    m = re.search(r"[?&]modal_id=(\d{10,})", u)
    if m:
        return "douyin", m.group(1)
    m = re.search(r"[?&]aweme_id=(\d{10,})", u)
    if m:
        return "douyin", m.group(1)
    # ★ 抖音短链中间跳：object_id 里就是作品 ID
    #   实际格式是 object_id=0_<ID>_<ID>_0 —— 要取中间那段纯数字
    m = re.search(r"[?&]object_id=(?:0_)?(\d{15,})", u)
    if m:
        return "douyin", m.group(1)
    m = re.search(r"[?&]object_id=.*?(\d{15,})", u)
    if m:
        return "douyin", m.group(1)

    # 快手：短 ID（字母数字混合）
    m = re.search(r"/short-video/([A-Za-z0-9_-]{6,})", u)
    if m:
        return "kuaishou", m.group(1)
    m = re.search(r"[?&]photoId=([A-Za-z0-9_-]{6,})", u)
    if m:
        return "kuaishou", m.group(1)
    m = re.search(r"live\.kuaishou\.com/u/[^/]+/([A-Za-z0-9_-]{6,})", u)
    if m:
        return "kuaishou", m.group(1)
    m = re.search(r"/fw/photo/([A-Za-z0-9_-]{6,})", u)
    if m:
        return "kuaishou", m.group(1)
    m = re.search(r"kuaishou\.com/f/([A-Za-z0-9_-]{6,})", u)
    if m:
        return "kuaishou", m.group(1)

    # 短链（还没展开）：先返回短码，展开后再解析
    m = re.search(r"v\.douyin\.com/([A-Za-z0-9_-]{4,})", u)
    if m:
        return "douyin", m.group(1)
    m = re.search(r"v\.kuaishou\.com/([A-Za-z0-9_-]{4,})", u)
    if m:
        return "kuaishou", m.group(1)
    m = re.search(r"kuaishou\.com/f/([A-Za-z0-9_-]{4,})", u)
    if m:
        return "kuaishou", m.group(1)

    return pf, ""


async def _expand_short(url: str) -> str:
    """展开短链 —— 手动跟整条 302 链。

    ⚠️ 必须手动跟（redirect="manual"）：
       httpx 的 follow_redirects 只给最终 URL，而抖音短链的中间跳
       （iesdouyin.com/?schema_type=20&object_id=<作品ID>）才带 ID。
    """
    import httpx
    chain = [url]
    cur = url
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                     headers={"User-Agent": UA_MOBILE,
                                              "Accept": "text/html,*/*"}) as c:
            for _ in range(10):
                r = await c.get(cur)
                loc = r.headers.get("location")
                if not loc:
                    break
                nxt = str(httpx.URL(cur).join(loc))
                chain.append(nxt)
                cur = nxt
    except Exception:
        pass
    # 在整条链里找能解析出作品 ID 的那一跳
    for u in reversed(chain):
        pf, vid = extract_id(u)
        if vid and not (pf and _is_short_code(vid)):
            return u
    # 链里没有明确的 ID，但 object_id 那种已在 extract_id 覆盖
    for u in chain:
        pf, vid = extract_id(u)
        if vid:
            return u
    return chain[-1] if chain else url


def _is_short_code(vid: str) -> bool:
    """判断是不是【短链短码】（不是真正的作品 ID）。

    · 抖音作品 ID = 纯数字且 ≥10 位 → 不是短码
    · 快手作品 ID = 形如 3xvv4r69s8yngni（3x 开头，长度 ≥10）→ 不是短码
    · 快手短链短码 = ng9eGTEH 这类（无 3x 前缀，长度 6-9）→ 是短码
    """
    if not vid:
        return True
    if vid.isdigit():
        return len(vid) < 10
    # 快手作品 ID：3x 开头 + 足够长
    if re.fullmatch(r"3x[A-Za-z0-9]{8,}", vid):
        return False
    # 其他（l_6tuFI6QSo / ng9eGTEH 这类）→ 当短码处理
    return True


# ---------------------------------------------------------------- 抖音


async def parse_douyin(ctx, vid: str) -> ParsedVideo:
    """抖音：从作品页内部 fetch detail 接口拿无水印 play_addr。"""
    out = ParsedVideo(platform="douyin", video_id=vid)
    page = await ctx.new_page()
    try:
        await page.goto(f"https://www.douyin.com/video/{vid}",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        r = await page.evaluate(
            """async () => {
              const id = location.pathname.split('/').pop();
              try {
                const resp = await fetch('/aweme/v1/web/aweme/detail/?device_platform=webapp'
                  + '&aid=6383&channel=channel_pc_web&aweme_id=' + id,
                  {credentials:'include'});
                const t = await resp.text();
                let j = {}; try { j = JSON.parse(t); } catch(e) {}
                const a = j.aweme_detail || {};
                const v = a.video || {};

                // ★ 抖音的 detail 里带【一组转码档】（video.bit_rate[]），
                //   还有 play_addr / play_addr_h264 / play_addr_265。
                //   url_list[0] 并不保证是最高清（社区实测有时是 720p、有时 480p），
                //   所以这里【全部枚举出来，按体积/分辨率挑最大的】。
                //   ⚠️ download_addr 是【带水印】的裁剪版，不参与比较。
                const cands = [];
                const seen = {};
                const add = (pa, tag) => {
                  if (!pa || !pa.url_list || !pa.url_list.length) return;
                  const u = pa.url_list[0];
                  if (!u || seen[u]) return;
                  seen[u] = 1;
                  cands.push({
                    url: u, size: pa.data_size || 0,
                    w: pa.width || 0, h: pa.height || 0, tag: tag
                  });
                };
                add(v.play_addr, '默认');
                add(v.play_addr_h264, 'H264');
                add(v.play_addr_265, 'H265');
                (v.bit_rate || []).forEach(b => {
                  add(b.play_addr, (b.gear_name || '') + ' ' + (b.bit_rate || 0));
                });
                // 先比体积 → 再比分辨率
                cands.sort((x, y) => (y.size - x.size) || ((y.w * y.h) - (x.w * x.h)));
                const best = cands[0] || {};

                return {
                  hasDetail: !!a.aweme_id,
                  play: best.url || '',
                  desc: (a.desc||'').slice(0,100),
                  author: ((a.author||{}).nickname||'').slice(0,40),
                  tiers: cands.length,
                  width: best.w || v.width || 0,
                  height: best.h || v.height || 0,
                  bytes: best.size || 0,
                  duration: v.duration || 0,
                  vw: v.width || 0, vh: v.height || 0,
                };
              } catch(e) { return {err: String(e).slice(0,150)}; }
            }""")
        if r.get("err"):
            out.error = r["err"]
            return out
        if not r.get("play"):
            out.error = "未取到 play_addr（可能需要滑块验证）"
            return out
        out.play_url = r["play"]
        out.desc = r.get("desc", "")
        out.author = r.get("author", "")
        out.method = "page_fetch_detail"
        out.size = int(r.get("bytes") or 0)
        # ★ 把画质信息带出去，前端可以显示「已经是最高档了」
        out.extra = {
            "tiers": r.get("tiers", 0),
            "width": r.get("width", 0),
            "height": r.get("height", 0),
            "bytes": r.get("bytes", 0),
            "duration": r.get("duration", 0),
        }
        out.ok = True
    except Exception as e:
        out.error = str(e)[:150]
    finally:
        await page.close()
    return out


# ---------------------------------------------------------------- 快手


async def _ks_caption(pid: str) -> str:
    """快手文案：分享页是服务端直出，内嵌 JSON 里带 "caption"。

    DOM 上 og:title / h1 往往只是「快手」这个壳，读不到真正的文案，
    所以直接 HTTP 拉一次原始 HTML 取 caption（用于下载后的文件名）。
    """
    import httpx
    for host in ("https://v.m.chenzhongtech.com",
                 "https://m.gifshow.com"):
        try:
            async with httpx.AsyncClient(
                timeout=20, follow_redirects=True,
                headers={"User-Agent": UA_MOBILE,
                         "Accept-Language": "zh-CN,zh;q=0.9"}
            ) as c:
                r = await c.get(f"{host}/fw/photo/{pid}")
                if r.status_code != 200:
                    continue
                m = re.search(r'"caption"\s*:\s*"((?:[^"\\]|\\.){4,200})"', r.text)
                if not m:
                    continue
                txt = m.group(1)
                try:
                    txt = json.loads('"' + txt + '"')
                except Exception:
                    pass
                txt = txt.replace("\\n", " ").replace("\n", " ").strip()
                txt = re.sub(r"\s{2,}", " ", txt)
                if txt and txt != "快手":
                    return txt[:100]
        except Exception:
            continue
    return ""


async def parse_kuaishou(ctx, pid: str) -> ParsedVideo:
    """快手：移动端 UA + 移动端分享页，拦截 .mp4 响应拿直链。"""
    out = ParsedVideo(platform="kuaishou", video_id=pid)
    page = await ctx.new_page()
    media: list[tuple[str, str, str]] = []

    def _on_media(r):
        u = r.url
        if ".mp4" in u and "video/mp4" in (r.headers.get("content-type", "") or ""):
            media.append((u, r.headers.get("content-type", ""),
                          r.headers.get("content-length", "")))

    page.on("response", _on_media)
    # ★ 抗风控：多域名 + 多 UA + 重试
    urls = [
        f"https://v.m.chenzhongtech.com/fw/photo/{pid}",
        f"https://m.gifshow.com/fw/photo/{pid}",
        f"https://v.m.chenzhongtech.com/fw/photo/{pid}?v={int(time.time())}",
    ]
    uas = list(UA_POOL)
    try:
        for attempt in range(3):
            for u in urls[attempt:attempt + 2] or urls[:1]:
                media.clear()
                # 每次换 UA，降低指纹命中率
                try:
                    await page.set_extra_http_headers({"User-Agent": uas[(attempt + 1) % len(uas)]})
                except Exception:
                    pass
                try:
                    await page.goto(u, wait_until="domcontentloaded", timeout=40000)
                    await page.wait_for_timeout(7000 + attempt * 2000)
                except Exception:
                    continue
                if not media:
                    # 手动触发播放
                    for sel in ("video", "[class*='play']", "[class*='Play']",
                                "[class*='Player']"):
                        try:
                            el = page.locator(sel).first
                            if await el.count() and await el.is_visible():
                                await el.click(timeout=3000)
                                break
                        except Exception:
                            continue
                    await page.wait_for_timeout(6000)
                if media:
                    best = None
                    for mt in media:
                        url_, ct, cl = mt
                        # 优先 CDN 白名单；没有就取最大的（兜底）
                        cdn = any(c in url_ for c in KS_CDN)
                        sz = int(cl or 0)
                        if cdn and (best is None or sz > best[1] or not best[2]):
                            best = (url_, sz, True)
                        elif not best:
                            best = (url_, sz, False)
                    if best and best[0]:
                        out.play_url = best[0]
                        out.size = best[1]
                        out.method = f"mobile_page_intercept({u.split('/')[2]})"
                        out.ok = True
                        # 抓文案：移动端分享页的 og:title / h1 往往是「快手」这种壳，
                        # 真正文案在服务端直出的 HTML 里（"caption":"..."），
                        # 所以先走 HTTP 直取，再退回 DOM 启发式。
                        out.desc = await _ks_caption(pid)
                        if not out.desc:
                            try:
                                og = await page.evaluate(
                                    """()=>{
                                      const o = document.querySelector('meta[property="og:title"]');
                                      const d = document.querySelector('meta[name="description"]');
                                      return (o && o.content) || (d && d.content) || '';
                                    }""")
                                if og and og.strip() and og.strip() != "快手":
                                    out.desc = og.strip()[:100]
                            except Exception:
                                pass
                        if not out.desc:
                            try:
                                t = (await page.title() or "").strip()
                                if t and t != "快手":
                                    out.desc = t[:100]
                            except Exception:
                                pass
                        return out
            # 一轮失败 → 换 UA、递增等待后重试
            await asyncio.sleep(3 + attempt * 3)
        out.error = "移动端页面未加载出视频直链（可能触发风控）"
    except Exception as e:
        out.error = str(e)[:150]
    finally:
        await page.close()
    return out


# ---------------------------------------------------------------- 统一入口


async def parse(url: str, headless: bool = True) -> ParsedVideo:
    """解析任意抖音/快手【链接或整段分享文本】→ 无水印直链。

    **不需要登录账号。**
    """
    from playwright.async_api import async_playwright

    # ① 分享文本 → 提取其中的平台 URL
    candidates = [u for u in extract_urls(url) if platform_of(u)]
    if not candidates:
        # 也可能就是纯 URL（没被正则覆盖）
        candidates = [url] if platform_of(url) else []
    if not candidates:
        return ParsedVideo(error="无法识别链接，请粘贴抖音或快手的分享内容")

    # ② 逐个候选：展开短链 → 提取作品 ID
    platform = vid = ""
    for cand in candidates:
        pf, v = extract_id(cand)
        if pf and v:
            platform, vid = pf, v
            # 短链需要展开
            if _is_short_code(v) or "/f/" in cand or "v.douyin.com" in cand \
                    or "v.kuaishou.com" in cand:
                exp = await _expand_short(cand)
                pf2, v2 = extract_id(exp)
                if pf2 and v2:
                    platform, vid = pf2, v2
                else:
                    # 链里没 ID，但短链本身可能仍可用
                    platform = platform or pf2
            break
    if not platform:
        return ParsedVideo(error="无法识别链接，请粘贴抖音或快手的分享内容")

    async with async_playwright() as pw:
        # 抖音用桌面 UA，快手用移动端 UA —— 分开建两个 context
        prof = _pick_profile("dy" if platform == "douyin" else "ks")
        if platform == "douyin":
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(_PROFILE_DIR / prof),
                headless=headless, channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 1440, "height": 900}, locale="zh-CN")
        else:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(_PROFILE_DIR / prof),
                headless=headless, channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 390, "height": 844},
                user_agent=UA_MOBILE, locale="zh-CN",
                is_mobile=True, has_touch=True)
        try:
            await ctx.clear_cookies()      # ★ 保证匿名态
            if platform == "douyin":
                return await parse_douyin(ctx, vid)
            return await parse_kuaishou(ctx, vid)
        finally:
            await ctx.close()
            _release_profile(prof)


async def _warm_profile(pw, prof: str, url: str, viewport: dict,
                         mobile: bool = False):
    """用【解析真正要用的 profile】起一次浏览器并访问站点。

    ★ 关键：之前预热用的是独立的 warm profile，只暖到了 Chromium 二进制；
      解析时还要新建 dy/ks profile（首次创建 profile 目录很慢）→ 首次仍慢。
    ★ 现在直接暖 dy/ks，并把对应站点访问一次（顺带填上 HTTP 缓存）。
    """
    prof = _pick_profile(prof)
    try:
        kw = {}
        if mobile:
            kw = {"user_agent": UA_MOBILE, "is_mobile": True, "has_touch": True}
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR / prof),
            headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport=viewport, locale="zh-CN", **kw)
        try:
            await ctx.clear_cookies()
            try:
                await ctx.goto(url, wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass
        finally:
            await ctx.close()
    finally:
        _release_profile(prof)


async def warmup() -> bool:
    """预热浏览器：把解析要用的 profile 目录和站点缓存都提前弄好。

    实测：冷解析 24s，预热后 8s。
    抖音优先（更常用），快手其次。
    """
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            await _warm_profile(pw, "dy", "https://www.douyin.com/",
                                {"width": 1440, "height": 900})
            await _warm_profile(pw, "ks", "https://v.m.chenzhongtech.com/",
                                {"width": 390, "height": 844}, mobile=True)
        return True
    except Exception:
        return False


async def download(url: str, dest: str, headless: bool = True) -> dict:
    """一步到位：解析 + 下载。返回 {ok, file, size, desc, error}。"""
    import httpx
    from pathlib import Path

    r = await parse(url, headless=headless)
    if not r.ok:
        return {"ok": False, "error": r.error, "platform": r.platform}

    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120,
                                     headers={"User-Agent": UA_DESKTOP}) as c:
            resp = await c.get(r.play_url)
            if resp.status_code == 200 and len(resp.content) > 10_000:
                dest_p.write_bytes(resp.content)
                return {"ok": True, "file": str(dest_p),
                        "size": len(resp.content),
                        "desc": r.desc, "author": r.author,
                        "method": r.method, "platform": r.platform}
            return {"ok": False, "error": f"下载失败 HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


# ---------------------------------------------------------------- CLI


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python nologin_engine.py <链接> [输出文件]")
        sys.exit(1)

    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "nologin_test.mp4"
    r = asyncio.run(download(url, out))
    if r.get("ok"):
        print(f"✅ 成功 {r['size']/1048576:.1f}MB  方法={r.get('method')}")
        print(f"   {r.get('desc','')}")
        print(f"   → {r['file']}")
    else:
        print(f"❌ 失败: {r.get('error')}")
