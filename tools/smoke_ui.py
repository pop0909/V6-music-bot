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

    async def menu(self, item_sel):
        """頂欄齒輪設定選單：打開選單再點其中一項（主題 / 狀態 / 成員 / 快捷鍵 / 登出都收在這裡）"""
        if await self.hidden("#setMenu"):
            await self.pg.click("#btnSettings"); await self.pg.wait_for_timeout(150)
        if await self.hidden("#setMenu"): self.bad("齒輪選單點了沒打開"); return
        sub = await self.J("s => document.querySelector(s).getAttribute('aria-controls')", item_sel)   # 點之前讀（登出會換頁）
        await self.pg.click(item_sel); await self.pg.wait_for_timeout(150)
        if item_sel == "#btnLogout": return
        if sub in ("statusPop", "kbdHelp"):      # 子選單列：選單留著、子選單打開
            if await self.hidden("#" + sub): self.bad(f"點 {item_sel} 子選單沒打開")
        elif not await self.hidden("#setMenu"): self.bad(f"選了 {item_sel} 後齒輪選單沒收起")

    async def click_or_menu(self, sel):
        if sel.startswith("@"):
            await self.menu(sel[1:])
        else:
            await self.pg.click(sel)
        await self.pg.wait_for_timeout(150)

    async def esc_and_outside(self, open_sel, pop_sel, tag):
        probe = open_sel[1:] if open_sel.startswith("@") else open_sel
        if open_sel.startswith("@"):
            if not await self.J("s => !!document.querySelector(s)", probe): return
        elif not await self.J("s => { const e=document.querySelector(s); return !!e && getComputedStyle(e).display!=='none' && !e.disabled; }", open_sel):
            return
        await self.click_or_menu(open_sel)
        if await self.hidden(pop_sel): self.bad(f"{tag}: 點了沒打開"); return
        await self.pg.keyboard.press("Escape"); await self.pg.wait_for_timeout(150)
        if not await self.hidden(pop_sel): self.bad(f"{tag}: Esc 沒關")
        await self.click_or_menu(open_sel)
        # 桌機點頂欄下方的空白（快捷鍵子選單變高後會蓋到畫面中間偏下）
        # 768 寬時 (w/2, 120) 會點到伺服器下拉的外框 → 先找一個真的空白、不是控制項的點
        x, y = await self.J("""([w, y0]) => { for (const [x, y] of [[w / 2, y0], [w - 20, y0], [20, y0], [w / 2, 300], [w / 2, 700]]) {
            const e = document.elementFromPoint(x, y); if (e && !e.closest('.cdd,button,a,input,select,textarea,[role=menu],[role=listbox],.flyout,.set-menu')) return [x, y]; }
            return [w / 2, y0]; }""", [self.w, 120 if self.w > 600 else 700])
        await self.pg.mouse.click(x, y); await self.pg.wait_for_timeout(150)
        if not await self.hidden(pop_sel): self.bad(f"{tag}: 點外面沒關")
        await self.pg.keyboard.press("Escape")


async def settings_menu_checks(R, pg, w):
    """頂欄齒輪設定選單：五個項目都在、舊的獨立按鈕不在、Esc / 點外面關、鍵盤上下移動、主題切換、不超出畫面"""
    if await R.J("() => [...document.querySelectorAll('.app-header .hdr-btn')].filter(e => getComputedStyle(e).display !== 'none').map(e => e.id).join(',')") != "btnSettings":
        R.bad("頂欄除了齒輪還有其他圓形按鈕")
    await R.esc_and_outside("#btnSettings", "#setMenu", "齒輪選單")
    await pg.click("#btnSettings"); await pg.wait_for_timeout(200)
    labels = await R.J("() => [...document.querySelectorAll('#setMenu .set-item')].filter(e => e.offsetParent).map(e => e.innerText.trim())")
    chev = await R.J("() => [...document.querySelectorAll('#setMenu .set-item')].filter(e => e.querySelector('.set-chev')).map(e => e.id)")
    if sorted(chev) != ["btnShortcuts", "btnStatus"]: R.bad(f"只有狀態 / 快捷鍵應該有 ›：{chev}")
    want = ["機器人狀態", "成員管理", "登出"] + ([] if w < 600 else ["鍵盤快捷鍵"])
    if not any("切換成" in t for t in labels): R.bad(f"齒輪選單少了主題切換 {labels}")
    for t in want:
        if not any(t in x for x in labels): R.bad(f"齒輪選單少了「{t}」{labels}")
    box = await R.J("() => { const r = document.getElementById('setMenu').getBoundingClientRect(); return [r.left, r.right, r.bottom, innerWidth, innerHeight]; }")
    if box[0] < 0 or box[1] > box[3] or box[2] > box[4]: R.bad(f"齒輪選單超出畫面 {box}")
    await R.audit("18_settings_menu")
    # 主題切換：選了會換主題、選單收起、文字跟著變；再切回來
    before = await R.J("() => document.documentElement.dataset.theme")
    await pg.click("#btnTheme"); await pg.wait_for_timeout(400)
    after = await R.J("() => document.documentElement.dataset.theme")
    if after == before: R.bad("齒輪選單的主題切換沒作用")
    if not await R.hidden("#setMenu"): R.bad("切換主題後齒輪選單沒收起")
    await R.menu("#btnTheme"); await pg.wait_for_timeout(400)
    if await R.J("() => document.documentElement.dataset.theme") != before: R.bad("主題沒有切回來")
    # 鍵盤：焦點在齒輪按 Enter → 第一項有焦點；↓ 移到下一項；End 到最後（登出）；Esc 關並回到齒輪
    if w > 600:
        np_before = await R.J("() => npOpen")
        await R.J("() => document.getElementById('btnSettings').focus()")
        await pg.keyboard.press("Enter"); await pg.wait_for_timeout(150)
        if await R.hidden("#setMenu"): R.bad("鍵盤 Enter 打不開齒輪選單")
        first = await R.J("() => document.activeElement && document.activeElement.id")
        await pg.keyboard.press("ArrowDown")
        second = await R.J("() => document.activeElement && document.activeElement.id")
        await pg.keyboard.press("End")
        last = await R.J("() => document.activeElement && document.activeElement.id")
        if (first, second, last) != ("btnTheme", "btnStatus", "btnLogout"): R.bad(f"齒輪選單鍵盤移動不對 {first, second, last}")
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
        if not await R.hidden("#setMenu"): R.bad("鍵盤 Esc 沒關齒輪選單")
        if await R.J("() => document.activeElement && document.activeElement.id") != "btnSettings": R.bad("Esc 後焦點沒回到齒輪")
        if await R.J("() => npOpen") != np_before: R.bad("在齒輪選單按 Esc 卻收合 / 影響了播放畫面")
        # 鍵盤：狀態列按 → 打開子選單（選單留著、焦點進子選單）；← 回到那一列；Enter 也能開；Esc 回選單、再 Esc 關選單
        await pg.keyboard.press("Enter"); await pg.wait_for_timeout(100)
        await pg.keyboard.press("ArrowDown"); await pg.keyboard.press("ArrowRight"); await pg.wait_for_timeout(300)
        if await R.hidden("#statusPop") or await R.hidden("#setMenu"): R.bad("鍵盤 → 開狀態子選單失敗（或選單被收起）")
        if await R.J("() => document.activeElement && document.activeElement.id") != "statusPop": R.bad("鍵盤開狀態子選單後焦點沒進去")
        await pg.keyboard.press("ArrowLeft"); await pg.wait_for_timeout(150)
        if not await R.hidden("#statusPop"): R.bad("← 沒關狀態子選單")
        if await R.J("() => document.activeElement && document.activeElement.id") != "btnStatus": R.bad("← 後焦點沒回到「機器人狀態」")
        await pg.keyboard.press("End"); await pg.keyboard.press("ArrowUp"); await pg.wait_for_timeout(50)
        if await R.J("() => document.activeElement && document.activeElement.id") == "btnShortcuts":
            await pg.keyboard.press("Enter"); await pg.wait_for_timeout(250)
            if await R.hidden("#kbdHelp"): R.bad("快捷鍵列按 Enter 沒開子選單")
            await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
            if not await R.hidden("#kbdHelp") or await R.hidden("#setMenu"): R.bad("子選單 Esc 應該只回到選單")
            if await R.J("() => document.activeElement && document.activeElement.id") != "btnShortcuts": R.bad("Esc 後焦點沒回到「鍵盤快捷鍵」")
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
        if not await R.hidden("#setMenu"): R.bad("第二次 Esc 沒關齒輪選單")
        if await R.J("() => npOpen") != np_before: R.bad("子選單的 Esc 影響了播放畫面")
        # 滑鼠：滑到「機器人狀態」自動展開、滑到「切換主題」收起；子選單貼在選單旁邊（放不下右邊就放左邊），不重疊、不超出畫面
        await pg.click("#btnSettings"); await pg.wait_for_timeout(150)
        await pg.hover("#btnStatus"); await pg.wait_for_timeout(500)
        if await R.hidden("#statusPop"): R.bad("滑到「機器人狀態」沒展開")
        g = await R.J("""() => { const m = document.getElementById('setMenu').getBoundingClientRect(), f = document.getElementById('statusPop').getBoundingClientRect();
            return [m.left, m.right, f.left, f.right, f.top, f.bottom, innerWidth, innerHeight, document.getElementById('statusPop').className]; }""")
        side_ok = g[3] <= g[0] + 1 or g[2] >= g[1] - 1
        if not side_ok: R.bad(f"狀態子選單沒有貼在選單旁邊（重疊）{g}")
        if g[2] < 0 or g[3] > g[6] or g[4] < 0 or g[5] > g[7] + 1: R.bad(f"狀態子選單超出畫面 {g}")
        await R.audit("19_status_flyout")
        await pg.hover("#btnShortcuts"); await pg.wait_for_timeout(500)
        if await R.hidden("#kbdHelp") or not await R.hidden("#statusPop"): R.bad("滑到快捷鍵：應該換成快捷鍵子選單")
        await R.audit("20_kbd_flyout")
        await pg.hover("#btnTheme"); await pg.wait_for_timeout(600)
        if not await R.hidden("#kbdHelp"): R.bad("滑到別的列子選單沒收起")
        # 按 ? 直接打開快捷鍵（連選單一起），Esc 收起
        await pg.mouse.click(w // 2, 600); await pg.wait_for_timeout(150)
        await R.J("() => document.activeElement && document.activeElement.blur()")
        await pg.keyboard.press("?"); await pg.wait_for_timeout(250)
        if await R.hidden("#kbdHelp"): R.bad("按 ? 沒打開快捷鍵")
        await pg.keyboard.press("Escape"); await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
        if not await R.hidden("#kbdHelp") or not await R.hidden("#setMenu"): R.bad("按 ? 打開後 Esc 收不起來")
    else:
        # 手機：沒有旁邊的空間 → 在選單裡換頁，上面有「‹ 返回」；返回後選單項目又看得到
        await pg.click("#btnSettings"); await pg.wait_for_timeout(150)
        await pg.click("#btnStatus"); await pg.wait_for_timeout(400)
        if await R.hidden("#statusPop"): R.bad("手機點「機器人狀態」沒換頁")
        g = await R.J("""() => { const m = document.getElementById('setMenu'), f = document.getElementById('statusPop'), r = f.getBoundingClientRect();
            return [f.classList.contains('fly-inplace'), getComputedStyle(m).visibility, getComputedStyle(document.getElementById('statusClose')).display, r.left, r.right, innerWidth]; }""")
        if not g[0] or g[1] != "hidden" or g[2] == "none": R.bad(f"手機子選單不是在選單內換頁 {g}")
        if g[3] < 0 or g[4] > g[5]: R.bad(f"手機子選單超出畫面 {g}")
        await R.audit("19_status_submenu")
        await pg.click("#statusClose"); await pg.wait_for_timeout(200)
        if not await R.hidden("#statusPop") or await R.hidden("#setMenu"): R.bad("手機子選單按返回沒回到選單")
        if await R.J("() => getComputedStyle(document.getElementById('setMenu')).visibility") != "visible": R.bad("返回後選單內容沒顯示")
        # 點「外面」要找真的空白處：(w/2, 780) 是底部播放列的上緣，現在點那裡會展開播放畫面
        ox, oy = await R.J("""([w]) => { for (const [x, y] of [[w / 2, 700], [w / 2, 600], [20, 700], [w - 20, 600]]) {
            const e = document.elementFromPoint(x, y); if (e && !e.closest('.cdd,button,a,input,select,textarea,[role=menu],.set-menu,.player-bar,.list-item,.shelf-card')) return [x, y]; }
            return [w / 2, 700]; }""", [w])
        await pg.mouse.click(ox, oy); await pg.wait_for_timeout(200)
        if not await R.hidden("#setMenu"): R.bad("手機點外面沒關齒輪選單")
    # 跟語音下拉互斥
    await pg.click("#btnSettings"); await pg.wait_for_timeout(150)
    await pg.click(".sel-wrap.cdd:has(#voiceChannelSelect) .cdd-btn"); await pg.wait_for_timeout(150)
    if not await R.hidden("#setMenu"): R.bad("開語音下拉時齒輪選單沒收起")
    await pg.keyboard.press("Escape"); await pg.wait_for_timeout(100)


async def normalize_checks(R, pg, w):
    """音量平衡：播放中切換要立即用新濾鏡重開 FFmpeg（不等下一首），再切回去"""
    if w < 1180 and not await R.J("() => npOpen"):
        await R.J("() => openNpView()"); await pg.wait_for_timeout(500)
    await open_modes(R, pg)                                   # 音量平衡收在 ⋮ 裡
    tg = pg.locator("label[for=normalizeToggle]:visible")
    if await tg.count() and await R.J("() => !!state.isPlaying"):
        was = await R.J("() => document.getElementById('normalizeToggle').checked")
        n0 = (await R.J("async () => (await fetch('/sim/ffmpeg')).json()"))["count"]
        await tg.first.click(); await pg.wait_for_timeout(1200)
        ff = await R.J("async () => (await fetch('/sim/ffmpeg')).json()")
        if ff["count"] <= n0 or (("dynaudnorm" in (ff["opens"][-1].get("options") or "")) == was):
            R.bad(f"音量平衡切換後沒有立即重開 FFmpeg {str(ff)[:160]}")
        await tg.first.click(); await pg.wait_for_timeout(900)
        if await R.J("() => document.getElementById('normalizeToggle').checked") != was: R.bad("音量平衡切回去失敗")
        if await R.hidden("#modeMenu"): R.bad("在 ⋮ 選單裡切換音量平衡後選單被收起")
    await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)


async def wait_status(pg, text, want, timeout=25000):
    """等頂欄狀態文字變成（want=True）/ 不再是（want=False）text；逾時 → False"""
    try:
        await pg.wait_for_function("([t, w]) => { const e = document.getElementById('statusText'); return !!e && ((e.textContent === t) === w); }",
                                   arg=[text, want], timeout=timeout, polling=200)
        return True
    except Exception:
        return False


async def open_modes(R, pg):
    """打開播放模式 ⋮ 選單（已開就不動）；回傳是否打開"""
    if not await R.hidden("#modeMenu"): return True
    b = pg.locator("#btnModes:visible")
    if not await b.count(): R.bad("找不到播放模式 ⋮ 按鈕"); return False
    await b.first.click(); await pg.wait_for_timeout(150)
    if await R.hidden("#modeMenu"): R.bad("播放模式 ⋮ 點了沒打開"); return False
    return True


MODE_ITEMS = ("btnLoop", "btnShuffle", "autoplayToggle", "normalizeToggle", "btnSleep")


async def mode_menu_checks(R, pg, w):
    """播放模式 ⋮：五個模式只在選單裡、開在 ⋮ 上方不超出畫面、每列 ≥ 44px、切換時選單不收、
    狀態文字 / 小圓點跟著變、點外面 / Esc / 再按 ⋮ 收起（Esc 不會連帶收合播放畫面）、鍵盤操作、快捷鍵 L / S 照常"""
    if w < 1180 and not await R.J("() => npOpen"):
        await R.J("() => openNpView()"); await pg.wait_for_timeout(600)
    # 先全部關掉（前面的檢查可能留下開著的模式）
    await R.J("""async () => { const g = state.guildId, post = (u, b) => fetch(u, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.assign({guild_id: g}, b))});
        await post('/api/shuffle', {enabled: false}); await post('/api/loop', {mode: 'none'}); await post('/api/autoplay', {enabled: false});
        await post('/api/normalize', {enabled: false}); await post('/api/sleep-timer', {mode: 'off'}); }""")
    await pg.wait_for_timeout(1600)
    vis = await R.J("ids => ids.filter(i => { const e = document.getElementById(i); return e && e.offsetParent !== null && !e.closest('#modeMenu'); })", list(MODE_ITEMS))
    if vis: R.bad(f"播放模式還有露在外面：{vis}")
    if await R.J("() => document.getElementById('btnModes').classList.contains('has-on')"):
        lab = await R.J("() => document.getElementById('btnModes').getAttribute('aria-label')")
        R.bad(f"全部模式關掉後 ⋮ 還亮著小圓點：{lab}")
    if not await open_modes(R, pg): return
    if await R.J("() => document.getElementById('btnModes').getAttribute('aria-expanded')") != "true": R.bad("⋮ 打開後 aria-expanded 不是 true")
    geo = await R.J("""() => { const m = document.getElementById('modeMenu').getBoundingClientRect(), b = document.getElementById('btnModes').getBoundingClientRect();
        const rows = [...document.querySelectorAll('#modeMenu .mm-item')].map(e => Math.round(e.getBoundingClientRect().height));
        return {m: [m.left, m.top, m.right, m.bottom], b: [b.left, b.top, b.right, b.bottom], vw: document.documentElement.clientWidth, vh: innerHeight, rows,
                labels: [...document.querySelectorAll('#modeMenu .mm-item')].map(e => e.innerText.replace(/\\s+/g, ' ').trim())}; }""")
    m, b = geo["m"], geo["b"]
    if m[0] < 0 or m[1] < 0 or m[2] > geo["vw"] or m[3] > geo["vh"]: R.bad(f"⋮ 選單超出畫面 {geo}")
    if m[3] > b[1] + 1: R.bad(f"⋮ 選單沒有開在按鈕上方 {geo}")
    if len(geo["rows"]) != 5 or min(geo["rows"]) < 44: R.bad(f"⋮ 選單每列要 ≥ 44px：{geo['rows']}")
    for want in ("循環 關", "隨機 關", "推薦 關", "平衡 關", "睡眠 關"):
        if want not in geo["labels"]: R.bad(f"⋮ 選單少了「{want}」：{geo['labels']}")
    await R.audit("18_modes_menu")
    # 選單裡切換：選單不收、狀態文字與小圓點立刻變（樂觀更新）
    st = lambda i: R.J("i => document.getElementById(i).textContent", i)
    await pg.click("#btnShuffle"); await pg.wait_for_timeout(120)
    if await R.hidden("#modeMenu"): R.bad("在 ⋮ 選單裡按「隨機」後選單被收起")
    if await st("shuffleState") != "開": R.bad(f"按「隨機」後狀態文字沒變：{await st('shuffleState')}")
    if not await R.J("() => document.getElementById('btnModes').classList.contains('has-on')"): R.bad("開了隨機，⋮ 沒有亮小圓點")
    await pg.click("label[for=autoplayToggle]"); await pg.wait_for_timeout(500)
    if await st("autoplayState") != "開": R.bad("按「推薦」後狀態文字沒變")
    await pg.click("#btnSleep"); await pg.wait_for_timeout(500)
    if await st("sleepState") != "30 分": R.bad(f"睡眠按一下應該是「30 分」：{await st('sleepState')}")
    await pg.click("#btnLoop"); await pg.wait_for_timeout(300)
    if await st("loopState") != "單曲": R.bad(f"循環按一下應該是「單曲」：{await st('loopState')}")
    if await R.hidden("#modeMenu"): R.bad("連續切換模式時選單被收起")
    tip = await R.J("() => document.getElementById('btnModes').getAttribute('aria-label')")
    for part in ("循環（單曲）", "隨機", "推薦", "睡眠（30 分）"):
        if part not in tip: R.bad(f"⋮ 的說明沒寫出開著的模式「{part}」：{tip}")
    await R.audit("19_modes_active")
    # Esc：只收選單，不收合播放畫面；焦點回到 ⋮
    np_before = await R.J("() => npOpen")
    await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
    if not await R.hidden("#modeMenu"): R.bad("⋮ 選單 Esc 沒關")
    if await R.J("() => npOpen") != np_before: R.bad("⋮ 選單按 Esc 時連帶收合了播放畫面")
    if await R.J("() => document.activeElement && document.activeElement.id") != "btnModes": R.bad("⋮ 選單 Esc 後焦點沒回到 ⋮")
    if await R.J("() => document.getElementById('btnModes').getAttribute('aria-expanded')") != "false": R.bad("⋮ 收起後 aria-expanded 不是 false")
    # 收起時：小圓點仍在（模式沒有被藏起來）
    if not await R.J("() => document.getElementById('btnModes').classList.contains('has-on')"): R.bad("選單收起後 ⋮ 沒有小圓點")
    # 再按一次 ⋮ 收起
    await pg.click("#btnModes"); await pg.wait_for_timeout(120)
    await pg.click("#btnModes"); await pg.wait_for_timeout(120)
    if not await R.hidden("#modeMenu"): R.bad("再按一次 ⋮ 沒有收起選單")
    # 點外面收起
    await pg.click("#btnModes"); await pg.wait_for_timeout(120)
    x, y = await R.J("""() => { const m = document.getElementById('modeMenu').getBoundingClientRect();
        for (let y = 40; y < innerHeight - 100; y += 37) for (let x = 12; x < innerWidth - 12; x += 41) {
            if (x >= m.left - 4 && x <= m.right + 4 && y >= m.top - 4 && y <= m.bottom + 4) continue;
            const e = document.elementFromPoint(x, y);
            if (e && !e.closest('#modeMenu, #btnModes, button, a, input, select, label, .list-item, .cdd, .np-cover-wrap, .progress-bar, .volume-row, .np-extra, .tab-btn, .toast')) return [x, y]; }
        return [innerWidth - 12, 40]; }""")
    await pg.mouse.click(x, y); await pg.wait_for_timeout(150)
    if not await R.hidden("#modeMenu"): R.bad("⋮ 選單點外面沒關")
    # 鍵盤：Enter 打開並聚焦第一項、↓ 下一項、End 最後一項、Tab 離開收起
    await R.J("() => document.getElementById('btnModes').focus()")
    await pg.keyboard.press("Enter"); await pg.wait_for_timeout(150)
    if await R.hidden("#modeMenu"): R.bad("⋮ 按 Enter 沒打開")
    if await R.J("() => document.activeElement && document.activeElement.id") != "btnLoop": R.bad("⋮ 用鍵盤打開後焦點不在第一項")
    await pg.keyboard.press("ArrowDown")
    if await R.J("() => document.activeElement && document.activeElement.id") != "btnShuffle": R.bad("⋮ 選單 ↓ 沒移到下一項")
    vol0 = await R.J("() => state.volume")
    await pg.keyboard.press("End")
    if await R.J("() => document.activeElement && document.activeElement.id") != "btnSleep": R.bad("⋮ 選單 End 沒移到最後一項")
    await pg.keyboard.press("ArrowUp")
    if await R.J("() => document.activeElement && document.activeElement.id") != "normalizeToggle": R.bad("⋮ 選單 ↑ 沒移到上一項")
    if await R.J("() => state.volume") != vol0: R.bad("在 ⋮ 選單裡按 ↑ 也調了音量")
    await pg.keyboard.press("Tab"); await pg.wait_for_timeout(100)
    if not await R.hidden("#modeMenu"): R.bad("⋮ 選單 Tab 離開後沒收起")
    # 快捷鍵 L / S（選單收著也能用），狀態同步到選單 / 小圓點
    await R.J("() => document.activeElement && document.activeElement.blur()")
    s0 = await st("shuffleState")
    await pg.keyboard.press("s"); await pg.wait_for_timeout(200)
    if await st("shuffleState") == s0: R.bad("快捷鍵 S 沒切換隨機（或選單狀態沒同步）")
    l0 = await st("loopState")
    await pg.keyboard.press("l"); await pg.wait_for_timeout(200)
    if await st("loopState") == l0: R.bad("快捷鍵 L 沒切換循環（或選單狀態沒同步）")
    # 各種寬度：⋮ 選單不超出畫面、頁面不能橫向捲動
    for vw in (360, 390, 600, 760, 900, 1179, 1180, 1440):
        await pg.set_viewport_size({"width": vw, "height": 800 if vw < 761 else 900}); await pg.wait_for_timeout(250)
        if vw < 1180 and not await R.J("() => npOpen"): await R.J("() => openNpView()"); await pg.wait_for_timeout(600)
        if not await pg.locator("#btnModes:visible").count(): R.bad(f"{vw}px：看不到 ⋮"); continue
        await R.J("() => document.getElementById('btnModes').click()"); await pg.wait_for_timeout(150)
        o = await R.J("""() => { const m = document.getElementById('modeMenu').getBoundingClientRect(), vw = document.documentElement.clientWidth;
            return [document.documentElement.scrollWidth > vw + 1, m.left < 0 || m.right > vw || m.top < 0 || m.bottom > innerHeight, m.width, m.height]; }""")
        if o[0] or o[1] or not o[2]: R.bad(f"{vw}px：⋮ 選單超出畫面或頁面可橫向捲動 {o}")
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(100)
    await pg.set_viewport_size({"width": w, "height": 900 if w > 600 else 844}); await pg.wait_for_timeout(300)
    # 收尾：全部關掉
    await R.J("""async () => { const g = state.guildId, post = (u, b) => fetch(u, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.assign({guild_id: g}, b))});
        await post('/api/shuffle', {enabled: false}); await post('/api/loop', {mode: 'none'}); await post('/api/autoplay', {enabled: false}); await post('/api/sleep-timer', {mode: 'off'}); }""")
    await pg.wait_for_timeout(1500)


async def ui6_checks(R, pg, w):
    """跑馬燈 / 封面主色 / 骨架 / 手機播放列 / 佇列鍵盤排序 / L、S 快捷鍵"""
    # 跑馬燈：放不下才捲；放得下是普通文字；減少動態效果時不捲
    long_t = "很長很長的歌名" * 12
    r = await R.J("""(t) => { setMarquee(el.barTitle, t); const a = el.barTitle.classList.contains('marquee') && !!el.barTitle.querySelector('.mq-seg + .mq-seg[aria-hidden=true]');
        setMarquee(el.barTitle, '短'); const b = !el.barTitle.classList.contains('marquee') && el.barTitle.textContent === '短';
        return [a, b]; }""", long_t)
    if r != [True, True]: R.bad(f"跑馬燈判斷不對（長／短）：{r}")
    # 展開的播放畫面：大歌名也要真的在捲（.np-view 為了效能關掉所有動畫，跑馬燈是例外）
    await R.J("() => openNpView()"); await pg.wait_for_timeout(700)
    big = await R.J("""(t) => { const n = document.getElementById('songTitle'), old = n._mqText || n.textContent; setMarquee(n, t);
        const m = n.querySelector('.marquee-inner'), r = [n.classList.contains('marquee'), m ? m.getAnimations().length : -1];
        n._mqText = undefined; setMarquee(n, old); return r; }""", long_t)
    if big != [True, 1]: R.bad(f"播放畫面大歌名的跑馬燈沒在動：{big}")
    if w >= 1180: await R.J("() => closeNpView()"); await pg.wait_for_timeout(400)
    await pg.emulate_media(reduced_motion="reduce")
    if await R.J("(t) => { el.barTitle._mqText = undefined; setMarquee(el.barTitle, t); return el.barTitle.classList.contains('marquee'); }", long_t + "x"):
        R.bad("減少動態效果時歌名還在捲動")
    await pg.emulate_media(reduced_motion="no-preference")
    await R.J("() => { el.barTitle._mqText = undefined; updateBarSong(state.currentTitle || '', state.thumbnail, state.currentUrl); }")
    # 封面主色：鮮豔的封面會上色，灰階的不會；上色後播放鍵的白色圖示對比要夠
    t = await R.J("""() => { const px = (r, g, b, n) => { const a = []; for (let i = 0; i < n; i++) a.push(r, g, b, 255); return a; };
        const red = pickTint(new Uint8ClampedArray(px(200, 30, 40, 576))), grey = pickTint(new Uint8ClampedArray(px(128, 128, 128, 576)));
        const c = (rgb) => 1.05 / (relLum(rgb) + 0.05);
        applyCoverTint(red); const on = document.documentElement.classList.contains('has-tint');
        applyCoverTint(null); const off = !document.documentElement.classList.contains('has-tint');
        return [!!red, grey, on, off, red ? c(red.dark) : 0, red ? c(red.light) : 0]; }""")
    if not (t[0] and t[1] is None and t[2] and t[3] and t[4] >= 3.2 and t[5] >= 4.5): R.bad(f"封面主色不對：{t}")
    # 骨架：外層 display: contents、讀屏念「載入中」
    sk = await R.J("""() => { const d = document.createElement('div'); d.innerHTML = skelHTML('row', 3); document.body.appendChild(d);
        const w = d.firstElementChild, r = [getComputedStyle(w).display, w.getAttribute('aria-busy'), d.querySelectorAll('.skel-row').length]; d.remove(); return r; }""")
    if sk != ["contents", "true", 3]: R.bad(f"骨架畫面不對：{sk}")
    # 手機播放列：按鍵至少 44px
    if w < 600:
        sz = await R.J("() => ['btnPlay', 'btnSkip', 'btnExpand'].map(id => { const r = document.getElementById(id).getBoundingClientRect(); return Math.min(r.width, r.height); })")
        if min(sz) < 44: R.bad(f"手機播放列按鍵太小：{sz}")
    # L / S 快捷鍵（焦點在頁面上；輸入框裡不觸發）
    if w > 600:
        await R.J("() => document.activeElement && document.activeElement.blur()")
        s0 = await R.J("() => state.shuffleMode")
        await pg.keyboard.press("s"); await pg.wait_for_timeout(700)
        if await R.J("() => state.shuffleMode") == s0: R.bad("按 S 沒切換隨機播放")
        await pg.keyboard.press("s"); await pg.wait_for_timeout(700)
        l0 = await R.J("() => state.loopMode")
        await pg.keyboard.press("l"); await pg.wait_for_timeout(700)
        if await R.J("() => state.loopMode") == l0: R.bad("按 L 沒切換循環")
        for _ in range(2): await pg.keyboard.press("l"); await pg.wait_for_timeout(500)
        if await R.J("() => state.loopMode") != l0: R.bad("按三次 L 沒回到原本的循環模式")
        await pg.focus("#searchInput"); await pg.keyboard.press("s"); await pg.wait_for_timeout(300)
        if await R.J("() => state.shuffleMode") != s0: R.bad("在搜尋框打字時 S 還是切換了隨機")
        await pg.fill("#searchInput", ""); await R.J("() => document.activeElement.blur()")
    # 佇列：鍵盤把手 ↓ 移一格，再 ↑ 移回來（送出排序 API）
    paused = await R.J("async () => { if (state.isPlaying && !state.isPaused) { await API.play(); return true; } return false; }")   # 先暫停：模擬器的歌很短，播完佇列會位移
    await R.J("""async () => { for (let i = 1; i <= 3; i++) await fetch('/api/queue/add', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({guild_id: state.guildId, url: 'https://www.youtube.com/watch?v=KBMOVE0000' + i, title: '鍵盤移動 ' + i})}); }""")
    await R.J("() => { openNpView(); document.querySelector('.tab-btn[data-np=\"queue\"]').click(); }"); await pg.wait_for_timeout(1800)   # 前面的測試可能停在「播放紀錄」分頁
    names = lambda: R.J("() => [...document.querySelectorAll('#npQueueList .list-item .item-title')].slice(0, 2).map(n => n.firstChild.textContent)")
    before = await names()
    if len(before) == 2:
        await pg.focus("#npQueueList .list-item:first-child .drag-handle")
        fdbg = await R.J("() => { const h = document.querySelector('#npQueueList .list-item:first-child .drag-handle'); return [document.activeElement === h, document.activeElement.tagName + '.' + document.activeElement.className, typeof npOpen !== 'undefined' && npOpen, !!h.closest('[inert]'), getComputedStyle(h).visibility, getComputedStyle(h).display, h.getBoundingClientRect().width]; }")
        await pg.keyboard.press("ArrowDown"); await pg.wait_for_timeout(900)
        after = await names()
        if after != before[::-1]:
            dbg = await R.J("() => [typeof _qDrag, !!_qDrag, _qKeyBusy, document.activeElement.className, [...document.querySelectorAll('.toast')].map(t => t.innerText), (state.queue||[]).slice(0,3).map(s=>s.title)]")
            R.bad(f"把手按 ↓ 沒有往下移：{before} → {after} {dbg} {fdbg}")
        srv = await R.J("async () => (await (await fetch('/api/status?guild_id=' + state.guildId)).json()).queue.slice(0, 2).map(s => s.title)")
        if srv != before[::-1]: R.bad(f"鍵盤移動沒同步到伺服器：{srv}")
        focused = await R.J("() => !!document.activeElement.closest('#npQueueList .list-item:nth-child(2)')")
        if not focused: R.bad("鍵盤移動後焦點沒跟著那首歌")
        await pg.keyboard.press("ArrowUp"); await pg.wait_for_timeout(900)
        if await names() != before: R.bad("把手按 ↑ 沒移回來")
        await R.audit("21_queue_kbd")
    else:
        R.bad(f"佇列歌太少，測不了鍵盤移動：{before}")
    # 連點兩下「刪除」只能刪一首（舊版第二下用同一個位置，會刪到下一首）
    srvq = lambda: R.J("async () => (await (await fetch('/api/status?guild_id=' + state.guildId)).json()).queue.map(s => s.url)")
    q0 = await srvq()
    if len(q0) >= 3:
        await pg.dblclick("#npQueueList .list-item:nth-child(2) [data-action=remove]"); await pg.wait_for_timeout(1200)
        q1 = await srvq()
        if q1 != q0[:1] + q0[2:]: R.bad(f"連點兩下刪除不是只刪那一首：{len(q0)} → {len(q1)}")
    if paused: await R.J("() => API.play()")


HITSIZE_JS = r"""(sels) => { const bad = [];
  for (const sel of sels) { const e = [...document.querySelectorAll(sel)].find(x => { const r = x.getBoundingClientRect(); const cs = getComputedStyle(x);
      return r.width && r.top >= 0 && r.bottom <= innerHeight - 90 && cs.visibility !== 'hidden' && !x.closest('[aria-hidden=true]'); });
    if (!e) continue;
    const b = e.getBoundingClientRect(), cx = b.left + b.width / 2, cy = b.top + b.height / 2;
    const hit = (x, y) => { const t = document.elementFromPoint(x, y); return !!t && (t === e || e.contains(t)); };
    let l = 0, r = 0, u = 0, d = 0; while (l < 40 && hit(cx - l - 1, cy)) l++; while (r < 40 && hit(cx + r + 1, cy)) r++; while (u < 40 && hit(cx, cy - u - 1)) u++; while (d < 40 && hit(cx, cy + d + 1)) d++;
    if (l + r + 1 < 43 || u + d + 1 < 43) bad.push(`${sel} ${l + r + 1}×${u + d + 1}`); }
  return bad; }"""


# 播放列上緣：在 x=120 由上往下掃，哪些 y 會點到細進度條（inside：播放列裡面 0~14px；above：播放列上方 12px 內）
MINIBAR_JS = r"""() => { const pb = document.querySelector('.player-bar').getBoundingClientRect(); const r = { inside: [], above: [] };
  for (let d = -12; d <= 14; d++) { const t = document.elementFromPoint(120, pb.top + d); if (t && t.closest('.player-bar .progress-bar')) (d >= 0 ? r.inside : r.above).push(d); }
  return r; }"""


async def feel_checks(R, pg, w):
    """操作手感：連點 / 連按只送一次、按下立刻有反應、吐司不會疊滿畫面、收合後焦點、觸控點擊範圍 ≥ 44"""
    posts = []
    on_req = lambda r: posts.append(r.url.split("?")[0]) if r.method == "POST" else None
    pg.on("request", on_req)
    async def slow(route):
        await asyncio.sleep(0.6); await route.continue_()
    count = lambda path: sum(u.endswith(path) for u in posts)
    try:
        await R.J("() => closeNpView()"); await pg.wait_for_timeout(500)
        # 貼網址按兩次 Enter：只加一次
        await pg.route("**/api/resolve-url", slow)
        await pg.fill("#searchInput", f"https://www.youtube.com/watch?v=FEELD{w:06d}")
        posts.clear()
        await pg.press("#searchInput", "Enter"); await pg.wait_for_timeout(120); await pg.press("#searchInput", "Enter")
        await pg.wait_for_timeout(2500)
        if count("/api/queue/add-batch") != 1: R.bad(f"貼網址連按兩次 Enter 加了 {count('/api/queue/add-batch')} 次")
        await pg.unroute("**/api/resolve-url", slow)
        # 愛心連點：只送一次，而且按下就變色
        await pg.route("**/api/favorites/add", slow)
        posts.clear()
        busy = await R.J("() => { const b = document.getElementById('btnBarFav'); b.click(); const on = b.classList.contains('fav-busy'); setTimeout(() => b.click(), 120); return on; }")
        await pg.wait_for_timeout(1500)
        if not busy: R.bad("按愛心沒有立刻反應（要等伺服器回應）")
        if count("/api/favorites/add") != 1: R.bad(f"愛心連點送了 {count('/api/favorites/add')} 次")
        await pg.unroute("**/api/favorites/add", slow)
        # 新增本地歌曲：確定連按兩下只新增一首
        await pg.route("**/api/playlist/add", slow)
        await R.J("() => document.getElementById('btnPlaylistAdd').click()"); await pg.wait_for_timeout(300)
        await pg.fill("#editUrl", f"https://www.youtube.com/watch?v=FEELM{w:06d}"); await pg.fill("#editTitle", "連點測試")
        posts.clear()
        await R.J("() => { const b = document.getElementById('modalConfirm'); b.click(); setTimeout(() => b.click(), 120); }")
        await pg.wait_for_timeout(1500)
        if count("/api/playlist/add") != 1: R.bad(f"新增歌曲的「確定」連按送了 {count('/api/playlist/add')} 次")
        await pg.unroute("**/api/playlist/add", slow)
        await R.J("() => document.getElementById('editModal').classList.remove('active')")
        # 隨機播放：伺服器慢的時候按鈕不會被舊狀態彈回去
        await pg.route("**/api/shuffle", slow)
        seq = await R.J("""async () => { const b = document.getElementById('btnShuffle'); const want = String(b.getAttribute('aria-pressed') !== 'true'); b.click(); const seen = [];
            for (let i = 0; i < 24; i++) { seen.push(b.getAttribute('aria-pressed')); await new Promise(r => setTimeout(r, 50)); } return [want, seen]; }""")
        if any(v != seq[0] for v in seq[1]): R.bad(f"隨機播放按下後被彈回去：{seq}")
        await pg.unroute("**/api/shuffle", slow)
        await pg.wait_for_timeout(600)
        await R.J("() => document.getElementById('btnShuffle').click()"); await pg.wait_for_timeout(600)
        # 吐司：一次跳很多則也只留幾則
        await R.J("() => { for (let i = 0; i < 7; i++) showToast('連續提示 ' + i, 'success'); }"); await pg.wait_for_timeout(350)
        n = await R.J("() => [...document.querySelectorAll('.toast')].filter(t => !t._leaving).length")
        if n > (3 if w < 600 else 4): R.bad(f"一次跳 7 則吐司，畫面上同時有 {n} 則")
        # 錯誤提示停得比一般提示久（約 5 秒 vs 2.5 秒）
        await R.J("() => showToast('手感測試：錯誤提示', 'error')")
        await pg.wait_for_timeout(3200)
        if not await R.J("() => [...document.querySelectorAll('.toast.error')].some(t => !t._leaving && t.textContent.includes('手感測試：錯誤提示'))"):
            R.bad("錯誤提示 3 秒內就消失了（應該比一般提示停久一點）")
        # 「跳過」連點兩下只跳一首（按鍵 / 快捷鍵 N）
        posts.clear()
        await R.J("() => { const b = document.getElementById('btnSkip'); b.click(); setTimeout(() => b.click(), 90); }"); await pg.wait_for_timeout(1000)
        await R.J("() => document.activeElement && document.activeElement.blur()")
        await pg.keyboard.press("n"); await pg.wait_for_timeout(120); await pg.keyboard.press("n"); await pg.wait_for_timeout(1000)
        if count("/api/skip") != 2: R.bad(f"「跳過」連點兩下 ×2 次，送了 {count('/api/skip')} 次跳過（應該 2 次）")
        # 手機底部播放列：收合時點上緣是展開，不是跳轉；展開時這條細進度條照樣可以跳轉
        if w < 600:
            hits = await R.J(MINIBAR_JS)
            if hits["inside"]: R.bad(f"手機播放列上緣點下去會跳轉（播放列內 y={hits['inside']}）")
            if not hits["above"]: R.bad("手機播放列上方的細進度條完全點不到了")
            await R.J("() => openNpView()"); await pg.wait_for_timeout(700)
            if not (await R.J(MINIBAR_JS))["inside"]: R.bad("展開畫面時底部進度條不能跳轉了")
            await R.J("() => closeNpView()"); await pg.wait_for_timeout(600)
        # 用展開畫面裡的「收合」關掉：焦點要回到看得到的按鈕
        await R.J("() => openNpView()"); await pg.wait_for_timeout(700)
        await R.J("() => { const b = document.getElementById('btnCollapse'); b.focus(); b.click(); }"); await pg.wait_for_timeout(700)
        if await R.J("() => npView.contains(document.activeElement)"): R.bad("按「收合」後焦點還留在看不到的展開畫面裡")
        # 觸控：主要按鈕的點擊範圍至少 44×44
        if w < 600:
            bad = await R.J(HITSIZE_JS, ["#btnSearch", "#btnSettings", "#homeTabs .tab-btn", "#btnPlaylistAdd", "#playlistList .item-btn"])
            await R.J("() => openNpView()"); await pg.wait_for_timeout(700)
            bad += await R.J(HITSIZE_JS, ["#btnCollapse", ".np-extra .ctrl-btn", "#npTabs .tab-btn", "#npBtnQueueClear", "#volumeSlider", ".np-view .list-item .item-btn", ".np-view .list-item .drag-handle"])
            await R.J("() => closeNpView()"); await pg.wait_for_timeout(600)
            if bad: R.bad(f"觸控點擊範圍小於 44×44：{bad}")
    finally:
        pg.remove_listener("request", on_req)


BLOCKED = set()


async def offline_guard(route):
    """測試不連外：本機模擬器照常；外部圖片回 404（等於縮圖不存在），其餘外部請求直接中止"""
    u = route.request.url
    if u.startswith(BASE):
        return await route.continue_()
    BLOCKED.add(u.split("/")[2] if "://" in u else u[:40])
    if route.request.resource_type == "image":
        return await route.fulfill(status=404, body="")
    return await route.abort()


async def scenario(br, theme, w):
    ctx = await br.new_context(viewport={"width": w, "height": 900 if w > 600 else 844}, is_mobile=w < 600, has_touch=w < 600, color_scheme=theme)
    await ctx.add_init_script(f"try{{localStorage.setItem('theme','{theme}')}}catch(e){{}}")
    await ctx.route("**/*", offline_guard)          # 只連本機模擬器；外部主機（discord.com、ytimg…）一律擋掉
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
    # 封面縮圖挑尺寸（純函式，不用連網）：大封面 maxresdefault.jpg 起跳、小圖 mqdefault；不用沒有 CORS 的 vi_webp
    th = await R.J("""() => { const u = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', t = 'https://i.ytimg.com/vi_webp/dQw4w9WgXcQ/maxresdefault.webp';
        return [thumbCandidates(u, t, 1340), thumbCandidates(u, t, 100), thumbCandidates(u, t, 500), thumbCandidates('', 'https://cdn.example/x.png', 999)]; }""")
    if not (th[0][0].endswith("/vi/dQw4w9WgXcQ/maxresdefault.jpg") and th[0][-1].endswith("/mqdefault.jpg") and not any("vi_webp" in x for x in th[0])
            and th[1] == ["https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"] and th[2][0].endswith("/sddefault.jpg")
            and th[3] == ["https://cdn.example/x.png"]):
        R.bad(f"縮圖候選不對：{th}")
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
        ab = await pg.query_selector('#npTabs .tab-btn[data-np="actions"]')
        if not ab or not await ab.is_visible():
            R.bad("展開畫面沒有「操作紀錄」分頁")
        else:
            await ab.click(); await pg.wait_for_timeout(900); await R.audit("11b_np_actions")
            n = await R.J("() => document.querySelectorAll('#npActList .act-item').length")
            if n < 1: R.bad("操作紀錄是空的（前面按過「播放全部」應該有一筆點歌）")
            await pg.click('#npActFilter .seg-btn[data-act="remove"]'); await pg.wait_for_timeout(300)
            if await R.J("() => [...document.querySelectorAll('#npActList .act-item')].some(e => !e.classList.contains('act-remove'))"):
                R.bad("操作紀錄篩選「刪歌」後還有別的種類")
            await pg.click('#npActFilter .seg-btn[data-act=""]'); await pg.wait_for_timeout(200)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(500)
        if await R.J("() => document.body.classList.contains('np-open') || document.documentElement.classList.contains('np-open')"): R.bad("Esc 沒收合播放畫面")
    # 播放控制回歸：先確定有歌在播（模擬器的歌很短，前面的步驟跑完時可能已經播完了）
    await R.J("""() => fetch('/api/queue/add', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({guild_id: state.guildId, url: 'https://www.youtube.com/watch?v=SMOKEPLAY01', title: '播放測試'})})""")
    for _ in range(30):
        if await R.J("() => !!(state.isPlaying || state.isPaused)"): break
        await pg.wait_for_timeout(100)
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
        if bid in MODE_ITEMS and w < 1180 and not await R.J("() => npOpen"):
            await R.J("() => openNpView()"); await pg.wait_for_timeout(500)
        if bid in MODE_ITEMS: await open_modes(R, pg)
        loc = pg.locator(f"#{bid}:visible")
        if not await loc.count():
            if bid in MODE_ITEMS: R.bad(f"按鈕 #{bid} 在 ⋮ 選單裡找不到（看不見）")
            continue
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
    await normalize_checks(R, pg, w)
    await ui6_checks(R, pg, w)
    await feel_checks(R, pg, w)
    await mode_menu_checks(R, pg, w)
    await R.J("() => closeNpView()"); await pg.wait_for_timeout(600)
    # 個人歌單：依登入身分（不用再貼金鑰）→ 新增 → 出現在清單
    tab = pg.locator('.tab-btn[data-tab="mylists"]:visible').first
    if await tab.count() and await tab.evaluate("e => getComputedStyle(e).pointerEvents") != "none":
        await tab.click(); await pg.wait_for_timeout(400)
        if await pg.locator("#mlToken").count(): R.bad("我的歌單還在要求貼金鑰（應該直接用登入身分）")
        if not await pg.locator("#btnMlNew:visible").count(): R.bad("我的歌單沒有顯示「新增」（登入身分沒帶過去）")
        name = f"煙霧{theme}{w}"
        await pg.click("#btnMlNew"); await pg.wait_for_timeout(400)
        if not await pg.locator("#askModal.active #askInput:visible").count(): R.bad("新增歌單沒有跳出面板對話框")
        else:
            await pg.fill("#askInput", name); await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(1500)
        if await pg.locator("#askModal.active").count(): R.bad("面板對話框按 Enter 後沒關")
        if name not in (await R.J("() => document.getElementById('mlList').innerText")): R.bad("新增個人歌單後清單沒出現")
        await R.audit("07_mylists_loggedin")
        more = pg.locator("#mlList .ml-more:visible").first
        if await more.count():
            await more.click(); await pg.wait_for_timeout(200)
            await pg.keyboard.press("Escape"); await pg.wait_for_timeout(150)
            if await R.J("() => !!document.querySelector('#mlList .menu-open')"): R.bad("⋯ 選單 Esc 沒關")
        await pg.keyboard.press("Escape")
    # 彈出框（桌面）
    await settings_menu_checks(R, pg, w)
    if w > 600:
        await R.esc_and_outside("@#btnStatus", "#statusPop", "狀態框")
        await R.esc_and_outside("@#btnShortcuts", "#kbdHelp", "快捷鍵")
        await R.esc_and_outside(".sel-wrap.cdd:has(#voiceChannelSelect) .cdd-btn", "#voiceChannelSelectList", "語音下拉")
        await R.menu("#btnStatus"); await R.menu("#btnShortcuts")
        if not await R.hidden("#statusPop"): R.bad("狀態框與快捷鍵同時開著")
        if await R.hidden("#setMenu"): R.bad("開子選單時齒輪選單被收起")
        await R.audit("12_kbd")
        await R.menu("#btnStatus"); await pg.wait_for_timeout(350); await R.audit("13_status")
        await pg.keyboard.press("Escape"); await pg.keyboard.press("Escape")
    else:
        await R.menu("#btnStatus"); await pg.wait_for_timeout(350); await R.audit("13_status")
        await pg.keyboard.press("Escape"); await pg.keyboard.press("Escape")
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
    # 搜尋框刪光 → 結果收起；只刪幾個字還留著；等結果時清空，結果回來也不能再冒出來
    has_res = lambda: R.J("() => !!document.getElementById('searchResults').firstChild")
    await pg.fill("#searchInput", "測試"); await pg.keyboard.press("Enter"); await pg.wait_for_timeout(1200)
    if not await has_res(): R.bad("搜尋沒有出現結果")
    await pg.keyboard.press("Backspace"); await pg.wait_for_timeout(150)
    if not await has_res(): R.bad("搜尋框只刪一個字，結果就不見了")
    await pg.keyboard.press("Backspace"); await pg.wait_for_timeout(200)
    if await has_res(): R.bad("搜尋框刪光後，搜尋結果沒有收起")
    async def slow_search(route):
        await asyncio.sleep(1.5)
        try: await route.continue_()
        except Exception: pass
    await pg.route("**/api/search", slow_search)
    await pg.fill("#searchInput", "測試"); await pg.keyboard.press("Enter"); await pg.wait_for_timeout(300)
    if not await has_res(): R.bad("搜尋中沒有顯示佔位")
    await pg.fill("#searchInput", ""); await pg.wait_for_timeout(2200)
    if await has_res(): R.bad("搜尋中清空搜尋框，結果回來後又冒出來")
    if await R.J("() => el.btnSearch.disabled"): R.bad("搜尋中清空後搜尋鈕一直停用")
    await pg.unroute_all(behavior="ignoreErrors")
    # 頂欄：桌機各種寬度都不能把右側按鈕 / 「播放中」擠出畫面（之前 1081–1279px 會超出）
    if w > 600:
        for vw in range(900, 1601, 20):
            await pg.set_viewport_size({"width": vw, "height": 900}); await pg.wait_for_timeout(60)
            o = await R.J("""() => { const h = document.querySelector('.app-header').getBoundingClientRect();
                return [document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
                        [...document.querySelectorAll('.app-header *')].some(e => { const r = e.getBoundingClientRect(); return r.width && (r.right > h.right + .5 || r.left < h.left - .5); })]; }""")
            if any(o): R.bad(f"頂欄在 {vw}px 超出 {o}")
        await pg.set_viewport_size({"width": w, "height": 900}); await pg.wait_for_timeout(200)
    # 通知
    await R.J("() => { showToast('✅ 測試成功', 'success'); showToast('連線失敗', 'error'); showToast('注意', 'warning'); }")
    await R.audit("14_toasts")
    # 成員頁
    m = await ctx.new_page(); R2 = Run(m, theme, w)
    await m.goto(BASE + "/members"); await m.wait_for_timeout(800); await R2.audit("15_members")
    # 斷線狀態
    # 等條件成立（最多 25 秒；正常約 3 秒 = EventSource 失敗 2 次）而不是固定等 7 秒：機器忙的時候會慢一點，固定時間會偶爾誤報
    await ctx.set_offline(True)
    await R.J("() => connectProgressStream(state.guildId)")
    if not await wait_status(pg, "離線", True):
        diag = await R.J("() => JSON.stringify({guild: state.guildId, fails: sseFails, rs: sseSource ? sseSource.readyState : null, text: document.getElementById('statusText').textContent})")
        R.bad(f"斷線後 25 秒內狀態沒有顯示「離線」{diag}")
    await R.audit("16_offline")
    await ctx.set_offline(False)
    if not await wait_status(pg, "離線", False, 40000): R.bad("網路恢復後仍顯示「離線」")
    # 登出：齒輪選單裡的「登出」；之後回到登入頁、再開面板也會被擋
    await pg.reload(); await pg.wait_for_timeout(1200)
    await R.menu("#btnLogout")
    await pg.wait_for_timeout(1000)
    if "/login" not in pg.url: R.bad(f"按登出後沒有回到登入頁（{pg.url}）")
    await pg.goto(BASE + "/"); await pg.wait_for_timeout(500)
    if "/login" not in pg.url: R.bad("登出後再開面板沒被擋")
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
    print(f"已擋下的外部主機：{sorted(BLOCKED) or '無'}")
    print(f"問題 {len(PROBLEMS)} 項；截圖在 {OUT}/")
    sys.exit(1 if PROBLEMS else 0)

asyncio.run(main())
