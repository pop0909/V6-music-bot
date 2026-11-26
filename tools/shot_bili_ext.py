"""模擬 B 站桌面版影片頁，截圖插件按鈕位置（離線，不連正式網站）。"""
import asyncio, http.server, os, socketserver, threading, sys
from playwright.async_api import async_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/bili-ext-shots"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili-ext-fixtures")
EXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chrome-extension")
os.makedirs(OUT, exist_ok=True)

class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def translate_path(self, path):
        if path.startswith("/ext/"):
            return os.path.join(EXT, path[len("/ext/"):].lstrip("/"))
        if path == "/" or path == "":
            path = "/page-toolbar.html"
        return os.path.join(ROOT, path.lstrip("/"))

def start_server():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd

async def check_and_shot(page, url, name, expect):
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_function("() => document.getElementById('musbot-add-btn')", timeout=5000)
    info = await page.evaluate("""() => {
      const btn = document.getElementById('musbot-add-btn');
      const h1 = document.querySelector('h1.video-title');
      const bar = document.querySelector('#arc_toolbar_report, .video-toolbar-container');
      const player = document.querySelector('.player');
      const br = btn.getBoundingClientRect();
      const pr = player.getBoundingClientRect();
      const overlap = !(br.bottom <= pr.top + 1 || br.top >= pr.bottom - 1 || br.right <= pr.left + 1 || br.left >= pr.right - 1);
      return {
        cls: btn.className,
        inH1: !!(h1 && h1.contains(btn)),
        inBar: !!(bar && bar.contains(btn)),
        afterTitle: !!(h1 && btn.previousElementSibling === h1),
        overlap, visible: br.width > 8 && br.height > 8,
        top: Math.round(br.top), pBottom: Math.round(pr.bottom)
      };
    }""")
    path = os.path.join(OUT, name + ".png")
    await page.screenshot(path=path, full_page=True)
    print("wrote", path, info, flush=True)
    if info["inH1"]:
        raise SystemExit(name + ": still inside h1")
    if info["overlap"]:
        raise SystemExit(name + ": overlaps player")
    if not info["visible"]:
        raise SystemExit(name + ": not visible")
    if expect == "bar" and not info["inBar"]:
        raise SystemExit(name + ": not in toolbar")
    if expect == "row" and not (info["afterTitle"] or "bili-row" in info["cls"]):
        raise SystemExit(name + ": not after-title row")

async def main():
    httpd = start_server()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True)
        ctx = await br.new_context(viewport={"width": 1280, "height": 820}, color_scheme="dark")
        async def guard(route):
            u = route.request.url
            if u.startswith(base) or "127.0.0.1" in u:
                return await route.continue_()
            return await route.abort()
        await ctx.route("**/*", guard)
        pg = await ctx.new_page()
        await check_and_shot(pg, base + "/page-toolbar.html", "01_desktop_toolbar", "bar")
        await check_and_shot(pg, base + "/page-titleonly.html", "02_desktop_after_title", "row")
        await br.close()
    httpd.shutdown()
    print("done", OUT)

if __name__ == "__main__":
    asyncio.run(main())
