// =====================================================================
// ==================== YouTube Music 式：展開的播放畫面 ====================
// 點底部播放列（按鈕以外的地方）或右側箭頭 → 從下往上滑出（只用 transform）；
// 箭頭 / Esc / 瀏覽器返回（手機返回手勢）/ 手機上往下滑 → 收合。
// =====================================================================
let _npHistoryPushed = false;
let updateHeaderBottom = () => {};
// 極簡動畫（順暢優先）：整個展開畫面是「單一、常駐合成」的圖層，開關只切換一個 class → 只有 transform 的 CSS 轉場。
// 動畫期間：不寫 DOM、不切換會重算大片樣式的 class、不鎖捲動、SSE 先暫存，等 transitionend 之後才一次套用。
let _npAnimating = false, _npPendingLive = null;
let _npEndTimer = 0, _npEndHandler = null, _npScrollY = 0;
const NP_OPEN_MS = 380, NP_CLOSE_MS = 300;   // 與 CSS 的 --np-open-ms / --np-close-ms 一致
const npView = el.npView;
function nextFrame2(fn) { requestAnimationFrame(() => requestAnimationFrame(fn)); }
function npCancelEnd() {
    clearTimeout(_npEndTimer);
    if (_npEndHandler) npView.removeEventListener('transitionend', _npEndHandler);
    _npEndHandler = null;
}
function npWhenDone(ms, cb) {
    npCancelEnd();
    const fin = (e) => {
        if (e && (e.target !== npView || e.propertyName !== 'transform')) return;
        npCancelEnd();
        cb();
    };
    _npEndHandler = fin;
    npView.addEventListener('transitionend', fin);
    _npEndTimer = setTimeout(fin, ms + 150);   // 保險：transitionend 沒觸發時
}
function npAnimEnd() {
    _npAnimating = false;
    const d = _npPendingLive; _npPendingLive = null;
    if (d) applyLiveState(d);
}
function setNpOpen(open, { fromHistory = false } = {}) {
    open = !!open;
    if (open === npOpen || !npView) return;
    if (window.closeModeMenu) window.closeModeMenu(false);
    const root = document.documentElement;
    if (open) { updateHeaderBottom(); _npScrollY = window.scrollY; }   // 只讀（版面是乾淨的）
    else if (window.scrollY !== _npScrollY) window.scrollTo(0, _npScrollY);   // 展開期間背後頁面被捲動過才還原
    npOpen = open;
    const instant = fxOff();
    const fps = window.__npFps;
    _npAnimating = !instant;
    // 以下都是很小的寫入：一個屬性、兩個 class（np-open 只影響手機底部那條細進度條的點擊範圍）、按鈕自己的 class
    npView.setAttribute('aria-hidden', open ? 'false' : 'true');
    root.classList.toggle('np-open', open);
    if (el.btnExpand) {
        el.btnExpand.setAttribute('aria-expanded', open ? 'true' : 'false');
        el.btnExpand.classList.toggle('is-open', open);
        setTip(el.btnExpand, open ? '收合播放畫面 (F / Esc)' : '展開播放畫面 (F)', open ? '收合播放畫面' : '展開播放畫面');
    }
    npView.classList.toggle('open', open);   // ← 唯一會動的東西：面板 transform
    // 收合時焦點還在展開畫面裡（例如按了裡面的「收合」）→ 移回播放列的展開鈕，不要停在看不到的按鈕上
    if (!open && npView.contains(document.activeElement)) {
        const back = [el.btnExpand, el.btnPlay].find(b => b && b.getClientRects().length && getComputedStyle(b).visibility !== 'hidden');
        if (back) back.focus({ preventScroll: true }); else document.activeElement.blur();
    }
    if (fps) fps.start(open ? '展開' : '收合');
    const done = () => {
        if (fps) fps.stop();
        npAnimEnd();
        if (open) {
            if (el.npTime) setText(el.npTime, _npTimeText);
            if (el.npProgressBar) el.npProgressBar.style.setProperty('--p', Math.max(0, _lastProgress).toFixed(4));
            refreshNpTab(npTab);   // 收合期間有變動才補畫（平常閒置時已預先建好）
        } else {
            if (_npHistoryPushed && !fromHistory) { _npHistoryPushed = false; history.back(); }
            scheduleIdle(npAfterClose);
        }
    };
    if (instant) { npCancelEnd(); done(); } else npWhenDone(open ? NP_OPEN_MS : NP_CLOSE_MS, done);
    if (open) {
        if (!fromHistory && history.pushState && !_npHistoryPushed) { history.pushState({ npView: 1 }, ''); _npHistoryPushed = true; }
    } else if (fromHistory) {
        _npHistoryPushed = false;
    }
}
// 收合時展開畫面仍留在 DOM（只是移到螢幕外；為了效能不切 visibility / inert）：
// 用 Tab 會走進去、停在看不到的按鈕上 → 依方向跳到展開畫面前 / 後第一個看得到的控制項
let _tabBack = false;
document.addEventListener('keydown', (e) => { if (e.key === 'Tab') _tabBack = e.shiftKey; }, true);
document.addEventListener('focusin', (e) => {
    if (npOpen || !npView || !npView.contains(e.target)) return;
    const cand = [...document.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea,[tabindex]:not([tabindex="-1"])')]
        .filter(x => !npView.contains(x) && x.tabIndex >= 0 && x.getClientRects().length && !x.closest('[hidden],[inert]'));
    const after = cand.filter(x => npView.compareDocumentPosition(x) & Node.DOCUMENT_POSITION_FOLLOWING);
    const before = cand.filter(x => npView.compareDocumentPosition(x) & Node.DOCUMENT_POSITION_PRECEDING);
    const next = _tabBack ? before[before.length - 1] : after[0];
    if (next) next.focus(); else e.target.blur();
});
// 展開時不鎖捲動（鎖 overflow 會改版面），改成攔下滑過面板的滾輪/觸控，避免後方主畫面被捲動。
// 只在使用者操作時執行，動畫期間不做任何事。內部清單捲得動時照常捲。
function npScrollGuard(e) {
    if (!npOpen) return;
    const dy = e.type === 'wheel' ? e.deltaY : 0;
    for (let t = e.target; t && t !== npView; t = t.parentElement) {
        if (e.type !== 'wheel' && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
        if (t.scrollHeight > t.clientHeight + 1) {
            const oy = getComputedStyle(t).overflowY;
            if (oy === 'auto' || oy === 'scroll') {
                if (e.type !== 'wheel') return;
                if ((dy < 0 && t.scrollTop > 0) || (dy > 0 && t.scrollTop + t.clientHeight < t.scrollHeight - 1)) return;
            }
        }
    }
    if (e.cancelable) e.preventDefault();
}
npView.addEventListener('wheel', npScrollGuard, { passive: false });
npView.addEventListener('touchmove', npScrollGuard, { passive: false });

function npAfterClose() {
    if (npOpen) return;
    if (npDirty.queue) scheduleIdle(prebuildNp);
}
function refreshNpTab(name, quiet = !npView.classList.contains('open')) {
    if (!state.guildId) return;
    fxQuiet = quiet;
    try { refreshNpTabInner(name); } finally { fxQuiet = false; }
}
function refreshNpTabInner(name) {
    if (name === 'queue' && npDirty.queue) { renderQueueList(state.queue || [], QV.np); npDirty.queue = false; }
    else if (name === 'history' && npDirty.history) { loadHistory(); }
    else if (name === 'actions') { loadActions(); }      // 切到操作紀錄一律重抓（換過伺服器也會對）
}
function activateNpTab(name, { instant = false } = {}) {
    const btn = document.querySelector(`#npTabs .tab-btn[data-np="${name}"]`);
    if (!btn) return;
    const changed = npTab !== name;
    npTab = name;
    document.querySelectorAll('#npTabs .tab-btn').forEach(b => b.classList.toggle('active', b === btn));
    [['queue', 'npTabQueue'], ['history', 'npTabHistory'], ['actions', 'npTabActions']]
        .forEach(([n, id]) => $(id).classList.toggle('active', n === name));
    fxMovePill(btn, instant);
    if (changed) fxListStagger = true;
    if (npOpen) refreshNpTab(name);
}
document.querySelectorAll('#npTabs .tab-btn').forEach(b => b.addEventListener('click', () => activateNpTab(b.dataset.np)));
// 收合狀態下的閒置時間先把展開畫面的清單建好一次（content-visibility: hidden，不觸發排版）
function prebuildNp() {
    if (npOpen || !state.guildId) return;
    fxQuiet = true;
    try {
        if (npDirty.queue) { renderQueueList(state.queue || [], QV.np); npDirty.queue = false; }
    } finally { fxQuiet = false; }
}
function openNpView() { setNpOpen(true); }
function closeNpView() { setNpOpen(false); }
window.addEventListener('popstate', () => { if (npOpen) setNpOpen(false, { fromHistory: true }); });
// ==================== 鍵盤快捷鍵 ====================
// Space 播放/暫停、←/→ ±10 秒、Shift+→ 或 N 下一首、↑/↓ 音量 ±5%、M 靜音、F/E 展開/收合、Esc 收合、? 說明
const kbdHelp = $('kbdHelp'), btnShortcuts = $('btnShortcuts');
function setKbdHelp(open) {
    if (!kbdHelp) return;
    if (kbdHelp.hidden === !open) return;
    kbdHelp.hidden = !open;
    if (btnShortcuts) btnShortcuts.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {   // 同一時間只開一個子選單：開快捷鍵就收起狀態
        if (window.closeDropdowns) window.closeDropdowns();
        if (window.setStatusOpen) window.setStatusOpen(false);
        if (window.setFlyPlace) window.setFlyPlace('kbdHelp');   // 齒輪選單旁邊展開（按 ? 時連選單一起打開）
    } else if (window.setFlyDone) window.setFlyDone('kbdHelp');
}
if ($('kbdHelpClose')) $('kbdHelpClose').addEventListener('click', () => setKbdHelp(false));
// 點外面：子選單在齒輪選單裡，點選單本身（例如別的列）不算外面；整個收起由 settingsMenu 處理
document.addEventListener('pointerdown', (e) => {
    const wrap = $('setWrap');
    if (kbdHelp && !kbdHelp.hidden && !(wrap && wrap.contains(e.target))) setKbdHelp(false);
}, { capture: true, passive: true });
// 焦點是滑鼠/觸控點出來的，還是鍵盤移過來的？（keydown 當下 :focus-visible 一律會變成 true，不能用）
let _lastPointerDown = 0;
document.addEventListener('pointerdown', () => { _lastPointerDown = performance.now(); }, { capture: true, passive: true });
document.addEventListener('focusin', (e) => { if (e.target) e.target._kbFocus = performance.now() - _lastPointerDown > 800; }, true);
const KB_NAV_KEYS = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'];
// 回傳 'ignore'（交給瀏覽器 / 正在打字）或 'ok'；滑鼠點過的按鈕/開關（沒有鍵盤焦點框）不攔截快捷鍵
function kbTarget(e) {
    const t = e.target;
    if (!t || t === document.body || t === document.documentElement) return 'ok';
    if (t.isContentEditable) return 'ignore';
    const tag = t.tagName;
    if (tag === 'TEXTAREA' || tag === 'SELECT') return 'ignore';
    const kbFocus = t._kbFocus === true;   // 用 Tab 等鍵盤移過來的焦點（見下方 focusin）
    if (tag === 'INPUT') {
        const ty = (t.type || 'text').toLowerCase();
        if (ty === 'range') return (kbFocus && KB_NAV_KEYS.includes(e.key)) ? 'ignore' : 'ok';
        if (ty === 'checkbox' || ty === 'radio' || ty === 'button' || ty === 'submit') {
            return (kbFocus && (e.key === ' ' || e.key === 'Enter')) ? 'ignore' : 'ok';
        }
        return 'ignore';   // 文字輸入框
    }
    if (e.key === ' ' || e.key === 'Enter') {
        if (tag === 'BUTTON' || tag === 'A' || t.getAttribute('role') === 'button') return kbFocus ? 'ignore' : 'ok';
    }
    return 'ok';
}
document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (document.querySelector('.modal-overlay.active')) return;   // 編輯視窗開著：交給視窗
    if (e.key === 'Escape') {
        if (kbdHelp && !kbdHelp.hidden) { setKbdHelp(false); return; }
        if (npOpen) closeNpView();
        return;
    }
    if (kbTarget(e) === 'ignore') return;
    const k = e.key;
    let handled = true;
    if (k === ' ' || k === 'Spacebar') {
        if (!e.repeat) {
            // 滑鼠點過的按鈕還留著焦點：先移開，避免放開空白鍵時又「點」一次那顆按鈕
            if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
            togglePlay();
        }
    } else if (k === 'ArrowRight' && e.shiftKey) { if (!e.repeat) skipSong(); }
    else if (k === 'ArrowRight') nudgeSeek(10);
    else if (k === 'ArrowLeft' && !e.shiftKey) nudgeSeek(-10);
    else if (k === 'ArrowUp') setVolumeQuick((state.volume || 0) + 5);
    else if (k === 'ArrowDown') setVolumeQuick((state.volume || 0) - 5);
    else if (k === 'n' || k === 'N') { if (!e.repeat) skipSong(); }
    else if (k === 'm' || k === 'M') { if (!e.repeat) toggleMute(); }
    else if (k === 'f' || k === 'F' || k === 'e' || k === 'E') { if (!e.repeat) setNpOpen(!npOpen); }
    else if (k === 'l' || k === 'L') { if (!e.repeat && el.btnLoop) el.btnLoop.click(); }          // 循環：關 → 單曲 → 全部
    else if (k === 's' || k === 'S') { if (!e.repeat && el.btnShuffle) el.btnShuffle.click(); }    // 隨機播放 開 / 關
    else if (k === '?') { if (!e.repeat) setKbdHelp(kbdHelp ? kbdHelp.hidden : false); }
    else handled = false;
    if (handled) e.preventDefault();
});
if (el.btnExpand) el.btnExpand.addEventListener('click', (e) => { e.stopPropagation(); setNpOpen(!npOpen); });
// 手機單手操作：在播放列上往上滑 → 展開播放畫面（往下滑 → 收合）。只認觸控、只在空白處 / 封面 / 歌名起手
if (el.playerBar) {
    let sw = null;
    el.playerBar.addEventListener('pointerdown', (e) => {
        if (e.pointerType !== 'touch' || e.target.closest('button, input, label, .progress-bar, .volume-row, .pb-extras')) { sw = null; return; }
        sw = { x: e.clientX, y: e.clientY, t: performance.now(), id: e.pointerId };
    }, { passive: true });
    const endSwipe = (e) => {
        if (!sw || e.pointerId !== sw.id) return;
        const dx = e.clientX - sw.x, dy = e.clientY - sw.y, dt = performance.now() - sw.t;
        sw = null;
        if (dt > 700 || Math.abs(dy) < 36 || Math.abs(dx) > Math.abs(dy)) return;
        el.playerBar._swiped = performance.now();   // 擋掉接著來的 click（不要又切一次）
        setNpOpen(dy < 0);
    };
    el.playerBar.addEventListener('pointerup', endSwipe, { passive: true });
    el.playerBar.addEventListener('pointercancel', () => { sw = null; }, { passive: true });
}
if (el.btnCollapse) el.btnCollapse.addEventListener('click', closeNpView);
if (el.playerBar) {
    el.playerBar.addEventListener('click', (e) => {
        // 只有點在播放列「空白處 / 封面 / 標題」才展開；按鈕、滑桿、開關照常運作
        if (e.target.closest('button, input, label, select, a, .progress-bar, .volume-row, .pb-extras')) return;
        if (el.playerBar._swiped && performance.now() - el.playerBar._swiped < 400) return;   // 剛剛是滑動手勢
        setNpOpen(!npOpen);
    });
}
// 手機：在封面區往下滑收合
(function npSwipeClose() {
    const stage = $('npStage');
    if (!stage) return;
    let y0 = null, x0 = 0, t0 = 0;
    stage.addEventListener('touchstart', (e) => {
        const inner = el.npView.querySelector('.np-view-inner');
        if (e.touches.length !== 1 || (inner && inner.scrollTop > 0)) { y0 = null; return; }
        y0 = e.touches[0].clientY; x0 = e.touches[0].clientX; t0 = Date.now();
    }, { passive: true });
    stage.addEventListener('touchend', (e) => {
        if (y0 === null) return;
        const t = e.changedTouches[0];
        const dy = t.clientY - y0, dx = Math.abs(t.clientX - x0);
        if (dy > 90 && dy > dx * 1.5 && Date.now() - t0 < 700) closeNpView();
        y0 = null;
    }, { passive: true });
})();

// 窄視窗（< 1180px，含手機）：底部播放列只留播放 / 下一首 / 封面標題 / 展開；
// 其餘控制鍵（音量、循環、隨機、睡眠、自動推薦、音量平衡、停止）搬進展開畫面
const mobileMQ = window.matchMedia('(max-width: 1179px)');
function placeExtras() {
    const extras = $('barExtras'), npExtra = $('npExtra'), right = el.playerBar && el.playerBar.querySelector('.pb-right');
    if (!extras || !npExtra || !right) return;
    if (window.closeModeMenu) window.closeModeMenu(false);   // ⋮ 換位置了：選單先收起
    if (mobileMQ.matches) { if (extras.parentElement !== npExtra) npExtra.appendChild(extras); }
    else if (extras.parentElement !== right) right.insertBefore(extras, el.btnExpand);
}
mobileMQ.addEventListener ? mobileMQ.addEventListener('change', placeExtras) : mobileMQ.addListener(placeExtras);
placeExtras();

// 播放模式選單（⋮）：循環 / 隨機 / 自動推薦 / 音量平衡 / 睡眠。
// 開在 ⋮ 上方（放不下才改下方）；點外面 / Esc / 再按 ⋮ 收起；切換模式時不收（可以連續調好幾個）。
// 鍵盤：Enter / 空白打開並聚焦第一項，↑ ↓ Home End 移動，Tab 離開（收起），Esc 收起回到 ⋮。快捷鍵 L / S 照常可用。
(function modeMenu() {
    const btn = $('btnModes'), menu = $('modeMenu');
    if (!btn || !menu) return;
    const EDGE = 8, GAP = 8;
    const items = () => [el.btnLoop, el.btnShuffle, el.autoplayToggle, el.normalizeToggle, $('btnSleep')].filter(Boolean);
    function place() {
        const r = btn.getBoundingClientRect();
        const vw = document.documentElement.clientWidth, vh = window.innerHeight;
        const mw = menu.offsetWidth, mh = menu.offsetHeight;
        // ⋮ 在畫面右半（播放列）→ 右緣對齊；在左半（窄視窗展開畫面）→ 左緣對齊
        let left = (r.left + r.width / 2 > vw / 2) ? r.right - mw : r.left;
        left = Math.max(EDGE, Math.min(left, vw - EDGE - mw));
        let top = r.top - GAP - mh;
        if (top < EDGE) top = Math.min(r.bottom + GAP, vh - EDGE - mh);
        menu.style.left = Math.round(left) + 'px';
        menu.style.top = Math.round(Math.max(EDGE, top)) + 'px';
    }
    function focusAt(i) {
        const list = items(); if (!list.length) return;
        list[(i + list.length) % list.length].focus({ preventScroll: true });
    }
    function open(viaKey) {
        if (!menu.hidden) return;
        if (window.closeSettingsMenu) window.closeSettingsMenu();
        syncModeMenu();
        menu.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
        btn.classList.remove('tip-show');
        place();
        if (viaKey) focusAt(0);
    }
    // restore：Esc / Tab 收起時焦點回到 ⋮；點外面收起時不搶焦點
    function close(restore) {
        if (menu.hidden) return;
        const inside = menu.contains(document.activeElement);
        menu.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
        if (restore) btn.focus({ preventScroll: true });
        else if (inside) document.activeElement.blur();
    }
    window.closeModeMenu = close;
    window.isModeMenuOpen = () => !menu.hidden;
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (menu.hidden) open(e.detail === 0); else close(false);
    });
    document.addEventListener('pointerdown', (e) => {
        if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) close(false);
    }, { capture: true, passive: true });
    document.addEventListener('keydown', (e) => {
        if (menu.hidden || e.isComposing) return;
        const stop = () => { e.preventDefault(); e.stopImmediatePropagation(); };
        if (e.key === 'Escape') { stop(); close(true); return; }
        const ae = document.activeElement;
        if (!menu.contains(ae)) return;
        const list = items(), i = list.indexOf(ae);
        if (e.key === 'ArrowDown') { stop(); focusAt(i + 1); }
        else if (e.key === 'ArrowUp') { stop(); focusAt(i < 0 ? -1 : i - 1); }
        else if (e.key === 'Home') { stop(); focusAt(0); }
        else if (e.key === 'End') { stop(); focusAt(-1); }
        else if (e.key === 'Tab') {
            // 選單在 body 最後面：Tab 離開時接回播放列的順序（往後 = ⋮ 的下一顆，往前 = ⋮）
            stop(); close(true);
            if (!e.shiftKey) { const nx = $('btnStop'); if (nx && nx.offsetParent !== null) nx.focus({ preventScroll: true }); }
        }
        else if (e.key === 'Enter' && ae.type === 'checkbox') { stop(); ae.click(); }   // 開關：Enter 也能切換（空白鍵原生就會）
    }, true);
    const reposition = () => { if (!menu.hidden) place(); };
    window.addEventListener('resize', reposition, { passive: true });
    document.addEventListener('scroll', reposition, { capture: true, passive: true });
    syncModeMenu();
})();

// 頂欄高度 → CSS 變數（桌面版展開畫面從頂欄下方開始）
(function trackHeaderHeight() {
    const header = document.querySelector('.app-header');
    if (!header) return;
    const set = () => {
        const r = header.getBoundingClientRect();
        document.documentElement.style.setProperty('--header-bottom', `${Math.round(r.bottom)}px`);
        // 頂欄遮罩高度基準 = 頂欄停住時的下緣（sticky top + 高度）；遮罩再往下多 28px 淡出，見 .hdr-mask
        const top = parseFloat(getComputedStyle(header).top) || 0;
        document.documentElement.style.setProperty('--hdr-mask-h', `${Math.ceil(top + r.height)}px`);
    };
    set();
    updateHeaderBottom = set;
    // hdr2：捲動後才淡入頂欄後方的漸層遮罩（只切換遮罩本身的 class，不動 <html>，避免整頁重算樣式）
    const mask = document.querySelector('.hdr-mask');
    if (mask) {
        let on = null, queued = false;
        const upd = () => { queued = false; const v = window.scrollY > 2; if (v !== on) { on = v; mask.classList.toggle('on', v); } };
        window.addEventListener('scroll', () => { if (!queued) { queued = true; requestAnimationFrame(upd); } }, { passive: true });
        upd();
    }
    if ('ResizeObserver' in window) new ResizeObserver(set).observe(header);
    window.addEventListener('resize', set, { passive: true });
    // 頂欄是 sticky：頁面捲動後位置會變（只在展開畫面開著時才需要跟著更新）
    let queued = false;
    window.addEventListener('scroll', () => {
        if (!npOpen || queued) return;
        queued = true;
        requestAnimationFrame(() => { queued = false; set(); });
    }, { passive: true });
})();

