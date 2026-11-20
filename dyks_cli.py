#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DYKSDownLoadPC —— PC 桌面端：抖音 / 快手 关键词批量下载（无水印 · 最高质量）

用法：
  # 交互式（推荐）
  python dyks_cli.py

  # 直接给参数
  python dyks_cli.py -k 象棋 -d "D:\\视频" -n 20 -o portrait

  # 提前把登录/验证做好（可选；过期了工具会自动再弹一次）
  python dyks_cli.py --prepare

  # 看看当前登录态/验证态还有效没
  python dyks_cli.py --status

产物：
  <下载目录>/
    2026年11月20日_关键字/
      抖音/  xxx.mp4
      快手/  xxx.mp4
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "core"))

import auth  # noqa: E402
import search as search_mod  # noqa: E402
from download import download, safe_name, unique_path  # noqa: E402

PLAT_DIR = {"douyin": "抖音", "kuaishou": "快手"}
PLAT_NAME = {"douyin": "抖音", "kuaishou": "快手"}


def today_cn() -> str:
    d = datetime.now()
    return f"{d.year}年{d.month}月{d.day}日"


def ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return v or default


# ------------------------------------------------------------------ 登录态

async def show_status() -> None:
    print("=== 登录 / 验证状态 ===")
    for plat in ("douyin", "kuaishou"):
        local = auth.has_local_auth(plat)
        ok, why = await auth.probe_remote(plat)
        mark = "✅" if ok else ("⚠ " if local else "❌")
        print(f"  {mark} {PLAT_NAME[plat]}: 本地凭证={'有' if local else '无'}  "
              f"实际可用={'是' if ok else '否'}（{why}）")
    print("\n提示：跑 `python dyks_cli.py --prepare` 可以扫码登录 / 过人机验证。")


async def ensure_auth(plat: str, keyword: str) -> bool:
    """确保该平台能搜：先探，探不过就弹浏览器让用户处理。"""
    ok, why = await auth.probe_remote(plat, keyword)
    if ok:
        return True
    print(f"\n⚠  {PLAT_NAME[plat]} 目前搜不了：{why}")
    print(f"   正在打开浏览器，请你完成【{'扫码登录' if plat == 'douyin' else '滑块验证'}】…")
    done = await auth.prepare(plat)
    if not done:
        print(f"   ❌ {PLAT_NAME[plat]} 仍未就绪，先跳过它")
        return False
    ok, why = await auth.probe_remote(plat, keyword)
    print(f"   {'✅' if ok else '❌'} {PLAT_NAME[plat]} {'就绪' if ok else '仍未生效：' + why}")
    return ok


# ------------------------------------------------------------------ 主流程

async def run(keyword: str, dl_dir: Path, count: int, orient: str,
              plats: tuple[str, ...], dry_run: bool, headless: bool) -> None:
    out_root = dl_dir / f"{today_cn()}_{safe_name(keyword, 40)}"
    for p in plats:
        (out_root / PLAT_DIR[p]).mkdir(parents=True, exist_ok=True)
    print(f"\n📁 输出目录：{out_root}")
    print(f"   方向：{orient}   每个平台：{count} 条")

    grand = 0
    for plat in plats:
        print(f"\n────────── 【{PLAT_DIR[plat]}】 ──────────")
        if not await ensure_auth(plat, keyword):
            continue
        print("  🔍 搜索中…")
        try:
            items = await search_mod.search(plat, keyword, limit=count * 3,
                                            headless=headless)
        except auth.AuthExpired as e:
            print(f"  ⚠  {e}")
            print("     正在打开浏览器，请完成验证…")
            if not await auth.prepare(plat):
                print("     ❌ 未完成，跳过")
                continue
            try:
                items = await search_mod.search(plat, keyword, limit=count * 3,
                                                headless=headless)
            except Exception as e2:
                print(f"     ❌ 仍然失败：{str(e2)[:100]}")
                continue
        except Exception as e:
            print(f"  ❌ 搜索失败：{str(e)[:120]}")
            continue

        if not items:
            print("  没搜到结果")
            continue
        if orient == "portrait":
            picked = [i for i in items if i.portrait or not (i.width and i.height)]
        elif orient == "landscape":
            picked = [i for i in items if i.width and i.height and i.width > i.height]
        else:
            picked = list(items)
        picked = picked[:count]
        print(f"  命中 {len(items)} 条 → 选中 {len(picked)} 条")
        folder = out_root / PLAT_DIR[plat]
        for i, it in enumerate(picked, 1):
            wh = f"{it.width}×{it.height}" if it.width else "尺寸未知"
            print(f"   {i:2d}. {wh:10s} {it.size_mb:6.2f}MB  {it.desc[:36]}")
            if dry_run:
                continue
            dest = unique_path(folder, safe_name(it.title))
            good, msg = await download(plat, it.play_url, dest)
            print(f"       {'✅' if good else '❌'} {dest.name}  {msg}")
            grand += 1 if good else 0
    print(f"\n✅ 完成 {grand} 个文件")
    print(f"   目录：{out_root}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="抖音/快手 关键词批量下载（无水印 · 最高质量）",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--keyword", "-k", help="搜索关键字")
    ap.add_argument("--dir", "-d", help="下载目录（默认 ./downloads）")
    ap.add_argument("--count", "-n", type=int, default=10, help="每个平台下载多少条（默认 10）")
    ap.add_argument("--orientation", "-o", choices=["portrait", "landscape", "both"],
                    default="portrait", help="画面方向：portrait 竖屏(默认) / landscape 横屏 / both")
    ap.add_argument("--platform", "-p", choices=["douyin", "kuaishou", "both"], default="both")
    ap.add_argument("--prepare", action="store_true", help="提前扫码登录 / 过人机验证")
    ap.add_argument("--status", action="store_true", help="查看登录/验证状态")
    ap.add_argument("--show-browser", action="store_true", help="搜索时显示浏览器窗口")
    ap.add_argument("--dry-run", action="store_true", help="只列清单不下载")
    args = ap.parse_args()

    if args.status:
        await show_status()
        return
    if args.prepare:
        for plat in ("douyin", "kuaishou"):
            if args.platform != "both" and args.platform != plat:
                continue
            await auth.prepare(plat)
        return

    interactive = args.keyword is None
    keyword = args.keyword or ask("关键字（如 象棋）：")
    if not keyword:
        print("❌ 没有关键字")
        return
    dl_dir = Path(args.dir or ask("下载目录：", str(Path.cwd() / "downloads")))
    count = args.count
    if interactive:
        v = ask(f"每个平台下载多少条 [{count}]：")
        count = int(v) if v.isdigit() else count
    orient = args.orientation
    if interactive and args.orientation == "portrait":
        v = ask("画面方向（1=竖屏 2=横屏 3=都要）[1]：", "1")
        orient = {"1": "portrait", "2": "landscape", "3": "both"}.get(v, "portrait")
    plats = ("douyin", "kuaishou") if args.platform == "both" else (args.platform,)

    await run(keyword, dl_dir, count, orient, plats, args.dry_run, not args.show_browser)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
