"""T-11 第 3 項面板截圖：影片屬於合集時的選擇、加入合集後的提示。
本機 sim.py，攔掉所有外網含 discord.com。
用法：python tools/shot_t11_bili_list.py [輸出資料夾]
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.async_api import async_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/t11-bili-list-shots"
OWNER = os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")
os.makedirs(OUT, exist_ok=True)

PNG1 = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")

VIDEO = "https://www.bilibili.com/video/BV1vny2znEiR"
VIDEO_RESULT = {
    "status": "ok", "is_playlist": False, "playlist_title": "",
    "songs": [{"url": VIDEO, "title": "海綿寶寶 第 1 集", "duration": 120, "thumbnail": ""}],
    "count": 1, "kind": "video", "truncated": False, "total": 1,
    "collection": {"id": 8986237, "title": "合集·海綿寶寶 合集", "count": 2, "mid": 700232090},
    "expanded": False,
}
SEASON_RESULT = {
    "status": "ok", "is_playlist": True, "playlist_title": "合集·海綿寶寶 合集",
    "songs": [
        {"url": VIDEO, "title": "海綿寶寶 第 1 集", "duration": 120, "thumbnail": ""},
        {"url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "海綿寶寶 第 2 集", "duration": 110, "thumbnail": ""},
    ],
    "count": 2, "kind": "season", "truncated": False, "total": 2,
    "collection": {"id": 8986237, "title": "合集·海綿寶寶 合集", "count": 2, "mid": 700232090},
    "expanded": True,
}
BATCH_OK = {"status": "ok", "count": 2, "full_skipped": False}

mode = {"resolve": "video"}
BASE = os.getenv("SMOKE_BASE", "")


async def offline_guard(route):
    u = route.request.url
    method = route.request.method
    if "/api/resolve-url" in u and method == "POST":
        body = SEASON_RESULT if mode["resolve"] == "season" else VIDEO_RESULT
        # expand_season 從 post data 判斷
        try:
            raw = route.request.post_data or ""
            payload = json.loads(raw) if raw else {}
            if payload.get("expand_season"):
                body = SEASON_RESULT
        except Exception:
            pass
        return await route.fulfill(status=200, content_type="application/json; charset=utf-8",
                                   body=json.dumps(body, ensure_ascii=False))
    if "/api/queue/add-batch" in u and method == "POST":
        return await route.fulfill(status=200, content_type="application/json; charset=utf-8",
                                   body=json.dumps(BATCH_OK, ensure_ascii=False))
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
    await pg.wait_for_function(
        "() => !document.getElementById('guildOverlay')?.classList.contains('active') && document.getElementById('guildSelect')?.value",
        timeout=8000)
    await pg.wait_for_timeout(300)


def start_sim():
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

            # 1) 貼屬於合集的影片網址 → 選擇對話框
            mode["resolve"] = "video"
            await pg.fill("#searchInput", VIDEO)
            await pg.click("#btnSearch")
            await pg.wait_for_selector("#askModal.active", timeout=8000)
            await pg.wait_for_function(
                "() => document.getElementById('askTitle')?.textContent.includes('合集')",
                timeout=5000)
            await pg.wait_for_timeout(250)
            await shot(pg, "01_collection_choice")

            # 2) 選加入整份合集 → 成功提示
            await pg.click("#askOk")
            await pg.wait_for_function(
                "() => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes('合集') && t.textContent.includes('加入'))",
                timeout=8000)
            await pg.wait_for_timeout(250)
            await shot(pg, "02_collection_added_toast")
            await br.close()
        print("done", OUT)
    finally:
        stop_sim(proc)


if __name__ == "__main__":
    asyncio.run(main())
