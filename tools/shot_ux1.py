"""ux1 截圖（本機 sim.py，不連外、不碰正式面板）。用法：先開 sim.py，再 python tools/shot_ux1.py [輸出資料夾]"""
import asyncio, os, sys
from playwright.async_api import async_playwright

BASE = os.getenv("SMOKE_BASE", "http://127.0.0.1:5055")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/ux1"
OWNER = os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")
os.makedirs(OUT, exist_ok=True)


async def offline_guard(route):
    u = route.request.url
    if u.startswith(BASE) or "127.0.0.1" in u or "localhost" in u:
        return await route.continue_()
    if route.request.resource_type == "image":
        return await route.fulfill(status=404, body="")
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
    gid = await pg.evaluate("""() => {
        const sel = document.getElementById('guildSelect');
        if (!sel) return null;
        const opt = [...sel.options].find(o => o.value);
        if (!opt) return null;
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        try { localStorage.setItem('mb_guild', opt.value); } catch (e) {}
        return opt.value;
    }""")
    if not gid:
        raise SystemExit("sim 沒有可用的伺服器")
    await pg.wait_for_function("() => !document.getElementById('guildOverlay')?.classList.contains('active') && document.getElementById('guildSelect')?.value", timeout=8000)
    await pg.wait_for_timeout(500)
    return gid


async def add_songs(pg, n=2):
    r = await pg.evaluate(
        """async (n) => {
            const out = [];
            for (let i = 1; i <= n; i++) {
                const res = await fetch('/api/queue/add', {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({url: 'https://www.youtube.com/watch?v=SIMSONG' + String(i).padStart(4,'0'), title: '模擬歌曲 ' + i})});
                out.push({ok: res.ok, status: res.status, body: await res.text()});
            }
            const gid = document.getElementById('guildSelect').value;
            return {out, gid, q: document.getElementById('npQueueCount') && document.getElementById('npQueueCount').textContent};
        }""", n)
    print("add_songs", r, flush=True)
    await open_np(pg)
    await pg.wait_for_function("() => !document.getElementById('npBtnQueueClear')?.disabled", timeout=8000)
    await pg.wait_for_timeout(400)


async def open_np(pg):
    await pg.evaluate("() => { if (typeof openNpView === 'function') openNpView(); else document.getElementById('btnExpand')?.click(); }")
    await pg.wait_for_timeout(500)


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True)
        ctx = await br.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        await ctx.route("**/*", offline_guard)
        pg = await ctx.new_page()

        # 先正常登入並選伺服器，把 guild 記在 localStorage
        await login(pg)
        await pick_guild(pg)
        await pg.evaluate("() => localStorage.setItem('mb_guild', state.guildId)")

        # 1. 載入前：新分頁，攔 status / SSE，重開面板
        pg2 = await ctx.new_page()

        async def hang(route):
            await asyncio.sleep(20)
            try:
                await route.abort()
            except Exception:
                pass
        await pg2.route("**/api/status*", hang)
        await pg2.route("**/api/progress-stream*", hang)
        await pg2.goto(BASE + "/", wait_until="domcontentloaded")
        if "/login" in pg2.url:
            await pg2.fill("#key", OWNER)
            await pg2.click("button[type=submit]")
            await pg2.wait_for_timeout(1000)
        else:
            await pg2.wait_for_timeout(800)
        info = await pg2.evaluate("""() => ({
            cls: document.documentElement.className,
            status: (document.getElementById('statusText')||{}).textContent,
            vol: (document.getElementById('volumeValue')||{}).textContent,
            volDisabled: !!(document.getElementById('volumeSlider')||{}).disabled,
            count: (document.getElementById('playlistCount')||{}).textContent,
            gid: window.state && state.guildId,
        })""")
        print("loading info:", info, flush=True)
        await shot(pg2, "01_loading")
        await pg2.close()

        # 載入完成對照
        await pg.reload(wait_until="domcontentloaded")
        await pg.wait_for_function("() => !document.documentElement.classList.contains('ui-loading')", timeout=8000)
        await pg.wait_for_timeout(600)
        info2 = await pg.evaluate("""() => ({
            cls: document.documentElement.className,
            status: (document.getElementById('statusText')||{}).textContent,
            vol: (document.getElementById('volumeValue')||{}).textContent,
            volDisabled: !!(document.getElementById('volumeSlider')||{}).disabled,
            count: (document.getElementById('playlistCount')||{}).textContent,
        })""")
        print("loaded info:", info2, flush=True)
        await shot(pg, "01b_loaded")

        await add_songs(pg, 2)
        await open_np(pg)
        await pg.wait_for_selector("#npBtnQueueClear:not([disabled])", timeout=8000)

        # 2. 清空佇列確認
        await pg.locator("#npBtnQueueClear").click(force=True)
        await pg.wait_for_function("() => document.getElementById('askTitle')?.textContent === '清空佇列' && document.getElementById('askModal')?.classList.contains('active')", timeout=4000)
        await pg.wait_for_timeout(200)
        await shot(pg, "02_clear_queue_confirm")
        await pg.locator("#askCancel").click()
        await pg.wait_for_timeout(200)

        # 3. 跑馬燈
        await pg.evaluate("""() => {
            const long = 'Lemon（Lemon）- 米津玄師 / Kenshi Yonezu [Official Music Video] (asterisk) 超長歌名用來檢查跑馬燈左右有沒有被切到';
            if (typeof updateSongInfo === 'function') window.updateSongInfo = function(){};
            if (typeof setMarquee === 'function') {
                setMarquee(document.getElementById('barTitle'), long);
                setMarquee(document.getElementById('songTitle'), long);
            }
            document.getElementById('playerBar')?.classList.add('has-song');
        }""")
        await pg.wait_for_function("() => document.getElementById('songTitle')?.classList.contains('marquee') || document.getElementById('barTitle')?.classList.contains('marquee')", timeout=4000)
        await pg.wait_for_timeout(1800)
        await shot(pg, "03_marquee")
        await pg.evaluate("() => { if (typeof setNpOpen === 'function') setNpOpen(false); }")
        await pg.wait_for_timeout(400)
        await pg.locator("#playerBar").screenshot(path=os.path.join(OUT, "03b_marquee_bar.png"))
        print("wrote", os.path.join(OUT, "03b_marquee_bar.png"), flush=True)
        await open_np(pg)

        # 4. 焦點框：用 focusVisible 讓 :focus-visible 生效（不靠滑鼠 click）
        await pg.evaluate("() => document.getElementById('btnPlay')?.focus({focusVisible: true})")
        on = await pg.evaluate("() => document.activeElement && document.activeElement.id")
        print("focus on", on, flush=True)
        await pg.wait_for_timeout(150)
        await shot(pg, "04_focus_ring")
        await pg.locator("#playerBar").screenshot(path=os.path.join(OUT, "04b_focus_ring_bar.png"))
        print("wrote", os.path.join(OUT, "04b_focus_ring_bar.png"), flush=True)

        # 5. 歌單名稱上限
        await pg.evaluate("() => { if (typeof setNpOpen === 'function') setNpOpen(false); }")
        await pg.wait_for_timeout(250)
        await pg.evaluate("() => { askDialog({ title: '新增歌單', input: { label: '歌單名稱', maxLength: 40, required: '請輸入歌單名稱', validate: typeof nameLimitMsg === 'function' ? nameLimitMsg : undefined }, okText: '建立' }); }")
        await pg.wait_for_selector("#askModal.active #askInput", timeout=4000)
        await pg.fill("#askInput", "這是一個用來示範名稱上限的歌單名稱一二三四五六七八九十XX")
        await pg.wait_for_timeout(200)
        cnt = await pg.evaluate("() => (document.getElementById('askCount')||{}).textContent")
        print("name counter:", cnt, flush=True)
        await shot(pg, "05_name_limit")
        await pg.locator("#askCancel").click()

        # 6. 模式說明
        await pg.locator("#btnModes").click()
        await pg.wait_for_selector("#modeMenu:not([hidden])", timeout=4000)
        await shot(pg, "06_mode_menu")
        await pg.keyboard.press("Escape")
        await pg.wait_for_timeout(200)

        # 7. 窄視窗
        await open_np(pg)
        await pg.evaluate("() => { const b = document.getElementById('npQueueBadge'); if (b) b.textContent = '佇列 2'; }")
        for w, h, name in ((390, 844, "07_narrow_390"), (640, 720, "07_narrow_640")):
            await pg.set_viewport_size({"width": w, "height": h})
            await pg.wait_for_timeout(400)
            await open_np(pg)
            await pg.wait_for_timeout(300)
            await shot(pg, name)

        # 8. 觸控佇列按鈕
        await ctx.close()
        ctx = await br.new_context(
            viewport={"width": 390, "height": 844},
            color_scheme="dark",
            has_touch=True,
            is_mobile=True,
        )
        await ctx.route("**/*", offline_guard)
        await ctx.add_init_script("""() => {
            const real = window.matchMedia.bind(window);
            window.matchMedia = (q) => {
                if (q === '(hover: hover)' || q === '(hover: hover) and (pointer: fine)')
                    return {matches:false, media:q, addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){}, onchange:null, dispatchEvent(){return false;}};
                if (q === '(hover: none)' || q === '(pointer: coarse)')
                    return {matches:true, media:q, addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){}, onchange:null, dispatchEvent(){return false;}};
                return real(q);
            };
        }""")
        pg = await ctx.new_page()
        await login(pg)
        await pick_guild(pg)
        await add_songs(pg, 2)
        await open_np(pg)
        await pg.wait_for_timeout(800)
        vis = await pg.evaluate("""() => {
            const b = document.querySelector('#npQueueList .item-btn');
            if (!b) return {missing: true};
            const s = getComputedStyle(b), row = b.closest('.list-item');
            const a = row && getComputedStyle(row.querySelector('.item-actions'));
            return {w: Math.round(parseFloat(s.width)), h: Math.round(parseFloat(s.height)), op: a && a.opacity,
                    hoverNone: matchMedia('(hover: none)').matches, coarse: matchMedia('(pointer: coarse)').matches};
        }""")
        print("touch buttons:", vis, flush=True)
        await shot(pg, "08_touch_queue_buttons")

        await ctx.close()
        ctx = await br.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        await ctx.route("**/*", offline_guard)
        pg = await ctx.new_page()
        await login(pg)
        await pick_guild(pg)
        await add_songs(pg, 2)
        await open_np(pg)
        await pg.wait_for_timeout(600)
        await pg.evaluate("""() => {
            const btn = document.querySelector('#npQueueList .list-item .item-btn');
            if (btn) btn.focus();
        }""")
        await pg.wait_for_timeout(150)
        await shot(pg, "08b_keyboard_queue_buttons")
        await br.close()


if __name__ == "__main__":
    asyncio.run(main())
