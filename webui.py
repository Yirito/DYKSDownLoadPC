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
PLAT_DIR = {"douyin": "抖音", "kuaishou": "快手"}
PLAT_NAME = {"douyin": "抖音", "kuaishou": "快手"}

# 任务状态（单任务，够用）
JOB: dict = {"running": False, "phase": "", "log": [], "items": [],
             "out_root": "", "done": 0, "total": 0, "error": ""}
_lock = threading.Lock()


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


# ------------------------------------------------------------------ 后台任务

async def run_job(keyword: str, dl_dir: Path, count: int, orient: str,
                  plats: tuple[str, ...]) -> None:
    """搜索 + 下载，全程把进度写进 JOB。"""
    out_root = dl_dir / f"{today_cn()}_{safe_name(keyword, 40)}"
    for p in plats:
        (out_root / PLAT_DIR[p]).mkdir(parents=True, exist_ok=True)
    with _lock:
        JOB.update({"running": True, "phase": "准备中", "items": [], "log": [],
                    "out_root": str(out_root), "done": 0, "total": 0, "error": ""})
    logline(f"输出目录：{out_root}")

    idx = 0
    for plat in plats:
        # ① 检查登录/验证
        ok, why = await auth.probe_remote(plat, keyword)
        if not ok:
            logline(f"{PLAT_NAME[plat]} 需要登录/验证：{why}", "warn")
            set_item(idx, plat=plat, name="（需要登录/验证）", state="need_auth",
                     pct=0, size=0, wh="")
            idx += 1
            with _lock:
                JOB["phase"] = f"等待 {PLAT_NAME[plat]} 登录/验证"
            logline(f"⚠ 请在弹出的浏览器里完成"
                    f"{'扫码登录' if plat == 'douyin' else '滑块验证'}，完成后本窗口会自动继续…")
            done = await auth.prepare(plat)
            ok, why = await auth.probe_remote(plat, keyword)
            if not done or not ok:
                logline(f"{PLAT_NAME[plat]} 仍未就绪（{why}），跳过", "err")
                set_item(idx, plat=plat, name="（未就绪）", state="failed", pct=0,
                         size=0, wh="")
                idx += 1
                continue

        # ② 搜索
        with _lock:
            JOB["phase"] = f"搜索 {PLAT_NAME[plat]}"
        logline(f"正在搜索 {PLAT_NAME[plat]}：{keyword}")
        try:
            items = await search_mod.search(plat, keyword, limit=count * 3)
        except auth.AuthExpired as e:
            logline(f"{e}，重新打开浏览器…", "warn")
            await auth.prepare(plat)
            try:
                items = await search_mod.search(plat, keyword, limit=count * 3)
            except Exception as e2:
                logline(f"{PLAT_NAME[plat]} 搜索失败：{str(e2)[:100]}", "err")
                set_item(idx, plat=plat, name="（搜索失败）", state="failed",
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
        picked = picked[:count]
        logline(f"{PLAT_NAME[plat]} 命中 {len(items)} 条，选中 {len(picked)} 条")

        # ③ 下载
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
            set_item(my, state="done" if good else "failed",
                     pct=100 if good else 0,
                     path=str(dest) if good else "", note=msg)
            if good:
                with _lock:
                    JOB["done"] += 1
                logline(f"✅ {dest.name}  {msg}")
            else:
                logline(f"❌ {it.title[:30]}  {msg}", "err")

    with _lock:
        JOB["running"] = False
        JOB["phase"] = "完成"
    logline(f"全部完成：{JOB['done']} / {JOB['total']} 个文件")


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "DYKS/1.0"

    def log_message(self, *a):        # 静音默认日志
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
        p = urlparse(self.path).path
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
        if p == "/api/status":
            out = []
            for plat in ("douyin", "kuaishou"):
                ok, why = probe_sync(plat)
                out.append({"plat": plat, "name": PLAT_NAME[plat], "ok": ok,
                            "why": why, "local": auth.has_local_auth(plat)})
            self._json({"items": out})
            return
        if p == "/api/pickdir":
            path = pick_dir()
            self._json({"dir": path})
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
            plats = tuple(body.get("platforms") or ("douyin", "kuaishou"))
            th = threading.Thread(target=lambda: asyncio.run(
                run_job(keyword, dl, count, orient, plats)), daemon=True)
            th.start()
            self._json({"ok": True})
            return
        if p == "/api/prepare":
            plat = body.get("plat") or "douyin"
            threading.Thread(target=lambda: asyncio.run(
                auth.prepare(plat)), daemon=True).start()
            self._json({"ok": True})
            return
        self._send(404, b"{}")


def probe_sync(plat: str):
    """在同步 handler 里跑一次异步探测（每次用独立的事件循环）。"""
    try:
        return asyncio.run(auth.probe_remote(plat))
    except Exception as e:
        return False, str(e)[:80]


def pick_dir() -> str:
    """Windows 上弹原生目录选择框（跨平台就退回输入框）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        d = filedialog.askdirectory(title="选择下载目录")
        root.destroy()
        if d:
            return d
    except Exception:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser(description="DYKSDownLoadPC 本地可视化界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dir", default=str(Path.cwd() / "downloads"))
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"▶ DYKSDownLoadPC 可视化界面：{url}")
    print(f"  默认下载目录：{args.dir}")
    print("  关闭这个黑窗口即停止服务")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
