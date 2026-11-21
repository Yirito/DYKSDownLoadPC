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
}

# 目录选择弹窗的状态（后台弹，前端轮询取结果）
PICK_STATE = {"dir": "", "running": False}

# 任务状态（单任务，够用）
JOB: dict = {"running": False, "phase": "", "log": [], "items": [],
             "out_root": "", "done": 0, "total": 0, "error": ""}
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

    ★ 用 System.Windows.Forms.FolderBrowserDialog：
      标准托管 UI、自带消息泵，本地桌面必弹
      （比 ctypes 直调 SHBrowseForFolder 稳，后者在非 UI 线程里常静默返回 None）。
    ★ 关键：必须【置顶】。原来的写法弹出来的窗体可能压在浏览器窗口底下，
      用户根本看不见 → 反馈就是「点不了」。
      做法：建一个 TopMost 的空 Form 当父窗体，把对话框挂上去。
    ★ 脚本走 -EncodedCommand（base64 UTF-16LE），彻底避免 shell 字符串解析问题。
    """
    import base64
    import subprocess
    try:
        ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
              "Add-Type -AssemblyName System.Windows.Forms;"
              # ★ 这个 Form 只是用来当「置顶父窗体」的，必须【完全不可见】：
              #   移到屏幕外 + 1x1 像素 + 全透明，用户看不见它，
              #   但它 TopMost，对话框就会跟着被顶到最前。
              "$f = New-Object System.Windows.Forms.Form;"
              "$f.TopMost = $true;"
              "$f.ShowInTaskbar = $false;"
              "$f.StartPosition = 'Manual';"
              # 放在屏幕中心（1x1 全透明 → 看不见），对话框会以它为父窗体居中显示
              "$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea;"
              "$f.Location = New-Object System.Drawing.Point("
              "  [int]($wa.Width/2), [int]($wa.Height/2));"
              "$f.Size = New-Object System.Drawing.Size(1,1);"
              "$f.Opacity = 0;"
              "$f.Show();"
              "$f.Activate();"
              "$f.BringToFront();"
              "[System.Windows.Forms.SendKeys]::SendWait('%');"
              "Start-Sleep -Milliseconds 300;"
              "$f.Activate();"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              "$d.Description = '选择下载目录';"
              "$d.ShowNewFolderButton = $true;")
        if initial and os.path.isdir(initial):
            ps += "$d.SelectedPath = '%s';" % initial.replace("'", "''")
        ps += ("if ($d.ShowDialog($f) -eq 'OK') { $d.SelectedPath }"
               " $f.Close()")
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", enc],
            capture_output=True, timeout=180, encoding="utf-8", errors="replace",
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
    out = []
    for plat in ("douyin", "kuaishou"):
        # ★ 用 check_auth（只看登录/验证状态），不要用「必须搜到结果」当标准
        try:
            ok, why = check_sync(plat)
        except Exception as e:
            ok, why = False, f"探测异常：{type(e).__name__}: {str(e)[:80]}"
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
        _refresh_status_worker()          # 首屏：同步探一次
    # ★ 刷新完必须重新读一次缓存 —— 否则返回的还是刷新【之前】的旧值
    with _lock:
        items = list(STATUS_CACHE["items"])
    return items


# ------------------------------------------------------------------ 后台任务

async def run_job(keyword: str, dl_dir: Path, count: int, orient: str,
                  plats: tuple[str, ...]) -> None:
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
    for plat in plats:
        # ★ 不再【预先探测登录态】—— 预检查会误判（探测口径和搜索口径不一致，
        #   于是出现「状态栏说可用、下载却提示待登录」）。
        #   直接开搜：搜索接口自己会告诉我们要不要登录/验证（AuthExpired），
        #   搜得到就是搜得到，搜不到就如实说「没搜到结果」。
        with _lock:
            JOB["phase"] = f"搜索 {PLAT_NAME[plat]}"
        logline(f"正在搜索 {PLAT_NAME[plat]}：{keyword}")
        try:
            items = await search_mod.search(plat, keyword, limit=count * 3)
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
        picked = picked[:count]
        logline(f"{PLAT_NAME[plat]} 命中 {len(items)} 条，选中 {len(picked)} 条")

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
            plats = tuple(body.get("platforms") or ("douyin", "kuaishou"))
            save_config({"dir": str(dl), "count": count, "orientation": orient,
                         "platforms": list(plats)})
            th = threading.Thread(target=lambda: asyncio.run(
                run_job(keyword, dl, count, orient, plats)), daemon=True)
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
