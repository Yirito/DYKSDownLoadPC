"""装配剪映草稿（走 jianying-assistant 技能的脚本）。

按用户要求：
  * 不要配音（tts_enabled=false，不挂音频轨）
  * 不要字幕（storyboard 不写 subtitle 字段 → 不生成字幕轨）
  * 默认关闭音频（片段本身用 ffmpeg -an 裁出，本来就没声音）
  * 素材全部是本机文件 → 写成本地占位 URL，零上传
"""
from __future__ import annotations

import json
import os
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
    """本机文件 → 占位 URL（保留 /，其余编码）。步骤 5 会直接复制原件，零上传。"""
    return LOCAL_PREFIX + quote(str(path).replace("\\", "/"), safe="/:")


def skill_ready() -> tuple[bool, str]:
    if not SKILL_DIR.exists():
        return False, f"找不到剪映技能目录：{SKILL_DIR}"
    if not PY_EXE.exists():
        return False, f"找不到技能解释器：{PY_EXE}"
    return True, "ok"


def build_draft(clips: list[dict], work_dir: Path, orientation: str = "portrait",
                resolution: str = "720p", draft_name: str = "DYKS自动剪辑",
                log=print) -> dict:
    """把裁好的片段装配成剪映草稿。

    clips: [{"path": Path, "duration": float, "desc": str}, ...]
    返回 {"ok": bool, "draft_id": str, "draft_root": str, "msg": str}
    """
    ok, why = skill_ready()
    if not ok:
        return {"ok": False, "msg": why}

    work_dir.mkdir(parents=True, exist_ok=True)

    # ── storyboard：每片段一镜；【不写 subtitle 字段】→ 不生成字幕轨 ──
    shots = []
    for i, c in enumerate(clips, 1):
        dur = round(float(c.get("duration") or 1.2), 2)
        shots.append({
            "shot_id": i,
            "duration": max(0.3, dur),
            "visual_desc": c.get("desc", ""),     # 给人看的画面摘要
        })
    storyboard = {
        "aspect_ratio": "16:9" if orientation == "landscape" else "9:16",
        "resolution": resolution,
        "tts_enabled": False,                    # ★ 不要配音
        "shots": shots,
    }
    (work_dir / "storyboard.json").write_text(
        json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── materials：本机文件 → 占位 URL ──
    mats = []
    for i, c in enumerate(clips, 1):
        mats.append({
            "shot_id": i,
            "video_url": local_asset_url(Path(c["path"])),
            "image_url": None,
            "fallback": False,
            "video_engine": "user",
            "image_engine": "user",
        })
    materials = {
        "shots": mats,
        "image_engine": "user",
        "video_engine": "user",
        "resolution": resolution,
        "ratio": storyboard["aspect_ratio"],
        "fallback_count": 0,
    }
    (work_dir / "materials.json").write_text(
        json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 步骤 4：组装草稿 ──
    out_json = work_dir / "draft_result.json"
    cmd = [str(PY_EXE), str(SKILL_DIR / "scripts" / "assemble_draft.py"),
           "--materials", str(work_dir / "materials.json"),
           "--storyboard", str(work_dir / "storyboard.json"),
           "--tts_enabled", "false",            # ★ 不要配音
           "--bgm_url", "",                     # ★ 不要 BGM
           "--resolution", resolution,
           "--output", str(out_json)]
    log(f"  · 调用草稿组装服务…")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600,
                           encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "msg": f"组装调用失败：{str(e)[:120]}"}
    if r.returncode != 0 or not out_json.exists():
        err = (r.stdout or "") + (r.stderr or "")
        tail = [l for l in err.strip().splitlines() if l.strip()][-2:]
        return {"ok": False, "msg": f"组装失败(exit {r.returncode})：" + " | ".join(tail)[:200]}

    try:
        result = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "msg": f"组装结果解析失败：{str(e)[:100]}"}
    draft_id = result.get("draft_id") or ""
    if not draft_id:
        return {"ok": False, "msg": f"没拿到 draft_id：{str(result)[:150]}"}

    # ── 步骤 5：下载草稿到本机（不做这步前面全白跑）──
    dl_cmd = [str(PY_EXE), str(SKILL_DIR / "scripts" / "download_draft.py"),
              "--draft_id", draft_id,
              "--output", str(work_dir / "download_result.json")]
    log(f"  · 下载草稿到本机…")
    try:
        r2 = subprocess.run(dl_cmd, capture_output=True, timeout=600,
                            encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "draft_id": draft_id,
                "msg": f"草稿已生成但下载失败：{str(e)[:120]}"}
    try:
        dl = json.loads((work_dir / "download_result.json").read_text(encoding="utf-8"))
    except Exception:
        dl = {}
    status = dl.get("status") or ("error" if r2.returncode else "unknown")
    if status.startswith("success") and dl.get("failure_count", 0) == 0:
        return {"ok": True, "draft_id": draft_id,
                "draft_root": dl.get("draft_root", ""),
                "shots": len(shots),
                "total": result.get("total_duration_seconds", 0),
                "msg": "草稿已落盘"}
    return {"ok": False, "draft_id": draft_id,
            "draft_root": dl.get("draft_root", ""),
            "msg": f"草稿下载未完全成功（{status}，失败 {dl.get('failure_count','?')} 个）"}
