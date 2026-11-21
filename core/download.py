"""下载：流式写盘 + 实时进度条。

抖音 CDN 硬性要求 Referer=douyin.com（防盗链），快手不需要。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from engine import UA_DESKTOP, UA_MOBILE


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


async def download(plat: str, url: str, dest: Path,
                   on_progress=None) -> tuple[bool, str]:
    """下载一个视频到 dest（先写 .part，成功后改名）。

    on_progress: 可选回调 (已下载字节, 总字节)，给界面画进度条用。
    """
    import httpx
    headers = {"User-Agent": UA_DESKTOP if plat == "douyin" else UA_MOBILE}
    if plat == "douyin":
        headers["Referer"] = "https://www.douyin.com/"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=180.0),
                                     follow_redirects=True,
                                     headers=headers) as hc:
            async with hc.stream("GET", url) as r:
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}"
                total = int(r.headers.get("content-length") or 0)
                got = 0
                t0 = time.time()
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                        got += len(chunk)
                        if on_progress:
                            try:
                                on_progress(got, total)
                            except Exception:
                                pass
                        if total:
                            pct = got * 100 // total
                            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                            sys.stdout.write(
                                f"\r    [{bar}] {pct:3d}%  {human(got)}/{human(total)}")
                        else:
                            sys.stdout.write(f"\r    已下载 {human(got)}")
                        sys.stdout.flush()
                sys.stdout.write("\n")
                # ★ 必须校验是不是【真的视频】：
                #   CDN 偶尔会返回几 KB 的错误页/占位图，体积过小但 >1KB，
                #   混过去就变成「✅ 但 0.0MB」的坏文件。
                #   判定：前 12 字节里要有 mp4 的 ftyp 标识
                if got < 1024:
                    tmp.unlink(missing_ok=True)
                    return False, "文件过小，可能返回的是错误页"
                try:
                    with open(tmp, "rb") as fp:
                        head = fp.read(16)
                except Exception:
                    head = b""
                if b"ftyp" not in head:
                    tmp.unlink(missing_ok=True)
                    return False, "不是视频文件（CDN 返回了错误页），已丢弃"
                tmp.replace(dest)
                return True, f"{human(got)} · {time.time()-t0:.0f}s"
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return False, str(e)[:100]


def safe_name(s: str, maxlen: int = 60) -> str:
    import re
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (s or "").strip()).strip(" ._")
    return (s or "video")[:maxlen]


def unique_path(folder: Path, name: str, ext: str = ".mp4") -> Path:
    p = folder / f"{name}{ext}"
    i = 1
    while p.exists():
        p = folder / f"{name}({i}){ext}"
        i += 1
    return p
