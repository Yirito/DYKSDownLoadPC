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
    reason: str = ""


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


async def extract_frames(video: Path, out_dir: Path, fps: float = 2.5,
                         max_frames: int = 10, total: float = 0.0) -> list[Frame]:
    """按【全片均匀采样】抽候选帧（jpg），并给每帧算「画面信息量」分数。

    ★ 为什么不用 ffmpeg 的 fps 滤镜：那是把整片都解码一遍（191 秒的片子
      要几十秒，而且实测只吐出开头几秒的帧）。
      改成按时间点精确 seek 抽帧：N 次快速 seek，几秒搞定，且覆盖全片。

    分数高 = 这一帧画面有真实内容（不是黑屏/静止封面/片头）。
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
        return (Frame(path=str(fp), t=round(t, 2),
                      score=round(_frame_score(str(fp)), 2))
                if (rc == 0 and fp.exists() and fp.stat().st_size > 500) else None)

    # ★ 并发抽帧：串行要 90 秒，并发只要十几秒
    tasks = [_one(i, total * (i + 0.5) / n) for i in range(n)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Frame):
            frames.append(r)
    frames.sort(key=lambda f: f.t)
    return frames


def pick_segment(frames: list[Frame], dur_min: float = 1.0,
                 dur_max: float = 1.5, total: float = 0.0) -> tuple[float, float]:
    """本地选段：挑信息量最高的帧，往前退一点开始，裁 dur_max 秒。

    返回 (start, duration)
    """
    if not frames:
        return 0.0, dur_max
    best = max(frames, key=lambda f: f.score)
    start = max(0.0, best.t - 0.35)
    if total and start + dur_max > total:
        start = max(0.0, total - dur_max)
    return round(start, 2), dur_max


# ------------------------------------------------------------------ 裁片段

async def cut_clip(video: Path, start: float, dur: float, out: Path,
                   orientation: str = "portrait") -> tuple[bool, str]:
    """裁出 [start, start+dur] 的片段，并统一成目标比例（不留黑边）。

    orientation: portrait（9:16 竖屏）/ landscape（16:9 横屏）
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if orientation == "landscape":
        vf = ("scale=1280:720:force_original_aspect_ratio=increase,"
              "crop=1280:720,setsar=1")
    else:
        vf = ("scale=720:1280:force_original_aspect_ratio=increase,"
              "crop=720:1280,setsar=1")
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
