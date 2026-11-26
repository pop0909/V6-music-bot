"""T-12 第 2 項面板截圖（本機 sim.py，攔掉所有外網含 discord.com）。
用法：python tools/shot_t12_panel.py [輸出資料夾]
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
OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/t12-panel-shots"
OWNER = os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")
os.makedirs(OUT, exist_ok=True)

PNG1 = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")

# A slightly nicer 64x64 placeholder (solid purple-ish) so covers aren't invisible
PNG64 = None

BASE = os.getenv("SMOKE_BASE", "")


async def offline_guard(route):
    u = route.request.url
    if u.startswith(BASE) or "127.0.0.1" in u or "localhost" in u:
        return await route.continue_()
    if route.request.resource_type == "image" or "hdslb.com" in u or "ytimg.com" in u or "discord" in u:
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
    await pg.wait_for_function("() => !document.documentElement.classList.contains('ui-loading')", timeout=8000)


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
    await pg.wait_for_function(
        "() => !document.getElementById('guildOverlay')?.classList.contains('active') && document.getElementById('guildSelect')?.value",
        timeout=8000,
    )
    await pg.wait_for_timeout(250)


async def seed_queue(pg, n=12):
    await pg.evaluate("""async (n) => {
        const g = state.guildId;
        const songs = [];
        for (let i = 1; i <= n; i++) {
            songs.push({
                url: 'https://www.youtube.com/watch?v=SHOT' + String(i).padStart(7, '0'),
                title: '示範歌曲 ' + i + '　面板流暢度截圖'
            });
        }
        await fetch('/api/queue/add-batch', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({guild_id: g, songs})});
        try {
            await fetch('/api/play', {method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({guild_id: g})});
        } catch (e) {}
    }""", n)
    await pg.wait_for_timeout(400)


def start_sim():
    import socket, subprocess
    py = sys.executable
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, SIM_PORT=str(port), SIM_SONG_LEN=os.getenv("SIM_SONG_LEN", "180"),
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
            time.sleep(0.25)
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
            ctx = await br.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", locale="zh-TW")
            await ctx.route("**/*", offline_guard)
            pg = await ctx.new_page()
            await login(pg)
            await pick_guild(pg)
            await seed_queue(pg, 12)
            await pg.wait_for_timeout(400)
            await shot(pg, "01_home_desktop")

            await pg.evaluate("() => { if (typeof openNpView === 'function') openNpView(); }")
            await pg.wait_for_selector("#npQueueList .list-item", timeout=8000)
            await pg.wait_for_timeout(350)
            await shot(pg, "02_np_queue")

            await pg.evaluate("""() => {
                if (typeof setNpOpen === 'function') setNpOpen(false);
                const tab = document.querySelector('#homeTabs .tab-btn[data-tab="playlist"]');
                if (tab) tab.click();
            }""")
            await pg.wait_for_timeout(400)
            await shot(pg, "03_playlist_tab")

            # 淺色
            await pg.evaluate("""() => {
                try { localStorage.setItem('theme', 'light'); } catch (e) {}
                document.documentElement.dataset.theme = 'light';
                const m = document.querySelector('meta[name=\"theme-color\"]');
                if (m) m.content = '#f7f6fb';
            }""")
            await pg.wait_for_timeout(200)
            await shot(pg, "04_home_light")

            await br.close()

            br2 = await p.chromium.launch(headless=True)
            ctx2 = await br2.new_context(
                viewport={"width": 390, "height": 844}, color_scheme="dark",
                locale="zh-TW", is_mobile=True, has_touch=True,
            )
            await ctx2.route("**/*", offline_guard)
            pg2 = await ctx2.new_page()
            await login(pg2)
            await pick_guild(pg2)
            await seed_queue(pg2, 8)
            await pg2.evaluate("() => { if (typeof openNpView === 'function') openNpView(); }")
            try:
                await pg2.wait_for_selector("#npQueueList .list-item", timeout=8000)
            except Exception:
                pass
            await pg2.wait_for_timeout(400)
            await shot(pg2, "05_mobile_390")
            await br2.close()
        print("done", OUT)
    finally:
        stop_sim(proc)


if __name__ == "__main__":
    asyncio.run(main())
