"""離線截圖：擴充功能 1.3.0 的 popup、YouTube 按鈕、清單頁、B 站工具列。
所有外部網路（含 discord.com、youtube、bilibili、musbot.kdns.fr 以外改由 stub）一律擋下。
"""
import asyncio, http.server, json, os, socketserver, sys, threading
from playwright.async_api import async_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/t13-ext-shots"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "t13-ext-fixtures")
BILI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili-ext-fixtures")
EXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chrome-extension")
os.makedirs(OUT, exist_ok=True)

STATUS = {
    "status": "ok",
    "current_title": "示範歌曲（擴充功能小播放器）",
    "is_playing": True,
    "is_paused": False,
    "connected": True,
    "queue_length": 11,
    "voice_channel": "語音一",
    "guild_name": "測試伺服器",
    "you": "擁有者",
    "elapsed": 42,
    "elapsed_str": "0:42",
    "duration": 200,
    "duration_str": "3:20",
    "volume": 80,
    "asleep": False,
    "owner": True,
}


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        path = path.split("?", 1)[0]
        if path.startswith("/ext/"):
            return os.path.join(EXT, path[len("/ext/"):].lstrip("/"))
        if path == "/cover.png":
            return os.path.join(ROOT, "cover.png")
        if path.startswith("/bili/"):
            rel = path[len("/bili/"):].lstrip("/") or "page-toolbar.html"
            return os.path.join(BILI, rel)
        name = path.lstrip("/") or "yt-watch.html"
        return os.path.join(ROOT, name)


def start_server():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


POPUP_BOOT = """
window.chrome = {
  storage: { local: { get: async () => ({ extToken: 'x' }), set: async () => {} } },
  runtime: {
    sendMessage: async (m) => ({ status: 'ok', message: '已加入佇列第 2 首：示範歌曲' }),
    openOptionsPage: () => {},
    onMessage: { addListener(){} },
  },
  tabs: { query: async () => [{ url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', title: '示範歌曲 - YouTube' }] },
};
"""

OPTIONS_BOOT = """
window.chrome = {
  storage: { local: { get: async () => ({ extToken: '' }), set: async () => {} } },
  runtime: { openOptionsPage: () => {}, onMessage: { addListener(){} } },
};
"""


async def shot(page, name):
    path = os.path.join(OUT, name)
    await page.screenshot(path=path, full_page=True)
    print("wrote", path, flush=True)


async def main():
    httpd = start_server()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    STATUS["thumbnail"] = base + "/cover.png"
    status_body = json.dumps(STATUS, ensure_ascii=False)

    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True)
        ctx = await br.new_context(viewport={"width": 1280, "height": 820}, color_scheme="dark")

        async def guard(route):
            u = route.request.url
            if u.startswith(base) or "127.0.0.1" in u:
                if "musbot.kdns.fr" in u:
                    return await route.fulfill(status=200, content_type="application/json; charset=utf-8", body=status_body)
                return await route.continue_()
            if "musbot.kdns.fr" in u:
                return await route.fulfill(status=200, content_type="application/json; charset=utf-8", body=status_body)
            return await route.abort()

        await ctx.route("**/*", guard)

        # popup（正在播放 + 控制列）
        await ctx.add_init_script(POPUP_BOOT)
        pg = await ctx.new_page()
        await pg.set_viewport_size({"width": 360, "height": 520})
        await pg.goto(base + "/ext/popup.html", wait_until="domcontentloaded")
        await pg.wait_for_function("() => document.getElementById('np') && !document.getElementById('np').textContent.includes('讀取中')", timeout=5000)
        await shot(pg, "01_popup_player.png")
        await pg.close()

        # popup：目前不是影片分頁
        await ctx.add_init_script("""
          window.chrome = window.chrome || {};
          window.chrome.tabs = { query: async () => [{ url: 'https://example.com/', title: 'Example' }] };
        """)
        pg = await ctx.new_page()
        await pg.set_viewport_size({"width": 360, "height": 520})
        await pg.goto(base + "/ext/popup.html", wait_until="domcontentloaded")
        await pg.wait_for_function("() => document.getElementById('msg') && document.getElementById('msg').textContent.length > 0", timeout=5000)
        await shot(pg, "02_popup_not_video.png")
        await pg.close()

        # options
        await ctx.add_init_script(OPTIONS_BOOT)
        pg = await ctx.new_page()
        await pg.set_viewport_size({"width": 640, "height": 480})
        await pg.goto(base + "/ext/options.html", wait_until="domcontentloaded")
        await pg.wait_for_selector("#save")
        await shot(pg, "03_options.png")
        await pg.close()

        # YouTube 影片頁按鈕
        pg = await ctx.new_page()
        await pg.set_viewport_size({"width": 1280, "height": 720})
        await pg.goto(base + "/yt-watch.html", wait_until="domcontentloaded")
        await pg.wait_for_function("() => document.getElementById('musbot-add-btn')", timeout=5000)
        await shot(pg, "04_youtube_watch.png")
        await pg.close()

        # YouTube 播放清單頁
        pg = await ctx.new_page()
        await pg.set_viewport_size({"width": 1280, "height": 720})
        await pg.goto(base + "/yt-playlist.html", wait_until="domcontentloaded")
        await pg.wait_for_function("() => document.getElementById('musbot-add-btn') && document.getElementById('musbot-add-btn').className.includes('musbot-list-only')", timeout=5000)
        await shot(pg, "05_youtube_playlist.png")
        await pg.close()

        # B 站工具列
        pg = await ctx.new_page()
        await pg.set_viewport_size({"width": 1280, "height": 820})
        await pg.goto(base + "/bili/page-toolbar.html", wait_until="domcontentloaded")
        await pg.wait_for_function("() => document.getElementById('musbot-add-btn')", timeout=5000)
        await shot(pg, "06_bilibili_toolbar.png")
        await pg.close()

        await br.close()
    httpd.shutdown()
    print("done", OUT)


if __name__ == "__main__":
    asyncio.run(main())
