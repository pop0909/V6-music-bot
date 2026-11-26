"""Bilibili 面板搜尋來源切換截圖（本機 sim.py，不連外、不碰正式面板）。
用法：先開 sim.py，再 python tools/shot_bili.py [輸出資料夾]"""
import asyncio, json, os, sys
from playwright.async_api import async_playwright

BASE = os.getenv("SMOKE_BASE", "http://127.0.0.1:5055")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/bili-shots"
OWNER = os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")
os.makedirs(OUT, exist_ok=True)

PNG1 = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")

FAKE_RESULTS = {
    "status": "ok", "count": 3, "source": "bilibili",
    "results": [
        {"url": "https://www.bilibili.com/video/BV1rTy2zFE7f", "title": "模擬：接下來會很瘋狂【原神】",
         "duration": 309, "thumbnail": "https://i0.hdslb.com/bfs/cover/sim1.jpg"},
        {"url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "模擬：B 站搜尋結果二",
         "duration": 180, "thumbnail": "https://i0.hdslb.com/bfs/cover/sim2.jpg"},
        {"url": "https://www.bilibili.com/video/BV1GJ411x7h7", "title": "模擬：B 站搜尋結果三",
         "duration": 95, "thumbnail": "https://i0.hdslb.com/bfs/cover/sim3.jpg"},
    ],
}


async def offline_guard(route):
    u = route.request.url
    if "/api/search" in u and route.request.method == "POST":
        return await route.fulfill(status=200, content_type="application/json; charset=utf-8",
                                   body=json.dumps(FAKE_RESULTS, ensure_ascii=False))
    if u.startswith(BASE) or "127.0.0.1" in u or "localhost" in u:
        return await route.continue_()
    if route.request.resource_type == "image" or "hdslb.com" in u or "ytimg.com" in u:
        return await route.fulfill(status=200, content_type="image/png", body=PNG1)
    return await route.abort()


async def shot(pg, name):
    path = os.path.join(OUT, name + ".png")
    await pg.screenshot(path=path, full_page=False)
    print("wrote", path, flush=True)


async def login(pg):
    await pg.goto(BASE + "/", wait_until="domcontentloaded")
    if "/login" in pg.url:
        await pg.fill("#key", OWNER)
        await pg.click("button[type=submit]")
        await pg.wait_for_function("() => !location.pathname.includes('/login')", timeout=8000)
    await pg.wait_for_timeout(300)


async def pick_guild(pg):
    await pg.evaluate("""() => {
        const sel = document.getElementById('guildSelect');
        if (!sel) return;
        const opt = [...sel.options].find(o => o.value);
        if (!opt) return;
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        try { localStorage.setItem('mb_guild', opt.value); } catch (e) {}
    }""")
    await pg.wait_for_function("() => !document.getElementById('guildOverlay')?.classList.contains('active') && document.getElementById('guildSelect')?.value", timeout=8000)
    await pg.wait_for_timeout(300)


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True)
        ctx = await br.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        await ctx.route("**/*", offline_guard)
        pg = await ctx.new_page()
        await login(pg)
        await pick_guild(pg)
        await pg.evaluate("() => { try { localStorage.removeItem('mb_search_src'); } catch (e) {} }")
        await pg.reload(wait_until="domcontentloaded")
        await pick_guild(pg)
        await pg.wait_for_selector("#searchInput", timeout=8000)
        await pg.wait_for_selector(".search-src", timeout=8000)
        # 1) 預設 YouTube
        await shot(pg, "01_search_youtube")
        # 2) 切到 Bilibili（空搜尋框，看 placeholder 與選取狀態）
        await pg.click("#searchSrcBili")
        await pg.wait_for_timeout(200)
        await shot(pg, "02_search_bilibili")
        # 3) 搜尋結果（走 stub，不連 B 站）
        await pg.fill("#searchInput", "原神")
        await pg.click("#btnSearch")
        await pg.wait_for_selector(".search-result-item", timeout=8000)
        await pg.wait_for_timeout(300)
        await shot(pg, "03_search_bilibili_results")
        await br.close()
    print("done", OUT)

if __name__ == "__main__":
    asyncio.run(main())
