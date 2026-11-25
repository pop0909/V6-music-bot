// ==================== 睡眠計時器 ====================
const SLEEP_OPTIONS = [
    { label: '30 分鐘', mode: 'minutes', minutes: 30 },
    { label: '60 分鐘', mode: 'minutes', minutes: 60 },
    { label: '播完這首', mode: 'track_end' },
];

// 徽章：剩餘分鐘（例如 30）/ 播完這首 = ♪；提示寫出完整狀態與下一個選項
function updateSleepIndicator() {
    const ind = document.getElementById('sleepIndicator'), btn = $('btnSleep');
    if (!ind || !btn) return;
    const on = !!state.sleepActive;
    const isTrack = on && String(state.sleepMode || '').startsWith('track_end');
    const mins = on && !isTrack ? (state.sleepMin || parseInt(String(state.sleepMode || '').split(':')[1]) || 0) : 0;
    const badge = !on ? '' : isTrack ? '♪' : (mins ? String(mins) : '⏾');
    if (ind.textContent !== badge) ind.textContent = badge;
    btn.classList.toggle('active', on);
    setPressed(btn, on);
    const now = !on ? '關' : isTrack ? '播完這首後停止' : `剩 ${mins} 分鐘`;
    const cur = state.sleepMode || 'off';
    const nxt = { 'off': '30 分鐘', 'minutes:30': '60 分鐘', 'minutes:60': '播完這首', 'track_end:': '關閉' }[cur] || '關閉';
    setTip(btn, `睡眠計時：${now}（點一下切換：${nxt}）`, `睡眠計時：${now}`);
    _sleepShort = !on ? '' : isTrack ? '播完這首' : mins ? `${mins} 分` : '開';
    scheduleModeSync();
}
// SSE：伺服器上的睡眠計時器（剩餘分鐘每分鐘才變一次 → 每分鐘最多多推一次）
function applySleepState(sl) {
    if (!sl) {
        if (!state.sleepActive && !state.sleepMode) return;
        state.sleepActive = false; state.sleepMode = null; state.sleepMin = 0;
    } else {
        state.sleepActive = true;
        if (sl.mode === 'track_end') { state.sleepMode = 'track_end:'; state.sleepMin = 0; }
        else {
            state.sleepMin = sl.minutes || 0;
            if (!state.sleepMode || !state.sleepMode.startsWith('minutes')) state.sleepMode = state.sleepMin > 30 ? 'minutes:60' : 'minutes:30';
        }
    }
    updateSleepIndicator();
}

$('btnSleep').addEventListener('click', async (e) => {
    if (!state.guildId) { showToast('請先選擇伺服器', 'error'); return; }
    // 用簡易循環選單：每次點擊切換到下一個狀態
    const modes = ['off', ...SLEEP_OPTIONS.map(o => o.mode + ':' + (o.minutes || ''))];
    const current = state.sleepMode || 'off';
    const idx = modes.findIndex(m => m === current);
    const next = modes[(idx + 1) % modes.length];
    if (next === 'off') {
        const res = await API.sleepTimer('off');
        if (res.status === 'ok') {
            state.sleepMode = null; state.sleepActive = false; state.sleepMin = 0;
            updateSleepIndicator();
            showToast(res.message || '已取消睡眠計時器', 'warning');
        }
        return;
    }
    const [mode, minStr] = next.split(':');
    const minutes = parseInt(minStr) || 0;
    const res = await API.sleepTimer(mode, minutes);
    if (res.status === 'ok') {
        state.sleepMode = next; state.sleepActive = true; state.sleepMin = minutes;
        updateSleepIndicator();
        showToast(res.message, 'success');
    } else {
        showToast('❌ ' + (res.message || '設定失敗'), 'error');
    }
});

function updateVolumeSlider(value) {
    const pct = value / 2;  /* 0-200 → 0-100% */
    el.volumeSlider.style.backgroundImage = `linear-gradient(to right, var(--accent) 0%, var(--accent-2) ${pct}%, var(--track, rgba(255,255,255,.12)) ${pct}%)`;
}

// 鍵盤調音量 / 靜音：畫面立即更新，停手 250ms 後才送出一次
let _volHoldUntil = 0, _preMuteVolume = 100;
// 音量即時送出（節流 60ms、最後一次一定送）：拖曳 / 按鍵期間聲音就跟著變，不等放開
let _volLastSent = 0, _volTrail = 0, _volPending = null;
function sendVolumeLive(v) {
    _volHoldUntil = performance.now() + 1500;
    _volPending = v;
    const wait = 60 - (performance.now() - _volLastSent);
    if (wait <= 0) { flushVolume(); return; }
    if (!_volTrail) _volTrail = setTimeout(flushVolume, wait);
}
function flushVolume() {
    clearTimeout(_volTrail); _volTrail = 0;
    if (_volPending === null) return;
    const v = _volPending; _volPending = null;
    _volLastSent = performance.now();
    _volHoldUntil = _volLastSent + 1500;
    API.volume(v);
}
function setVolumeQuick(v, label) {
    if (!volKnown) return;
    v = Math.max(0, Math.min(200, Math.round(v)));
    state.volume = v;
    el.volumeSlider.value = v;
    el.volumeValue.textContent = `${v}%`;
    updateVolumeSlider(v);
    sendVolumeLive(v);
    showToast(label || `${v === 0 ? '🔇' : '🔊'} 音量 ${v}%`, 'info', { key: 'volume' });
}
function toggleMute() {
    if (state.volume > 0) { _preMuteVolume = state.volume; setVolumeQuick(0, '🔇 已靜音'); }
    else setVolumeQuick(_preMuteVolume || 100, `🔊 取消靜音（${_preMuteVolume || 100}%）`);
}

el.volumeSlider.addEventListener('input', (e) => {
    if (!volKnown) { e.target.value = state.volume || 0; return; }
    const val = parseInt(e.target.value);
    el.volumeValue.textContent = `${val}%`;
    updateVolumeSlider(val);
    state.volume = val;
    sendVolumeLive(val);
});
el.volumeSlider.addEventListener('change', (e) => {
    if (!volKnown) return;
    const val = parseInt(e.target.value);
    state.volume = val;
    sendVolumeLive(val);
});

function syncQueueClearBtn() {
    const b = $('npBtnQueueClear');
    if (!b) return;
    const n = (state.queue || []).length;
    b.disabled = n === 0;
    b.setAttribute('aria-disabled', n === 0 ? 'true' : 'false');
}
async function clearQueue() {
    if (!state.guildId) return;
    const n = (state.queue || []).length;
    if (!n) return;
    const ok = await askDialog({
        title: '清空佇列',
        message: `清空佇列？將移除 ${n} 首`,
        okText: '清空',
        danger: true,
    });
    if (!ok) return;
    await API.queueClear();
    showToast('\u{1F5D1}️ 已清空佇列', 'warning');
    state.queue = [];
    renderQueueFromCache();
}
if ($('npBtnQueueClear')) $('npBtnQueueClear').addEventListener('click', clearQueue);

el.btnPlaylistPlayAll.addEventListener('click', async () => {
    if (!state.guildId) { showToast('請先選擇伺服器', 'error'); return; }
    const r = await API.playlistPlayAll(false);
    showToast(r && r.full_skipped ? `▶ 正在播放歌單（${r.message}）` : '▶ 正在播放全部歌單', r && r.full_skipped ? 'warning' : 'success');
    setTimeout(refreshAll, 1000);
});

// ==================== 標籤頁 ====================
// 分頁按鈕只影響「自己所在的面板」：寬螢幕下本地歌單與我的歌單分屬兩個面板
function tabIdOf(name) { return 'tab' + name.charAt(0).toUpperCase() + name.slice(1); }
function isTabShown(name) {
    const t = document.getElementById(tabIdOf(name));
    return !!t && t.classList.contains('active');
}
function activateTab(btn, { render = true, instant = false, pill = true } = {}) {
    const panel = btn.closest('.card-panel');
    if (!panel) return;
    // 已是目前顯示中的分頁（例如寬螢幕下再點一次「播放佇列」）：不重建清單，避免整列重繪
    const already = btn.classList.contains('active') && isTabShown(btn.dataset.tab);
    if (panel.id) tabPref[panel.id] = btn.dataset.tab;
    panel.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
    panel.querySelectorAll(':scope > .tab-content').forEach(t => t.classList.remove('active'));
    document.getElementById(tabIdOf(btn.dataset.tab)).classList.add('active');
    if (pill) fxMovePill(btn, instant);
    if (!render || already) return;
    fxListStagger = true;
    if (btn.dataset.tab === 'playlist') renderPlaylist();
    // 寬螢幕一載入就顯示「我的歌單」（右側面板）：這時 11-mylist.js 還沒載入，由它載入後自己畫
    else if (btn.dataset.tab === 'mylists' && typeof ML !== 'undefined') ML.render();
}
document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => activateTab(btn));
});

// ==================== 分頁配置（主畫面寬螢幕 / 窄螢幕） ====================
// 只搬移既有的分頁按鈕與內容（ID、事件綁定都不變），只在視窗寬度跨過 1280px 時發生：
// - 寬螢幕（>= 1280px）：左＝本地歌單，右＝我的歌單（並排）
// - 窄螢幕：兩個分頁在同一個面板
// （播放佇列 / 播放紀錄只在展開的播放畫面，有自己的清單）
const wideMQ = window.matchMedia('(min-width: 1280px)');
const tabPref = { homePanel: 'playlist', sidePanel: 'mylists' };
let _tabLayoutKey = null;
function tabPlan() {
    // （展開畫面有自己的清單，開關時不再搬移；這裡只處理視窗寬度變化）
    if (wideMQ.matches) return { homePanel: ['playlist'], sidePanel: ['mylists'] };
    return { homePanel: ['playlist', 'mylists'], sidePanel: [] };
}
function layoutTabs(force) {
    const plan = tabPlan();
    const key = JSON.stringify(plan);
    if (key === _tabLayoutKey && !force) return;
    _tabLayoutKey = key;
    for (const [panelId, names] of Object.entries(plan)) {
        const panel = $(panelId);
        if (!panel) continue;
        const tabs = panel.querySelector('.tabs');
        names.forEach(n => {
            const b = document.querySelector(`.tab-btn[data-tab="${n}"]`);
            const c = $(tabIdOf(n));
            if (b && b.parentElement !== tabs) tabs.appendChild(b);
            if (c && c.parentElement !== panel) panel.appendChild(c);
        });
        if (panelId === 'sidePanel') panel.hidden = names.length === 0;
    }
    for (const [panelId, names] of Object.entries(plan)) {
        if (!names.length) continue;
        const want = names.includes(tabPref[panelId]) ? tabPref[panelId] : names[0];
        const btn = document.querySelector(`.tab-btn[data-tab="${want}"]`);
        // 膠囊位置延到下一個畫格再量（剛搬移 DOM 後立刻讀 offsetLeft 會強制多一次同步排版）
        if (btn) activateTab(btn, { render: !isTabShown(want) || !btn.classList.contains('active'), instant: true, pill: false });
    }
    requestAnimationFrame(() => document.querySelectorAll('.tabs .tab-btn.active').forEach(a => fxMovePill(a, true)));
    $('homePanel')?.classList.toggle('single-tab', plan.homePanel.length === 1);
    $('sidePanel')?.classList.toggle('single-tab', plan.sidePanel.length === 1);
}
function applyWideLayout() { layoutTabs(); }
wideMQ.addEventListener ? wideMQ.addEventListener('change', applyWideLayout) : wideMQ.addListener(applyWideLayout);
// 初次套用放在檔案最後（特效模組的變數宣告之後）

// ==================== 音訊事件 ====================
audio.addEventListener('ended', async () => {
    showToast('⏭️ 歌曲結束', 'warning');
    setTimeout(refreshAll, 500);
});
audio.addEventListener('error', () => { /* ignore - web doesn't play audio directly */ });
audio.addEventListener('loadedmetadata', () => {
    if (audio.duration && !isNaN(audio.duration)) el.totalTime.textContent = formatTime(audio.duration);
});

// ==================== 工具 ====================
function escapeHtml(text) {
    // 也要跳脫引號：此函式會被用在 HTML 屬性（例如 alt="..."），
    // 只用 textContent→innerHTML 不會跳脫 " 與 '，歌名可注入屬性造成 XSS
    if (!text) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ==================== 初始載入（僅在首次頁面載入時執行一次） ====================
async function refreshAll() {
    // 從 SSE 快取渲染所有分頁（不會發送 API）
    renderQueueFromCache();
    renderPlaylistFromCache();
    if (Date.now() - shelfState.last > 5000) scheduleShelfReload(0);
    scheduleIdle(prebuildNp, { timeout: 3000 });
}

// ==================== 初始化 ====================
async function init() {
    await loadGuilds();

    if (state.guildId) {
        // 首次載入時，SSE 可能需要 1 秒才推送資料
        // 用初始 status API 填補空白
        await pollStatus();
        // SSE 快取渲染所有分頁
        refreshAll();
    }

    if (volKnown) applyKnownVolume(state.volume);
    markUiReady();

    // 所有即時更新由 SSE 每秒推送，不再需要輪詢或 refreshAll
}

document.addEventListener('DOMContentLoaded', init);

// ==================== 低效能模式 ====================
// 毛玻璃（backdrop-filter）與大範圍模糊很吃 GPU。偵測到低階裝置或實測掉幀時，
// 加上 html.low-power：改用半透明純色、停用裝飾動畫（外觀幾乎一樣）。
// 手動切換：網址加 ?lowpower=1 / ?lowpower=0（會記在 localStorage）。
(function setupLowPower() {
    const root = document.documentElement;
    const q = new URLSearchParams(location.search).get('lowpower');
    if (q === '1' || q === '0') localStorage.setItem('mb_lowpower', q);
    const saved = localStorage.getItem('mb_lowpower');
    if (saved === '1') { root.classList.add('low-power'); return; }
    if (saved === '0') return;
    const weak = (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 2) ||
                 (navigator.deviceMemory && navigator.deviceMemory <= 2) ||
                 (navigator.connection && navigator.connection.saveData);
    if (weak) { root.classList.add('low-power'); return; }
    // 實測：載入 2 秒後量 90 個畫格，平均超過 28ms（< ~35fps）就切到低效能模式
    setTimeout(() => {
        if (document.hidden) return;
        let n = 0, start = 0;
        const tick = (t) => {
            if (!start) start = t;
            if (++n < 90) return requestAnimationFrame(tick);
            if ((t - start) / (n - 1) > 28) {
                root.classList.add('low-power');
                console.info('[perf] 偵測到掉幀，已啟用低效能模式（?lowpower=0 可關閉）');
            }
        };
        requestAnimationFrame(tick);
    }, 2000);
})();


