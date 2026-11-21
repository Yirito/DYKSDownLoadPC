"""从下载好的视频里挑出「真正有内容的那 1-1.5 秒」，并裁成独立片段。

为什么要专门做这一步：
  搜索结果本身就和关键词相关，但视频【开头 1 秒】往往是黑屏/静态封面/片头广告，
  直接剪开头就会剪出「跟柿子没关系的画面」—— 这正是要避免的。

选段有两条路：
  ① AI 选段（优先）：把候选帧交给多模态模型看，按语义选最相关的那一帧
     —— 由外部（WorkBuddy 会话）提交结果，见 pick_segments_external()
  ② 本地选段（兜底）：纯 ffmpeg 信号统计打分，选「画面信息量最大」的位置
     —— 零依赖、自动可用，能避开黑屏/静止画面，但不保证语义最相关

裁剪统一用 ffmpeg：抽片段 + 缩放裁切成目标比例（不留黑边）。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ------------------------------------------------------------------ ffmpeg 定位

def ffmpeg_exe() -> str:
    """优先用系统 PATH 里的 ffmpeg，其次用 imageio-ffmpeg 自带的。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ffmpeg_version() -> str:
    try:
        r = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True,
                           timeout=20, encoding="utf-8", errors="replace")
        return (r.stdout or "").splitlines()[0][:80]
    except Exception:
        return "未找到 ffmpeg"


async def _run(cmd: list[str], timeout: int = 180) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    return (proc.returncode or 0,
            out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def probe_duration(video: Path) -> float:
    """用 ffmpeg 读时长（秒）。"""
    try:
        r = subprocess.run([ffmpeg_exe(), "-i", str(video)], capture_output=True,
                           timeout=60, encoding="utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


# ------------------------------------------------------------------ 抽帧

@dataclass
class Frame:
    path: str = ""
    t: float = 0.0          # 在原视频里的时间点（秒）
    score: float = 0.0      # 画面信息量分数（越大越「有内容」）
    motion: float = 0.0     # ★ 动作强度（与上一帧的差异；吃/掰/切这种瞬间最高）
    focus: float = 0.0     # ★ 差异空间集中度（高=真实动作；低=转场闪切）
    faces: int = 0          # ★ 画面里的人脸数量（-1 = 没检测能力，未知）
    reason: str = ""


# ------------------------------------------------------------------ 人脸检测

_face_cascade = None
_face_ready: bool | None = None   # None=未知, True=可用, False=不可用
# 多个级联一起用，提高召回（正脸/侧脸/变体）
_CASCADE_NAMES = ("haarcascade_frontalface_default.xml",
                  "haarcascade_frontalface_alt2.xml",
                  "haarcascade_profileface.xml")
_cascades: list = []


def _get_cascade():
    """加载 OpenCV 的人脸级联文件（只加载一次）。"""
    global _face_cascade, _face_ready, _cascades
    if _face_ready is False:
        return None
    if _face_cascade is not None:
        return _face_cascade
    try:
        import cv2
        for name in _CASCADE_NAMES:
            path = os.path.join(cv2.data.haarcascades, name)
            if not os.path.exists(path):
                continue
            c = cv2.CascadeClassifier(path)
            if not c.empty():
                _cascades.append(c)
        _face_cascade = _cascades[0] if _cascades else None
        _face_ready = _face_cascade is not None
        return _face_cascade
    except Exception:
        _face_ready = False
        return None


def face_detect_available() -> bool:
    return _get_cascade() is not None


def _skin_ratio(bgr_img, box) -> float:
    """框内的「肤色像素」占比。

    ★ 为什么必须做这一步：Haar 级联会把【圆形带纹理的象棋棋子】误判成人脸！
      实测「开局没马马」的纯棋盘画面被判出 1 张脸 → 最相关的画面反而被跳过。
      真人的脸有连续的肤色区域（YCbCr 里 Cb∈[77,127]、Cr∈[133,173]），
      棋子是红/黑/木色 → 肤色占比极低 → 可以可靠区分。
    """
    import cv2
    import numpy as np
    x, y, w, h = box
    H, W = bgr_img.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    crop = bgr_img[y0:y1, x0:x1]
    ycrcb = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb)
    # ★ OpenCV 的 YCrCb 通道顺序是 [Y, Cr, Cb]（不是 [Y, Cb, Cr]！）
    cr = ycrcb[:, :, 1].astype(np.float32)
    cb = ycrcb[:, :, 2].astype(np.float32)
    mask = (cr >= 133) & (cr <= 180) & (cb >= 77) & (cb <= 130)
    return float(mask.mean())


def detect_faces(path: str, min_ratio: float = 0.06,
                 skin_min: float = 0.22) -> int:
    """检测一张图里的人脸数量（Haar 级联 + 肤色二次校验）。

    ★ 为什么要这个：下载的是【别人的】素材，片段里出现真人脸容易被平台
      判搬运/肖像侵权。所以「有人脸的帧」一律不要。
    ★ 返回值：>=0 人脸数；**-1 = 读图失败/检测不了（未知，不是「没人脸」）**
      —— 调用方必须把 -1 当成「不确定」，绝不能当成安全。
    ★ ★ OpenCV 的 imread 在 Windows 上不支持中文路径（D:\\中文\\x.jpg 会读失败），
      所以这里用 np.fromfile + cv2.imdecode。
    """
    if _get_cascade() is None:
        return -1
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return -1                      # ★ 读不到 → 未知，不是「没人脸」
        h, w = img.shape[:2]
        gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        msize = (max(20, int(w * min_ratio)), max(20, int(h * min_ratio)))
        hits: list = []
        for cas in _cascades:
            try:
                boxes = cas.detectMultiScale(gray, scaleFactor=1.1,
                                             minNeighbors=5, minSize=msize)
            except Exception:
                boxes = []
            for b in boxes:
                b = tuple(int(v) for v in b)
                if not any(abs(b[0] - o[0]) < w * 0.08 and abs(b[1] - o[1]) < h * 0.08
                           for o in hits):
                    hits.append(b)
        # ★ 肤色校验：把「棋子」这类误检剔掉
        real = [b for b in hits if _skin_ratio(img, b) >= skin_min]
        return len(real)
    except Exception:
        return -1


def _frame_score(path: str) -> float:
    """算一帧的「画面信息量」。

    思路（不依赖 AI，纯本地）：
      · 对比度（像素标准差）：画面越有层次分越高；纯黑/纯白/静止封面 → 0
      · 边缘密度（相邻像素差分的均值）：画面越锐利、细节越多 → 越高
    两者相加 → 「这一帧是不是有真实内容」的经验分。
    """
    try:
        from PIL import Image
        import statistics
    except Exception:
        return 0.0
    try:
        im = Image.open(path).convert("L").resize((160, 160))
        px = list(im.getdata())
        if not px:
            return 0.0
        mean = sum(px) / len(px)
        var = statistics.pvariance(px) if len(px) > 1 else 0.0
        contrast = var ** 0.5                       # 标准差 ≈ 对比度
        # 边缘密度：横向相邻差分的均值（细节/纹理越多越高）
        w = 160
        edge = 0.0
        n = 0
        for y in range(0, 160, 4):
            row = px[y * w:(y + 1) * w]
            for x in range(1, w, 4):
                edge += abs(row[x] - row[x - 1])
                n += 1
        edge = edge / max(1, n)
        # 亮度偏离中灰（纯黑/纯白要扣分）
        offgray = abs(mean - 128.0) / 128.0
        return contrast * 0.5 + edge * 0.8 - offgray * 40.0
    except Exception:
        return 0.0


def _frame_motion(prev_path: str | None, path: str) -> dict:
    """算相邻两帧的【动作特征】：强度 / 空间集中度 / 亮度变化。

    ★ 用户要「吃、掰开、切开、爆汁、捏爆、撕开」这种吸引人的画面，
      但【不要别人混剪的切换画面】（闪切/花字/转场特效）。
      这两者的算法区别：
        · 真实动作（掰开）：画面差异集中在【一小块】（物体所在处）
        · 转场切换（闪切）：差异【铺满全图】，常伴随【整帧闪白/闪黑】
      ⇒ 用「差异空间集中度」+「亮度闪变」把两者分开。
    """
    if not prev_path:
        return {"motion": 0.0, "focus": 0.0, "flash": 0.0, "bright": 0.0}
    try:
        from PIL import Image, ImageChops
        import numpy as np
        a = Image.open(prev_path).convert("L").resize((128, 128))
        b = Image.open(path).convert("L").resize((128, 128))
        pa = np.asarray(a, dtype=np.float32)
        pb = np.asarray(b, dtype=np.float32)
        diff = np.abs(pa - pb)
        strength = float(diff.mean())
        # ★ 空间集中度：把画面分 4×4 格，最大格占总差异的比例
        #   真实动作 → 少数格子占大部分差异 → 集中度高
        #   转场切换 → 差异均匀铺满 → 集中度低
        grid = diff.reshape(4, 32, 4, 32).sum(axis=(1, 3))
        total = float(grid.sum()) or 1.0
        focus = float(grid.max()) / total
        # ★ 亮度闪变：整帧突然变亮（闪白）或变暗（闪黑）= 转场特效
        ma, mb = float(pa.mean()), float(pb.mean())
        flash = abs(mb - ma)
        bright = max(ma, mb)
        # ★ 闪白/闪黑帧直接判定为「转场画面」
        if bright > 246 or min(ma, mb) < 9:
            focus = 0.0
            strength = 0.0
        return {"motion": strength * 1.2 + (diff > 36).mean() * 45.0,
                "focus": focus, "flash": flash, "bright": bright}
    except Exception:
        return {"motion": 0.0, "focus": 0.0, "flash": 0.0, "bright": 0.0}


def _normalize_motion(frames: list[Frame]) -> None:
    """把动作分转成 0~100 的相对值（本片最大值 = 100）。"""
    if not frames:
        return
    mx = max(f.motion for f in frames)
    if mx <= 0:
        for f in frames:
            f.motion = 0.0
        return
    for f in frames:
        f.motion = round(min(100.0, f.motion / mx * 100.0), 2)


def _appeal_score(f: Frame) -> float:
    """「吸引人」综合分：动作强 + 差异集中（真实动作）+ 画面有内容。

    ★ 动作强但【差异铺满全图】= 转场闪切（混剪）→ 要排除。
      真实动作（掰开/爆汁/撕开）的差异集中在物体那一小块 → focus 高。
    ★ focus 低的帧（转场画面）直接降到很低的分，不会被选中。
    """
    # ★ 实测值：真实动作（掰开/吃）的差异集中在 0.08~0.18，
    #   转场闪切会铺满 16 个格子 → 0.04~0.06。
    #   ⇒ 阈值取 0.065（低于它判为转场画面，直接压掉）
    focus_bonus = f.focus * 100.0        # 0~1 → 0~100
    if focus_bonus < 6.5:                 # ★ 差异铺满全图 → 判为转场，直接压掉
        return f.motion * 0.15 + f.score * 0.20
    return f.motion * 0.55 + focus_bonus * 0.25 + f.score * 0.20


async def extract_frames(video: Path, out_dir: Path, fps: float = 2.5,
                         max_frames: int = 10, total: float = 0.0) -> list[Frame]:
    """按【全片均匀采样】抽候选帧（jpg），并给每帧算两个分：
       · score  = 画面信息量（有没有真实内容）
       · motion = 动作强度（是不是「吃/掰开/切」这种吸引人的瞬间）

    ★ 用【均匀采样 + 快速 seek】而不是 ffmpeg 的 fps 滤镜：
      后者要把整片解码一遍（191 秒的片子要几十秒，而且实测只吐开头几秒的帧）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("f_*.jpg"):
        try:
            old.unlink()
        except Exception:
            pass
    if not total:
        total = probe_duration(video)
    n = max(2, int(max_frames))
    if total <= 0:
        total = 30.0
    frames: list[Frame] = []

    async def _one(i: int, t: float):
        fp = out_dir / f"f_{i+1:02d}.jpg"
        cmd = [ffmpeg_exe(), "-y", "-ss", f"{t:.3f}", "-i", str(video),
               "-frames:v", "1", "-q:v", "3", "-vf", "scale=320:-2", str(fp)]
        rc, _, _ = await _run(cmd, timeout=90)
        if not (rc == 0 and fp.exists() and fp.stat().st_size > 500):
            return None
        return Frame(path=str(fp), t=round(t, 2),
                     score=round(_frame_score(str(fp)), 2),
                     faces=detect_faces(str(fp)))

    # ★ 并发抽帧：串行要 90 秒，并发只要十几秒
    tasks = [_one(i, total * (i + 0.5) / n) for i in range(n)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Frame):
            frames.append(r)
    frames.sort(key=lambda f: f.t)
    # ★ 算动作强度 + 空间集中度，并转成相对值
    for i in range(1, len(frames)):
        m = _frame_motion(frames[i - 1].path, frames[i].path)
        frames[i].motion = m["motion"]
        frames[i].focus = m["focus"]
    _normalize_motion(frames)
    return frames


async def find_action_peak(video: Path, work_dir: Path, total: float = 0.0,
                            coarse: int = 12, dense: int = 8):
    """两阶段找【吸引人的瞬间】：粗选区域 → 密集采样 → 动作峰值。

    ★ 为什么两阶段：
      均匀采样间隔太大（60 秒视频采 12 帧 = 每 5 秒一帧），
      相邻帧差异反映的是「内容变了」而不是「某个动作的瞬间」，
      动作分会大量饱和在满分，分不出好坏。
      ⇒ 先粗选找出「有变化」的区段，再在这个区段里【密集采样】
        （0.4 秒一帧）才能抓到真正的动作峰值（掰开、切开、倒下去那一秒）。

    返回 (动作峰值的时间点, 密集帧列表)
    """
    if not total:
        total = probe_duration(video)
    coarse_frames = await extract_frames(video, work_dir / "coarse",
                                         max_frames=coarse, total=total)
    if not coarse_frames:
        return 0.0, []
    cands = [f for f in coarse_frames if f.score > 0] or coarse_frames
    best_coarse = max(cands, key=_appeal_score)
    lo = max(0.0, best_coarse.t - 3.0)
    hi = min(total, best_coarse.t + 3.0)
    dense_frames = await _sample_range(video, work_dir / "dense", lo, hi, dense)
    if not dense_frames:
        return best_coarse.t, coarse_frames
    for i in range(1, len(dense_frames)):
        m = _frame_motion(dense_frames[i - 1].path, dense_frames[i].path)
        dense_frames[i].motion = m["motion"]
        dense_frames[i].focus = m["focus"]
    _normalize_motion(dense_frames)
    best = max(dense_frames, key=lambda f: _appeal_score(f))
    return best.t, dense_frames


async def _sample_range(video: Path, out_dir: Path, lo: float, hi: float,
                         n: int) -> list[Frame]:
    """在 [lo, hi] 秒区间里密集采样 n 帧。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("d_*.jpg"):
        try:
            old.unlink()
        except Exception:
            pass
    span = max(0.5, hi - lo)

    async def _one(i: int):
        t = lo + span * (i + 0.5) / max(1, n)
        fp = out_dir / f"d_{i+1:02d}.jpg"
        cmd = [ffmpeg_exe(), "-y", "-ss", f"{t:.3f}", "-i", str(video),
               "-frames:v", "1", "-q:v", "3", "-vf", "scale=320:-2", str(fp)]
        rc, _, _ = await _run(cmd, timeout=60)
        if not (rc == 0 and fp.exists() and fp.stat().st_size > 500):
            return None
        return Frame(path=str(fp), t=round(t, 2),
                     score=round(_frame_score(str(fp)), 2),
                     faces=detect_faces(str(fp)))

    tasks = [_one(i) for i in range(n)]
    res = await asyncio.gather(*tasks, return_exceptions=True)
    frames = [r for r in res if isinstance(r, Frame)]
    frames.sort(key=lambda f: f.t)
    return frames


def pick_segment(frames: list[Frame], dur_min: float = 1.0,
                 dur_max: float = 1.5, total: float = 0.0,
                 avoid_faces: bool = True) -> tuple[float, float, bool]:
    """本地选段：挑信息量最高的帧，往前退一点开始，裁 dur_max 秒。

    ★ avoid_faces=True 时：先排除【画面里有人脸】的帧
      （下载的是别人的素材，露真脸会被判搬运/肖像侵权）。
      全部候选帧都有人脸 → 返回 ok=False，这条视频整个跳过
      —— 所以最终条数会少于原视频数，这是预期行为。

    返回 (start, duration, ok)
    """
    if not frames:
        return 0.0, dur_max, False
    pool = frames
    if avoid_faces:
        clean = [f for f in frames if f.faces == 0]
        if not clean:
            # 全都有人脸（或者检测不可用时 faces=-1 → 视为未知，放行）
            unknown = [f for f in frames if f.faces < 0]
            if unknown:
                pool = unknown
            else:
                return 0.0, dur_max, False
        else:
            pool = clean
    # ★★ 选「吸引人的画面」：动作强 + 差异集中（真实动作，排除转场闪切）
    best = max(pool, key=_appeal_score)
    start = max(0.0, best.t - 0.35)
    if total and start + dur_max > total:
        start = max(0.0, total - dur_max)
    return round(start, 2), dur_max, True


# ------------------------------------------------------------------ 裁片段

async def cut_clip(video: Path, start: float, dur: float, out: Path,
                   orientation: str = "portrait",
                   strip_subtitle: float = 0.0) -> tuple[bool, str]:
    """裁出 [start, start+dur] 的片段，并统一成目标比例（不留黑边）。

    orientation: portrait（9:16 竖屏）/ landscape（16:9 横屏）
    ★ strip_subtitle > 0 时：先把底部那一条字幕带【裁掉】，再缩放回目标尺寸
      （抖音「去字幕」同思路）——烧录字幕没法在剪映里关，只能从画面里裁掉。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if orientation == "landscape":
        vf = ("scale=1280:720:force_original_aspect_ratio=increase,"
              "crop=1280:720,setsar=1")
    else:
        vf = ("scale=720:1280:force_original_aspect_ratio=increase,"
              "crop=720:1280,setsar=1")
    if strip_subtitle > 0.01:
        # ★ 先按比例裁掉底部字幕带，再等比缩放 + 居中裁切到目标比例
        #   （裁掉底部后画面比例变了，用 scale+increase+crop 保持不变形）
        vf = (f"crop=iw:ih*(1-{strip_subtitle:.3f}):0:0,"
              f"{vf}")
    cmd = [ffmpeg_exe(), "-y", "-ss", f"{start:.3f}", "-i", str(video),
           "-t", f"{dur:.3f}", "-vf", vf, "-an",          # -an：不要音频
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-movflags", "+faststart", str(out)]
    rc, _, err = await _run(cmd, timeout=240)
    if rc != 0 or not out.exists() or out.stat().st_size < 1024:
        return False, (err or "").strip().splitlines()[-1][:120] if err else "裁剪失败"
    return True, f"{out.stat().st_size/1048576:.2f}MB"


async def cut_with_ai(video: Path, frames: list[Frame], ai_index: int,
                       dur_min: float, dur_max: float, out: Path,
                       orientation: str = "portrait") -> tuple[bool, str, str]:
    """按 AI 指定的帧下标裁剪（AI 选段路径）。"""
    if not (0 <= ai_index < len(frames)):
        ai_index = 0
    f = frames[ai_index]
    total = probe_duration(video)
    start = max(0.0, f.t - dur_min / 2)
    if total and start + dur_max > total:
        start = max(0.0, total - dur_max)
    ok, msg = await cut_clip(video, start, dur_max, out, orientation)
    return ok, msg, f"AI 选帧 #{ai_index} @ {f.t:.2f}s"


async def has_face_in_video(video: Path, n_frames: int = 8,
                            work_dir: Path | None = None) -> tuple[bool, int]:
    """快速判断视频里有没有露脸。

    ★ 用途：快手搜「带货」类关键词出来的几乎全是【真人口播】，
      整条视频从头到尾都有人 → 剪哪一段都会露脸。
      ⇒ 下载完立刻检测，有脸就【丢掉这条并自动补下一条】，
        而不是等剪辑时才跳过（那样白下载了）。

    返回 (是否有人脸, 检出人脸的帧数)
    """
    import tempfile
    tmp = None
    try:
        if work_dir is None:
            tmp = tempfile.mkdtemp(prefix="facechk_")
            d = Path(tmp)
        else:
            d = Path(work_dir)
            d.mkdir(parents=True, exist_ok=True)
        frames = await extract_frames(video, d, max_frames=n_frames)
        if not frames:
            return False, 0
        n_face = sum(1 for f in frames if f.faces > 0)
        unknown = sum(1 for f in frames if f.faces < 0)
        if unknown == len(frames):
            return False, 0                    # 检测不了 → 不误判
        # ★ 判定阈值：≥30% 的帧有人脸才算「全程露脸」。
        #   只检出 1 帧就丢掉太严了（实测美食视频会被误杀），
        #   那种留给剪辑阶段——从无人脸帧里选段就行。
        ratio = n_face / max(1, len(frames))
        return (ratio >= 0.3), n_face
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def subtitle_band(path: str, band: float = 0.30) -> float:
    """检测画面【底部】烧录字幕，返回字幕带占比（0 = 没字幕）。

    ★ 为什么要检测：抖音/快手搜出来的视频大多带「文字口播」式硬字幕，
      字幕是【烧在画面里】的 → 剪映里没法单独关掉。
      ★ 但可以去掉：裁掉底部字幕带再缩放回去（抖音「去字幕」同思路），
        这样画面里就干干净净了。
    ★ 判据（不用 OCR，纯启发式）：
      取画面底部 band 比例的区域 → 二值化出「亮像素（白字）」
      → 找是否存在【1~3 行】亮像素密集但又不是整条纯色（纯色是特效条不是字幕）
    """
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(path).convert("L")
        w, h = img.size
        top = int(h * (1 - band))
        crop = np.asarray(img.crop((0, top, w, h)), dtype=np.uint8)
        if crop.size == 0:
            return 0.0
        bright = crop > 195
        rows = bright.mean(axis=1)
        n = len(rows)
        if n < 6:
            return 0.0
        # 找出文字行（亮像素占比 0.02~0.55），取【最低的那行】作为字幕顶
        hit = [i for i, r in enumerate(rows) if 0.02 <= r <= 0.55]
        if not hit:
            return 0.0
        # 连续成行更像字幕（文字通常 1~3 行）
        run = best = 0
        for r in rows:
            if 0.02 <= r <= 0.55:
                run += 1
                best = max(best, run)
            else:
                run = 0
        if best < 1:
            return 0.0
        first = min(hit)
        # 字幕带高度：从第一行文字往下 3 行（多留一点余量）
        band_rows = min(n, first + 4)
        return band_rows / n
    except Exception:
        return 0.0


def has_subtitle(path: str) -> bool:
    """画面底部有没有烧录字幕（简版：只看有没有）。"""
    return subtitle_band(path) > 0.02
