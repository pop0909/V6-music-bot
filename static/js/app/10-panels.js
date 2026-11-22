// ==================== 再聽一次（播放紀錄卡片列） ====================
const shelfState = { seq: 0, timer: 0, last: 0 };
function scheduleShelfReload(delay = 0) {
    clearTimeout(shelfState.timer);
    shelfState.timer = setTimeout(loadShelf, delay);
}
async function loadShelf() {
    if (!el.shelfRow) return;
    if (!state.guildId) { renderShelf([]); return; }
    const seq = ++shelfState.seq;
    shelfState.last = Date.now();
    if (!el.shelfRow.querySelector('.shelf-card')) showSkeleton(el.shelfRow, 'card', 6);
    let res = await API.historyTop('30d');
    let items = (res && res.items) || [];
    if (items.length < 4) {
        // 紀錄還少：用最近播放補（依網址去重）
        const recent = await API.history();
        const seen = new Set(items.map(i => i.url));
        for (const r of (recent && recent.items) || []) {
            if (!seen.has(r.url)) { seen.add(r.url); items.push(r); }
        }
    }
    if (seq !== shelfState.seq) return;
    renderShelf(items.slice(0, 16));
}
function renderShelf(items) {
    const row = el.shelfRow;
    row.textContent = '';
    if (el.shelfAgain) el.shelfAgain.classList.toggle('is-empty', !items.length);
    if (!items.length) {
        const p = document.createElement('div');
        p.className = 'shelf-empty';
        p.innerHTML = '<i class="fas fa-compact-disc"></i><span>播放超過 30 秒的歌會出現在這裡</span>';
        row.appendChild(p);
        return;
    }
    const frag = document.createDocumentFragment();
    const pending = [];
    items.forEach((it, i) => {
        const card = document.createElement('div');
        card.className = 'shelf-card';
        card.style.setProperty('--i', i);
        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        const art = document.createElement('div');
        art.className = 'shelf-art';
        if (thumbCandidates(it.url, it.thumbnail, 320).length) {
            const img = document.createElement('img');
            img.loading = 'lazy'; img.decoding = 'async'; img.alt = '';
            img.width = 320; img.height = 180;   // 固定尺寸：載入時不改版面
            art.appendChild(img);
            pending.push([img, it]);
        }
        const play = document.createElement('span');
        play.className = 'shelf-play';
        play.innerHTML = '<i class="fas fa-play"></i>';
        art.appendChild(play);
        const title = document.createElement('div');
        title.className = 'shelf-name';
        title.textContent = it.title || it.url;    // 純文字，不會被當 HTML
        title.title = it.title || '';
        const meta = document.createElement('div');
        meta.className = 'shelf-meta';
        meta.textContent = it.plays ? `播放 ${it.plays} 次` : (it.played_at ? fmtAgo(it.played_at) : '');
        card.append(art, title, meta);
        const add = async () => {
            if (card.classList.contains('is-adding')) return;   // 連點兩下只加一次
            card.classList.add('is-adding'); card.setAttribute('aria-busy', 'true');
            const pi = play.querySelector('i'); pi.className = 'fas fa-spinner fa-pulse';
            try {
                const r = await API.playUrl(it.url, it.title, null, undefined, it.duration);
                toastAdded(r, it.title);
            } finally { card.classList.remove('is-adding'); card.removeAttribute('aria-busy'); pi.className = 'fas fa-play'; }
        };
        card.addEventListener('click', add);
        card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); add(); } });
        frag.appendChild(card);
    });
    row.appendChild(frag);
    // 卡片放進畫面後才知道實際寬度（桌機約 180~220px、手機約 300~415px）→ 依寬度 × 螢幕倍率挑縮圖大小
    pending.forEach(([img, it]) => {
        const need = thumbNeed(img.parentNode) || 320;
        loadThumb(img, thumbCandidates(it.url, it.thumbnail, need), () => img.remove());
    });
}
['shelfPrev', 'shelfNext'].forEach((id, k) => {
    const b = $(id);
    if (b) b.addEventListener('click', () => {
        const row = el.shelfRow;
        row.scrollBy({ left: (k ? 1 : -1) * Math.max(240, row.clientWidth * 0.8), behavior: fxOff() ? 'auto' : 'smooth' });
    });
});

// 初始化分頁膠囊位置
applyWideLayout();
requestAnimationFrame(() => document.querySelectorAll('.tabs .tab-btn.active').forEach(a => fxMovePill(a, true)));

// ==================== FPS 診斷（網址加 ?fpsdebug=1） ====================
// 每次展開 / 收合時記錄 requestAnimationFrame 間隔（只在動畫期間跑），結束後顯示：
// 畫格數、平均 FPS、最慢一格、超過 20ms / 34ms 的格數；「自動測試」會自動展開 / 收合 5 次並彙總。
(function fpsDebug() {
    if (!/[?&]fpsdebug=1(?:&|$)/.test(location.search)) return;
    const box = document.createElement('div');
    box.className = 'fps-debug';
    box.innerHTML = '<div class="fps-title">FPS 診斷</div><pre class="fps-out">展開 / 收合播放畫面，或按「自動測試」</pre><button type="button" class="fps-run">自動測試</button>';
    document.body.appendChild(box);
    const out = box.querySelector('.fps-out');
    const btn = box.querySelector('.fps-run');
    const runs = [];
    let rec = null;
    const stat = (d) => {
        const total = d.reduce((a, b) => a + b, 0);
        return {
            frames: d.length,
            avgFps: total ? Math.round(1000 * d.length / total * 10) / 10 : 0,
            worst: Math.round(Math.max(0, ...d) * 10) / 10,
            over20: d.filter(x => x > 20).length,
            over34: d.filter(x => x > 34).length,
        };
    };
    const fmt = (t, s) => `${t}：${s.frames} 格，平均 ${s.avgFps} fps\n  最慢 ${s.worst}ms，>20ms ${s.over20}，>34ms ${s.over34}`;
    const api = {
        start(kind) {
            if (rec) cancelAnimationFrame(rec.id);
            const r = { kind, d: [], last: performance.now(), id: 0 };
            rec = r;
            const tick = (t) => { if (rec !== r) return; r.d.push(t - r.last); r.last = t; r.id = requestAnimationFrame(tick); };
            r.id = requestAnimationFrame(tick);
        },
        stop() {
            // 多記 150ms：連同落定後那一格（補套用暫存的即時更新）一起算
            const r0 = rec;
            if (r0) setTimeout(() => { if (rec === r0) api.finish(); }, 150);
        },
        finish() {
            if (!rec) return;
            const r = rec; rec = null;
            cancelAnimationFrame(r.id);
            const s = Object.assign({ kind: r.kind, deltas: r.d.map(x => Math.round(x * 10) / 10) }, stat(r.d));
            runs.push(s);
            if (!api.auto) out.textContent = fmt('上次' + r.kind, s);
        },
        runs,
        auto: false,
    };
    window.__npFps = api;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const waitIdle = async () => { for (let i = 0; i < 60 && (_npAnimating || rec); i++) await sleep(50); };
    async function autoTest(n = 5) {
        if (api.auto) return;
        api.auto = true; btn.disabled = true;
        if (npOpen) { setNpOpen(false); await waitIdle(); await sleep(400); }
        const from = runs.length;
        for (let i = 0; i < n; i++) {
            out.textContent = `自動測試中… ${i + 1}/${n}`;
            setNpOpen(true); await waitIdle(); await sleep(450);
            setNpOpen(false); await waitIdle(); await sleep(450);
        }
        const mine = runs.slice(from);
        const all = stat(mine.flatMap(r => r.deltas));
        const op = stat(mine.filter(r => r.kind === '展開').flatMap(r => r.deltas));
        const cl = stat(mine.filter(r => r.kind === '收合').flatMap(r => r.deltas));
        const summary = { all, open: op, close: cl, n };
        window.__npFpsSummary = summary;
        out.textContent = `自動測試（${n} 次展開 + ${n} 次收合）\n` + fmt('全部', all) + '\n' + fmt('展開', op) + '\n' + fmt('收合', cl);
        api.auto = false; btn.disabled = false;
        return summary;
    }
    btn.addEventListener('click', () => autoTest(5));
    window.__npFpsAuto = autoTest;
})();

// ==================== 控制鍵提示：觸控長按顯示名稱（滑鼠是 :hover 即時顯示，純 CSS） ====================
// 顯示提示；若會超出視窗左右邊，用 --tip-dx 往內推（只在顯示那一下量一次，不在動畫中）
function showTip(t) {
    if (t.style.getPropertyValue('--tip-dx')) t.style.removeProperty('--tip-dx');
    t.classList.add('tip-show');
    const cs = getComputedStyle(t, '::after');
    const w = parseFloat(cs.width) || 0, r = t.getBoundingClientRect();
    if (!w) return;
    const left = cs.left === 'auto' ? r.right - parseFloat(cs.right) - w : r.left + parseFloat(cs.left);
    const m = new DOMMatrixReadOnly(cs.transform === 'none' ? undefined : cs.transform);
    const x0 = left + m.e, x1 = x0 + w, vw = document.documentElement.clientWidth;
    const dx = x0 < 8 ? 8 - x0 : (x1 > vw - 8 ? (vw - 8) - x1 : 0);
    if (dx) t.style.setProperty('--tip-dx', `${Math.round(dx)}px`);
}
(function tipLongPress() {
    let timer = 0, shown = null, suppress = false, hideTimer = 0;
    const hide = () => { clearTimeout(hideTimer); if (shown) { shown.classList.remove('tip-show'); shown = null; } };
    document.addEventListener('pointerdown', (e) => {
        suppress = false;
        if (e.pointerType !== 'touch') return;
        const t = e.target.closest('.has-tip');
        if (!t || e.target.closest('input[type=range]')) return;
        clearTimeout(timer);
        timer = setTimeout(() => {
            hide(); shown = t; showTip(t); suppress = true;
            if (navigator.vibrate) try { navigator.vibrate(8); } catch (_) {}
        }, 450);
    }, { passive: true, capture: true });
    const end = () => { clearTimeout(timer); if (shown) { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 1400); } };
    document.addEventListener('pointerup', end, { passive: true, capture: true });
    document.addEventListener('pointercancel', () => { clearTimeout(timer); hide(); }, { passive: true, capture: true });
    // 長按只看名稱，不觸發按鈕 / 開關
    document.addEventListener('click', (e) => {
        if (suppress && e.target.closest('.has-tip')) { e.preventDefault(); e.stopPropagation(); }
        suppress = false;
    }, true);
    document.addEventListener('contextmenu', (e) => { if (e.target.closest && e.target.closest('.has-tip')) e.preventDefault(); });
    // 滑鼠 / 觸控筆：移上去立刻顯示（只加減一個 class；用 pointerType 判斷，觸控點一下不會留下黏住的提示）
    let hovered = null;
    document.addEventListener('pointerover', (e) => {
        if (e.pointerType === 'touch') return;
        const t = e.target.closest ? e.target.closest('.has-tip') : null;
        if (t === hovered) return;
        if (hovered && hovered !== shown) hovered.classList.remove('tip-show');
        hovered = t;
        if (t) showTip(t);
    }, { passive: true });
    document.addEventListener('pointerout', (e) => {
        if (!hovered || e.pointerType === 'touch') return;
        if (e.relatedTarget && hovered.contains(e.relatedTarget)) return;
        if (hovered !== shown) hovered.classList.remove('tip-show');
        hovered = null;
    }, { passive: true });
})();
updateSleepIndicator();
setShuffleUI(!!state.shuffleMode);


// ==================== 深色 / 淺色主題（head 內的小腳本已先套用，這裡只處理切換） ====================
(function themeToggle() {
    const btn = $('btnTheme');
    const mq = window.matchMedia ? matchMedia('(prefers-color-scheme: light)') : null;
    const meta = document.querySelector('meta[name="theme-color"]');
    const apply = (t) => {
        document.documentElement.dataset.theme = t;
        if (meta) meta.content = t === 'light' ? '#f7f6fb' : '#07060b';
        if (btn) {   // 齒輪選單裡的一項：圖示 + 文字（顯示「按下去會變成什麼」）
            const ic = btn.querySelector('i'), lb = btn.querySelector('.set-label');
            if (ic) ic.className = t === 'light' ? 'fas fa-moon' : 'fas fa-sun';
            if (lb) lb.textContent = t === 'light' ? '切換成深色' : '切換成淺色';
        }
    };
    apply(document.documentElement.dataset.theme || 'dark');
    if (btn) btn.addEventListener('click', () => {
        const t = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
        try { localStorage.setItem('theme', t); } catch (_) {}
        document.documentElement.classList.add('theme-anim');
        apply(t);
        setTimeout(() => document.documentElement.classList.remove('theme-anim'), 320);
    });
    if (mq && mq.addEventListener) mq.addEventListener('change', (e) => {
        let saved = null; try { saved = localStorage.getItem('theme'); } catch (_) {}
        if (!saved) apply(e.matches ? 'light' : 'dark');
    });
})();

// ==================== 頂欄齒輪設定選單（主題 / 狀態 › / 成員管理 / 快捷鍵 › / 登出） ====================
// 點齒輪開關；點外面或 Esc 關；鍵盤：↑↓ / Home / End 移動、Enter / 空白選、Tab 離開。
// 「機器人狀態」「鍵盤快捷鍵」是子選單（像 Chrome 選單的 ›）：滑過去（桌面）或點 / Enter / → 打開，
// 旁邊放得下就貼著選單右邊展開，放不下改左邊；兩邊都放不下（手機）就在選單裡換頁、上面有「‹ 返回」。← / Esc 回到選單。
(function settingsMenu() {
    const btn = $('btnSettings'), menu = $('setMenu'), wrap = $('setWrap');
    if (!btn || !menu || !wrap) return;
    const FLY = { statusPop: 'btnStatus', kbdHelp: 'btnShortcuts' };
    const GAP = 6, EDGE = 8;
    const items = () => Array.from(menu.querySelectorAll('.set-item')).filter(x => x.offsetParent !== null);
    const openFly = () => Object.keys(FLY).map($).find(p => p && !p.hidden) || null;
    function focusItem(i) {
        const list = items(); if (!list.length) return;
        const k = (i + list.length) % list.length;
        list.forEach((x, j) => x.tabIndex = j === k ? 0 : -1);
        list[k].focus();
    }
    function focusRow(row) {
        const list = items(), k = list.indexOf(row);
        if (k >= 0) focusItem(k);
    }
    function showMenu() {
        if (!menu.hidden) return;
        if (window.closeDropdowns) window.closeDropdowns();
        menu.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
        btn.classList.remove('tip-show');
    }
    function open(viaKey) {
        if (!menu.hidden) return;
        showMenu();
        if (viaKey) focusItem(0);
    }
    function closeFlys() {
        if (typeof setKbdHelp === 'function') setKbdHelp(false);
        if (window.setStatusOpen) window.setStatusOpen(false);
    }
    function close(focusBtn) {
        if (menu.hidden) return;
        const had = wrap.contains(document.activeElement) && document.activeElement !== btn;
        closeFlys();
        menu.hidden = true;
        menu.classList.remove('sub-open');
        btn.setAttribute('aria-expanded', 'false');
        if (focusBtn || had) btn.focus({ preventScroll: true });
    }
    window.closeSettingsMenu = () => close(false);

    // 子選單定位：由狀態框 / 快捷鍵自己開關時呼叫（含按 ? 打開快捷鍵 → 連選單一起打開）
    function place(id) {
        const pop = $(id), row = $(FLY[id]);
        if (!pop || pop.hidden) return;
        showMenu();
        if (row) row.setAttribute('aria-expanded', 'true');
        pop.classList.remove('fly-left', 'fly-right', 'fly-inplace');
        menu.classList.remove('sub-open');
        pop.style.left = pop.style.top = pop.style.width = pop.style.maxHeight = '';
        const m = menu.getBoundingClientRect(), w = wrap.getBoundingClientRect();
        const vw = document.documentElement.clientWidth, vh = window.innerHeight;
        const fw = Math.min(340, vw - 2 * EDGE);
        let left = null, side = '';
        if (m.right + GAP + fw <= vw - EDGE) { left = m.right + GAP; side = 'fly-right'; }
        else if (m.left - GAP - fw >= EDGE) { left = m.left - GAP - fw; side = 'fly-left'; }
        if (left === null) {
            // 手機：在選單的位置換頁（蓋住選單、寬度至少跟選單一樣，不超出畫面）
            const width = Math.min(Math.max(m.width, fw), vw - 2 * EDGE);
            const l = Math.max(EDGE, Math.min(m.right - width, vw - EDGE - width));
            pop.classList.add('fly-inplace'); menu.classList.add('sub-open');
            pop.style.width = width + 'px';
            pop.style.left = (l - w.left) + 'px';
            pop.style.top = (m.top - w.top) + 'px';
            pop.style.maxHeight = Math.max(160, vh - m.top - EDGE) + 'px';
            return;
        }
        pop.classList.add(side);
        pop.style.width = fw + 'px';
        pop.style.left = (left - w.left) + 'px';
        const rr = (row || menu).getBoundingClientRect();
        let top = rr.top - 7;                      // 跟那一列對齊（扣掉子選單的上內距）
        pop.style.maxHeight = (vh - 2 * EDGE) + 'px';
        const h = pop.getBoundingClientRect().height;
        top = Math.max(EDGE, Math.min(top, vh - EDGE - h));
        pop.style.top = (top - w.top) + 'px';
    }
    function unplace(id) {
        const pop = $(id), row = $(FLY[id]);
        if (row) row.setAttribute('aria-expanded', 'false');
        if (pop) pop.classList.remove('fly-left', 'fly-right', 'fly-inplace');
        if (!openFly()) menu.classList.remove('sub-open');
    }
    window.setFlyPlace = place;
    window.setFlyDone = unplace;
    window.addEventListener('resize', () => { const p = openFly(); if (p) place(p.id); }, { passive: true });

    // 從某一列打開它的子選單；viaKey → 焦點移進子選單（手機換頁時放在「返回」）
    function openSub(row, viaKey) {
        const id = row.getAttribute('aria-controls');
        const pop = $(id); if (!pop) return;
        if (pop.hidden) {
            if (id === 'kbdHelp') setKbdHelp(true);
            else if (window.setStatusOpen) window.setStatusOpen(true);
        }
        if (viaKey) {
            const back = pop.querySelector('.set-back');
            (pop.classList.contains('fly-inplace') && back ? back : pop).focus({ preventScroll: true });
        }
    }
    function closeSub(focus) {
        const pop = openFly(); if (!pop) return;
        const row = $(FLY[pop.id]);
        closeFlys();
        if (focus && row && !menu.hidden) focusRow(row);
    }

    btn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden ? open(e.detail === 0) : close(false); });
    btn.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); open(true); if (e.key === 'ArrowUp') focusItem(-1); }
    });
    menu.addEventListener('keydown', (e) => {
        const list = items(), i = list.indexOf(document.activeElement);
        const row = document.activeElement && document.activeElement.closest && document.activeElement.closest('.set-item');
        const sub = row && row.getAttribute('aria-controls') in FLY;
        if (e.key === 'ArrowDown') { closeFlys(); focusItem(i + 1); }
        else if (e.key === 'ArrowUp') { closeFlys(); focusItem(i - 1); }
        else if (e.key === 'Home') { closeFlys(); focusItem(0); }
        else if (e.key === 'End') { closeFlys(); focusItem(-1); }
        else if (sub && (e.key === 'ArrowRight' || e.key === 'Enter' || e.key === ' ')) openSub(row, true);
        else if (e.key === 'Tab') { close(false); return; }
        else return;
        e.preventDefault(); e.stopPropagation();
    });
    // 點一列：子選單列 → 打開子選單、選單留著；其他列 → 照常執行並收起選單（捕獲階段，搶在各按鈕自己的 click 之前）
    menu.addEventListener('click', (e) => {
        const it = e.target.closest('.set-item'); if (!it) return;
        const viaKey = e.detail === 0;
        if (it.getAttribute('aria-controls') in FLY) {
            e.stopPropagation(); e.preventDefault();          // 不交給原本的開關（會變成按一下又關掉）
            openSub(it, viaKey);
            return;
        }
        closeFlys();
        menu.hidden = true; menu.classList.remove('sub-open'); btn.setAttribute('aria-expanded', 'false');
        if (it.id === 'btnTheme' && viaKey) btn.focus({ preventScroll: true });
    }, true);
    // 桌面滑鼠：滑到子選單列就打開（稍微延遲，斜著移過其他列時不閃），滑到其他列就收起
    let hoverT = 0;
    menu.addEventListener('pointerover', (e) => {
        if (e.pointerType !== 'mouse') return;
        const it = e.target.closest('.set-item'); if (!it) return;
        clearTimeout(hoverT);
        const id = it.getAttribute('aria-controls');
        if (id in FLY) { if ($(id).hidden) hoverT = setTimeout(() => { if (!menu.hidden) openSub(it, false); }, 140); }
        else if (openFly()) hoverT = setTimeout(() => { if (!openFly() || openFly().classList.contains('fly-inplace')) return; closeFlys(); }, 260);
    });
    Object.keys(FLY).forEach(id => {
        const pop = $(id); if (!pop) return;
        pop.addEventListener('pointerenter', () => clearTimeout(hoverT));
        const back = pop.querySelector('.set-back');
        if (back) back.addEventListener('click', (e) => {
            e.stopPropagation();
            closeSub(true);
        }, true);
    });
    // 點外面：整個收起（子選單也在 wrap 裡）
    document.addEventListener('pointerdown', (e) => { if (!menu.hidden && !wrap.contains(e.target)) close(false); }, { capture: true, passive: true });
    // Esc / ←：子選單開著 → 回到選單；否則 Esc 關掉選單。捕獲階段處理，不讓 Esc 順便收合播放畫面
    document.addEventListener('keydown', (e) => {
        if (menu.hidden || e.isComposing) return;
        const ae = document.activeElement, inWrap = wrap.contains(ae);
        const fly = openFly();
        if (e.key === 'Escape') {
            if (fly) closeSub(inWrap || ae === document.body);
            else close(inWrap);
        } else if (e.key === 'ArrowLeft' && fly && fly.contains(ae) && ae.tagName !== 'INPUT') {
            closeSub(true);
        } else return;
        e.preventDefault(); e.stopImmediatePropagation();
    }, true);
})();

// ==================== 狀態頁 + 一鍵更新 ====================
(function statusPanel() {
    const pop = $('statusPop'), btn = $('btnStatus');
    if (!pop || !btn) return;
    let timer = 0, waitingRestart = false;
    const fmtBytes = (b) => b == null ? '—' : (b >= 1 << 30 ? (b / (1 << 30)).toFixed(1) + ' GB' : Math.round(b / (1 << 20)) + ' MB');
    const fmtUp = (s) => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
        return (d ? d + ' 天 ' : '') + (d || h ? h + ' 小時 ' : '') + m + ' 分'; };
    const set = (k, v, cls) => { const e = pop.querySelector(`[data-k="${k}"]`); if (e) { e.textContent = v; e.className = cls || ''; } };
    const upBtn = $('btnUpdate'), upMsg = $('updateMsg');
    async function refresh() {
        const r = await api('GET', '/api/system' + (state.guildId ? `?guild_id=${state.guildId}` : ''));
        if (!r || r.status === 'error' || r.uptime == null) {
            set('ready', waitingRestart ? '重新啟動中…' : '連不上', 'st-warn');
            return;
        }
        if (waitingRestart && r.uptime < 120) { waitingRestart = false; showToast('✅ 機器人已重新啟動', 'success', { key: 'update' }); }
        set('ready', r.ready ? '在線' : '連線中', r.ready ? 'st-ok' : 'st-warn');
        set('latency', r.latency_ms == null ? '—' : r.latency_ms + ' ms', r.latency_ms > 300 ? 'st-warn' : '');
        set('vlatency', r.voice_latency_ms == null ? '—' : r.voice_latency_ms + ' ms', r.voice_latency_ms > 300 ? 'st-warn' : '');
        set('uptime', fmtUp(r.uptime));
        set('cpu', r.cpu == null ? '—' : `${r.cpu.toFixed(0)}%（機器人 ${r.proc_cpu}%）`, r.cpu > 85 ? 'st-warn' : '');
        set('mem', r.mem_total ? `${fmtBytes(r.mem_used)} / ${fmtBytes(r.mem_total)}（機器人 ${fmtBytes(r.proc_rss)}）` : '—', r.mem_pct > 90 ? 'st-warn' : '');
        set('guilds', `${r.guilds} 個伺服器 · ${r.voice} 個語音${r.playing ? `（${r.playing} 個播放中）` : ''}`);
        set('bitrate', state.bitrate ? `Opus ${state.bitrate} kbps` : '未在語音中');
        set('ytdlp', r.ytdlp || '—');
        const ck = r.cookies || {};
        // 到期日（只有擁有者拿得到 ck.expiry）：剩 ≤7 天 / 已過期 → 標黃並在下方說明
        const ex = ck.expiry, exSoon = !!(ex && ex.level != null);
        const exDate = ex ? new Date(ex.earliest * 1000).toLocaleDateString('zh-TW', { timeZone: 'Asia/Taipei' }) : '';
        const exText = !ex ? '' : ex.days_left <= 0 ? ` · 已過期（${exDate}）` : ` · ${exDate} 到期（剩 ${ex.days_left < 1 ? '不到 1' : Math.floor(ex.days_left)} 天）`;
        if (!ck.configured) set('cookies', '未設定', 'st-warn');
        else if (ck.session_ok) set('cookies', '已設定（含登入工作階段）' + exText, exSoon ? 'st-warn' : 'st-ok');
        else set('cookies', '已設定（缺 LOGIN_INFO / SID）' + exText, 'st-warn');
        const cw = $('cookieWarn');
        if (cw) {
            const msgs = [];
            if (ck.configured && !ck.session_ok && ck.warning) msgs.push(ck.warning);
            if (ck.configured && exSoon) msgs.push(ex.days_left <= 0
                ? `cookies 的 ${ex.name} 已過期：請重新匯出 cookies.txt 覆寫主檔。`
                : `cookies 的 ${ex.name} 快過期了：請在到期前重新匯出 cookies.txt 覆寫主檔（Google 也可能提早讓登入失效）。`);
            cw.textContent = msgs.join(' ');
            cw.className = 'update-msg cookie-warn' + (msgs.length ? ' st-warn' : '');
        }
        if (window.setFlyPlace) requestAnimationFrame(() => window.setFlyPlace('statusPop'));   // 內容高度變了 → 重新貼齊、不超出畫面
        set('ffmpeg', r.ffmpeg && r.ffmpeg.ok ? ('有' + (r.ffmpeg.version ? `（${r.ffmpeg.version}）` : '')) : '找不到！', r.ffmpeg && r.ffmpeg.ok ? '' : 'st-bad');
        const u = r.update || {};
        if (upBtn) {
            upBtn.disabled = !!u.running || u.cooldown > 0 || waitingRestart;
            upBtn.innerHTML = u.running ? '<i class="fas fa-spinner fa-spin"></i> 更新中…' : '<i class="fas fa-arrows-rotate"></i> 更新 yt-dlp 並重啟';
        }
        if (upMsg) {
            if (u.result) upMsg.textContent = u.result.message + (u.result.detail ? '\n' + u.result.detail : '');
            else if (u.cooldown > 0 && !u.running) upMsg.textContent = `${Math.ceil(u.cooldown / 60)} 分鐘後可再更新`;
            else if (!u.running) upMsg.textContent = '';
            upMsg.className = 'update-msg' + (u.result && !u.result.ok ? ' st-bad' : '');
            if (u.result && u.result.restart && !waitingRestart && r.uptime > 120) waitingRestart = true;
        }
    }
    function open(o) {
        if (pop.hidden === !o) return;
        pop.hidden = !o;
        if (o) { setKbdHelp(false); if (window.closeDropdowns) window.closeDropdowns(); }   // 跟快捷鍵說明互斥
        btn.setAttribute('aria-expanded', String(o));
        if (o) { if (window.setFlyPlace) window.setFlyPlace('statusPop'); }   // 齒輪選單旁邊展開
        else if (window.setFlyDone) window.setFlyDone('statusPop');
        clearInterval(timer);
        if (o) { refresh(); timer = setInterval(() => { if (!document.hidden) refresh(); }, 3000); }
    }
    window.setStatusOpen = open;
    $('statusClose') && $('statusClose').addEventListener('click', () => open(false));
    // 點外面就收起（齒輪選單本身不算外面；整個收起由 settingsMenu 處理）
    document.addEventListener('pointerdown', (e) => { const w = $('setWrap'); if (!pop.hidden && !(w && w.contains(e.target))) open(false); }, { capture: true, passive: true });
    upBtn && upBtn.addEventListener('click', async () => {
        if (!confirm('要更新 yt-dlp 並重新啟動機器人嗎？\n播放中的音樂會中斷幾秒鐘。')) return;
        upBtn.disabled = true;
        const r = await api('POST', '/api/system/update', { confirm: 'UPDATE' });
        if (!r || r.status !== 'ok') { showToast((r && r.message) || '無法更新', 'error', { key: 'update' }); refresh(); return; }
        showToast('⬆️ 正在更新 yt-dlp…', 'success', { key: 'update', ms: 8000 });
        refresh();
    });
})();

(function restoreBar() {
    const box = document.getElementById('restoreBar');
    if (!box) return;
    box.querySelector('[data-act="resume"]').addEventListener('click', async () => {
        box.hidden = true;
        showToast('▶ 恢復上次的佇列…', 'success', { key: 'restore' });
        const r = await api('POST', '/api/restore', { guild_id: gid(), play: true });
        if (!r || r.status !== 'ok') { showToast((r && r.message) || '恢復失敗', 'error', { key: 'restore' }); box.hidden = false; }
        else showToast(`✅ 已恢復 ${r.count} 首`, 'success', { key: 'restore' });
    });
    box.querySelector('[data-act="queue"]').addEventListener('click', async () => {
        box.hidden = true;
        const r = await api('POST', '/api/restore', { guild_id: gid(), play: false });
        if (!r || r.status !== 'ok') showToast((r && r.message) || '恢復失敗', 'error', { key: 'restore' });
        else showToast(`✅ 已放回佇列 ${r.count} 首`, 'success', { key: 'restore' });
    });
})();


