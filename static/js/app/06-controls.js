// ==================== 事件綁定 ====================
const eqEl = document.getElementById('equalizer');

function updateEqualizer(isPlaying) {
    if (!eqEl) return;
    if (eqEl.classList.contains('active') !== !!isPlaying) eqEl.classList.toggle('active', !!isPlaying);
}

el.btnPlay.addEventListener('click', togglePlay);
el.btnSkip.addEventListener('click', skipSong);
el.btnStop.addEventListener('click', () => { stopPlayback(); });

let _loopShown = null;
// 按下循環後 1.5 秒內不讓 SSE 推來的舊值蓋回去（舊版會閃回原狀，使用者以為沒按到又再按一次）
var _loopHoldUntil = 0;
function updateLoopButton() {
    const mode = state.loopMode;
    if (mode === _loopShown) return;
    _loopShown = mode;
    el.btnLoop.classList.remove('loop-one', 'loop-all');
    const name = mode === 'one' ? '單曲' : mode === 'all' ? '全部' : '關';
    const nextName = mode === 'one' ? '全部' : mode === 'all' ? '關' : '單曲';
    setTip(el.btnLoop, `循環：${name}（點一下切換：${nextName}・L）`, `循環：${name}`);
    setPressed(el.btnLoop, mode === 'one' || mode === 'all');
    el.loopIndicator.textContent = '';

    if (mode === 'one') {
        el.btnLoop.classList.add('loop-one');
        el.loopIndicator.textContent = '1';
    } else if (mode === 'all') {
        el.btnLoop.classList.add('loop-all');
        el.loopIndicator.textContent = '∞';
    }
    scheduleModeSync();
}

el.btnLoop.addEventListener('click', async () => {
    if (!state.guildId) { showToast('請先選擇伺服器', 'error'); return; }
    const modes = ['none', 'one', 'all'];
    const currentIdx = modes.indexOf(state.loopMode);
    const nextMode = modes[(currentIdx + 1) % 3];
    const prevMode = state.loopMode;
    state.loopMode = nextMode;   // 樂觀更新，失敗再退回
    _loopHoldUntil = performance.now() + 1500;
    updateLoopButton();
    const msgs = { none: '➡️ 已關閉循環', one: '🔂 單曲循環', all: '🔁 全部循環' };
    showToast(msgs[nextMode], nextMode === 'none' ? 'info' : 'success', { key: 'loop' });
    const data = await API.loop(nextMode);
    _loopHoldUntil = performance.now() + 400;   // 伺服器已確認；再等一下讓 SSE 帶上新值
    if (!data || data.status !== 'ok') {
        if (state.loopMode === nextMode) { state.loopMode = prevMode; updateLoopButton(); }
        showToast((data && data.message) || '切換循環失敗', 'error', { key: 'loop' });
    }
});

let _shuffleHoldUntil = 0;
el.btnShuffle.addEventListener('click', async () => {
    if (!state.guildId) return;
    const newMode = !state.shuffleMode;
    state.shuffleMode = newMode;   // 樂觀更新，失敗再退回
    _shuffleHoldUntil = performance.now() + 3000;
    setShuffleUI(newMode);
    showToast(newMode ? '\u{1F3B2} 已啟用隨機循環' : '\u{1F3B2} 已關閉隨機循環', 'success', { key: 'shuffle' });
    const r = await API.shuffle(newMode);
    _shuffleHoldUntil = performance.now() + 400;   // 伺服器已確認；再等一下讓 SSE 帶上新值
    if (!r || r.status !== 'ok') {
        state.shuffleMode = !newMode;
        setShuffleUI(!newMode);
        showToast((r && r.message) || '切換隨機失敗', 'error', { key: 'shuffle' });
    }
});

// ==================== 自動推薦接歌 ====================
let autoplayBusy = false;
function updateAutoplayToggle(enabled) {
    if (!el.autoplayToggle || autoplayBusy) return;
    state.autoplay = !!enabled;
    el.autoplayToggle.checked = state.autoplay;
    setToggleUI(el.autoplayToggle, state.autoplay);
}

if (el.autoplayToggle) {
    el.autoplayToggle.addEventListener('change', async (e) => {
        if (!state.guildId) { e.target.checked = state.autoplay; return; }
        const want = e.target.checked;
        autoplayBusy = true;  // 送出期間不讓 SSE 舊狀態把開關彈回去
        setToggleUI(e.target, want);
        const res = await API.autoplay(want);
        autoplayBusy = false;
        if (res && res.status === 'ok') {
            updateAutoplayToggle(res.autoplay);
            showToast(res.autoplay ? '📻 已開啟自動推薦接歌' : '📻 已關閉自動推薦接歌', res.autoplay ? 'success' : 'warning');
        } else {
            updateAutoplayToggle(!want);
            showToast('❌ 設定失敗', 'error');
        }
    });
}

// ==================== 音量平衡 ====================
let normalizeBusy = false;
function updateNormalizeToggle(enabled) {
    if (!el.normalizeToggle || normalizeBusy) return;
    if (el.normalizeToggle.checked !== !!enabled) el.normalizeToggle.checked = !!enabled;
    setToggleUI(el.normalizeToggle, !!enabled);
}
if (el.normalizeToggle) {
    el.normalizeToggle.addEventListener('change', async (e) => {
        if (!state.guildId) { e.target.checked = !e.target.checked; return; }
        const want = e.target.checked;
        normalizeBusy = true;
        setToggleUI(e.target, want);
        const res = await API.normalize(want);
        normalizeBusy = false;
        if (res && res.status === 'ok') {
            updateNormalizeToggle(res.normalize);
            const when = { now: '已從目前位置套用', paused: '繼續播放時生效', live: '直播無法中途切換，下一首生效', next: '下一首開始生效' }[res.applies] || '';
            showToast(`🎚️ 已${res.normalize ? '開啟' : '關閉'}音量平衡${when ? `（${when}）` : ''}`, 'success', { key: 'normalize' });
        } else {
            updateNormalizeToggle(!want);
            showToast('❌ 設定失敗', 'error');
        }
    });
}

// ==================== 播放紀錄 / 排行 ====================
// 只在切到分頁、按重新整理、或換歌時用 REST 抓（不放進每秒的 SSE，避免 payload 變大）
const histState = { mode: 'recent', range: 'all', seq: 0, timer: 0 };
function fmtAgo(ts) {
    const d = Math.max(0, Date.now() / 1000 - ts);
    if (d < 60) return '剛剛';
    if (d < 3600) return `${Math.floor(d / 60)} 分鐘前`;
    if (d < 86400) return `${Math.floor(d / 3600)} 小時前`;
    if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
    const t = new Date(ts * 1000);
    return `${t.getFullYear()}/${t.getMonth() + 1}/${t.getDate()}`;
}
function scheduleHistoryReload(delay = 0) {
    clearTimeout(histState.timer);
    histState.timer = setTimeout(loadHistory, delay);
}
function historyVisible() { return npOpen && npTab === 'history'; }
async function loadHistory() {
    // 播放紀錄只在展開畫面：收合中只標記，開啟並切到該分頁時才向伺服器抓
    const list = HV.np;
    if (!list) return;
    if (!npOpen) { npDirty.history = true; return; }
    npDirty.history = false;
    if (!state.guildId) { list.innerHTML = '<div class="empty-state"><i class="fas fa-clock-rotate-left"></i><p>選擇伺服器以檢視播放紀錄</p></div>'; return; }
    const seq = ++histState.seq;
    const mode = histState.mode;
    if (!list.querySelector('.list-item')) showSkeleton(list, 'row', 7);
    const res = mode === 'top' ? await API.historyTop(histState.range) : await API.history();
    if (seq !== histState.seq) return;   // 期間又切換了模式 / 範圍
    renderHistoryList(res && res.items ? res.items : [], mode, res && res.status !== 'ok', list);
}
function renderHistoryList(items, mode, failed, list = HV.np) {
    cancelChunks(list);
    list.innerHTML = '';
    if (failed) { list.innerHTML = '<div class="empty-state"><i class="fas fa-triangle-exclamation"></i><p>讀取播放紀錄失敗</p></div>'; return; }
    if (!items.length) {
        list.innerHTML = mode === 'top'
            ? '<div class="empty-state"><i class="fas fa-trophy"></i><p>這段期間還沒有播放紀錄</p></div>'
            : '<div class="empty-state"><i class="fas fa-clock-rotate-left"></i><p>還沒有播放紀錄<br>每首歌播放超過 30 秒或一半才會記錄</p></div>';
        return;
    }
    appendChunked(list, items.length, (i) => {
        const it = items[i];
        const div = document.createElement('div');
        div.className = 'list-item history-item' + (mode === 'top' && i < 3 ? ` rank-${i + 1}` : '');
        const idx = document.createElement('span');
        idx.className = 'item-index';
        idx.textContent = String(i + 1);
        const main = document.createElement('div');
        main.className = 'hist-main';
        const title = document.createElement('span');
        title.className = 'item-title';
        title.textContent = it.title || it.url;      // textContent：歌名不會被當成 HTML
        title.title = it.title || '';
        const meta = document.createElement('span');
        meta.className = 'hist-meta';
        if (mode === 'top') {
            meta.textContent = `${it.plays} 次・最近 ${fmtAgo(it.last_played)}`;
        } else {
            const who = it.source === 'autoplay' ? '📻 自動推薦' : (it.requester ? `👤 ${it.requester}` : '');
            meta.textContent = fmtAgo(it.played_at) + (who ? `・${who}` : '');
        }
        main.append(title, meta);
        const actions = document.createElement('div');
        actions.className = 'item-actions';
        const add = document.createElement('button');
        add.className = 'item-btn btn-play';
        add.title = '加入佇列';
        add.innerHTML = '<i class="fas fa-plus"></i>';
        add.addEventListener('click', async () => {
            const r = await API.playUrl(it.url, it.title, null, undefined, it.duration);
            toastAdded(r, it.title);
        });
        actions.appendChild(add);
        div.append(idx, main, actions);
        return div;
    });
}
// 展開畫面的「最近 / 排行」「7 天 / 30 天 / 全部」按鈕
const HIST_MODE_GROUPS = [$('npHistoryMode')].filter(Boolean);
const HIST_RANGE_GROUPS = [$('npHistoryRange')].filter(Boolean);
HIST_MODE_GROUPS.forEach(g => g.addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b || b.dataset.mode === histState.mode) return;
    histState.mode = b.dataset.mode;
    HIST_MODE_GROUPS.forEach(gg => gg.querySelectorAll('.seg-btn').forEach(x => x.classList.toggle('active', x.dataset.mode === histState.mode)));
    HIST_RANGE_GROUPS.forEach(gg => { gg.hidden = histState.mode !== 'top'; });
    loadHistory();
}));
HIST_RANGE_GROUPS.forEach(g => g.addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b || b.dataset.range === histState.range) return;
    histState.range = b.dataset.range;
    HIST_RANGE_GROUPS.forEach(gg => gg.querySelectorAll('.seg-btn').forEach(x => x.classList.toggle('active', x.dataset.range === histState.range)));
    loadHistory();
}));
if ($('npBtnHistoryRefresh')) $('npBtnHistoryRefresh').addEventListener('click', () => loadHistory());

// ==================== 操作紀錄（誰點歌 / 跳過 / 刪歌；有金鑰的人都看得到，保留 7 天） ====================
const actState = { filter: '', seq: 0, timer: 0, items: [], failed: false };
const ACT_LABEL = { add: '點歌', skip: '跳過', remove: '刪歌', shuffle: '隨機' };
const ACT_ICON = { add: 'fa-plus', skip: 'fa-forward', remove: 'fa-trash', shuffle: 'fa-random' };
const ACT_SRC = {
    discord: { label: 'Discord', icon: 'fab fa-discord' },
    web: { label: '網頁面板', icon: 'fas fa-globe' },
    ext: { label: '擴充功能', icon: 'fab fa-chrome' },
};
// 「今天 / 昨天」以台北時間判斷（紀錄的時間也是台北時間）
const _actDayFmt = (() => {
    try { return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit' }); }
    catch (e) { return null; }
})();
function actDayLabel(date) {
    if (!date) return '';
    if (_actDayFmt) {
        const now = Date.now();
        if (date === _actDayFmt.format(now)) return '今天';
        if (date === _actDayFmt.format(now - 86400000)) return '昨天';
    }
    const [, m, d] = date.split('-');
    const wd = '日一二三四五六'[new Date(date + 'T12:00:00+08:00').getUTCDay()];
    return `${+m}/${+d}（${wd}）`;
}
function actionsVisible() { return npOpen && npTab === 'actions'; }
function scheduleActionsReload(delay = 0) {
    clearTimeout(actState.timer);
    actState.timer = setTimeout(loadActions, delay);
}
async function loadActions() {
    const list = $('npActList');
    if (!list) return;
    if (!npOpen) { npDirty.actions = true; return; }
    npDirty.actions = false;
    if (!state.guildId) { list.innerHTML = '<div class="empty-state"><i class="fas fa-list-check"></i><p>選擇伺服器以檢視操作紀錄</p></div>'; return; }
    const seq = ++actState.seq;
    if (!list.querySelector('.list-item')) showSkeleton(list, 'row', 7);
    const res = await API.actions();
    if (seq !== actState.seq) return;
    actState.failed = !res || res.status !== 'ok';
    actState.items = res && Array.isArray(res.items) ? res.items : [];
    renderActions();
}
function renderActions() {
    const list = $('npActList');
    if (!list) return;
    cancelChunks(list);
    list.innerHTML = '';
    const f = actState.filter;
    const items = f ? actState.items.filter(it => it.act === f) : actState.items;
    const cnt = $('npActCount');
    if (cnt) cnt.textContent = items.length ? `${items.length} 筆・保留 7 天` : '保留 7 天';
    if (actState.failed) { list.innerHTML = '<div class="empty-state"><i class="fas fa-triangle-exclamation"></i><p>讀取操作紀錄失敗</p></div>'; return; }
    if (!items.length) {
        list.innerHTML = `<div class="empty-state"><i class="fas fa-list-check"></i><p>${f ? `最近 7 天沒有「${ACT_LABEL[f]}」紀錄` : '最近 7 天還沒有操作紀錄<br>點歌、跳過、刪歌都會記在這裡'}</p></div>`;
        return;
    }
    // 依日期分段：日期標題 + 當天的紀錄（新的在前）
    const rows = [];
    let day = null;
    for (const it of items) {
        if (it.date !== day) { day = it.date; rows.push({ day }); }
        rows.push({ it });
    }
    appendChunked(list, rows.length, (i) => {
        const r = rows[i];
        if (r.day !== undefined) {
            const h = document.createElement('div');
            h.className = 'act-day';
            h.textContent = actDayLabel(r.day);
            return h;
        }
        const it = r.it;
        const div = document.createElement('div');
        div.className = `list-item history-item act-item act-${it.act}`;
        const ico = document.createElement('span');
        ico.className = 'item-index act-ico';
        ico.title = ACT_LABEL[it.act] || '';
        ico.innerHTML = `<i class="fas ${ACT_ICON[it.act] || 'fa-circle'}"></i>`;
        const main = document.createElement('div');
        main.className = 'hist-main';
        const line = document.createElement('div');
        line.className = 'act-line';
        const verb = document.createElement('span');
        verb.className = 'act-verb';
        verb.textContent = ACT_LABEL[it.act] || it.act;
        const title = document.createElement('span');
        title.className = 'item-title';
        title.textContent = it.title || it.url || (it.act === 'shuffle' ? '' : '（未知歌曲）');   // textContent：歌名不會被當成 HTML
        title.title = it.title || '';
        line.append(verb, title);
        if (it.n > 1) {
            const n = document.createElement('span');
            n.className = 'act-count';
            n.textContent = `等 ${it.n} 首`;
            line.appendChild(n);
        }
        const meta = document.createElement('span');
        meta.className = 'hist-meta act-meta';
        const src = ACT_SRC[it.src] || ACT_SRC.web;
        const who = document.createElement('span');
        who.className = 'act-who';
        who.textContent = it.who || '未知';
        const via = document.createElement('span');
        via.className = 'act-src';
        via.innerHTML = `<i class="${src.icon}"></i>`;
        via.append(' ' + src.label);
        meta.append(who, via);
        if (it.detail) {
            const d = document.createElement('span');
            d.className = 'act-detail';
            d.textContent = it.detail;
            meta.appendChild(d);
        }
        const time = document.createElement('span');
        time.className = 'act-time';
        time.textContent = (it.time || '').slice(-8, -3);    // HH:MM（完整時間在滑鼠提示）
        time.title = (it.time || '') + '（台北時間）';
        main.append(line, meta);
        div.append(ico, main, time);
        return div;
    });
}
if ($('npActFilter')) $('npActFilter').addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b || b.dataset.act === actState.filter) return;
    actState.filter = b.dataset.act;
    $('npActFilter').querySelectorAll('.seg-btn').forEach(x => x.classList.toggle('active', x.dataset.act === actState.filter));
    renderActions();
});
if ($('npBtnActRefresh')) $('npBtnActRefresh').addEventListener('click', () => loadActions());

