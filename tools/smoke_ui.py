"""面板 UI 煙霧測試（需要 playwright + 本機模擬器 sim.py 在 5055 埠）
用法：python tools/sim.py &  然後  python tools/smoke_ui.py [輸出資料夾]
會在 深色/淺色 × 1440/390 走過所有主要畫面，檢查：JS 錯誤、橫向溢出、文字對比、彈出框互斥 / Esc / 點外面關閉。
有問題時結束碼為 1，並把截圖存到輸出資料夾。"""
import asyncio, json, os, sys
from playwright.async_api import async_playwright

BASE = os.getenv("SMOKE_BASE", "http://127.0.0.1:5055")
OUT = sys.argv[1] if len(sys.argv) > 1 else "smoke_out"
OWNER = os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")
os.makedirs(OUT, exist_ok=True)
PROBLEMS = []

AUDIT_JS = r"""(tag) => {
  const vw = document.documentElement.clientWidth, res = {overflow: [], contrast: [], small: []};
  function rgb(s){ const m = s.match(/[\d.]+/g); return m ? m.map(Number) : [0,0,0,0]; }
  function lum([r,g,b]){ const f=v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4}; return .2126*f(r)+.7152*f(g)+.0722*f(b); }
  function blend(top, bot){ const a = top[3] ?? 1; return [0,1,2].map(i => top[i]*a + bot[i]*(1-a)).concat([1]); }
  function bgOf(el){
    const stack = [];
    for (let e = el; e; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage && cs.backgroundImage !== 'none' && !cs.backgroundImage.startsWith('url')) return null;  // 漸層底：略過
      const c = rgb(cs.backgroundColor); if (c.length < 4) c.push(1);
      if (c[3] > 0) { stack.push(c); if (c[3] >= .99) break; }
    }
    let base = [document.documentElement.dataset.theme === 'light' ? 247 : 7, 246, 251, 1];
    if (document.documentElement.dataset.theme !== 'light') base = [7, 6, 11, 1];
    for (let i = stack.length - 1; i >= 0; i--) base = blend(stack[i], base);
    return base;
  }
  for (const e of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const b = e.getBoundingClientRect(); if (!b.width || !b.height) continue;
    if (b.bottom < 0 || b.top > innerHeight) continue;
    let op = 1; for (let x = e; x; x = x.parentElement) op *= +getComputedStyle(x).opacity; if (op < .6) continue;
    let clipped = false; for (let x = e.parentElement; x && x !== document.body; x = x.parentElement) { const o = getComputedStyle(x); if (o.overflowX !== 'visible' || o.overflow !== 'visible') { const xb = x.getBoundingClientRect(); if (xb.right <= vw + 1) { clipped = true; break; } } }
    if (!clipped && b.right > vw + 1 && cs.position !== 'fixed' && !e.closest('[class*=marquee],.shelf-track,.shelf-row,.np-queue-strip'))
      res.overflow.push((e.id || e.className || e.tagName).toString().slice(0, 40) + ':' + Math.round(b.right));
    const own = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (own && cs.color && !e.closest('.cdd-native,option,select,.tabs.has-pill .tab-btn.active')) {
      if (cs.webkitTextFillColor && cs.webkitTextFillColor.includes('0, 0, 0, 0')) continue;   // 漸層文字
      const bg = bgOf(e); if (!bg) continue;
      let fg = rgb(cs.color); if (fg.length < 4) fg.push(1); fg = blend(fg, bg);
      const L1 = lum(fg), L2 = lum(bg), cr = (Math.max(L1,L2)+.05)/(Math.min(L1,L2)+.05);
      const big = parseFloat(cs.fontSize) >= 18 || (parseFloat(cs.fontSize) >= 14 && +cs.fontWeight >= 700);
      const need = e.closest('.cdd-note,.kbd-note,.tab-count,[disabled],.empty-state p,.faint,.update-msg') ? 2.6 : (big ? 3 : 3.8);
      if (cr < need) res.contrast.push(`${(e.id||e.className||e.tagName).toString().slice(0,34)} "${e.textContent.trim().slice(0,18)}" ${cr.toFixed(2)}`);
    }
    if (e.matches('button,a[href],[role=button],[role=option]') && (b.width < 28 || b.height < 28) && innerWidth < 600 && !e.closest('.kbd-help'))
      res.small.push((e.id||e.className||e.tagName).toString().slice(0,30) + ` ${Math.round(b.width)}x${Math.round(b.height)}`);
  }
  res.hiddenShown = [...document.querySelectorAll('[hidden]')].filter(e => getComputedStyle(e).display !== 'none').map(e => e.id || e.className).slice(0, 8);
  for (const k of Object.keys(res)) res[k] = [...new Set(res[k])].slice(0, 15);
  res.hscroll = document.documentElement.scrollWidth > vw + 1;
  return res;
}"""


class Run:
    def __init__(self, pg, theme, w):
        self.pg, self.theme, self.w, self.errs = pg, theme, w, []
        pg.on("pageerror", lambda e: self.errs.append("pageerror: " + str(e)))
        pg.on("console", lambda m: self.errs.append("console: " + m.text) if m.type == "error" and "404" not in m.text and "Failed to load resource" not in m.text else None)

    def bad(self, what):
        PROBLEMS.append(f"[{self.theme} {self.w}] {what}")

    async def J(self, js, *a):
        return await self.pg.evaluate(js, *a)

    async def audit(self, tag):
        await self.pg.wait_for_timeout(250)
        r = await self.J(AUDIT_JS, tag)
        shot = f"{OUT}/{self.theme}_{self.w}_{tag}.png"
        issue = r["hscroll"] or r["overflow"] or r["contrast"] or (r["small"] if self.w < 600 else [])
        await self.pg.screenshot(path=shot)
        if r["hscroll"]: self.bad(f"{tag}: 頁面可橫向捲動 {r['overflow']}")
        elif r["overflow"]: self.bad(f"{tag}: 超出畫面 {r['overflow']}")
        if r.get("hiddenShown"): self.bad(f"{tag}: 有 hidden 屬性卻還顯示 {r['hiddenShown']}")
        if r["contrast"]: self.bad(f"{tag}: 對比不足 {r['contrast']}")
        if r["small"]: self.bad(f"{tag}: 點擊區太小 {r['small']}")
        errs = [e for e in self.errs if not (tag.endswith("offline") and "Failed to fetch" in e)]
        if errs: self.bad(f"{tag}: JS 錯誤 {errs[:3]}")
        self.errs.clear()
        return r

    async def hidden(self, sel):
        return await self.J("s => { const e = document.querySelector(s); return !e || e.hidden || getComputedStyle(e).display === 'none'; }", sel)

    async def esc_and_outside(self, open_sel, pop_sel, tag):
        if not await self.J("s => { const e=document.querySelector(s); return !!e && getComputedStyle(e).display!=='none' && !e.disabled; }", open_sel):
            return
        await self.pg.click(open_sel); await self.pg.wait_for_timeout(150)
        if await self.hidden(pop_sel): self.bad(f"{tag}: 點了沒打開"); return
        await self.pg.keyboard.press("Escape"); await self.pg.wait_for_timeout(150)
        if not await self.hidden(pop_sel): self.bad(f"{tag}: Esc 沒關")
        await self.pg.click(open_sel); await self.pg.wait_for_timeout(150)
        await self.pg.mouse.click(self.w // 2, 600 if self.w > 600 else 700); await self.pg.wait_for_timeout(150)
        if not await self.hidden(pop_sel): self.bad(f"{tag}: 點外面沒關")
        await self.pg.keyboard.press("Escape")


async def scenario(br, theme, w):
    ctx = await br.new_context(viewport={"width": w, "height": 900 if w > 600 else 844}, is_mobile=w < 600, has_touch=w < 600, color_scheme=theme)
    await ctx.add_init_script(f"try{{localStorage.setItem('theme','{theme}')}}catch(e){{}}")
    pg = await ctx.new_page(); R = Run(pg, theme, w)
    # 先登入（模擬器的擁有者金鑰）；登入頁本身也跑一次稽核
    await pg.goto(BASE + "/"); await pg.wait_for_timeout(400)
    if "/login" not in pg.url: R.bad("沒登入卻沒被導到登入頁")
    await R.audit("00_login")
    await pg.fill("#key", OWNER); await pg.click("button[type=submit]"); await pg.wait_for_timeout(1200)
    if "/login" in pg.url: R.bad("擁有者金鑰登入失敗")
    if await R.J("() => document.documentElement.dataset.theme") != theme:
        await R.J(f"() => document.documentElement.dataset.theme='{theme}'")
    await R.audit("01_no_guild")
    # 選伺服器（自訂下拉）
    await pg.click(".sel-wrap.cdd:has(#guildSelect) .cdd-btn"); await pg.wait_for_timeout(150)
    await R.audit("02_guild_dropdown")
    await pg.keyboard.press("Enter"); await pg.wait_for_timeout(1500)
    if not await R.J("() => document.getElementById('guildSelect').value"): R.bad("選伺服器失敗")
    await R.audit("03_home")
    await pg.click(".sel-wrap.cdd:has(#voiceChannelSelect) .cdd-btn"); await pg.wait_for_timeout(150)
    await R.audit("04_voice_dropdown")
    # 選單左右邊緣要和觸發按鈕一致
    for sid in ("guildSelect", "voiceChannelSelect"):
        await pg.keyboard.press("Escape")
        await pg.click(f".sel-wrap.cdd:has(#{sid}) .cdd-btn"); await pg.wait_for_timeout(200)
        d = await R.J("""(sid) => { const b=document.querySelector(`.sel-wrap.cdd:has(#${sid}) .cdd-btn`).getBoundingClientRect(), l=document.getElementById(sid+'List').getBoundingClientRect(); return [b.left-l.left, b.right-l.right]; }""", sid)
        if abs(d[0]) > 1 or abs(d[1]) > 1: R.bad(f"{sid} 選單沒對齊按鈕 左差{d[0]:.1f} 右差{d[1]:.1f}")
        ln = await R.J("""(sid) => { const seg=new Intl.Segmenter(undefined,{granularity:'grapheme'}); const n=t=>[...seg.segment(t)].length;
            const els=[...document.querySelectorAll(`#${sid}List .cdd-name`), document.querySelector(`.sel-wrap.cdd:has(#${sid}) .cdd-label`)];
            return els.filter(e => e && e.title && (n(e.textContent.replace(/…$/,'')) > 7 || (e.textContent !== e.title && !e.textContent.endsWith('…')))).map(e => e.textContent); }""", sid)
        if ln: R.bad(f"{sid} 名稱超過 7 字沒截斷 {ln}")
    await pg.keyboard.press("Escape")
    # 播放全部 → 播放列
    await pg.click("#btnPlaylistPlayAll"); await pg.wait_for_timeout(2000)
    await R.audit("05_playing")
    if not await R.J("() => (document.getElementById('npQueueBadge')||{}).textContent !== undefined"): pass
    # 搜尋
    await pg.fill("#searchInput", "測試"); await pg.keyboard.press("Enter"); await pg.wait_for_timeout(1500)
    await R.audit("06_search")
    await pg.keyboard.press("Escape"); await pg.fill("#searchInput", "")
    # 分頁：最愛 / 我的歌單
    for tab in ("favorites", "mylists", "playlist"):
        loc = pg.locator(f'.tab-btn[data-tab="{tab}"]:visible').first
        if await loc.count():
            if await loc.evaluate("e => getComputedStyle(e).pointerEvents") == "none": continue
            await loc.click(); await pg.wait_for_timeout(700); await R.audit(f"07_tab_{tab}")
    # 編輯視窗
    ed = await pg.query_selector('#playlistList [data-action="edit"], #playlistList .item-btn[title*="編輯"]')
    if ed:
        await ed.click(); await pg.wait_for_timeout(300); await R.audit("08_modal")
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
        if await R.J("() => !!document.querySelector('.modal-overlay.active')"): R.bad("編輯視窗 Esc 沒關")
    # 展開播放畫面
    exp = await pg.query_selector("#btnExpand, .pb-expand")
    if exp and await exp.is_visible():
        await exp.click(); await pg.wait_for_timeout(700); await R.audit("09_np_queue")
        hb = await pg.query_selector('#npTabs .tab-btn[data-np="history"]')
        if hb and await hb.is_visible():
            await hb.click(); await pg.wait_for_timeout(800); await R.audit("10_np_history_recent")
            seg = await pg.query_selector('#npHistoryMode button:nth-child(2)')
            if seg: await seg.click(); await pg.wait_for_timeout(800); await R.audit("11_np_history_rank")
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(500)
        if await R.J("() => document.body.classList.contains('np-open') || document.documentElement.classList.contains('np-open')"): R.bad("Esc 沒收合播放畫面")
    # 播放控制回歸
    before = await R.J("() => state.isPaused")
    play = await pg.query_selector("#btnPlayPause, .ctrl-play")
    if play and await play.is_visible():
        await play.click(); await pg.wait_for_timeout(900)
        if await R.J("() => state.isPaused") == before: R.bad("播放/暫停按鈕沒反應")
        await play.click(); await pg.wait_for_timeout(600)
    # 每顆播放控制鍵都要有反應（送出 API 請求，或出現選單 / 通知）
    reqs = {"n": 0}
    pg.on("request", lambda r: reqs.__setitem__("n", reqs["n"] + 1) if "/api/" in r.url and "progress-stream" not in r.url else None)
    await R.J("() => connectProgressStream(state.guildId)")
    if w < 1180:
        await R.J("() => openNpView()"); await pg.wait_for_timeout(700)
    for bid in ("btnSkip", "btnLoop", "btnShuffle", "btnLoop", "btnLoop", "btnShuffle", "btnBarFav", "btnSleep"):
        loc = pg.locator(f"#{bid}:visible")
        if not await loc.count(): continue
        if w < 1180 and bid != "btnSkip":
            await R.J("() => openNpView()"); await pg.wait_for_timeout(500)
        n0 = reqs["n"]; t0 = await R.J("() => document.querySelectorAll('.toast').length")
        await loc.first.evaluate("e => e.scrollIntoView({block:'center'})")
        try: await loc.first.click(timeout=4000)
        except Exception: R.bad(f"按鈕 #{bid} 被其他元素擋住點不到"); continue
        await pg.wait_for_timeout(500)
        opened = await R.J("() => !!document.querySelector('.sleep-menu:not([hidden]), .sleep-pop:not([hidden]), [role=menu]:not([hidden])')")
        if reqs["n"] == n0 and not opened and await R.J("() => document.querySelectorAll('.toast').length") == t0:
            R.bad(f"按鈕 #{bid} 按了沒反應")
        await pg.keyboard.press("Escape")
    vol = pg.locator("#volumeSlider:visible")
    if await vol.count():
        n0 = reqs["n"]
        await vol.first.evaluate("e => { e.value = 40; e.dispatchEvent(new Event('input', {bubbles:true})); e.dispatchEvent(new Event('change', {bubbles:true})); }")
        await pg.wait_for_timeout(800)
        if reqs["n"] == n0: R.bad("音量滑桿沒送出請求")
    await R.J("() => closeNpView()"); await pg.wait_for_timeout(600)
    # 個人歌單：登入 → 新增 → 出現在清單
    tab = pg.locator('.tab-btn[data-tab="mylists"]:visible').first
    if await tab.count() and await tab.evaluate("e => getComputedStyle(e).pointerEvents") != "none":
        await tab.click(); await pg.wait_for_timeout(400)
        if await pg.locator("#mlToken:visible").count():
            await pg.fill("#mlToken", os.getenv("SMOKE_OWNER_TOKEN", "OWNER-TOKEN-TEST")); await pg.click("#btnMlLogin"); await pg.wait_for_timeout(1200)
        if await pg.locator("#mlLogin:visible").count(): R.bad("個人歌單登入失敗")
        name = f"煙霧{theme}{w}"
        pg.once("dialog", lambda d: asyncio.ensure_future(d.accept(name)))
        await pg.click("#btnMlNew"); await pg.wait_for_timeout(1500)
        if name not in (await R.J("() => document.getElementById('mlList').innerText")): R.bad("新增個人歌單後清單沒出現")
        await R.audit("07_mylists_loggedin")
        more = pg.locator("#mlList .ml-more:visible").first
        if await more.count():
            await more.click(); await pg.wait_for_timeout(200)
            await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
            if await R.J("() => !!document.querySelector('#mlList .menu-open')"): R.bad("⋯ 選單 Esc 沒關")
        pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await pg.click("#btnMlLogout"); await pg.wait_for_timeout(500)
        if await pg.locator("#btnMlNew:visible").count(): R.bad("登出後仍看得到「新增」按鈕")
    # 彈出框（桌面）
    if w > 600:
        await R.esc_and_outside("#btnStatus", "#statusPop", "狀態框")
        await R.esc_and_outside("#btnShortcuts", "#kbdHelp", "快捷鍵")
        await R.esc_and_outside(".sel-wrap.cdd:has(#voiceChannelSelect) .cdd-btn", "#voiceChannelSelectList", "語音下拉")
        await pg.click("#btnStatus"); await pg.wait_for_timeout(150); await pg.click("#btnShortcuts"); await pg.wait_for_timeout(150)
        if not await R.hidden("#statusPop"): R.bad("狀態框與快捷鍵同時開著")
        await R.audit("12_kbd")
        await pg.click("#btnStatus"); await pg.wait_for_timeout(500); await R.audit("13_status")
        await pg.keyboard.press("Escape")
    else:
        await pg.click("#btnStatus"); await pg.wait_for_timeout(500); await R.audit("13_status")
        await pg.keyboard.press("Escape")
    # 語音下拉：人數變化 / 新頻道（長名稱）/ 機器人移動 / 很多頻道捲動
    if w > 600 or True:
        post = lambda d: pg.evaluate("d => fetch('/sim/channel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(r=>r.json())", d)
        await post({"id": 1002, "n": 4})
        await post({"add": "超級無敵長的語音頻道名稱用來測試選單會不會爆版面ABCDEFGHIJKLMNOP", "n": 2})
        for i in range(20): await post({"add": f"頻道{i:02d}", "n": i % 3})
        vb = ".sel-wrap.cdd:has(#voiceChannelSelect) .cdd-btn"
        await pg.click(vb); await pg.wait_for_timeout(900)
        txt = await R.J("() => [...document.querySelectorAll('#voiceChannelSelectList .cdd-opt')].map(n => n.innerText.replace(/\\s+/g,' '))")
        if not any("LOL" in t and "4" in t for t in txt): R.bad(f"下拉人數沒更新：{[t for t in txt if 'LOL' in t]}")
        if not any("超級無敵長" in t for t in txt): R.bad("新頻道沒出現在下拉")
        sc = await R.J("() => { const l=document.getElementById('voiceChannelSelectList'); return [l.scrollHeight > l.clientHeight, getComputedStyle(l).overflowY]; }")
        if not sc[0]: R.bad(f"很多頻道時下拉沒有捲動 {sc}")
        lb = await R.J("() => { const r=document.getElementById('voiceChannelSelectList').getBoundingClientRect(); return [r.left, r.right, innerWidth]; }")
        if lb[0] < 0 or lb[1] > lb[2]: R.bad(f"語音下拉超出畫面 {lb}")
        await R.audit("17_voice_many")
        await pg.keyboard.press("End"); await pg.wait_for_timeout(100)
        vis = await R.J("() => { const l=document.getElementById('voiceChannelSelectList'), a=l.querySelector('.cdd-opt.active'); if(!a) return false; const r=a.getBoundingClientRect(), lr=l.getBoundingClientRect(); return r.bottom <= lr.bottom + 1; }")
        if not vis: R.bad("鍵盤 End 後選到的項目沒有捲到可見")
        await pg.keyboard.press("Escape")
        if not await R.J("async () => (await (await fetch('/api/status?guild_id=111')).json()).is_connected"):
            await R.J("() => API.joinVoice('999')"); await pg.wait_for_timeout(1500)
        await post({"move": 1001})
        for _ in range(12):
            await pg.wait_for_timeout(500)
            lab = await R.J("() => document.querySelector('.sel-wrap.cdd:has(#voiceChannelSelect) .cdd-label').textContent")
            if lab == "APEX": break
        if lab != "APEX":
            dbg = await R.J("() => { const s=document.getElementById('voiceChannelSelect'); return [s.value, s.disabled, !!s.querySelector('option[value=\"1001\"]'), state.guildId, typeof _es]; }")
            dbg.append(await R.J("async () => { const r=await (await fetch('/api/status?guild_id=111')).json(); return [r.voice_channel, r.is_connected, r.is_playing]; }"))
            R.bad(f"機器人移動後下拉沒同步（顯示 {lab}）{dbg}")
        await pg.click(vb); await pg.wait_for_timeout(500)
        here = await R.J("() => [...document.querySelectorAll('#voiceChannelSelectList .cdd-opt.bot-here')].map(n=>n.innerText.split('\\n')[0])")
        if here != ["APEX"]: R.bad(f"「機器人在這」標記不對：{here}")
        await pg.keyboard.press("Escape")
        await post({"move": 999}); await pg.wait_for_timeout(1500)
    # 搜尋結果 Esc
    await pg.fill("#searchInput", "測試"); await pg.keyboard.press("Enter"); await pg.wait_for_timeout(1200)
    await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
    if await R.J("() => !!document.getElementById('searchResults').firstChild"): R.bad("搜尋結果 Esc 沒收起")
    # 通知
    await R.J("() => { showToast('✅ 測試成功', 'success'); showToast('連線失敗', 'error'); showToast('注意', 'warning'); }")
    await R.audit("14_toasts")
    # 成員頁
    m = await ctx.new_page(); R2 = Run(m, theme, w)
    await m.goto(BASE + "/members"); await m.wait_for_timeout(800); await R2.audit("15_members")
    # 斷線狀態
    await ctx.set_offline(True)
    await R.J("() => connectProgressStream(state.guildId)")
    await pg.wait_for_timeout(7000)
    if await R.J("() => document.getElementById('statusText').textContent") != "離線": R.bad("斷線後狀態沒有顯示「離線」")
    await R.audit("16_offline")
    await ctx.set_offline(False); await pg.wait_for_timeout(9000)
    if await R.J("() => document.getElementById('statusText').textContent") == "離線": R.bad("網路恢復後仍顯示「離線」")
    await ctx.close()


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch()
        for theme in ("dark", "light"):
            for w in (1440, 390):
                try:
                    await scenario(br, theme, w)
                except Exception as e:
                    PROBLEMS.append(f"[{theme} {w}] 腳本例外：{e!r}"[:300])
        await br.close()
    print(json.dumps(PROBLEMS, ensure_ascii=False, indent=1))
    print(f"問題 {len(PROBLEMS)} 項；截圖在 {OUT}/")
    sys.exit(1 if PROBLEMS else 0)

asyncio.run(main())
