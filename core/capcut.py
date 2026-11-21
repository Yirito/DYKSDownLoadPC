"""装配剪映草稿。

★ 用【本地直写】(write_draft.py) 而不是云服务组装：
  云服务产出的是剪映 **5.9 旧结构**，在本机 11.x 上会降级
  —— 表现为「素材拖不动 / 没有导入的素材 / 素材名不对」。
  本地直写按本机剪映版本生成原生结构，且不用调云服务（更快）。

按用户要求：
  * 不要配音（不挂音频轨）
  * 不要字幕（edit_plan 不写 subtitles）
  * 默认关闭音频（片段本身用 ffmpeg -an 裁出，本来就没声音）
  * 素材全是本机文件 → 零上传
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

# 技能根目录
SKILL_DIR = Path.home() / ".workbuddy" / "skills" / "jianying-assistant"
# 技能推荐的隔离解释器
PY_EXE = Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default" / "Scripts" / "python.exe"
if not PY_EXE.exists():
    PY_EXE = Path.home() / ".workbuddy" / "binaries" / "python" / "versions" / "3.13.12" / "python.exe"

LOCAL_PREFIX = "http://local.asset.invalid/__LOCAL__/"


def local_asset_url(path: Path) -> str:
    """本机文件 → 占位 URL（保留 /，其余编码）。"""
    return LOCAL_PREFIX + quote(str(path).replace("\\", "/"), safe="/:")


def skill_ready() -> tuple[bool, str]:
    if not SKILL_DIR.exists():
        return False, f"找不到剪映技能目录：{SKILL_DIR}"
    if not PY_EXE.exists():
        return False, f"找不到技能解释器：{PY_EXE}"
    return True, "ok"


def _probe_size(path: Path) -> tuple[int, int]:
    """读视频宽高（ffprobe 优先，ffmpeg 兜底）。"""
    import re
    exe = shutil.which("ffprobe")
    if exe:
        try:
            r = subprocess.run([exe, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height",
                                "-of", "csv=p=0:s=x", str(path)],
                               capture_output=True, timeout=30,
                               encoding="utf-8", errors="replace")
            parts = (r.stdout or "").strip().split("\n")[0].split("x")
            return int(parts[0]), int(parts[1])
        except Exception:
            pass
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import clip as clip_mod
        r = subprocess.run([clip_mod.ffmpeg_exe(), "-i", str(path)],
                           capture_output=True, timeout=60,
                           encoding="utf-8", errors="replace")
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr or "")
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 720, 1280


def _probe_duration(path: Path) -> float:
    import re
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import clip as clip_mod
        r = subprocess.run([clip_mod.ffmpeg_exe(), "-i", str(path)],
                           capture_output=True, timeout=60,
                           encoding="utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def build_draft(clips: list[dict], work_dir: Path, orientation: str = "portrait",
                resolution: str = "720p", draft_name: str = "DYKS自动剪辑",
                log=print, draft_root: str = "") -> dict:
    """把裁好的片段装配成剪映草稿（本地直写，11.x 原生结构）。

    clips: [{"path": Path, "duration": float, "desc": str, "name": str}, ...]
    返回 {"ok": bool, "draft_id": str, "draft_root": str, "msg": str}
    """
    ok, why = skill_ready()
    if not ok:
        return {"ok": False, "msg": why}

    work_dir.mkdir(parents=True, exist_ok=True)
    ratio = "16:9" if orientation == "landscape" else "9:16"

    # ── media_index.json：素材清单（write_draft 只认 id 与 path）──
    items = []
    for i, c in enumerate(clips, 1):
        p = Path(c["path"])
        w, h = _probe_size(p)
        dur = float(c.get("duration") or _probe_duration(p) or 1.5)
        items.append({
            "id": f"v{i:03d}",
            "type": "video",
            "path": str(p),              # ★ 本机绝对路径（本地直写不需要占位 URL）
            "duration": round(dur, 3),
            "width": w, "height": h, "fps": 30.0,
            "vcodec": "h264", "pix_fmt": "yuv420p", "has_audio": False,
            "needs_transcode": False, "transcode_reason": [],
            "material_name": c.get("name") or p.stem,   # ★ 素材名用原文件名
        })
    media_index = {
        "items": items,
        "summary": {"video_count": len(items), "image_count": 0, "audio_count": 0,
                    "total_video_duration": round(sum(i["duration"] for i in items), 3)},
    }
    (work_dir / "media_index.json").write_text(
        json.dumps(media_index, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── edit_plan.json：时间轴（★ 不要字幕、不要配音、不要原声）──
    timeline = []
    t = 0.0
    for i, c in enumerate(clips):
        d = round(float(c.get("duration") or 1.5), 2)
        timeline.append({
            "index": i,
            "source_id": f"v{i+1:03d}",
            "src_in": 0.0,
            "src_out": d,
            "duration": d,
            "volume": 0.0,               # ★ 关闭音频
        })
        t += d
    edit_plan = {
        "meta": {"title": draft_name, "resolution": resolution,
                 "aspect_ratio": ratio, "fps": 30},
        "timeline": timeline,
        "transitions": [],
        "overlays": [],
        "audio": {"keep_original": False, "original_volume": 0.0},
        "tts": {"enabled": False},
        "subtitles": [],                # ★ 不要字幕
        "titles": [],                   # ★ 不要标题
    }
    (work_dir / "edit_plan.json").write_text(
        json.dumps(edit_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 本地直写草稿 ──
    cmd = [str(PY_EXE), str(SKILL_DIR / "scripts" / "write_draft.py"),
           "--plan", str(work_dir / "edit_plan.json"),
           "--media-index", str(work_dir / "media_index.json"),
           "--name", draft_name,
           "--json"]
    if draft_root:
        cmd += ["--draft-root", draft_root]
    log("  · 本地直写剪映草稿（11.x 原生结构）…")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600,
                           encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "msg": f"草稿直写调用失败：{str(e)[:120]}"}
    out = (r.stdout or "").strip()
    res = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                res = json.loads(line)
                break
            except Exception:
                pass
    if r.returncode != 0 and not res:
        err = (r.stderr or out)[-300:]
        return {"ok": False, "msg": f"草稿直写失败(exit {r.returncode})：{err}"}
    if not res:
        return {"ok": False, "msg": f"没拿到草稿结果：{(out or r.stderr)[-150:]}"}

    draft_id = str(res.get("draft_id") or res.get("draft_folder")
                   or res.get("name") or "")
    root = str(res.get("draft_root") or res.get("draft_path") or "")
    # ★ write_draft 按【草稿名】建目录（不是 draft_id）
    if not root:
        base = Path(draft_root) if draft_root else Path("D:/JianyingPro Drafts")
        root = str(base / (draft_id or draft_name))
    return {
        "ok": True,
        "draft_id": draft_id or draft_name,
        "draft_root": root,
        "shots": len(timeline),
        "total": round(t, 2),
        "msg": res.get("message") or "草稿已生成",
        "raw": res,
    }
