"""T-11 第 1 項面板截圖（本機 sim.py，攔掉所有外網含 discord.com）。
用法：python tools/shot_t11_ux.py [輸出資料夾]
未設定 SMOKE_BASE 時會自己啟動 tools/sim.py。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request

from playwright.async_api import async_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/t11-ux-shots"
OWNER = os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")
os.makedirs(OUT, exist_ok=True)

PNG1 = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")

YT_RESULTS = {
    "status": "ok", "count": 3, "source": "youtube",
    "results": [
        {"url": "https://www.youtube.com/watch?v=dQw4w9wgCcc", "title": "永不失聯的愛",
         "duration": 245, "thumbnail": "https://i.ytimg.com/vi/dQw4w9wgCcc/hqdefault.jpg", "channel": "周興哲 Eric Chou"},
        {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "模擬搜尋結果二",
         "duration": 180, "thumbnail": "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg", "channel": "示範頻道"},
        {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "title": "模擬搜尋結果三",
         "duration": 95, "thumbnail": "https://i.ytimg.com/vi/bbbbbbbbbbb/hqdefault.jpg", "channel": "Another Channel"},
    ],
}
BILI_RESULTS = {
    "status": "ok", "count": 3, "source": "bilibili",
    "results": [
        {"url": "https://www.bilibili.com/video/BV1rTy2zFE7f", "title": "模擬：接下來會很瘋狂【原神】",
         "duration": 309, "thumbnail": "https://i0.hdslb.com/bfs/cover/sim1.jpg", "author": "原神UP主"},
        {"url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "模擬：B 站搜尋結果二",
         "duration": 180, "thumbnail": "https://i0.hdslb.com/bfs/cover/sim2.jpg", "author": "示範UP"},
        {"url": "https://www.bilibili.com/video/BV1GJ411x7h7", "title": "模擬：B 站搜尋結果三",
         "duration": 95, "thumbnail": "https://i0.hdslb.com/bfs/cover/sim3.jpg", "author": "鋼琴收藏夾"},
    ],
}

mode = {"search": "results", "source": "youtube", "play": "normal"}
BASE = os.getenv("SMOKE_BASE", "")


async def offline_guard(route):
    u = route.request.url
    method = route.request.method
    if "/api/search" in u and method == "POST":
        if mode["search"] == "hang":
            await asyncio.sleep(30)
            try:
                return await route.abort()
            except Exception:
                return
        body = BILI_RESULTS if mode["source"] == "bilibili" else YT_RESULTS
        return await route.fulfill(status=200, content_type="application/json; charset=utf-8",
                                   body=json.dumps(body, ensure_ascii=False))
    if "/api/queue/play" in u and method == "POST" and mode["play"] == "slow":
        await asyncio.sleep(20)
        try:
            return await route.fulfill(status=200, content_type="application/json; charset=utf-8",
                                       body=json.dumps({"status": "playing", "title": "模擬歌曲 2", "accepted": True}, ensure_ascii=False))
        except Exception:
            return
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


def start_sim():
    import socket, subprocess
    py = sys.executable
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, SIM_PORT=str(port), SIM_SONG_LEN=os.getenv("SIM_SONG_LEN", "20"),
               SIM_DATA=tempfile.mkdtemp(prefix="v6sim-"), PYTHONIOENCODING="utf-8")
    log = open(os.path.join(env["SIM_DATA"], "sim.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([py, os.path.join(ROOT, "tools", "sim.py")], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"模擬器啟動失敗（看 {log.name}）")
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/login", headers={"Host": "localhost"}), timeout=2)
            return proc, base
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("模擬器 60 秒內沒有回應")


def stop_sim(proc):
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(10)
    except Exception:
        proc.kill()


async def main():
    global BASE
    proc = None
    if not BASE:
        proc, BASE = start_sim()
        print("sim", BASE, flush=True)
    try:
        async with async_playwright() as p:
            br = await p.chromium.launch(headless=True)
            ctx = await br.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
            await ctx.route("**/*", offline_guard)
            pg = await ctx.new_page()
            await login(pg)
            await pick_guild(pg)
            await pg.wait_for_selector("#searchInput", timeout=8000)

            # 1) 搜尋中
            mode["search"] = "hang"
            await pg.fill("#searchInput", "永不失聯的愛")
            await pg.click("#btnSearch")
            await pg.wait_for_selector(".search-loading-text", timeout=8000)
            await pg.wait_for_timeout(250)
            await shot(pg, "01_search_loading")

            # 2) YouTube 結果含頻道名（取消上一筆還在等的搜尋，否則搜尋鈕會一直停用）
            mode["search"] = "results"
            mode["source"] = "youtube"
            await pg.evaluate("() => { if (typeof clearSearchResults === 'function') clearSearchResults(); }")
            await pg.fill("#searchInput", "永不失聯的愛")
            await pg.click("#btnSearch")
            await pg.wait_for_selector(".search-result-item .result-channel", timeout=8000)
            await pg.wait_for_timeout(300)
            await shot(pg, "02_search_results_channel")

            # 3) 加歌後清單仍在
            await pg.click(".search-result-item")
            await pg.wait_for_timeout(800)
            n = await pg.locator(".search-result-item").count()
            print("results after add:", n, flush=True)
            if n < 1:
                raise SystemExit("加歌後搜尋清單被關掉了")
            await shot(pg, "03_search_after_add")

            # 4) B 站結果含 UP 主
            mode["source"] = "bilibili"
            await pg.click("#searchSrcBili")
            await pg.fill("#searchInput", "原神")
            await pg.click("#btnSearch")
            await pg.wait_for_selector(".search-result-item .result-channel", timeout=8000)
            await pg.wait_for_timeout(300)
            await shot(pg, "04_search_results_bilibili_up")

            # 5) 播這首：切換中…
            await pg.evaluate("""async () => {
                for (let i = 1; i <= 2; i++) {
                    await fetch('/api/queue/add', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({url: 'https://www.youtube.com/watch?v=SIMSONG' + String(i).padStart(4,'0'), title: '模擬歌曲 ' + i})});
                }
                if (typeof openNpView === 'function') openNpView();
            }""")
            await pg.wait_for_selector("#npQueueList .list-item .btn-play", timeout=8000)
            await pg.wait_for_timeout(400)
            mode["play"] = "slow"
            btns = pg.locator("#npQueueList .list-item .btn-play")
            await btns.nth(1).click()
            await pg.wait_for_function("() => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes('切換中'))", timeout=5000)
            await pg.wait_for_timeout(200)
            await shot(pg, "05_play_switching")
            mode["play"] = "normal"

            # 6) 網址視窗出錯不關閉（程式觸發開啟，避免展開播放器擋住分頁按鈕）
            await pg.evaluate("""() => {
                if (typeof setNpOpen === 'function') setNpOpen(false);
                const tab = document.querySelector('[data-tab="playlist"]');
                if (tab) tab.click();
                document.getElementById('btnPlaylistAdd')?.click();
            }""")
            await pg.wait_for_selector("#editModal.active", timeout=5000)
            await pg.fill("#editUrl", "https://evil.example.com/song")
            await pg.fill("#editTitle", "不該被加入的歌")
            await pg.click("#modalConfirm")
            await pg.wait_for_function("() => document.getElementById('editModal')?.classList.contains('active') && document.getElementById('editUrl')?.value.includes('evil.example.com')", timeout=5000)
            await pg.wait_for_timeout(400)
            await shot(pg, "06_url_modal_keeps_input")
            await br.close()
        print("done", OUT)
    finally:
        stop_sim(proc)


if __name__ == "__main__":
    asyncio.run(main())
