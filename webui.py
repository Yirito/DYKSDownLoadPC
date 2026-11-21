"""本地可视化界面：起一个本地 HTTP 服务，浏览器打开就能用。

只用标准库 + 已有模块，不引入 web 框架。
启动：
    python webui.py            # 默认 http://127.0.0.1:8765
    python webui.py --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "core"))

import auth  # noqa: E402
import search as search_mod  # noqa: E402
from download import download, safe_name, unique_path  # noqa: E402

WEB_DIR = HERE / "web"
CONFIG_FILE = HERE / "config.json"
PLAT_DIR = {"douyin": "抖音", "kuaishou": "快手"}
PLAT_NAME = {"douyin": "抖音", "kuaishou": "快手"}
DEFAULT_CONFIG = {
    "dir": "",
    "count": 10,
    "orientation": "portrait",
    "platforms": ["douyin", "kuaishou"],
    "order": "like",
    "clip_enabled": False,
    "clip_orientation": "portrait",
    "clip_dur": 1.5,
    "ai_pick": False,
}

# 目录选择弹窗的状态（后台弹，前端轮询取结果）
PICK_STATE = {"dir": "", "running": False}

# 任务状态（单任务，够用）
JOB: dict = {"running": False, "phase": "", "log": [], "items": [],
             "out_root": "", "done": 0, "total": 0, "error": "",
             "draft_root": "", "draft_id": "", "pending_clips": 0}
_lock = threading.Lock()

# ── 登录/验证状态缓存（避免每次刷新都真发网络请求，把机器打爆）──
STATUS_CACHE: dict = {"items": [], "at": 0.0}
STATUS_TTL = 90.0          # 90 秒内复用缓存（每次探测要开一次浏览器）
_status_thread = None


def today_cn() -> str:
    d = datetime.now()
    return f"{d.year}年{d.month}月{d.day}日"


def logline(msg: str, level: str = "info") -> None:
    with _lock:
        JOB["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg, "lv": level})
        if len(JOB["log"]) > 500:
            JOB["log"] = JOB["log"][-500:]


def set_item(idx: int, **kw) -> None:
    with _lock:
        for it in JOB["items"]:
            if it["i"] == idx:
                it.update(kw)
                return
        JOB["items"].append({"i": idx, **kw})


# ------------------------------------------------------------------ 配置持久化

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if not cfg.get("dir"):
        cfg["dir"] = str(Path.home() / "Downloads" / "DYKS")
    return cfg


def save_config(cfg: dict) -> dict:
    """只保存白名单字段，避免脏数据进配置文件。"""
    out = {
        "dir": str(cfg.get("dir") or ""),
        "count": int(cfg.get("count") or 10),
        "orientation": cfg.get("orientation") or "portrait",
        "platforms": [p for p in (cfg.get("platforms") or []) if p in PLAT_DIR],
        "order": cfg.get("order") if cfg.get("order") in ORDER_NAME else "like",
        "clip_enabled": bool(cfg.get("clip_enabled")),
        "clip_orientation": (cfg.get("clip_orientation")
                             if cfg.get("clip_orientation") in ("portrait", "landscape")
                             else "portrait"),
        "clip_dur": max(1.0, min(1.5, float(cfg.get("clip_dur") or 1.5))),
        "ai_pick": bool(cfg.get("ai_pick")),
    }
    try:
        CONFIG_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ 目录选择

def pick_directory(initial: str = "") -> str:
    """弹 Windows 原生「浏览文件夹」对话框，返回选中的目录（取消返回 ""）。

    ★ 做法和「一键签到」看板完全一致（那边实测好用）：
      · System.Windows.Forms.FolderBrowserDialog
      · ShowDialog() 不传 owner → 【只弹一个窗口】
      · powershell -STA（WinForms 模态必需）
      · CREATE_NO_WINDOW（不闪黑框）
      · 脚本走 -EncodedCommand（base64 UTF-16LE）避免 shell 转义问题

    ★★ 别再加什么 TopMost 父窗体：模态对话框本来就会自己置顶，
       多加那个 Form 反而会抢焦点、把对话框挤到浏览器后面去，
       还会多弹一个空白窗口（用户反馈过）。
    """
    import base64
    import subprocess
    try:
        ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
              "Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              "$d.Description = '选择下载目录';"
              "$d.ShowNewFolderButton = $true;")
        if initial and os.path.isdir(initial):
            ps += "$d.SelectedPath = '%s';" % initial.replace("'", "''")
        ps += "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", enc],
            capture_output=True, timeout=120, encoding="utf-8", errors="replace",
            creationflags=0x08000000)          # CREATE_NO_WINDOW：不闪黑框
        line = (proc.stdout or "").strip()
        if line and ("\\" in line or line.startswith("/")):
            return line
        return ""
    except Exception:
        return ""


def list_dirs(path: str) -> dict:
    """列出子目录；path 为空时列出盘符 + 常用位置（网页内手动浏览用）。"""
    if not path:
        out = []
        if os.name == "nt":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                d = f"{letter}:\\"
                if os.path.exists(d):
                    out.append({"name": d, "path": d})
        for extra in [str(Path.home()), str(Path.home() / "Downloads"),
                      str(Path.home() / "Desktop"), str(Path.home() / "Videos"),
                      str(Path.cwd())]:
            if os.path.isdir(extra):
                out.append({"name": Path(extra).name or extra, "path": extra})
        return {"path": "", "parent": None, "dirs": out}
    p = Path(path)
    if not p.is_dir():
        return {"path": path, "parent": None, "dirs": [], "error": "路径不存在"}
    parent = str(p.parent) if p.parent != p else None
    out = []
    try:
        for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and not c.name.startswith("."):
                out.append({"name": c.name, "path": str(c)})
    except PermissionError:
        pass
    return {"path": str(p), "parent": parent, "dirs": out}


# ------------------------------------------------------------------ 状态探测（带缓存）

def probe_sync(plat: str):
    try:
        return asyncio.run(auth.probe_remote(plat))
    except Exception as e:
        return False, str(e)[:80]


def check_sync(plat: str):
    """登录/验证状态 —— 必须走浏览器拿真实 cookie（磁盘上的值是加密的）。"""
    try:
        return asyncio.run(auth.check_auth_in_browser(plat))
    except Exception as e:
        return False, str(e)[:80]


def refresh_status_async():
    """后台线程：真去探测两平台状态，结果写进缓存。"""
    global _status_thread
    with _lock:
        if _status_thread and _status_thread.is_alive():
            return
        _status_thread = threading.Thread(target=_refresh_status_worker, daemon=True)
        _status_thread.start()


def _refresh_status_worker():
    with _lock:
        if JOB.get("running"):
            # ★ 任务在跑 → 浏览器正被搜索/下载占着，这时候去探测必然失败。
            #   直接不探，保留上一次的结论（别在下载途中把状态翻脸）。
            return
        old = {i["plat"]: i for i in STATUS_CACHE["items"]}
    out = []
    for plat in ("douyin", "kuaishou"):
        # ★ 用 check_auth（只看登录/验证状态），不要用「必须搜到结果」当标准
        try:
            ok, why = check_sync(plat)
        except Exception as e:
            ok, why = False, f"探测异常：{type(e).__name__}: {str(e)[:80]}"
        prev = old.get(plat)
        if not ok and prev and prev.get("ok"):
            # ★★ 探测失败（浏览器被别人占着 / 网络抽风）时，
            #   【绝不推翻已知的好结论】—— 否则用户会看到
            #   「刚才还登录好的，怎么突然说没登录」。
            ok, why = True, prev.get("why", "")
        out.append({"plat": plat, "name": PLAT_NAME[plat], "ok": ok, "why": why,
                    "local": auth.has_local_auth(plat)})
    with _lock:
        STATUS_CACHE["items"] = out
        STATUS_CACHE["at"] = time.time()


def get_status(force: bool = False) -> list:
    """拿状态；缓存过期就异步刷新一次，先返回旧的（不阻塞请求）。

    ★ 关键：探测【不能并发】—— 两个浏览器同时开同一个 profile，
      后启动的那个会因 profile 被占用而失败，
      失败结果会把缓存覆盖成「未就绪」（明明登录了也报没登录）。
    """
    with _lock:
        fresh = (time.time() - STATUS_CACHE["at"]) < STATUS_TTL
        items = list(STATUS_CACHE["items"])
        probing = bool(_status_thread and _status_thread.is_alive())
    if (force or not fresh) and not probing:
        refresh_status_async()
    if not items and not probing:
        with _lock:
            busy = bool(JOB.get("running"))
        if not busy:
            _refresh_status_worker()      # 首屏：同步探一次
    # ★ 刷新完必须重新读一次缓存 —— 否则返回的还是刷新【之前】的旧值
    with _lock:
        items = list(STATUS_CACHE["items"])
    return items


# ------------------------------------------------------------------ 后台任务

ORDER_NAME = {"like": "点赞多", "comment": "评论多", "newest": "最新", "default": "默认"}


async def run_job(keyword: str, dl_dir: Path, count: int, orient: str,
                  plats: tuple[str, ...], order: str = "like",
                  clip_enabled: bool = False, clip_orientation: str = "portrait",
                  clip_dur: float = 1.5, ai_pick: bool = False) -> None:
    """搜索 + 下载，全程把进度写进 JOB。

    ★ 不再自动弹浏览器要求登录 —— 登录由界面上的「登录」按钮手动触发，
      这里发现没登录就跳过该平台并提示。
    """
    out_root = dl_dir / f"{today_cn()}_{safe_name(keyword, 40)}"
    for p in plats:
        (out_root / PLAT_DIR[p]).mkdir(parents=True, exist_ok=True)
    with _lock:
        JOB.update({"running": True, "phase": "准备中", "items": [], "log": [],
                    "out_root": str(out_root), "done": 0, "total": 0, "error": ""})
    logline(f"输出目录：{out_root}")

    idx = 0
    done_videos: list[Path] = []
    for plat in plats:
        # ★ 不再【预先探测登录态】—— 预检查会误判（探测口径和搜索口径不一致，
        #   于是出现「状态栏说可用、下载却提示待登录」）。
        #   直接开搜：搜索接口自己会告诉我们要不要登录/验证（AuthExpired），
        #   搜得到就是搜得到，搜不到就如实说「没搜到结果」。
        with _lock:
            JOB["phase"] = f"搜索 {PLAT_NAME[plat]}"
        logline(f"正在搜索 {PLAT_NAME[plat]}：{keyword}")
        try:
            items = await search_mod.search(plat, keyword, limit=count * 5)
        except auth.AuthExpired as e:
            logline(f"{e}", "warn")
            logline(f"请点上方「登录」按钮处理 {PLAT_NAME[plat]}，再重新下载。", "warn")
            set_item(idx, plat=plat, name="（需要登录/验证）", state="need_auth",
                     pct=0, size=0, wh="")
            idx += 1
            continue
        except Exception as e:
            logline(f"{PLAT_NAME[plat]} 搜索失败：{str(e)[:120]}", "err")
            set_item(idx, plat=plat, name="（搜索失败）", state="failed", pct=0,
                     size=0, wh="")
            idx += 1
            continue

        if not items:
            logline(f"{PLAT_NAME[plat]} 没搜到结果", "warn")
            set_item(idx, plat=plat, name="（无结果）", state="failed", pct=0,
                     size=0, wh="")
            idx += 1
            continue

        if orient == "portrait":
            picked = [i for i in items if i.portrait or not (i.width and i.height)]
        elif orient == "landscape":
            picked = [i for i in items if i.width and i.height and i.width > i.height]
        else:
            picked = list(items)
        # ★ 排序：点赞多（默认）/ 评论多 / 最新 / 不排
        # 快手搜索接口【不返回评论数】（只有 comment.us_c = 我的评论数），
        # 所以「评论多」在快手上没法真排序 → 降级成按点赞排，并说明
        eff_order = order
        if order == "comment" and plat == "kuaishou":
            eff_order = "like"
            logline("  ⚠ 快手搜索接口不返回评论数，已改为按点赞排序", "warn")
        picked = search_mod.sort_items(picked, eff_order)
        picked = picked[:count]
        logline(f"{PLAT_NAME[plat]} 命中 {len(items)} 条 → 下载 {len(picked)} 条"
                f"（排序：{ORDER_NAME.get(eff_order, eff_order)}）")
        if len(picked) < count:
            logline(f"  ⚠ {PLAT_NAME[plat]} 只够 {len(picked)} 条（要 {count} 条），"
                    f"平台里可能真没那么多，或被横竖屏过滤掉了", "warn")

        folder = out_root / PLAT_DIR[plat]
        for it in picked:
            idx += 1
            my = idx
            wh = f"{it.width}×{it.height}" if it.width else ""
            set_item(my, plat=plat, name=it.title[:60] or it.video_id, wh=wh,
                     size=it.bytes, pct=0, state="downloading")
            with _lock:
                JOB["total"] += 1
            dest = unique_path(folder, safe_name(it.title))
            with _lock:
                JOB["phase"] = f"下载 {PLAT_NAME[plat]} · {it.title[:24]}"

            def cb(got, total, _m=my):
                pct = int(got * 100 / total) if total else 0
                set_item(_m, pct=pct, got=got, total=total)

            good, msg = await download(plat, it.play_url, dest, on_progress=cb)
            real_size = dest.stat().st_size if dest.exists() else 0
            set_item(my, state="done" if good else "failed",
                     pct=100 if good else 0,
                     size=real_size or it.bytes,     # ★ 用实际落盘大小（搜索响应里没有 size）
                     path=str(dest) if good else "", note=msg)
            if good:
                with _lock:
                    JOB["done"] += 1
                logline(f"✅ {dest.name}  {msg}")
                done_videos.append(dest)
            else:
                logline(f"❌ {it.title[:30]}  {msg}", "err")

    # ── 剪映自动剪辑（可选）──
    if clip_enabled and done_videos:
        try:
            if ai_pick:
                # ★ AI 模式：只抽帧登记，【不阻塞等 AI】（任务先结束）
                #   等 AI 选段提交后，点页面上的「生成剪映草稿」即可
                await prepare_ai_clips(done_videos, keyword, out_root, logline)
            else:
                await make_clip_draft(done_videos, keyword, out_root,
                                      clip_orientation, clip_dur, False, logline)
        except Exception as e:
            logline(f"⚠ 剪映剪辑失败：{str(e)[:140]}", "err")

    with _lock:
        JOB["running"] = False
        JOB["phase"] = "完成"
    logline(f"全部完成：{JOB['done']} / {JOB['total']} 个文件")


# ------------------------------------------------------------------ 剪映剪辑

# AI 选段：外部（WorkBuddy 会话）看帧后把结果写进来
AI_PICK: dict = {}
# 正在等待选段的视频（含候选帧路径，供外部读取）
CLIP_PENDING: dict = {}


PLAT_DIRNAME = {"douyin": "抖音", "kuaishou": "快手"}


def scan_media(dl_dir: Path, plats: list[str]) -> dict:
    """扫描下载目录，找出各平台子目录下的所有视频。

    下载目录结构：<下载目录>/<日期_关键字>/抖音/*.mp4、同级 快手/*.mp4
    ⇒ 递归找名为「抖音」「快手」的目录，把里面的视频都收集起来。
    """
    out = {"douyin": [], "kuaishou": [], "roots": []}
    if not dl_dir or not Path(dl_dir).exists():
        return out
    root = Path(dl_dir)
    seen_dirs = set()
    for p in root.rglob("*"):
        if not p.is_dir() or p.name not in PLAT_DIRNAME.values():
            continue
        key = str(p).lower()
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        plat = next(k for k, v in PLAT_DIRNAME.items() if v == p.name)
        vids = sorted([f for f in p.iterdir()
                       if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv")])
        if vids:
            out[plat].extend(vids)
            out["roots"].append(str(p))
    return out


async def prepare_ai_clips(videos: list[Path], keyword: str, out_root: Path,
                           logline) -> None:
    """AI 模式：只抽帧并登记成「待选段」，让 AI 看完再生成草稿。"""
    import clip as clip_mod
    logline(f"\n🎬 已抽取候选画面，等待 AI 选段（{len(videos)} 个视频）")
    clips_dir = out_root / "_剪映片段"
    clips_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for v in videos:
        frames = await clip_mod.extract_frames(v, clips_dir / v.stem, max_frames=8)
        if not frames:
            continue
        total = clip_mod.probe_duration(v)
        with _lock:
            CLIP_PENDING[str(v)] = {
                "video": str(v),
                "keyword": keyword,
                "duration": round(total, 2),
                "frames": [{"path": f.path, "t": f.t, "score": f.score}
                           for f in frames],
            }
        n += 1
    with _lock:
        JOB["pending_clips"] = n
    logline(f"  · {n} 个视频已抽出候选画面")
    logline("  · 让 WorkBuddy 帮你选段（跟它说「帮我选段」），"
            "选完点页面上的「🎬 生成剪映草稿」", "warn")


async def run_clip_job(dl_dir: Path, plats: list[str], orientation: str,
                       dur: float, avoid_faces: bool, ai_pick: bool,
                       logline) -> dict:
    """按【下载文件夹里的视频】做剪映剪辑。

    ★ 视频条数 = 文件夹里实际有多少（不强行凑数）；
      有人脸的片段会被跳过，所以最终可能少于原数。
    """
    media = scan_media(dl_dir, plats)
    # ★ 只取用户勾选的平台
    videos = []
    for k in ("douyin", "kuaishou"):
        if k in plats:
            videos.extend(media[k])
    if not videos:
        return {"ok": False, "msg": "下载目录里没找到视频（先下载一批再来剪）"}
    names = "、".join(PLAT_NAME[p] for p in plats)
    logline(f"🎬 扫描到 {len(videos)} 个视频（{names}）")
    for k in ("douyin", "kuaishou"):
        if k in plats and media[k]:
            logline(f"    {PLAT_NAME[k]}：{len(media[k])} 个")
    if not clip_mod_available():
        logline("  ⚠ 没有 ffmpeg / 人脸检测能力，跳过剪辑", "warn")
        return {"ok": False, "msg": "缺少 ffmpeg 或人脸检测能力"}
    import clip as clip_mod
    root = Path(dl_dir)
    if ai_pick:
        await prepare_ai_clips(videos, "", root, logline)
        return {"ok": True, "pending": len(videos), "msg": "已抽出候选画面，等待 AI 选段"}
    return await make_clip_draft(videos, "", root, orientation, dur,
                                 False, logline, avoid_faces=avoid_faces)


def clip_mod_available() -> bool:
    try:
        import clip as clip_mod
        return bool(clip_mod.ffmpeg_exe())
    except Exception:
        return False


async def make_draft_from_pending(orientation: str, dur: float,
                                  logline) -> dict:
    """把待选段的视频裁成片段并装配成草稿（AI 选段结果优先，没有就用本地选段）。"""
    import clip as clip_mod
    import capcut as capcut_mod
    with _lock:
        pending = dict(CLIP_PENDING)
    if not pending:
        return {"ok": False, "msg": "没有待选段的素材"}
    videos = [Path(k) for k in pending.keys() if Path(k).exists()]
    if not videos:
        return {"ok": False, "msg": "待选段的素材文件都不见了"}
    # 找出这些视频所在目录（作为输出根）
    root = videos[0].parent.parent
    # ★ key 是 str(v)（Windows 反斜杠路径），不能用 as_posix()（会变正斜杠 → KeyError）
    kw = pending[str(videos[0])].get("keyword", "")
    return await make_clip_draft(videos, kw, root, orientation, dur, True, logline)


async def make_clip_draft(videos: list[Path], keyword: str, out_root: Path,
                          orientation: str, dur: float, ai_pick: bool,
                          logline, avoid_faces: bool = True) -> None:
    """抽帧 → 选段 → 裁出 1-1.5 秒片段 → 装配成剪映草稿。

    选段两条路：
      ① AI 选段（ai_pick=True）：把候选帧交给多模态模型看，按语义挑
      ② 本地选段：按「画面信息量」打分，避开黑屏/静止封面/片头
    """
    import clip as clip_mod
    import capcut as capcut_mod

    logline(f"\n🎬 开始剪映自动剪辑（{len(videos)} 个视频，每段 {dur:.1f} 秒，"
            f"{'竖屏' if orientation != 'landscape' else '横屏'}）")
    clips_dir = out_root / "_剪映片段"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips: list[dict] = []
    skipped_face = 0

    for i, v in enumerate(videos, 1):
        logline(f"  [{i}/{len(videos)}] 分析画面：{v.name[:30]}")
        frames = await clip_mod.extract_frames(v, clips_dir / v.stem, max_frames=8)
        if not frames:
            logline(f"      ⚠ 抽不出画面，跳过", "warn")
            continue
        total = clip_mod.probe_duration(v)
        # 登记待选段（供外部 AI 读取候选帧并提交选择）
        with _lock:
            CLIP_PENDING[str(v)] = {
                "video": str(v),
                "keyword": keyword,
                "duration": round(total, 2),
                "frames": [{"path": f.path, "t": f.t, "score": f.score}
                           for f in frames],
            }

        idx = -1
        how = ""
        if ai_pick:
            with _lock:
                got = AI_PICK.get(str(v))
            if got:
                idx = int(got.get("index", 0))
                how = "AI 选帧"
            else:
                logline("      ⚠ 这条没有 AI 选段结果，改用本地选段", "warn")
        if idx < 0:
            # 本地选段：信息量最高的帧
            best = max(frames, key=lambda f: f.score)
            idx = frames.index(best)
            how = f"本地选帧（信息量 {best.score:.1f}）"

        # ★ 有人脸 → 换一帧；整条视频都没有无人脸帧 → 跳过这条视频
        cand = [f for f in frames if (not avoid_faces or f.faces <= 0)]
        if not cand:
            logline(f"      ⚠ 全片都有露脸画面，跳过这条（{v.stem[:20]}）", "warn")
            skipped_face += 1
            continue
        best = max(cand, key=lambda f: f.score)
        idx = frames.index(best)
        how = f"选帧 {best.t:.1f}s" + ("（有人脸，已避开）" if best.faces > 0 else "")
        start, d, ok2 = clip_mod.pick_segment([best], max(0.5, dur - 0.5),
                                              dur, total, avoid_faces=False)
        if not ok2:
            continue
        out = clips_dir / f"{i:02d}_{v.stem[:24]}.mp4"
        ok, msg = await clip_mod.cut_clip(v, start, d, out, orientation)
        if not ok:
            logline(f"      ❌ 裁剪失败：{msg}", "err")
            continue
        clips.append({"path": out, "duration": d,
                      "desc": f"{v.stem[:24]} @ {frames[idx].t:.1f}s（{how}）"})
        logline(f"      ✅ 片段 {i}：{frames[idx].t:.1f}s 处 · {msg}")

    if not clips:
        logline("  ⚠ 没有可用片段，未生成草稿", "warn")
        return
    if skipped_face:
        logline(f"  · 因含人脸跳过 {skipped_face} 个片段（{len(clips)}/{len(videos)} 个视频入选）")

    logline(f"  · 共 {len(clips)} 个片段 → 装配剪映草稿")
    work = out_root / "_剪映草稿"
    res = capcut_mod.build_draft(clips, work, orientation, "720p",
                                 f"{keyword}自动剪辑", log=logline)
    if res.get("ok"):
        logline(f"  ✅ 剪映草稿已生成：{res.get('draft_root')}")
        logline(f"     草稿ID {res.get('draft_id')} · {res.get('shots')} 镜 · "
                f"总时长 {res.get('total')} 秒 · 打开剪映即可看到")
        with _lock:
            JOB["draft_root"] = res.get("draft_root", "")
            JOB["draft_id"] = res.get("draft_id", "")
    else:
        logline(f"  ❌ 草稿生成失败：{res.get('msg')}", "err")


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "DYKS/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        qs = parse_qs(u.query)
        if p in ("/", "/index.html"):
            f = WEB_DIR / "index.html"
            if not f.exists():
                self._send(404, b"index.html missing", "text/plain")
                return
            self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            return
        if p == "/api/state":
            with _lock:
                self._json(dict(JOB))
            return
        if p == "/api/config":
            self._json(load_config())
            return
        if p == "/api/status":
            force = (qs.get("force") or ["0"])[0] in ("1", "true")
            self._json({"items": get_status(force)})
            return
        if p == "/api/dirs":
            path = (qs.get("path") or [""])[0]
            self._json(list_dirs(path))
            return
        if p == "/api/pickdir":
            # ★ 立刻返回，弹窗在后台线程弹 —— 同步等待会把浏览器请求卡住，
            #   用户看着像「点不了」。前端改成轮询 /api/pickdir_result。
            init = (qs.get("init") or [""])[0]
            with _lock:
                PICK_STATE["dir"] = ""
                PICK_STATE["running"] = True

            def _pick():
                try:
                    d = pick_directory(init)
                except Exception:
                    d = ""
                with _lock:
                    PICK_STATE["dir"] = d
                    PICK_STATE["running"] = False

            threading.Thread(target=_pick, daemon=True).start()
            self._json({"ok": True, "started": True})
            return
        if p == "/api/clip/pending":
            with _lock:
                self._json({"items": list(CLIP_PENDING.values())})
            return
        if p == "/api/pickdir_result":
            with _lock:
                self._json({"dir": PICK_STATE["dir"],
                            "running": PICK_STATE["running"]})
            return
        self._send(404, b"{}")

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if p == "/api/start":
            with _lock:
                if JOB["running"]:
                    self._json({"ok": False, "msg": "已有任务在跑"})
                    return
            keyword = (body.get("keyword") or "").strip()
            if not keyword:
                self._json({"ok": False, "msg": "请填关键字"})
                return
            dl = Path(body.get("dir") or (Path.cwd() / "downloads"))
            count = max(1, int(body.get("count") or 10))
            orient = body.get("orientation") or "portrait"
            order = body.get("order") or "like"
            clip_enabled = bool(body.get("clip_enabled"))
            clip_orientation = body.get("clip_orientation") or "portrait"
            clip_dur = max(1.0, min(1.5, float(body.get("clip_dur") or 1.5)))
            ai_pick = bool(body.get("ai_pick"))
            plats = tuple(body.get("platforms") or ("douyin", "kuaishou"))
            save_config({"dir": str(dl), "count": count, "orientation": orient,
                         "platforms": list(plats), "order": order,
                         "clip_enabled": clip_enabled,
                         "clip_orientation": clip_orientation,
                         "clip_dur": clip_dur, "ai_pick": ai_pick})
            th = threading.Thread(target=lambda: asyncio.run(
                run_job(keyword, dl, count, orient, plats, order,
                        clip_enabled, clip_orientation, clip_dur, ai_pick)),
                daemon=True)
            th.start()
            self._json({"ok": True})
            return
        if p == "/api/prepare":
            plat = body.get("plat") or "douyin"
            if plat not in PLAT_DIR:
                self._json({"ok": False, "msg": "平台不对"})
                return
            logline(f"正在打开浏览器，请完成【{PLAT_NAME[plat]}的"
                    f"{'扫码登录' if plat == 'douyin' else '滑块验证'}】…")

            def _do():
                try:
                    asyncio.run(auth.prepare(plat))
                finally:
                    # ★ 登录/验证刚完成 → 立刻重探一次并写进缓存，
                    #   否则前端要等缓存过期（90s）才看到「已就绪」
                    _refresh_status_worker()

            threading.Thread(target=_do, daemon=True).start()
            self._json({"ok": True})
            return
        if p == "/api/clip/scan":
            dl = body.get("dir") or ""
            plats = body.get("plats") or ["douyin", "kuaishou"]
            m = scan_media(Path(dl), plats)
            n_dy = len(m["douyin"]) if "douyin" in plats else 0
            n_ks = len(m["kuaishou"]) if "kuaishou" in plats else 0
            self._json({"douyin": n_dy, "kuaishou": n_ks, "total": n_dy + n_ks})
            return
        if p == "/api/clip/make_draft":
            orientation = body.get("orientation") or "portrait"
            dur = max(1.0, min(1.5, float(body.get("dur") or 1.5)))
            plats = body.get("plats") or ["douyin", "kuaishou"]
            avoid_faces = body.get("avoid_faces", True)
            ai_pick = bool(body.get("ai_pick"))
            dl = body.get("dir") or ""
            res_holder = {}

            def _mk():
                try:
                    res_holder["r"] = asyncio.run(run_clip_job(
                        Path(dl), plats, orientation, dur, avoid_faces, ai_pick,
                        logline))
                except Exception as e:
                    # ★ 失败一定要写日志，否则用户只看到「点了没反应」
                    logline(f"  ❌ 生成草稿失败：{type(e).__name__}: {str(e)[:150]}", "err")
                    res_holder["r"] = {"ok": False, "msg": str(e)[:150]}

            threading.Thread(target=_mk, daemon=True).start()
            self._json({"ok": True, "started": True})
            return
        if p == "/api/clip/select":
            path = body.get("path") or ""
            idx = int(body.get("index") or 0)
            with _lock:
                AI_PICK[path] = {"index": idx, "at": time.time()}
            self._json({"ok": True})
            return
        if p == "/api/save":
            self._json({"ok": True, "config": save_config(body)})
            return
        self._send(404, b"{}")


def main():
    ap = argparse.ArgumentParser(description="DYKSDownLoadPC 本地可视化界面")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    class Server(ThreadingHTTPServer):
        # ★ 防卡死：连接线程设为 daemon（关窗口即退出），
        #   并限制单连接存活时间，避免 keep-alive 连接堆积占满线程
        daemon_threads = True
        allow_reuse_address = True

    Handler.timeout = 30          # 单连接最长存活 30 秒
    # ★ 端口被占用时【自动往后顺延】（最多试 10 个），而不是报错退出。
    #   残留进程占着端口太常见，让人自己换端口是折腾；
    #   实际用的端口会打印出来，浏览器也自动打开它。
    import socket
    port = args.port
    for _ in range(10):
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _probe.settimeout(1)
            busy = _probe.connect_ex((args.host, port)) == 0
        finally:
            _probe.close()
        if not busy:
            break
        print(f"⚠ 端口 {port} 被占用，自动改用 {port + 1} …")
        port += 1
    else:
        print(f"\n❌ {args.port} ~ {port} 都被占用了，请先关掉多余的窗口再试。")
        sys.exit(1)
    args.port = port
    srv = Server((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"▶ DYKSDownLoadPC 可视化界面：{url}")
    print(f"  下载目录：{cfg['dir']}")
    print(f"  配置文件：{CONFIG_FILE}")
    print("  关闭这个黑窗口即停止服务")
    refresh_status_async()
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
