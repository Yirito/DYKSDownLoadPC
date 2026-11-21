import asyncio, json, urllib.request
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, channel="chrome", args=["--no-sandbox"])
        pg = await (await b.new_context(viewport={"width":1180,"height":900})).new_page()
        errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)[:100]))
        await pg.goto("http://127.0.0.1:8790/", wait_until="commit", timeout=30000)
        await asyncio.sleep(3)
        await pg.click("#openSettings"); await asyncio.sleep(1)
        await pg.click("#openLog"); await asyncio.sleep(2)
        print("③ 打开日志:", (await pg.locator("#draftInfo").text_content() or "")[:70])
        await pg.click("#cancelSettings"); await asyncio.sleep(1)
        # ② 模拟"生成中"：直接调 onclick
        await pg.evaluate("document.getElementById('mkDraft').disabled=false; document.getElementById('mkDraft').click()")
        await asyncio.sleep(2)
        print("② 生成中 disabled=", await pg.locator("#mkDraft").is_disabled(),
              " 文本=", repr((await pg.locator("#mkDraft").text_content() or "")))
        print("   有 spinner 图标:", await pg.locator("#mkDraft .spin").count() > 0)
        print("页面错误:", errs or "无 ✅")
        await b.close()
asyncio.run(main())
