/**
 * 🎵 Music Bot V5 — 前端控制面板 JS
 * Discord 機器人 + 網頁控制面板
 * 支援多伺服器選擇與管理
 */

// ==================== 狀態 ====================

const state = {
    guildId: null,
    guilds: [],
    isPlaying: false,
    isPaused: false,
    currentTitle: '',
    currentUrl: '',
    thumbnail: '',
    queue: [],
    favorites: [],
    playlist: [],
    volume: 100,
    shuffleMode: false,
    loopMode: 'none',
    autoplay: false,
};

// ==================== DOM 參考 ====================
const $ = (id) => document.getElementById(id);
const audio = $('audioPlayer');

const el = {
    guildSelect: $('guildSelect'),
    voiceChannelSelect: $('voiceChannelSelect'),
    statusDot: $('statusDot'),
    statusText: $('statusText'),
    songTitle: $('songTitle'),
    songUrl: $('songUrl'),
    albumArt: $('albumArt'),
    progressFill: $('progressFill'),
    progressBar: $('progressBar'),
    currentTime: $('currentTime'),
    totalTime: $('totalTime'),
    playIcon: $('playIcon'),
    btnPlay: $('btnPlay'),
    btnSkip: $('btnSkip'),
    btnStop: $('btnStop'),
    btnShuffle: $('btnShuffle'),
    btnLoop: $('btnLoop'),
    loopIndicator: $('loopIndicator'),
    volumeSlider: $('volumeSlider'),
    volumeValue: $('volumeValue'),
    searchInput: $('searchInput'),
    btnSearch: $('btnSearch'),
    searchResults: $('searchResults'),
    playlistList: $('playlistList'),
    favList: $('favList'),
    playlistCount: $('playlistCount'),
    favCount: $('favCount'),
    btnPlaylistPlayAll: $('btnPlaylistPlayAll'),
    btnPlaylistAdd: $('btnPlaylistAdd'),
    btnFavPlayAll: $('btnFavPlayAll'),
    btnFavAddCurrent: $('btnFavAddCurrent'),
    autoplayToggle: $('autoplayToggle'),
    normalizeToggle: $('normalizeToggle'),
    // YouTube Music 式版面：底部播放列 / 展開的播放畫面 / 再聽一次
    playerBar: $('playerBar'),
    barArt: $('barArt'),
    barTitle: $('barTitle'),
    barSub: $('barSub'),
    btnBarFav: $('btnBarFav'),
    btnExpand: $('btnExpand'),
    btnCollapse: $('btnCollapse'),
    npView: $('npView'),
    npQueueBadge: $('npQueueBadge'),
    npTime: $('npTime'),
    npProgressBar: $('npProgressBar'),
    shelfAgain: $('shelfAgain'),
    shelfRow: $('shelfRow'),
};

// ==================== 主畫面 / 展開畫面各有一份清單（不搬移 DOM） ====================
// 展開畫面常駐在 DOM 裡；收合時不立即更新它的清單（只標記 dirty），閒置時 / 開啟時才補畫。
// 播放佇列與播放紀錄只存在於展開畫面；最愛在主畫面與展開畫面各一份。
const QV = { np: { list: $('npQueueList'), count: $('npQueueCount') } };
// 最愛歌曲只在主畫面（展開畫面只有播放佇列 / 播放紀錄）
const FV = { home: { list: el.favList, count: el.favCount } };
const HV = { np: $('npHistoryList') };
const npDirty = { queue: true, history: true };
const homeDirty = { favorites: false };
let npOpen = false;
let npTab = 'queue';
let _npTimeText = '0:00 / 0:00';

// ==================== 效能小工具 ====================
// 只在值真的改變時才寫入 DOM：SSE 每秒推送一次，重複寫入相同的 textContent / className
// 也會讓瀏覽器重新計算樣式與排版（layout），累積起來就是卡頓的來源之一。
function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
}
function setClass(node, cls) {
    if (node && node.className !== cls) node.className = cls;
}
// SSE 觸發的重繪合併到下一個畫格做（同一畫格內多次觸發只做一次）
const _frameJobs = new Set();
function scheduleFrame(fn) {
    if (_frameJobs.has(fn)) return;
    _frameJobs.add(fn);
    requestAnimationFrame(() => { _frameJobs.delete(fn); fn(); });
}

// ==================== 分批渲染長清單 ====================
// 先同步畫出可見的前幾列（含入場動畫），其餘在瀏覽器空閒時分批補上，
// 避免一次建立 50 列造成單一長畫格（切分頁 / 佇列變動時的頓一下）。
const LIST_FIRST_CHUNK = 12, LIST_CHUNK = 10;
const scheduleIdle = window.requestIdleCallback
    ? (cb) => requestIdleCallback(cb, { timeout: 150 })
    : (cb) => setTimeout(cb, 32);
function cancelChunks(list) { list._chunkToken = (list._chunkToken || 0) + 1; }
function appendChunked(list, n, make) {
    const token = list._chunkToken;
    let i = 0;
    const run = (count) => {
        const frag = document.createDocumentFragment();
        const end = Math.min(n, i + count);
        for (; i < end; i++) frag.appendChild(make(i));
        list.appendChild(frag);
    };
    run(LIST_FIRST_CHUNK);
    const next = (deadline) => {
        if (list._chunkToken !== token) return;   // 已經重新渲染 → 放棄舊批次
        // 有空閒時間就多做一點，但每批最多 LIST_CHUNK 列
        run(deadline && deadline.timeRemaining && deadline.timeRemaining() > 12 ? LIST_CHUNK * 2 : LIST_CHUNK);
        if (i < n) scheduleIdle(next);
    };
    if (i < n) scheduleIdle(next);
}

// 拖曳點用 translateX 定位，需要知道進度條寬度（只在尺寸改變時更新，不在每秒 tick 讀取 layout）
if (el.progressBar && 'ResizeObserver' in window) {
    const ro = new ResizeObserver(entries => {
        for (const e of entries) e.target.style.setProperty('--bar-w', `${e.contentRect.width}px`);
    });
    ro.observe(el.progressBar);
    if (el.npProgressBar) ro.observe(el.npProgressBar);
}
// 進度條：用 CSS 變數 --p（0~1）驅動 transform: scaleX()，交給 GPU 合成，不觸發 layout
let _lastProgress = -1;
function setProgress(pct, force = false) {
    const p = Math.max(0, Math.min(1, (pct || 0) / 100));
    if (!force) {
        if (Math.abs(p - _lastProgress) < 1e-6) return;
        // 暫停時預測值會比實際多 1 秒 → 極小的倒退直接忽略，避免抖一下
        if (p < _lastProgress && _lastProgress - p < 0.006) return;
    }
    // 往回跳（換歌）或大幅跳躍（拖曳 / 跳過 / 跳轉）時直接到位；一般情況用 1 秒線性滑動，
    // 剛好銜接下一次 SSE，進度條連續滑行而不是每秒跳一格
    const snap = force || p < _lastProgress || p - _lastProgress > 0.05;
    if (el.progressBar.classList.contains('no-anim') !== snap) el.progressBar.classList.toggle('no-anim', snap);
    _lastProgress = p;
    el.progressBar.style.setProperty('--p', p.toFixed(4));
    // 展開畫面的進度條：只在開著時更新（收合時不碰展開畫面的 DOM；開啟完成時補一次）
    if (npOpen && el.npProgressBar) el.npProgressBar.style.setProperty('--p', p.toFixed(4));
}

// ==================== Toast ====================
// opts.key：同一個 key 的提示只保留一則，重複觸發時只換文字（例如連按音量鍵 / 快轉鍵）
const _toastByKey = new Map();
// 訊息自己開頭就有表情符號（✅ ▶ 🔁 ❤️ ⚠️…）→ 不再加類型圖示，避免「✅✅ 已加入」
const _TOAST_EMOJI = /^\s*(?:\p{Extended_Pictographic}|[\u2190-\u21FF\u2300-\u23FF\u25A0-\u27BF])/u;
function showToast(msg, type = 'success', opts = null) {
    const key = opts && opts.key;
    const ms = (opts && opts.ms) || (key ? 1400 : 2500);
    const hideLater = (t) => setTimeout(() => {
        if (key && _toastByKey.get(key) && _toastByKey.get(key).t === t) _toastByKey.delete(key);
        t.style.transition = 'opacity .3s ease, transform .3s cubic-bezier(.5,0,.75,0)';
        t.style.opacity = '0';
        t.style.transform = 'translateY(14px) scale(0.9)';
        setTimeout(() => t.remove(), 300);
    }, ms);
    if (key) {
        const ex = _toastByKey.get(key);
        if (ex && ex.t.isConnected) {
            ex.msg.textContent = msg;
            if (ex.ic) ex.ic.hidden = _TOAST_EMOJI.test(String(msg));
            if (ex.t.className !== `toast ${type}`) ex.t.className = `toast ${type}`;
            clearTimeout(ex.timer);
            ex.timer = hideLater(ex.t);
            return;
        }
    }
    let c = document.querySelector('.toast-container');
    if (!c) { c = document.createElement('div'); c.className = 'toast-container'; document.body.appendChild(c); }
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    const icons = { success: '✅', error: '❌', warning: '⚠️' };
    const ic = document.createElement('span');
    ic.style.fontSize = '14px';
    ic.textContent = icons[type] || 'ℹ️';
    ic.hidden = _TOAST_EMOJI.test(String(msg));
    const m = document.createElement('span');
    m.textContent = msg;                 // 純文字：歌名不會被當成 HTML
    t.append(ic, m);
    t.title = msg;
    c.appendChild(t);
    const timer = hideLater(t);
    if (key) _toastByKey.set(key, { t, msg: m, ic, timer });
}
// 加入佇列的結果提示：排在第幾首 / 即將播放（後端舊版沒有 position 時退回一般訊息）
function toastAdded(r, title) {
    if (r && r.status === 'duplicate') { showToast(`⚠️ ${r.message || '這首歌已在佇列中'}`, 'warning'); return; }
    if (!r || (r.status !== 'added' && r.status !== 'ok')) { showToast((r && r.message) || '加入失敗', 'error'); return; }
    if (r.starts_now) showToast(`▶ 即將播放：${title}`, 'success');
    else if (r.position) showToast(`✅ 已加入，排在第 ${r.position} 首：${title}`, 'success');
    else showToast(`✅ 已加入佇列：${title}`, 'success');
}

// ==================== 控制鍵提示（data-tip 即時提示 + aria-label，有變才寫） ====================
function setTip(node, tip, aria) {
    if (!node) return;
    if (node.dataset.tip !== tip) node.dataset.tip = tip;
    if (aria !== undefined && node.getAttribute('aria-label') !== aria) node.setAttribute('aria-label', aria);
}
function setPressed(node, on) {
    if (node && node.getAttribute('aria-pressed') !== String(!!on)) node.setAttribute('aria-pressed', String(!!on));
}
function setShuffleUI(on) {
    el.btnShuffle.classList.toggle('active', !!on);
    setPressed(el.btnShuffle, on);
    setTip(el.btnShuffle, on ? '隨機播放：開' : '隨機播放：關', on ? '隨機播放：開' : '隨機播放：關');
}
const TOGGLE_TIPS = {
    autoplayToggle: ['自動推薦接歌', '佇列播完自動接相關歌曲'],
    normalizeToggle: ['音量平衡', '讓每首歌音量差不多，下一首生效'],
};
function setToggleUI(input, on) {
    const row = input && input.closest('.toggle-row');
    if (!row) return;
    row.classList.toggle('on', !!on);
    const t = TOGGLE_TIPS[input.id];
    if (t) {
        setTip(row, `${t[0]}：${on ? '開' : '關'}（${t[1]}）`);
        const a = `${t[0]}：${on ? '開' : '關'}`;
        if (input.getAttribute('aria-label') !== a) input.setAttribute('aria-label', a);
    }
}

// ==================== API ====================
// 失敗退避：同一個端點連續失敗（網路錯誤 / 5xx / 429）3 次後，自動請求（GET）改成指數退避
// 1s → 2s → … 最多 30s，不會在伺服器掛掉時一直狂打；使用者按的操作（POST）照常送出。成功一次就恢復。
const apiFail = new Map();   // key → { n, until }
function apiKey(method, path) { return method + ' ' + String(path).split('?')[0]; }
function apiNoteFail(key) {
    const f = apiFail.get(key) || { n: 0, until: 0 };
    f.n++;
    if (f.n >= 3) f.until = Date.now() + Math.min(30000, 1000 * 2 ** (f.n - 3));
    apiFail.set(key, f);
}
// 沒登入 / 金鑰被停用 → 回登入頁（登入後回到原本的頁面）
let _goingLogin = false;
function goLogin() {
    if (_goingLogin) return;
    _goingLogin = true;
    const here = location.pathname + location.search;
    location.href = '/login' + (here && here !== '/' ? '?next=' + encodeURIComponent(here) : '');
}
async function api(method, path, body = null) {
    const key = apiKey(method, path);
    const f = apiFail.get(key);
    if (method === 'GET' && f && f.until > Date.now()) {
        return { status: 'error', message: 'backoff' };
    }
    try {
        const opts = { method, headers: { 'Content-Type': 'application/json' } };
        if (body) opts.body = JSON.stringify(body);
        const res = await fetch(path, opts);
        if (res.status === 401) { goLogin(); return { status: 'error', message: '請先登入', auth: true }; }
        if (res.status >= 500 || res.status === 429) apiNoteFail(key);
        else if (f) apiFail.delete(key);
        return await res.json();
    } catch (err) {
        apiNoteFail(key);
        console.error(`API Error [${method} ${path}]:`, err);
        return { status: 'error', message: err.message };
    }
}

function gid() { return state.guildId || 0; }

const API = {
    guilds: () => api('GET', '/api/guilds'),
    voiceChannels: (g) => api('GET', `/api/guilds/${g || gid()}/voice-channels`),
    joinVoice: (c, g) => api('POST', '/api/join', { guild_id: g || gid(), channel_id: c }),
    status: (g) => api('GET', `/api/status?guild_id=${g || gid()}`),
    play: (g) => api('POST', '/api/play', { guild_id: g || gid() }),
    skip: (g) => api('POST', '/api/skip', { guild_id: g || gid() }),
    stop: (g) => api('POST', '/api/stop', { guild_id: g || gid() }),
    volume: (l, g) => api('POST', '/api/volume', { guild_id: g || gid(), level: l }),
    shuffle: (e, g) => api('POST', '/api/shuffle', { guild_id: g || gid(), enabled: e }),
    loop: (m, g) => api('POST', '/api/loop', { guild_id: g || gid(), mode: m }),
    queueAdd: (u, t, g) => api('POST', '/api/queue/add', { guild_id: g || gid(), url: u, title: t }),
    queueRemove: (i, g) => api('POST', '/api/queue/remove', { guild_id: g || gid(), index: i }),
    queueReorder: (f, t, g, u) => api('POST', '/api/queue/reorder', { guild_id: g || gid(), from: f, to: t, url: u }),
    queuePlay: (i, g) => api('POST', '/api/queue/play', { guild_id: g || gid(), index: i }),
    queueClear: (g) => api('POST', '/api/queue/clear', { guild_id: g || gid() }),
    getPlaylist: () => api('GET', '/api/playlist'),
    playlistAdd: (u, t) => api('POST', '/api/playlist/add', { url: u, title: t }),
    playlistEdit: (i, u, t, ex) => api('POST', '/api/playlist/edit', { index: i, url: u, title: t, expect_url: ex }),
    playlistDelete: (i, ex) => api('POST', '/api/playlist/delete', { index: i, expect_url: ex }),
    playlistPlay: (i, g) => api('POST', '/api/playlist/play', { guild_id: g || gid(), index: i }),
    playlistPlayAll: (s, g) => api('POST', '/api/playlist/play-all', { guild_id: g || gid(), shuffle: s }),
    getFavorites: (g) => api('GET', `/api/favorites?guild_id=${g || gid()}`),
    favAdd: (u, t, g) => api('POST', '/api/favorites/add', { guild_id: g || gid(), url: u, title: t }),
    favRemove: (i, g, ex) => api('POST', '/api/favorites/remove', { guild_id: g || gid(), index: i, expect_url: ex }),
    search: (q) => api('POST', '/api/search', { query: q }),
    importStart: (u, g) => api('POST', '/api/import', { guild_id: g || gid(), url: u }),
    importStatus: (j) => api('GET', `/api/import/${j}`),
    resolveUrl: (u) => api('POST', '/api/resolve-url', { url: u }),
    queueAddBatch: (songs, g) => api('POST', '/api/queue/add-batch', { guild_id: g || gid() || 0, songs: songs }),
    playUrl: (u, t, g, noPlay, dur) => api('POST', '/api/queue/add', { guild_id: g || gid() || 0, url: u, title: t, no_play: noPlay, duration: dur || undefined }),
    seek: (pos, g) => api('POST', '/api/seek', { guild_id: g || gid(), position: pos }),
    autoplay: (e, g) => api('POST', '/api/autoplay', { guild_id: g || gid(), enabled: e }),
    normalize: (e, g) => api('POST', '/api/normalize', { guild_id: g || gid(), enabled: e }),
    history: (g) => api('GET', `/api/history?guild_id=${g || gid()}&limit=60`),
    historyTop: (r, g) => api('GET', `/api/history/top?guild_id=${g || gid()}&range=${encodeURIComponent(r)}&limit=30`),
    sleepTimer: (mode, minutes) => api('POST', '/api/sleep-timer', { guild_id: gid() || 0, mode, minutes }),
};

// ==================== 伺服器選擇 ====================
function updateControlsState() {
    const hasGuild = !!state.guildId;
    const mainContent = document.getElementById('mainContent');
    const overlay = document.getElementById('guildOverlay');
    if (mainContent) mainContent.classList.toggle('no-guild', !hasGuild);
    if (overlay) overlay.classList.toggle('active', !hasGuild);
    // 點歌不受「未選伺服器」限制：後端會自動 fallback 到活躍中的伺服器
    el.searchInput.disabled = false;
    if (el.btnSearch) el.btnSearch.disabled = false;
}

async function loadGuilds() {
    const data = await API.guilds();
    const sel = el.guildSelect;
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">選擇伺服器</option>';

    if (!data.guilds || data.guilds.length === 0) {
        sel.innerHTML += '<option value="" disabled>⚠️ 尚未連線到任何伺服器</option>';
        state.guilds = [];
        updateControlsState();
        return;
    }

    state.guilds = data.guilds;
    data.guilds.forEach(g => {
        const opt = document.createElement('option');
        opt.value = g.id;
        opt.textContent = `${g.active ? '🟢' : '⚪'} ${g.name} (${g.queue_length})`;
        sel.appendChild(opt);
    });

    // 先嘗試取後端 active guild
    const activeResp = await api('GET', '/api/active-guild');
    const activeId = (activeResp && activeResp.active_guild_id) ? String(activeResp.active_guild_id) : null;

    // 再取 localStorage 記住的上次選擇（下次開分頁免重選）
    const savedGuild = localStorage.getItem('mb_guild');
    const savedVoice = localStorage.getItem('mb_voice');

    // 決定要選哪個 guild：目前選單值 > 後端 active > 上次記住的 > 不選
    let targetId = null;
    if (currentVal && data.guilds.find(g => String(g.id) === currentVal)) {
        targetId = currentVal;
    } else if (activeId && data.guilds.find(g => String(g.id) === activeId)) {
        targetId = activeId;
    } else if (savedGuild && data.guilds.find(g => String(g.id) === savedGuild)) {
        targetId = savedGuild;
    }

    if (targetId) {
        sel.value = targetId;
        state.guildId = targetId;
        updateControlsState();
        connectProgressStream(targetId);
        await loadVoiceChannels();
        await refreshAll();
        // 記住的語音頻道仍存在於清單中 → 自動選回（bot 已在該頻道或稍後點歌時才加入）
        if (savedVoice && el.voiceChannelSelect.querySelector(`option[value="${savedVoice}"]`)) {
            el.voiceChannelSelect.value = savedVoice;
        }
    } else {
        // 預設不選取任何伺服器 → 顯示「請先選擇伺服器」遮罩
        state.guildId = null;
        sel.value = '';
        updateControlsState();
    }
}

el.guildSelect.addEventListener('change', async (e) => {
    const val = e.target.value;
    
    // 重要：先關閉舊 SSE，避免關閉前最後一次推送觸發自動切換回閃
    if (sseSource) { sseSource.close(); sseSource = null; }
    
    if (!val) { 
        state.guildId = null; 
        el.voiceChannelSelect.innerHTML = '<option value="">選擇語音頻道</option>';
        updateControlsState();
        // 同步到後端：取消 active guild
        await api('POST', '/api/active-guild', { guild_id: null });
        return; 
    }
    state.guildId = val;
    updateControlsState();
    // 記住選擇：下次開分頁免重選
    localStorage.setItem('mb_guild', val);
    // 同步到後端
    await api('POST', '/api/active-guild', { guild_id: val });
    // 再連線 SSE 和更新 UI
    connectProgressStream(val);
    await loadVoiceChannels();
    await refreshAll();
});

// ==================== 語音頻道選擇 ====================
let voiceChannels = [];

async function loadVoiceChannels() {
    const sel = el.voiceChannelSelect;
    sel.innerHTML = '<option value="">選擇語音頻道</option>';
    sel.disabled = true;

    if (!state.guildId) {
        sel.disabled = false;
        return;
    }

    const data = await API.voiceChannels();
    sel.disabled = false;

    if (data.status === 'error' || !data.channels || data.channels.length === 0) {
        sel.innerHTML += '<option value="" disabled>⚠️ 沒有可用的語音頻道</option>';
        voiceChannels = [];
        return;
    }

    voiceChannels = data.channels;
    data.channels.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = `\u{1F50A} ${c.name} (${c.members})`;
        sel.appendChild(opt);
    });

    // 重建後立即恢復目前已連線的語音頻道（避免閃爍）
    API.status().then(status => {
        if (status && status.voice_channel && status.voice_channel.id) {
            const opt = sel.querySelector(`option[value="${status.voice_channel.id}"]`);
            if (opt) {
                sel.value = status.voice_channel.id;
                opt.textContent = opt.textContent.replace(/^[\u{1F50A}\u{1F7E2}]/u, '\u{1F7E2}');
            }
        }
    });
}

// 重新抓頻道清單並就地更新（人數 / 新增 / 刪除 / 改名），保留目前選取與 🟢 標記
let _vcRefreshing = null, _vcLastRefresh = 0;
function refreshVoiceOptions() {
    if (!state.guildId || _vcRefreshing) return _vcRefreshing;
    const gidNow = state.guildId;
    _vcRefreshing = API.voiceChannels().then(data => {
        _vcLastRefresh = Date.now();
        if (state.guildId !== gidNow || !data || data.status === 'error' || !Array.isArray(data.channels)) return;
        const sel = el.voiceChannelSelect, keep = sel.value;
        const have = new Map(Array.from(sel.options).filter(o => o.value).map(o => [o.value, o]));
        const seen = new Set();
        sel.querySelectorAll('option[disabled]').forEach(o => { if (!o.value && data.channels.length) o.remove(); });
        data.channels.forEach(c => {
            const id = String(c.id); seen.add(id);
            let opt = have.get(id);
            const icon = opt && /^\u{1F7E2}/u.test(opt.textContent) ? '\u{1F7E2}' : '\u{1F50A}';
            const text = `${icon} ${c.name} (${c.members})`;
            if (!opt) { opt = document.createElement('option'); opt.value = id; sel.appendChild(opt); }
            setText(opt, text);
        });
        have.forEach((o, id) => { if (!seen.has(id)) o.remove(); });
        voiceChannels = data.channels;
        if (keep && sel.querySelector(`option[value="${keep}"]`)) sel.value = keep;
    }).finally(() => { _vcRefreshing = null; });
    return _vcRefreshing;
}

function updateVoiceChannelStatus(data) {
    const sel = el.voiceChannelSelect;
    const vc = data.voice_channel;

    // 先重置所有 option 的圖示為 🔊
    // 只改真的不同的 option（每秒都會呼叫）
    const activeId = vc && vc.id ? String(vc.id) : null;
    Array.from(sel.options).forEach(opt => {
        if (opt.value) {
            const icon = opt.value === activeId ? '\u{1F7E2}' : '\u{1F50A}';
            setText(opt, opt.textContent.replace(/^[\u{1F50A}\u{1F7E2}]/u, icon));
        }
    });

    if (activeId && !sel.querySelector(`option[value="${activeId}"]`) && !sel.disabled && Date.now() - _vcLastRefresh > 5000) refreshVoiceOptions();
    if (activeId && sel.value !== activeId && sel.querySelector(`option[value="${activeId}"]`)) {
        sel.value = activeId;
    } else if (!activeId && sel.value && !sel.disabled) {
        // bot 已不在語音（斷線 / 重啟 / 休眠）：選單回到預設，才能再選同一個頻道重新加入
        sel.value = '';
    }
}
el.voiceChannelSelect.addEventListener('change', async (e) => {
    const channelId = e.target.value;
    if (!channelId || !state.guildId) return;
    
    el.voiceChannelSelect.disabled = true;
    showToast('\u{1F50A} 正在加入語音頻道...', 'success');
    
    const data = await API.joinVoice(channelId);
    el.voiceChannelSelect.disabled = false;

    if (data.status === 'joined') {
        // 記住選擇：下次開分頁免重選
        localStorage.setItem('mb_voice', channelId);
        showToast(`✅ 已加入語音頻道：${data.channel_name}`, 'success');
        // 自動開始播放佇列
        setTimeout(() => {
            if (state.guildId) {
                API.play().then(r => setTimeout(refreshAll, 500));
            }
        }, 300);
    } else {
        showToast('❌ 加入失敗：' + (data.message || '未知錯誤'), 'error');
        e.target.value = '';
    }
});

// ==================== 樂觀更新：按鈕即時反應，不等待 API ====================
function togglePlay() {
    if (!state.guildId) { showToast('請先選擇一個伺服器', 'error'); return; }
    
    // 樂觀更新：立即切換 UI
    const wasPlaying = state.isPlaying && !state.isPaused;
    const wasPaused = state.isPaused;
    const prev = { isPlaying: state.isPlaying, isPaused: state.isPaused };
    
    if (wasPlaying) {
        // 假設暫停成功
        state.isPaused = true;
        state.isPlaying = false;
        showToast('⏸ 已暫停', 'success');
    } else if (wasPaused) {
        state.isPaused = false;
        state.isPlaying = true;
        showToast('▶ 已恢復播放', 'success');
    } else {
        state.isPlaying = true;
        showToast('▶ 開始播放', 'success');
    }
    updatePlayButton();
    updateStatusDot(state.isPlaying && !state.isPaused ? 'playing' : state.isPaused ? 'active' : 'inactive');
    updateEqualizer(state.isPlaying && !state.isPaused);
    updateDocTitle();
    
    // 然後非同步發送 API；回應前不讓 SSE 的舊狀態把按鈕蓋回去，失敗就退回原狀
    _playHoldUntil = performance.now() + 3000;
    API.play().then(r => {
        _playHoldUntil = 0;
        if (actionFailed(r)) { Object.assign(state, prev); renderPlayState(); }
        reportActionResult(r);
    });
}
let _playHoldUntil = 0;
function actionFailed(r) { return !r || r.status === 'no_voice' || r.status === 'error'; }
function renderPlayState() {
    updatePlayButton();
    updateStatusDot(state.isPlaying && !state.isPaused ? 'playing' : state.isPaused ? 'active' : 'inactive');
    updateEqualizer(state.isPlaying && !state.isPaused);
    updateDocTitle();
}

function skipSong() {
    if (!state.guildId) return;
    // 樂觀更新
    setProgress(0);
    el.currentTime.textContent = '0:00';
    showToast('⏭️ 已跳過', 'success');
    API.skip().then(reportActionResult);  // SSE 會推送新歌資訊
}

function stopPlayback() {
    if (!state.guildId) return;
    // 樂觀更新
    state.isPlaying = false;
    state.isPaused = false;
    updatePlayButton();
    updateStatusDot('inactive');
    updateEqualizer(false);
    setProgress(0);
    el.currentTime.textContent = '0:00';
    state.queue = [];
    state.currentTitle = '';
    updateDocTitle();
    showToast('⏹️ 已停止', 'warning');
    _playHoldUntil = performance.now() + 3000;
    API.stop().then(r => { _playHoldUntil = 0; reportActionResult(r); });  // SSE 會立刻推送空狀態；失敗 → reportActionResult 馬上重抓狀態
}

// ==================== UI 更新 ====================
function updatePlayButton() {
    const icon = el.playIcon;
    const playing = state.isPlaying && !state.isPaused;
    const cls = playing ? 'fas fa-pause' : 'fas fa-play';
    if (icon.className !== cls) {
        const first = !icon.className.startsWith('fas');
        setClass(icon, cls);
        if (!first) fxMorph(icon);
    }
    fxSetPlayState(!!state.isPlaying, !!state.isPaused);
    const t = playing ? '暫停' : '播放';
    setTip(el.btnPlay, `${t} (Space)`, t);
}

function updateStatusDot(mode) {
    // 真實狀態：播放中 / 已暫停 / 就緒（在語音、閒置）/ 💤 休眠中（休眠或不在語音）/ 離線（SSE 斷線）
    if (mode === 'offline') state.sseOffline = true;
    else if (state.sseOffline) mode = 'offline';
    if (mode !== 'playing' && mode !== 'offline' && (state.asleep || state.voiceConnected === false)) mode = 'sleeping';
    setClass(el.statusDot, 'status-indicator ' + mode);
    const pill = el.statusDot.parentElement;
    if (pill) { pill.classList.toggle('is-sleeping', mode === 'sleeping'); pill.classList.toggle('is-offline', mode === 'offline'); }
    setText(el.statusText, mode === 'playing' ? '播放中' : mode === 'active' ? '已暫停' : mode === 'sleeping' ? '💤 休眠中' : mode === 'offline' ? '離線' : '就緒');
}

// ==================== 閒置休眠：顯示「休眠中」＋恢復上次的佇列 ====================
let _restoreShownTs = 0;
function applyIdleSleep(data) {
    if (!('asleep' in data)) return;
    const was = !!state.asleep;
    state.asleep = !!data.asleep;
    if (was !== state.asleep) {
        updateStatusDot(state.isPlaying ? 'playing' : state.isPaused ? 'active' : 'idle');
        document.documentElement.classList.toggle('bot-asleep', state.asleep);
    }
    const r = data.restore;
    const box = document.getElementById('restoreBar');
    if (!box) return;
    const show = !!(r && r.count && !state.isPlaying);
    if (show) {
        const t = box.querySelector('.rb-text');
        const txt = `${state.asleep ? '💤 休眠中 · ' : ''}上次的佇列還在（${r.count} 首${r.title ? '，從「' + r.title + '」' : ''}）`;
        if (t.textContent !== txt) t.textContent = txt;
    }
    if (box.hidden === show) box.hidden = !show;
}

// ==================== 縮圖：16:9 來源優先 + 裁掉內嵌黑邊 ====================
// 大封面：伺服器給的縮圖（yt-dlp 已挑好實際存在的最佳解析度，通常是 maxresdefault）→ hqdefault（4:3，上下黑邊靠 object-fit: cover 裁掉）→ mqdefault
// 小圖：mqdefault（320x180、16:9、每支影片都有）→ 伺服器縮圖。不盲猜 maxresdefault（常 404，主控台會一直噴錯）
function ytId(url) {
    const m = String(url || '').match(/(?:v=|youtu\.be\/|shorts\/|\/vi(?:_webp)?\/)([A-Za-z0-9_-]{11})/);
    return m ? m[1] : '';
}
function thumbCandidates(url, fallback, big) {
    const id = ytId(url) || ytId(fallback);
    const list = [];
    if (id) {
        const base = `https://i.ytimg.com/vi/${id}/`;
        if (big) { if (fallback) list.push(fallback); list.push(base + 'hqdefault.jpg'); }
        list.push(base + 'mqdefault.jpg');
    }
    if (fallback && !list.includes(fallback)) list.push(fallback);
    return list;
}
// 依序嘗試候選網址（失敗換下一個），全部失敗才呼叫 onFail

function strokePlaceholder(kind) {
    const id = kind === 'phones' ? 'tplStrokePhones' : 'tplStrokeNote';
    const tpl = document.getElementById(id);
    return tpl ? tpl.innerHTML : '';
}

function loadThumb(img, candidates, onFail) {
    let i = 0;
    const next = () => {
        if (i >= candidates.length) { img.onerror = null; if (onFail) onFail(); return; }
        const src = candidates[i++];
        img.onerror = next;
        autoCrop(img, src);
        img.src = src;
    };
    next();
}
// 影片本身內嵌的邊（4:3 / 方形內容放在 16:9 畫面裡的左右色邊、黑邊）：載入後縮成 64x36 量一次，
// 算出要放大多少才能裁掉，寫進 --crop（靜態 transform: scale，結果依網址快取）。只對允許 CORS 的 i.ytimg.com 做
const _cropCache = new Map();
let _cropCanvas = null;
function autoCrop(img, src) {
    img.style.removeProperty('--crop');
    let host = '';
    try { host = new URL(src, location.href).hostname; } catch (_) {}
    if (!/(^|\.)ytimg\.com$/.test(host)) { if (img.crossOrigin) img.removeAttribute('crossorigin'); return; }
    img.crossOrigin = 'anonymous';
    const apply = (s) => { if (s > 1.005) img.style.setProperty('--crop', s.toFixed(3)); };
    if (_cropCache.has(src)) { apply(_cropCache.get(src)); return; }
    img.addEventListener('load', function onLoad() {
        if (img.src !== src) return;
        const s = measureBars(img);
        _cropCache.set(src, s);
        apply(s);
    }, { once: true });
}
function measureBars(img) {
    try {
        const W = 64, H = 36;
        if (!_cropCanvas) { _cropCanvas = document.createElement('canvas'); _cropCanvas.width = W; _cropCanvas.height = H; }
        const ctx = _cropCanvas.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, W, H);
        const d = ctx.getImageData(0, 0, W, H).data;
        const lum = (x, y) => { const k = (y * W + x) * 4; return 0.299 * d[k] + 0.587 * d[k + 1] + 0.114 * d[k + 2]; };
        // 一整欄（列）顏色幾乎一樣、而且跟最外側那欄（列）相近 → 算邊
        const flatLine = (n, get, ref) => {
            let sum = 0, sq = 0;
            for (let i = 2; i < n - 2; i++) { const v = get(i); sum += v; sq += v * v; }
            const m = sum / (n - 4), sd = Math.sqrt(Math.max(0, sq / (n - 4) - m * m));
            return sd < 7 && (ref === null || Math.abs(m - ref) < 12) ? m : -1;
        };
        // 從外往內數「平的」欄（列）；數到上限還是平的 = 整張圖都很單調（例如純色底的字卡），不算邊
        const scan = (max, lineAt) => {
            const ref = flatLine(...lineAt(0), null);
            if (ref < 0) return { n: 0, c: -1 };
            let k = 1;
            while (k < max && flatLine(...lineAt(k), ref) >= 0) k++;
            return k >= max ? { n: 0, c: -1 } : { n: k, c: ref };
        };
        const maxX = Math.floor(W * 0.3), maxY = Math.floor(H * 0.3);
        const colAt = (x) => [H, (y) => lum(x, y)];
        const rowAt = (y) => [W, (x) => lum(x, y)];
        // 兩邊都要有、而且兩邊顏色相近才算（避免把單邊暗色 / 漸層內容當成邊）
        const pair = (a, b) => (a.n && b.n && Math.abs(a.c - b.c) < 12) ? Math.min(a.n, b.n) : 0;
        const bx = pair(scan(maxX, colAt), scan(maxX, (k) => colAt(W - 1 - k)));
        const by = pair(scan(maxY, rowAt), scan(maxY, (k) => rowAt(H - 1 - k)));
        if (!bx && !by) return 1;
        const cw = 1 - 2 * bx / W, ch = 1 - 2 * by / H;
        // object-fit: cover 之後畫面看得到的比例（容器長寬比以元素本身計算）
        const ia = (img.naturalWidth || 16) / (img.naturalHeight || 9);
        const r = img.getBoundingClientRect();
        const ba = r.width && r.height ? r.width / r.height : 16 / 9;
        const vw = Math.min(1, ba / ia), vh = Math.min(1, ia / ba);
        const s = Math.max(1, vw / cw, vh / ch);
        return s > 1 ? Math.min(2, s * 1.02) : 1;
    } catch (_) { return 1; }   // 讀不到像素（CORS）就不裁
}

let _songInfoKey = null;
function updateSongInfo(title, url, thumbnail) {
    // 內容沒變就什麼都不做：舊版每秒重設 innerHTML，會讓跑馬燈每秒跳回開頭、封面與模糊背景重繪
    const key = `${title}\u0000${url}\u0000${thumbnail}`;
    if (key === _songInfoKey) return;
    const prevKey = _songInfoKey;
    _songInfoKey = key;
    if (prevKey !== null && title) fxSongSwap();
    // 跑馬燈：長標題自動觸發
    const titleEl = el.songTitle;
    if (title && title.length > 20) {
        titleEl.className = 'song-title marquee';
        titleEl.innerHTML = `<span class="marquee-inner">${escapeHtml(title)}  •  ${escapeHtml(title)}</span>`;
    } else {
        titleEl.className = 'song-title';
        titleEl.textContent = title || '尚未播放';
    }
    titleEl.title = title || '';
    el.songUrl.textContent = url || '';
    el.songUrl.title = url || '';
    setClass(el.albumArt, `album-art${title ? ' playing' : ''}`);

    // 更新封面縮圖
    // 重點！不重建 DOM，只換 src 屬性，避免打斷瀏覽器載入
    const coverList = thumbCandidates(url, thumbnail, true);
    if (coverList.length) {
        let img = el.albumArt.querySelector('img');
        if (!img) {
            el.albumArt.textContent = '';
            img = document.createElement('img');
            el.albumArt.appendChild(img);
        }
        img.alt = title || '';
        img.style.display = '';
        el.albumArt.querySelectorAll('i, .stroke-ico').forEach(n => n.remove());
        if (img.dataset.for !== coverList[0]) {
            img.dataset.for = coverList[0];
            loadThumb(img, coverList, () => {
                img.style.display = 'none';
                if (!el.albumArt.querySelector('.stroke-ico, i')) {
                    el.albumArt.insertAdjacentHTML('beforeend', strokePlaceholder('phones'));
                }
            });
        }
    } else {
        el.albumArt.innerHTML = strokePlaceholder('phones');
    }
    updateNowPlayingBackdrop(thumbnail);
    updateBarSong(title, thumbnail, url);
}

// 底部播放列的小封面 + 標題（textContent / 屬性設定，不拼 HTML）
let _barArtSrc = null;
function updateBarSong(title, thumbnail, url) {
    if (!el.barTitle) return;
    setText(el.barTitle, title || '尚未播放');
    el.barTitle.title = title || '';
    if (el.playerBar) el.playerBar.classList.toggle('has-song', !!title);
    const list = thumbCandidates(url, thumbnail, false);   // 小圖：mqdefault（16:9）就夠
    const src = list[0] || '';
    if (src === _barArtSrc) return;
    _barArtSrc = src;
    el.barArt.textContent = '';
    if (src) {
        const img = document.createElement('img');
        img.alt = '';
        img.decoding = 'async';
        el.barArt.appendChild(img);
        loadThumb(img, list, () => { el.barArt.innerHTML = strokePlaceholder('note'); });
    } else {
        el.barArt.innerHTML = strokePlaceholder('note');
    }
}

// Now Playing 卡片的模糊封面背景（純視覺）
function updateNowPlayingBackdrop(thumbnail) {
    const card = el.albumArt.closest('.np-card');
    if (!card) return;
    if (thumbnail === _npArtSrc) return;
    _npArtSrc = thumbnail;
    const safe = thumbnail ? String(thumbnail).replace(/["\\\n\r()]/g, encodeURIComponent) : '';
    // 先用原圖 + CSS 即時模糊（舊行為，當作備援）
    card.classList.remove('np-preblur');
    card.style.setProperty('--np-art', safe ? `url("${safe}")` : 'none');
    card.style.setProperty('--np-glow', 'none');
    if (!safe) return;
    // 效能：把封面「預先模糊」成一張小圖（只算一次），取代每幀由 GPU 重算的 filter: blur(50px)
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.decoding = 'async';
    img.onload = () => {
        if (_npArtSrc !== thumbnail) return;
        const data = preblurImage(img);
        if (!data) return;
        card.style.setProperty('--np-art', `url("${data}")`);
        card.classList.add('np-preblur');
        const glow = makeGlowImage(img);
        if (glow) card.style.setProperty('--np-glow', `url("${glow}")`);
    };
    img.src = thumbnail;
}
let _npArtSrc;
// 封面光暈：中間是模糊的封面、四周自然淡出成透明的小圖（只算一次，之後只用 transform/opacity 做呼吸動畫）
function makeGlowImage(img) {
    try {
        const W = 120, H = 80;
        const c = document.createElement('canvas');
        c.width = W; c.height = H;
        const ctx = c.getContext('2d');
        if (!('filter' in ctx)) return null;
        ctx.filter = 'blur(9px) saturate(180%) brightness(1.15)';
        ctx.drawImage(img, 24, 18, W - 48, H - 36);
        return c.toDataURL('image/png');
    } catch (e) {
        return null;
    }
}
function preblurImage(img) {
    try {
        const W = 64, H = Math.max(1, Math.round(W * (img.naturalHeight || 9) / (img.naturalWidth || 16)));
        const c = document.createElement('canvas');
        c.width = W; c.height = H;
        const ctx = c.getContext('2d');
        if ('filter' in ctx) {
            ctx.filter = 'blur(5px) saturate(160%)';
            ctx.drawImage(img, -10, -10, W + 20, H + 20);   // 往外多畫一圈，避免模糊邊緣變透明
        } else {
            // 不支援 ctx.filter（舊版 Safari）：縮到極小再放大，同樣得到柔和模糊
            const t = document.createElement('canvas');
            t.width = 12; t.height = Math.max(1, Math.round(12 * H / W));
            t.getContext('2d').drawImage(img, 0, 0, t.width, t.height);
            ctx.imageSmoothingQuality = 'high';
            ctx.drawImage(t, 0, 0, W, H);
        }
        return c.toDataURL('image/jpeg', 0.85);
    } catch (e) {
        return null;   // 圖片沒有 CORS 標頭 → 保留 CSS 模糊備援
    }
}

function formatTime(seconds) {
    if (!seconds || seconds <= 0) return '0:00';
    const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
             : `${m}:${String(sec).padStart(2, '0')}`;
}

// 瀏覽器分頁標題：▶ 歌名 — MusicBot（暫停 ⏸，閒置只顯示 MusicBot）；只在內容改變時寫入
let _docTitle = null;
function updateDocTitle() {
    const t = state.currentTitle ? `${state.isPaused ? '⏸' : '▶'} ${state.currentTitle} — MusicBot` : 'MusicBot';
    if (t !== _docTitle) { _docTitle = t; document.title = t; }
}

// 佇列標頭：「12 首・48 分鐘」（加總已知長度；有未知長度的歌 → 尾端加 +）
function fmtTotal(sec) {
    const m = Math.max(1, Math.round(sec / 60));
    if (m < 60) return `${m} 分鐘`;
    const h = Math.floor(m / 60), mm = m % 60;
    return mm ? `${h} 小時 ${mm} 分鐘` : `${h} 小時`;
}
function queueSummary(q) {
    const n = (q && q.length) || 0;
    if (!n) return '0 首';
    let sum = 0, unknown = 0;
    for (const s of q) { const d = +s.duration || 0; if (d > 0) sum += d; else unknown++; }
    if (unknown === n) return `${n} 首`;
    return `${n} 首・${fmtTotal(sum)}${unknown ? '+' : ''}`;
}

// ==================== 跳轉（seek） ====================
// 拖曳中只更新預覽（進度條 + 時間提示），放開才送出一次；送出後短暫忽略 SSE 的舊進度，避免進度條跳回去
const seekHold = { pos: 0, url: '', until: 0 };
const seekDrag = { active: false };
function curPos() {
    const playing = state.isPlaying && !state.isPaused;
    const p = (state.posBase || 0) + (playing ? (performance.now() - (state.posAt || 0)) / 1000 : 0);
    return Math.max(0, Math.min(state.duration || 0, p));
}
function previewSeek(pos) {
    const dur = state.duration || 0;
    setProgress(dur ? pos / dur * 100 : 0, true);
    const txt = formatTime(pos);
    setText(el.currentTime, txt);
    _npTimeText = `${txt} / ${formatTime(dur)}`;
    if (npOpen && el.npTime) setText(el.npTime, _npTimeText);
}
function commitSeek(pos) {
    const now = performance.now();
    seekHold.pos = pos; seekHold.url = state.currentUrl; seekHold.until = now + 4000;
    state.posBase = pos; state.posAt = now;
    API.seek(pos).then(r => {
        if (r && r.status === 'ok') {
            if (typeof r.position === 'number') { seekHold.pos = r.position; state.posBase = r.position; }
            return;
        }
        seekHold.until = 0;
        showToast((r && r.message) || '跳轉失敗', 'error', { key: 'seek' });
        resyncSoon(150);
    });
}
function canSeek() { return !!(state.guildId && state.currentTitle && state.duration > 0); }
let _seekTip = null;
function setupSeekBar(bar) {
    if (!bar) return;
    bar.addEventListener('pointerdown', (e) => {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        e.stopPropagation();
        if (!canSeek()) { showToast('目前沒有可跳轉的歌曲', 'warning', { key: 'seek' }); return; }
        e.preventDefault();
        const dur = state.duration;
        const rect = bar.getBoundingClientRect();   // 只在按下時量一次
        try { bar.setPointerCapture(e.pointerId); } catch (_) {}
        if (!_seekTip) { _seekTip = document.createElement('div'); _seekTip.className = 'seek-tip'; document.body.appendChild(_seekTip); }
        const tip = _seekTip;
        seekDrag.active = true;
        bar.classList.add('seeking', 'no-anim');
        tip.hidden = false;
        const calc = (x) => Math.max(0, Math.min(1, (x - rect.left) / (rect.width || 1)));
        let frac = calc(e.clientX), raf = 0;
        const paint = () => {
            raf = 0;
            bar.style.setProperty('--p', frac.toFixed(4));
            tip.textContent = formatTime(frac * dur);
            tip.style.transform = `translate(${Math.round(rect.left + frac * rect.width)}px, ${Math.round(rect.top - 8)}px) translate(-50%, -100%)`;
        };
        const move = (ev) => { frac = calc(ev.clientX); if (!raf) raf = requestAnimationFrame(paint); };
        const finish = (ev, cancel) => {
            bar.removeEventListener('pointermove', move);
            bar.removeEventListener('pointerup', up);
            bar.removeEventListener('pointercancel', cancelFn);
            if (raf) cancelAnimationFrame(raf);
            seekDrag.active = false;
            bar.classList.remove('seeking');
            tip.hidden = true;
            if (cancel) { bar.style.setProperty('--p', Math.max(0, _lastProgress).toFixed(4)); return; }
            frac = calc(ev.clientX);
            const pos = Math.min(frac * dur, Math.max(0, dur - 1));
            previewSeek(pos);
            commitSeek(pos);
        };
        const up = (ev) => finish(ev, false);
        const cancelFn = (ev) => finish(ev, true);
        bar.addEventListener('pointermove', move);
        bar.addEventListener('pointerup', up);
        bar.addEventListener('pointercancel', cancelFn);
        paint();
    });
    bar.addEventListener('click', (e) => e.stopPropagation());   // 不觸發播放列的「點擊展開」
}
setupSeekBar(el.progressBar);
setupSeekBar(el.npProgressBar);
// 鍵盤 ←/→：連按會累加，停手 350ms 後才送出一次
const kbSeek = { target: null, timer: 0 };
function nudgeSeek(delta) {
    if (!canSeek()) { showToast('目前沒有可跳轉的歌曲', 'warning', { key: 'seek' }); return; }
    const dur = state.duration;
    const base = kbSeek.target !== null ? kbSeek.target : curPos();
    const t = Math.max(0, Math.min(base + delta, Math.max(0, dur - 1)));
    kbSeek.target = t;
    seekHold.pos = t; seekHold.url = state.currentUrl; seekHold.until = performance.now() + 4000;
    previewSeek(t);
    showToast(`${delta > 0 ? '⏩' : '⏪'} ${formatTime(t)} / ${formatTime(dur)}`, 'info', { key: 'seek' });
    clearTimeout(kbSeek.timer);
    kbSeek.timer = setTimeout(() => { const v = kbSeek.target; kbSeek.target = null; if (v !== null) commitSeek(v); }, 350);
}

// SSE 與「動作後重新同步」共用：把伺服器狀態套到畫面上
function applyLiveState(data) {
    if (data && 'bitrate' in data) state.bitrate = data.bitrate;
    try { if (data && 'is_connected' in data) { const vc = !!data.is_connected; if (state.voiceConnected !== vc) { state.voiceConnected = vc; updateStatusDot(state.isPlaying && !state.isPaused ? 'playing' : state.isPaused ? 'active' : 'idle'); } } applyIdleSleep(data); } catch (_) {}
    if (!data || data.error) return;
    // 跳轉剛送出（或正在拖曳）：伺服器還沒反映新位置前，不讓舊進度把進度條拉回去
    if (seekHold.until && (performance.now() > seekHold.until || (data.current_url || '') !== seekHold.url ||
                           (kbSeek.target === null && Math.abs((data.elapsed || 0) - seekHold.pos) <= 2.5))) seekHold.until = 0;
    const holdProgress = seekDrag.active || seekHold.until > 0;
    state.duration = data.duration || 0;
    if (!holdProgress) {
        state.posBase = data.elapsed || 0;
        state.posAt = performance.now();
        // === 進度條 ===
        if (data.duration > 0) {
            // 播放中：目標設在「1 秒後」的位置，CSS 用 1 秒線性過渡滑過去，正好接上下一筆 SSE
            const ahead = data.is_playing && !data.is_paused ? 1 : 0;
            setProgress(Math.min(((data.elapsed + ahead) / data.duration) * 100, 100));
        } else {
            setProgress(0);
        }
        // === 時間 ===
        setText(el.currentTime, data.elapsed_str || '0:00');
        _npTimeText = `${data.elapsed_str || '0:00'} / ${data.duration_str || '0:00'}`;
        if (npOpen && el.npTime) setText(el.npTime, _npTimeText);   // 收合時不碰展開畫面的 DOM
    }
    setText(el.totalTime, data.duration_str || '0:00');
    if (el.barSub && data.queue_length !== undefined) {
        const n = data.queue_length;
        setText(el.barSub, data.current_title
            ? (n ? `接下來還有 ${n} 首・點擊展開` : '佇列已播完・點擊展開')
            : (n ? `佇列 ${n} 首・按播放開始` : '佇列是空的，搜尋或從歌單加入歌曲'));
    }
    
    // === 播放狀態 ===（剛按下播放 / 暫停 / 停止、伺服器還沒回應：保留畫面上的樂觀狀態）
    const holdPlay = performance.now() < _playHoldUntil;
    if (!holdPlay) {
        state.isPlaying = data.is_playing;
        state.isPaused = data.is_paused;
    }
    
    // === 標題/縮圖（SSE 每次推送都嘗試更新） ===
    // 瀏覽器會快取相同 src 的圖片，不會重複下載
    if (data.current_title) {
        state.currentTitle = data.current_title;
        state.currentUrl = data.current_url || '';
        if (data.thumbnail) state.thumbnail = data.thumbnail;
        updateSongInfo(state.currentTitle, state.currentUrl, state.thumbnail);
    } else if (!data.is_playing && !data.is_paused && data.queue_length !== undefined) {
        // 伺服器上沒有任何歌在播：清掉殘留的舊標題（舊版只在有標題時更新，停止後會一直顯示上一首）
        state.currentTitle = '';
        state.currentUrl = '';
        state.thumbnail = '';
        updateSongInfo('', '', '');
    }
    
 if (holdPlay) { updateDocTitle(); } else {
    updateStatusDot(data.is_playing && !data.is_paused ? 'playing' : data.is_paused ? 'active' : 'inactive');
    updatePlayButton();
    updateEqualizer(data.is_playing && !data.is_paused);
    updateDocTitle();
    }
    
    // === 完整佇列快取（來自 SSE，renderQueue 直接使用） ===
    if (data.queue !== undefined) {
        // 內容有變才重繪：播放中 elapsed 每秒都變，但佇列本身沒變，
        // 若每秒重建 DOM 會讓入場動畫不停重播（看起來像一直在刷新）。
        // 長度（duration）不算在內：背景解析到某首的長度時只更新標頭的總長度，不重建清單
        const queueKey = JSON.stringify(data.queue.map(s => [s.url, s.title]));
        setText(QV.np.count, queueSummary(data.queue));
        if (queueKey !== state._queueKey) {
            state._queueKey = queueKey;
            state.queue = data.queue;
            if (npOpen) {
                scheduleFrame(renderQueueFromCache);
            } else {
                npDirty.queue = true;
                scheduleIdle(prebuildNp);   // 收合中：閒置時才補建展開畫面的佇列
            }
        } else {
            state.queue = data.queue;
        }
    }
    
    // === 個人歌單版本號（來自 SSE）：有人改了歌單（擴充功能 / Discord / 其他分頁）→ 重抓自己的 ===
    if (data.ul_rev !== undefined) ML.onRev(data.ul_rev);

    // === 最愛快取（來自 SSE） ===
    if (data.favorites !== undefined) {
        // 內容有變才重繪，避免播放中每秒重建 DOM 導致動畫重播
        const favKey = JSON.stringify(data.favorites);
        if (favKey !== state._favKey) {
            state._favKey = favKey;
            state.favorites = data.favorites;
            if (!npOpen && isTabShown('favorites')) {
                scheduleFrame(renderFavoritesFromCache);
            } else {
                homeDirty.favorites = true;   // 被展開畫面蓋住 / 分頁沒顯示：之後再補畫
            }
        } else {
            state.favorites = data.favorites;
        }
    }
    
    // === 本地歌單快取（來自 SSE） ===
    if (data.playlist !== undefined) {
        const plKey = JSON.stringify(data.playlist);
        if (plKey !== state._plKey) {
            state._plKey = plKey;
            state.playlist = data.playlist;
            if (isTabShown('playlist')) {
                scheduleFrame(renderPlaylistFromCache);
            }
        } else {
            state.playlist = data.playlist;
        }
    }
    
    // === 佇列長度（即時更新徽章；重繪由上方佇列內容比對負責） ===
    if (data.queue_length !== undefined) {
        const badge = `佇列 ${data.queue_length}`;
        if (el.npQueueBadge && el.npQueueBadge.textContent !== badge) {
            el.npQueueBadge.textContent = badge;
            updateGuildSelect();
        }
    }
    
    // === 隨機模式 ===
    if (data.shuffle_mode !== undefined) {
        state.shuffleMode = data.shuffle_mode;
        setShuffleUI(data.shuffle_mode);
    }
    
    // === 自動推薦接歌 ===
    if (data.normalize !== undefined) updateNormalizeToggle(data.normalize);
    // 播放問題提示（例如 YouTube 機器人驗證）：同一則只跳一次
    if (data.notice && data.notice.ts && data.notice.ts !== state._noticeTs) {
        state._noticeTs = data.notice.ts;
        showToast(`⚠️ ${data.notice.message}`, 'error');
    }
    // 換歌時若正在看播放紀錄，稍後重新抓（上一首這時通常已記錄）
    if (data.current_title !== undefined && data.current_title !== state._histTitle) {
        state._histTitle = data.current_title;
        npDirty.history = true;
        if (historyVisible()) scheduleHistoryReload(1500);
        // 「再聽一次」也順便更新（上一首這時通常已記錄；最多 30 秒一次）
        if (Date.now() - shelfState.last > 30000) scheduleShelfReload(2500);
    }
    if (data.autoplay !== undefined) {
        updateAutoplayToggle(data.autoplay);
    }

    // === 睡眠計時器 ===
    if (data.sleep !== undefined) applySleepState(data.sleep);

    // === 循環模式 ===
    if (data.loop_mode !== undefined && performance.now() > _loopHoldUntil) {
        state.loopMode = data.loop_mode;
        updateLoopButton();
    }
    
    // === 音量 ===（鍵盤調整後短時間內不讓 SSE 舊值蓋回去）
    if (data.volume !== undefined && data.volume !== state.volume && performance.now() > _volHoldUntil) {
        state.volume = data.volume;
        el.volumeSlider.value = data.volume;
        el.volumeValue.textContent = `${data.volume}%`;
        updateVolumeSlider(data.volume);
    }
    
    // === 語音頻道狀態 ===
    if (data.voice_channel) {
        updateVoiceChannelStatus(data);
    }
    
    // === 偵測 active guild 切換 ===
    if (data.active_guild_id && String(data.active_guild_id) !== state.guildId) {
        handleGuildAutoSwitch(data.active_guild_id, data.guild_name);
    }
}

// 樂觀更新後向伺服器確認一次：SSE 只在狀態「有變」時才推送，
// 若操作失敗（例如 bot 不在語音）伺服器狀態沒變，畫面會一直停在樂觀假設的「播放中」
let _resyncTimer = 0;
function resyncSoon(delay = 1200) {
    clearTimeout(_resyncTimer);
    _resyncTimer = setTimeout(async () => {
        if (!state.guildId) return;
        const data = await API.status();
        if (data && !data.error && data.queue !== undefined) applyLiveState(data);
    }, delay);
}
function reportActionResult(r) {
    if (actionFailed(r)) {
        showToast((r && r.message) || '操作失敗（bot 可能不在語音頻道）', 'error');
        resyncSoon(150);   // 失敗：馬上抓回伺服器真正的狀態（退回樂觀更新）
        return;
    }
    resyncSoon();
}

// ==================== SSE 即時進度串流 ====================
let sseSource = null;
let sseFails = 0, sseRetryTimer = 0;

function connectProgressStream(guildId) {
    if (sseSource) {
        sseSource.close();
        sseSource = null;
    }
    clearTimeout(sseRetryTimer);
    if (!guildId) return;
    
    sseSource = new EventSource(`/api/progress-stream?guild_id=${guildId}`);
    
    sseSource.onmessage = (e) => {
        try {
            sseFails = 0;
            if (state.sseOffline) { state.sseOffline = false; updateStatusDot(state.isPlaying && !state.isPaused ? 'playing' : state.isPaused ? 'active' : 'idle'); }
            const data = JSON.parse(e.data);
            // 展開 / 收合動畫進行中（約 0.4 秒）：先合併暫存，動畫結束再一次套用，不在動畫中改 DOM
            if (_npAnimating) { _npPendingLive = Object.assign(_npPendingLive || {}, data); return; }
            applyLiveState(data);
        } catch (err) {
            console.error('SSE parse error:', err);
        }
    };
    
    // 伺服器通知登入已失效（金鑰被停用 / 刪除 / 重設）→ 回登入頁
    sseSource.addEventListener('auth', () => { if (sseSource) sseSource.close(); sseSource = null; goLogin(); });

    sseSource.onerror = () => {
        // 前 2 次交給 EventSource 內建自動重連；連續失敗就自己關掉、指數退避（2s → 4s → … 最多 30s）再連，
        // 伺服器掛掉時不會每 3 秒狂打。guild 切換時仍會主動重新 connectProgressStream
        sseFails++;
        if (sseFails >= 2) updateStatusDot('offline');
        if (sseFails === 2) fetch('/api/me', { cache: 'no-store' }).then(r => { if (r.status === 401) goLogin(); }).catch(() => {});
        if (sseFails < 3) { console.warn('SSE 連線中斷，等待自動重連...'); return; }
        const src = sseSource;
        if (src) src.close();
        if (sseSource === src) sseSource = null;
        clearTimeout(sseRetryTimer);
        const wait = Math.min(30000, 2000 * 2 ** (sseFails - 3));
        console.warn(`SSE 連續中斷，${Math.round(wait / 1000)} 秒後重連`);
        sseRetryTimer = setTimeout(() => { if (!sseSource && state.guildId == guildId) connectProgressStream(guildId); }, wait);
    };
}

function updateGuildSelect() {
    const sel = el.guildSelect;
    state.guilds.forEach(g => {
        const opt = sel.querySelector(`option[value="${g.id}"]`);
        if (opt) {
            // 如果這個 guild 就是目前選中的，用 SSE 的 queue_length
            const qLen = g.id === state.guildId 
                ? ((el.npQueueBadge && el.npQueueBadge.textContent.match(/\d+/)) || [0])[0]
                : g.queue_length;
            setText(opt, `${g.active ? '🟢' : '⚪'} ${g.name} (${qLen})`);
        }
    });
}

// ==================== Guild 自動切換 ====================
async function handleGuildAutoSwitch(newGuildId, guildName) {
    const newId = String(newGuildId);
    const sel = el.guildSelect;
    if (!sel.querySelector(`option[value="${newId}"]`)) return;
    
    sel.value = newId;
    state.guildId = newId;
    updateControlsState();
    connectProgressStream(newId);
    await loadVoiceChannels();
    await refreshAll();
    showToast(`🔄 已自動跟隨至 ${guildName || '伺服器'}`, 'success');
}

// ==================== 一次性初始載入（不再輪詢） ====================
async function pollStatus() {
    if (!state.guildId) return;
    const data = await API.status();

    if (data.error) {
        if (data.error === 'no_guilds') {
            el.songTitle.textContent = '⚠️ Bot 尚未連線到任何伺服器';
            updateStatusDot('inactive');
        }
        return;
    }

    // 進度/標題/縮圖由 SSE 即時更新，這裡只做初始載入
    if (!state.currentTitle && data.current_title) {
        state.currentTitle = data.current_title;
        state.currentUrl = data.current_url || '';
        state.thumbnail = data.thumbnail || '';
        updateSongInfo(data.current_title, data.current_url, data.thumbnail);
    }

    state.shuffleMode = data.shuffle_mode || false;
    setShuffleUI(state.shuffleMode);

    if (data.loop_mode !== undefined && performance.now() > _loopHoldUntil) {
        state.loopMode = data.loop_mode;
        updateLoopButton();
    }

    state.volume = data.volume || 100;
    el.volumeSlider.value = data.volume || 100;
    el.volumeValue.textContent = `${data.volume || 100}%`;
    updateVolumeSlider(data.volume || 100);

    state.queue = data.queue || [];
    if (!state.duration) { state.duration = data.duration || 0; state.posBase = data.elapsed || 0; state.posAt = performance.now(); }
    if (el.npQueueBadge) el.npQueueBadge.textContent = `佇列 ${data.queue_length || 0}`;
    updateGuildSelect();

    // 語音頻道狀態
    updateVoiceChannelStatus(data);
}

// ==================== 佇列 UI（含拖曳排序） ====================
// 佇列只在展開畫面：開著 → 立即重畫；收合中 → 標記待更新，閒置時補建（開啟時就不必現畫）
function renderQueueFromCache() {
    if (!state.guildId) { QV.np.list.innerHTML = '<div class="empty-state"><i class="fas fa-list"></i><p>選擇伺服器以檢視佇列</p></div>'; return; }
    if (npOpen) {
        renderQueueList(state.queue || [], QV.np); npDirty.queue = false;
    } else {
        npDirty.queue = true; scheduleIdle(prebuildNp);
    }
}

// ==================== 佇列拖曳排序（Pointer Events：滑鼠 / 觸控 / 觸控筆通用） ====================
// 只拖「把手」：按住把手 → 該列跟著手指走，其他列用 transform 讓位（只動 transform，不觸發 layout）；
// 放開後才真正搬 DOM、送 API；失敗就重畫。
let _qDrag = null;
function bindQueueDrag(list, div) {
    const handle = div.querySelector('.drag-handle');
    if (!handle) return;
    handle.addEventListener('pointerdown', (e) => {
        if (_qDrag || (e.pointerType === 'mouse' && e.button !== 0)) return;
        e.preventDefault();
        const items = [...list.querySelectorAll('.list-item')];
        const from = items.indexOf(div);
        if (from < 0) return;
        const rects = items.map(it => it.getBoundingClientRect());
        const step = rects.length > 1 ? (rects[1].top - rects[0].top) : rects[0].height;
        _qDrag = { list, div, items, rects, from, to: from, startY: e.clientY, step, id: e.pointerId,
                   scrollTop: list.scrollTop, raf: 0, y: e.clientY };
        try { handle.setPointerCapture(e.pointerId); } catch (_) {}
        div.classList.add('q-lift');
        list.classList.add('q-dragging');
        items.forEach(it => { if (it !== div) it.classList.add('q-shift'); });
    });
    const move = (e) => {
        const d = _qDrag;
        if (!d || d.div !== div || e.pointerId !== d.id) return;
        d.y = e.clientY;
        if (!d.raf) d.raf = requestAnimationFrame(() => { d.raf = 0; dragFrame(d); });
    };
    const end = (e) => {
        const d = _qDrag;
        if (!d || d.div !== div || e.pointerId !== d.id) return;
        if (d.raf) cancelAnimationFrame(d.raf);
        _qDrag = null;
        finishQueueDrag(d, e.type === 'pointercancel');
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
}
function dragFrame(d) {
    // 靠近清單上下緣時自動捲動
    const lr = d.list.getBoundingClientRect();
    if (d.y < lr.top + 36) d.list.scrollTop -= 10;
    else if (d.y > lr.bottom - 36) d.list.scrollTop += 10;
    const scrollDy = d.list.scrollTop - d.scrollTop;
    const dy = d.y - d.startY + scrollDy;
    d.div.style.transform = `translate3d(0, ${dy}px, 0)`;
    const centerY = d.rects[d.from].top + d.rects[d.from].height / 2 + dy;
    let to = d.from;
    for (let i = 0; i < d.rects.length; i++) {
        const mid = d.rects[i].top + d.rects[i].height / 2;
        if (i < d.from && centerY < mid) { to = i; break; }
        if (i > d.from && centerY > mid) to = i;
    }
    if (to === d.to) return;
    d.to = to;
    d.items.forEach((it, i) => {
        if (it === d.div) return;
        let off = 0;
        if (d.from < to && i > d.from && i <= to) off = -d.step;
        else if (d.from > to && i >= to && i < d.from) off = d.step;
        it.style.transform = off ? `translate3d(0, ${off}px, 0)` : '';
    });
}
async function finishQueueDrag(d, cancelled) {
    const { list, div, items, from } = d;
    const to = cancelled ? from : d.to;
    items.forEach(it => { it.classList.remove('q-shift', 'q-lift'); it.style.transform = ''; });
    list.classList.remove('q-dragging');
    if (to === from) { if (_qRenderPending) { _qRenderPending = false; renderQueueFromCache(); } return; }
    const song = (state.queue || [])[from];
    // 搬 DOM（transform 已清掉，畫面直接落在新位置）＋重新編號
    if (to > from) list.insertBefore(div, items[to].nextSibling); else list.insertBefore(div, items[to]);
    list.querySelectorAll('.list-item').forEach((it, idx) => {
        it.dataset.index = idx;
        const n = it.querySelector('.item-index-num'); if (n) n.textContent = idx + 1;
    });
    div.classList.add('fx-dropped');
    setTimeout(() => div.classList.remove('fx-dropped'), 450);
    if (state.queue && song) { state.queue.splice(from, 1); state.queue.splice(to, 0, song); }
    const r = await API.queueReorder(from, to, undefined, song && song.url);
    if (!r || r.status !== 'ok') {
        showToast((r && r.message) || '排序失敗', 'error', { key: 'reorder' });
        if (state.queue && song) { state.queue.splice(to, 1); state.queue.splice(from, 0, song); }
        renderQueueFromCache();
    } else if (_qRenderPending) { _qRenderPending = false; renderQueueFromCache(); }
}

let _qRenderPending = false;
function renderQueueList(q, view = QV.np) {
    const list = view.list;
    if (_qDrag && _qDrag.list === list) { _qRenderPending = true; return; }   // 拖曳中不重畫，放開後補
    cancelChunks(list);
    list.innerHTML = '';

    if (q.length === 0) {
        list.innerHTML = '<div class="empty-state"><i class="fas fa-list"></i><p>佇列為空</p></div>';
        view.count.textContent = '0 首';
        return;
    }

    view.count.textContent = queueSummary(q);

    // （舊版在這裡讀 offsetHeight 強制 reflow 來重播動畫；新列本來就會播入場動畫，已移除）


    appendChunked(list, q.length, (i) => {
        const song = q[i];
        const div = document.createElement('div');
        div.className = 'list-item';
        div.dataset.index = i;
        div.innerHTML = `
            <span class="drag-handle"><i class="fas fa-grip-vertical"></i></span>
            <span class="item-index-num">${i + 1}</span>
            <span class="item-title">${escapeHtml(song.title)}${song.by ? `<span class="item-by">👤 ${escapeHtml(song.by)}</span>` : ''}</span>
            <div class="item-actions">
                <button class="item-btn btn-play" data-action="play"><i class="fas fa-play"></i></button>
                <button class="item-btn btn-remove" data-action="remove"><i class="fas fa-times"></i></button>
            </div>`;
        div.querySelector('.item-title').title = song.title || '';   // 完整歌名（屬性用 property 設定，不拼 HTML）

        bindQueueDrag(list, div);

        div.querySelector('[data-action="play"]').addEventListener('click', () => {
            // 樂觀更新：先提示、進度歸零，伺服器在背景切歌（SSE 會立刻推送新歌）
            showToast(`▶ 播放：${song.title}`, 'success');
            setProgress(0);
            setText(el.currentTime, '0:00');
            API.queuePlay(i).then(reportActionResult);
        });
        div.querySelector('[data-action="remove"]').addEventListener('click', async () => {
            await API.queueRemove(i);
            renderQueueFromCache();
        });
        return div;
    });
}

// ==================== 本地歌單 UI ====================
async function renderPlaylist() {
    if (!state.guildId) { el.playlistList.innerHTML = '<div class="empty-state"><i class="fas fa-book"></i><p>選擇伺服器以檢視歌單</p></div>'; return; }
    renderPlaylistFromCache();
}

// ==================== 編輯 Modal ====================
let editingIndex = -1, editingUrl = '', modalMode = 'edit';

function openEditModal(index, url, title) {
    modalMode = 'edit'; editingIndex = index; editingUrl = url || '';
    $('modalTitle').textContent = '編輯歌曲';
    $('editUrl').value = url || '';
    $('editTitle').value = title || '';
    $('editModal').classList.add('active');
    $('editUrl').focus();
}

$('modalClose').addEventListener('click', () => $('editModal').classList.remove('active'));
$('modalCancel').addEventListener('click', () => $('editModal').classList.remove('active'));
$('editModal').addEventListener('click', (e) => { if (e.target === e.currentTarget) $('editModal').classList.remove('active'); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('editModal').classList.contains('active')) $('editModal').classList.remove('active'); });

$('modalConfirm').addEventListener('click', async () => {
    const url = $('editUrl').value.trim(), title = $('editTitle').value.trim();
    if (!url) { showToast('請輸入網址', 'error'); return; }
    if (!title) { showToast('請輸入標題', 'error'); return; }
    if (modalMode === 'edit' && editingIndex >= 0) {
        const r = await API.playlistEdit(editingIndex, url, title, editingUrl);
        if (r && r.status === 'error') { showToast(r.message || '更新失敗', 'error'); return; }
        showToast('✅ 已更新', 'success');
    } else {
        const r = await API.playlistAdd(url, title);
        if (r && r.status === 'error') { showToast(r.message || '新增失敗', 'error'); return; }
        showToast(`✅ 已新增：${title}`, 'success');
    }
    $('editModal').classList.remove('active');
    renderPlaylistFromCache();
});

$('btnPlaylistAdd').addEventListener('click', () => {
    modalMode = 'add'; $('modalTitle').textContent = '新增到本地歌單';
    $('editUrl').value = ''; $('editTitle').value = '';
    $('editModal').classList.add('active'); $('editUrl').focus();
});

$('editUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('editTitle').focus(); });
$('editTitle').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('modalConfirm').click(); });

function renderFavList(favs, view = FV.home) {
    const list = view.list;
    cancelChunks(list);
    list.innerHTML = '';
    if (!favs || favs.length === 0) {
        list.innerHTML = '<div class="empty-state"><i class="fas fa-heart"></i><p>最愛清單為空</p></div>';
        view.count.textContent = '0 首';
        return;
    }
    view.count.textContent = `${favs.length} 首`;
    appendChunked(list, favs.length, (i) => {
        const song = favs[i];
        const div = document.createElement('div');
        div.className = 'list-item';
        div.innerHTML = `
            <span class="item-index"><i class="fas fa-heart" style="color:var(--danger);font-size:11px;"></i></span>
            <span class="item-title">${escapeHtml(song.title)}${song.by ? `<span class="item-by">👤 ${escapeHtml(song.by)}</span>` : ''}</span>
            <div class="item-actions">
                <button class="item-btn btn-play" data-action="play"><i class="fas fa-play"></i></button>
                <button class="item-btn btn-remove" data-action="remove"><i class="fas fa-times"></i></button>
            </div>`;
        div.querySelector('.item-title').title = song.title || '';
        div.querySelector('[data-action="play"]').addEventListener('click', async () => {
            const r = await API.playUrl(song.url, song.title, null, true);
            toastAdded(r, song.title);
        });
        div.querySelector('[data-action="remove"]').addEventListener('click', async (ev) => {
            const b = ev.currentTarget; if (b.disabled) return; b.disabled = true;   // 連點不會刪到下一首
            const r = await API.favRemove(i, null, song.url);
            if (r && r.status === 'error') showToast(r.message || '移除失敗', 'error');
            else showToast('\u{1F494} 已移除', 'warning');
            await renderFavorites();
        });
        return div;
    });
}

function renderFavoritesFromCache() {
    if (!state.guildId) { el.favList.innerHTML = '<div class="empty-state"><i class="fas fa-heart"></i><p>選擇伺服器以檢視最愛</p></div>'; return; }
    if (npOpen) { homeDirty.favorites = true; return; }   // 主畫面被蓋住：收合後再補畫
    renderFavList(state.favorites || [], FV.home); homeDirty.favorites = false;
}

// ==================== 最愛歌曲 UI ====================
async function renderFavorites() {
    if (!state.guildId) { el.favList.innerHTML = '<div class="empty-state"><i class="fas fa-heart"></i><p>選擇伺服器以檢視最愛</p></div>'; return; }
    renderFavoritesFromCache();
}


function renderPlaylistFromCache() {
    if (!state.guildId) { el.playlistList.innerHTML = '<div class="empty-state"><i class="fas fa-book"></i><p>選擇伺服器以檢視歌單</p></div>'; return; }
    const pl = state.playlist || [];
    renderPlaylistList(pl);
}

function renderPlaylistList(pl) {
    const list = el.playlistList;
    cancelChunks(list);
    list.innerHTML = '';
    if (!pl || pl.length === 0) {
        list.innerHTML = '<div class="empty-state"><i class="fas fa-book"></i><p>本地歌單為空</p></div>';
        el.playlistCount.textContent = '0 首';
        return;
    }
    el.playlistCount.textContent = `${pl.length} 首`;
    appendChunked(list, pl.length, (i) => {
        const song = pl[i];
        const div = document.createElement('div');
        div.className = 'list-item';
        div.innerHTML = `
            <span class="item-index">${i + 1}</span>
            <span class="item-title editable-hint">${escapeHtml(song.title)}</span>
            <div class="item-actions">
                <button class="item-btn btn-play" data-action="play"><i class="fas fa-play"></i></button>
                <button class="item-btn btn-edit" data-action="edit"><i class="fas fa-pen"></i></button>
                <button class="item-btn btn-remove" data-action="delete"><i class="fas fa-trash-alt"></i></button>
            </div>`;
        div.querySelector('.item-title').title = song.title || '';
        div.querySelector('[data-action="play"]').addEventListener('click', async () => {
            const r = await API.playlistPlay(i);
            toastAdded(r, song.title);
        });
        div.querySelector('[data-action="edit"]').addEventListener('click', () => openEditModal(i, song.url, song.title));
        div.querySelector('[data-action="delete"]').addEventListener('click', async (ev) => {
            const b = ev.currentTarget; if (b.disabled) return; b.disabled = true;   // 連點不會刪到下一首
            const r = await API.playlistDelete(i, song.url);
            if (r && r.status === 'error') showToast(r.message || '刪除失敗', 'error');
            else showToast(`\u{1F5D1}️ 已刪除：${song.title}`, 'warning');
            renderPlaylistFromCache();
        });
        return div;
    });
}

// ==================== 搜尋 ====================
el.btnSearch.addEventListener('click', doSearch);
el.searchInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

let _importing = false;
async function importPlaylist(url) {
    if (_importing) { showToast('上一個歌單還在匯入中…', 'warning', { key: 'import' }); return; }
    _importing = true;
    el.searchInput.value = '';
    const spotify = /open\.spotify\.com/.test(url);
    showToast(spotify ? '🔎 讀取 Spotify 歌單…' : '📥 讀取播放清單…', 'success', { key: 'import', ms: 60000 });
    try {
        const r = await API.importStart(url);
        if (!r || !r.job) { showToast((r && r.message) || '匯入失敗', 'error', { key: 'import' }); return; }
        for (let i = 0; i < 600; i++) {
            await new Promise(res => setTimeout(res, 800));
            const j = await API.importStatus(r.job);
            if (!j || j.status === 'error') { showToast('❌ ' + ((j && j.message) || '匯入失敗'), 'error', { key: 'import', ms: 4000 }); return; }
            if (j.status === 'done') {
                const extra = (j.skipped ? `（${j.skipped} 首找不到）` : '') + (j.full_skipped ? `（佇列已滿，另有 ${j.full_skipped} 首沒加入）` : '');
                showToast(`✅ 已從「${j.name}」加入 ${j.added} 首${extra}`, 'success', { key: 'import', ms: 3500 });
                return;
            }
            if (j.total) showToast(`🔎 搜尋中 ${j.done}/${j.total}…`, 'success', { key: 'import', ms: 60000 });
        }
        showToast('匯入逾時', 'error', { key: 'import' });
    } finally { _importing = false; }
}

async function doSearch() {
    const query = el.searchInput.value.trim();
    if (!query) return;
    closeNpView();   // 搜尋結果顯示在主畫面

    // === 匯入歌單：YouTube 播放清單 / Spotify 歌單或專輯（背景解析，toast 顯示進度） ===
    if (/^https?:\/\//.test(query) && (/open\.spotify\.com\//.test(query) || /[?&]list=|\/playlist/.test(query))) {
        importPlaylist(query);
        return;
    }

    // === 處理網址（支援播放清單） ===
    if (query.match(/^https?:\/\//)) {
        el.searchResults.innerHTML = '<div style="padding:12px;text-align:center;color:var(--sub)"><i class="fas fa-spinner fa-pulse"></i> 正在解析網址...</div>';
        el.btnSearch.disabled = true;

        const data = await API.resolveUrl(query);
        el.btnSearch.disabled = false;
        el.searchResults.innerHTML = '';

        if (data.status === 'error') {
            showToast('❌ 解析失敗：' + (data.message || '未知錯誤'), 'error');
            return;
        }

        if (!data.songs || data.songs.length === 0) {
            showToast('❌ 找不到歌曲', 'error');
            return;
        }

        // 將所有歌曲加入佇列（不自動播放）；guild 未選時後端自動 fallback
        const batchResult = await API.queueAddBatch(data.songs);
        if (!batchResult || batchResult.status !== 'ok') {
            showToast(batchResult && batchResult.message ? `❌ ${batchResult.message}` : '❌ 加入佇列失敗', 'error');
            return;
        }

        if (data.is_playlist) {
            const full = batchResult.full_skipped ? `（${batchResult.message}）` : '';
            showToast(`✅ 已將播放清單 ${data.playlist_title} 的 ${batchResult.count} 首歌加入佇列！${full}`, full ? 'warning' : 'success');
        } else {
            toastAdded(batchResult, data.songs[0].title);
        }

        el.searchInput.value = '';
        await refreshAll();
        return;
    }

    // === 關鍵字搜尋 ===
    el.searchResults.innerHTML = '<div style="padding:12px;text-align:center;color:var(--sub)"><i class="fas fa-spinner fa-pulse"></i> 搜尋中...</div>';
    el.btnSearch.disabled = true;
    const data = await API.search(query);
    el.btnSearch.disabled = false;
    el.searchResults.innerHTML = '';

    if (!data.results || data.results.length === 0) {
        el.searchResults.innerHTML = '<div style="padding:12px;text-align:center;color:var(--sub)">找不到結果</div>';
        return;
    }

    data.results.forEach(r => {
        const div = document.createElement('div');
        div.className = 'search-result-item';
        div.innerHTML = `
            <img src="${escapeHtml(r.thumbnail)}" alt="" loading="lazy" onerror="this.style.display='none'">
            <span class="result-title">${escapeHtml(r.title)}</span>
            <span class="result-duration">${r.duration ? formatTime(r.duration) : ''}</span>
            <span class="result-add"><i class="fas fa-plus-circle"></i></span>`;
        div.querySelector('.result-title').title = r.title || '';
        div.addEventListener('click', async () => {
            const res = await API.playUrl(r.url, r.title, null, true, r.duration);  // no_play=true；guild 未選時後端自動 fallback
            toastAdded(res, r.title);
            el.searchInput.value = '';
            el.searchResults.innerHTML = '';
            setTimeout(refreshAll, 500);
        });
        el.searchResults.appendChild(div);
    });
}

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
    setTip(el.btnLoop, `循環：${name}（點一下切換：${nextName}）`, `循環：${name}`);
    setPressed(el.btnLoop, mode === 'one' || mode === 'all');
    el.loopIndicator.textContent = '';

    if (mode === 'one') {
        el.btnLoop.classList.add('loop-one');
        el.loopIndicator.textContent = '1';
    } else if (mode === 'all') {
        el.btnLoop.classList.add('loop-all');
        el.loopIndicator.textContent = '∞';
    }
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

el.btnShuffle.addEventListener('click', async () => {
    if (!state.guildId) return;
    const newMode = !state.shuffleMode;
    state.shuffleMode = newMode;   // 樂觀更新，失敗再退回
    setShuffleUI(newMode);
    showToast(newMode ? '\u{1F3B2} 已啟用隨機循環' : '\u{1F3B2} 已關閉隨機循環', 'success', { key: 'shuffle' });
    const r = await API.shuffle(newMode);
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
            showToast(res.normalize ? '🎚️ 已開啟音量平衡（下一首生效）' : '🎚️ 已關閉音量平衡（下一首生效）', 'success');
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
    el.volumeSlider.style.background = `linear-gradient(to right, var(--accent) 0%, var(--accent-2) ${pct}%, var(--track, rgba(255,255,255,.12)) ${pct}%)`;
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
    const val = parseInt(e.target.value);
    el.volumeValue.textContent = `${val}%`;
    updateVolumeSlider(val);
    state.volume = val;
    sendVolumeLive(val);
});
el.volumeSlider.addEventListener('change', (e) => {
    const val = parseInt(e.target.value);
    state.volume = val;
    sendVolumeLive(val);
});

async function clearQueue() {
    if (!state.guildId) return;
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

async function playAllFavorites() {
    if (!state.guildId) { showToast('請先選擇伺服器', 'error'); return; }
    const data = await API.getFavorites();
    if (!data.favorites || data.favorites.length === 0) {
        showToast('最愛清單為空', 'error');
        return;
    }
    const shuffled = [...data.favorites].sort(() => Math.random() - 0.5);
    await API.queueClear();
    let added = 0, fullMsg = '';
    for (const s of shuffled) {
        const r = await API.queueAdd(s.url, s.title);
        if (r && r.code === 'queue_full') { fullMsg = r.message; break; }
        if (r && r.status === 'added') added++;
    }
    if (fullMsg) showToast(`▶ 已加入 ${added} 首最愛（${fullMsg}）`, 'warning');
    else showToast(`▶ 正在播放 ${shuffled.length} 首最愛`, 'success');
}
el.btnFavPlayAll.addEventListener('click', playAllFavorites);

async function addCurrentToFavorites() {
    if (!state.guildId || !state.currentUrl) { showToast('目前沒有播放的歌曲', 'error'); return; }
    const data = await API.favAdd(state.currentUrl, state.currentTitle);
    showToast(data.status === 'exists' ? '❤️ 已存在於最愛' : '❤️ 已加入最愛', 'success');
    await renderFavorites();
}
el.btnFavAddCurrent.addEventListener('click', addCurrentToFavorites);
if (el.btnBarFav) el.btnBarFav.addEventListener('click', addCurrentToFavorites);

// ==================== 標籤頁 ====================
// 分頁按鈕只影響「自己所在的面板」：寬螢幕三欄模式下，播放佇列與歌單/最愛分屬兩個面板
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
    else if (btn.dataset.tab === 'favorites') renderFavorites();
    else if (btn.dataset.tab === 'mylists') ML.render();
}
document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => activateTab(btn));
});

// ==================== 分頁配置（主畫面寬螢幕 / 窄螢幕） ====================
// 只搬移既有的分頁按鈕與內容（ID、事件綁定都不變），只在視窗寬度跨過 1280px 時發生：
// - 寬螢幕（>= 1280px）：左＝本地歌單，右＝最愛歌曲（並排）
// - 窄螢幕：兩個分頁在同一個面板
// （播放佇列 / 播放紀錄只在展開的播放畫面，有自己的清單）
const wideMQ = window.matchMedia('(min-width: 1280px)');
const tabPref = { homePanel: 'playlist', sidePanel: 'favorites' };
let _tabLayoutKey = null;
function tabPlan() {
    // （展開畫面有自己的清單，開關時不再搬移；這裡只處理視窗寬度變化）
    if (wideMQ.matches) return { homePanel: ['playlist'], sidePanel: ['favorites', 'mylists'] };
    return { homePanel: ['playlist', 'favorites', 'mylists'], sidePanel: [] };
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
    renderFavoritesFromCache();
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

    el.volumeSlider.value = state.volume;
    el.volumeValue.textContent = `${state.volume}%`;
    updateVolumeSlider(state.volume);

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


// =====================================================================
// ==================== 動畫特效（只用 transform / opacity） ====================
// 原則：所有動畫只改 transform 與 opacity（交給 GPU 合成，不觸發 layout / paint）；
// 不做即時 blur；裝飾性特效在低效能模式 / prefers-reduced-motion 下自動關閉。
// =====================================================================
const fxReduced = window.matchMedia('(prefers-reduced-motion: reduce)');
function fxOff() { return fxReduced.matches; }
function fxDecorOff() { return fxReduced.matches || document.documentElement.classList.contains('low-power'); }
// 統一緩動曲線：柔和減速（expo-out）與輕微回彈的彈簧感
const FX_SPRING = 'cubic-bezier(.34,1.32,.64,1)';
const FX_EASE = 'cubic-bezier(.16,1,.3,1)';
const FX_MAX_ANIM = 14;   // 一次最多同時動畫的清單項目數

// ---- 播放 / 暫停圖示切換：旋轉 + 縮放 ----
function fxMorph(icon) {
    if (fxOff() || !icon.animate) return;
    icon.animate([
        { transform: 'rotate(-90deg) scale(.3)', opacity: 0 },
        { transform: 'rotate(8deg) scale(1.12)', opacity: 1, offset: .7 },
        { transform: 'none', opacity: 1 }
    ], { duration: 460, easing: FX_EASE });
}

// ---- 播放狀態 → 卡片 class（暫停時封面淡暗縮小、光暈與流光停止） ----
let _fxPlayKey = '';
function fxSetPlayState(isPlaying, isPaused) {
    const card = el.albumArt.closest('.np-card');
    const k = `${isPlaying}|${isPaused}`;
    if (!card || k === _fxPlayKey) return;
    _fxPlayKey = k;
    card.classList.toggle('is-playing', isPlaying && !isPaused);
    card.classList.toggle('is-paused', isPlaying && isPaused);
    if (el.playerBar) el.playerBar.classList.toggle('is-playing', isPlaying && !isPaused);
}

// ---- 換歌轉場：封面與標題淡入 + 輕微縮放 / 上滑 ----
// 換歌轉場已移除（展開畫面完全靜態，順暢優先）
function fxSongSwap() {}

// ---- 清單 FLIP：位置變動的項目平滑滑動、新項目淡入、被移除的項目淡出 ----
let fxQuiet = false;       // true：清單更新不讀版面、不做動畫
let fxListStagger = true;   // 首次載入與切換分頁時：逐項錯開滑入
function fxItemKey(item, seen) {
    const t = (item.querySelector('.item-title') || item).textContent;
    const n = (seen[t] = (seen[t] || 0) + 1);
    return t + '\u0000' + n;
}
function fxSnapshot(list) {
    const map = new Map(), seen = {};
    const box = list.getBoundingClientRect();
    list.querySelectorAll(':scope > .list-item').forEach(item => {
        const r = item.getBoundingClientRect();
        // 只追蹤可見範圍內的項目（長清單不必全部計算）
        const visible = r.bottom > box.top - 60 && r.top < box.bottom + 60 && r.height > 0;
        map.set(fxItemKey(item, seen), visible ? { r, item } : null);
    });
    return map;
}
function fxFlip(list, mutate) {
    if (fxOff() || !list || !document.body.animate) { mutate(); return; }
    const before = fxSnapshot(list);
    mutate();
    const seen = {}, moves = [];
    // 先全部讀（只觸發一次排版），再全部寫
    list.querySelectorAll(':scope > .list-item').forEach(item => {
        const old = before.get(fxItemKey(item, seen));
        if (!old || moves.length >= FX_MAX_ANIM) return;
        const dy = old.r.top - item.getBoundingClientRect().top;
        if (Math.abs(dy) > 1 && Math.abs(dy) < 1500) moves.push([item, dy]);
    });
    moves.forEach(([item, dy]) => item.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }],
                                               { duration: 420, easing: FX_EASE }));
}
function fxWrapList(fn, getList) {
    return function (...args) {
        const list = getList(...args);
        if (fxQuiet) {   // 開啟展開畫面 / 預先建立時：不做 FLIP（避免讀版面）也不播入場動畫
            const r = fn.apply(this, args);
            if (list) list.classList.add('fx-update');
            return r;
        }
        const stagger = fxListStagger || !list || !list.querySelector('.list-item');
        if (stagger || fxOff()) {
            fxListStagger = false;
            if (list) list.classList.remove('fx-update');
            const r = fn.apply(this, args);
            if (list) list.querySelectorAll(':scope > .list-item').forEach((it, i) => { if (i < 12) it.style.setProperty('--i', i); });
            return r;
        }
        // 資料更新：保留舊項目位置做 FLIP；只有新項目播入場動畫
        const before = fxSnapshot(list);
        list.classList.add('fx-update');
        const r = fn.apply(this, args);
        const seen = {}, kept = new Set(), moves = [], enters = [];
        const rendered = list.querySelectorAll(':scope > .list-item');
        // 讀取階段（一次排版）
        rendered.forEach(item => {
            const key = fxItemKey(item, seen);
            if (!before.has(key)) { if (enters.length < FX_MAX_ANIM) enters.push(item); return; }
            kept.add(key);
            const old = before.get(key);
            if (!old || moves.length >= FX_MAX_ANIM) return;
            const dy = old.r.top - item.getBoundingClientRect().top;
            if (Math.abs(dy) > 1 && Math.abs(dy) < 1500) moves.push([item, dy]);
        });
        // 寫入階段
        enters.forEach(item => item.classList.add('fx-enter'));
        moves.forEach(([item, dy]) => item.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }],
                                                   { duration: 440, easing: FX_EASE }));
        // 被移除的項目：用複製的「殘影」原地縮小淡出（最多 4 個）。
        // 分批渲染時後段尚未建立的項目不算被移除。
        const renderedCount = rendered.length;
        let ghosts = 0, idx = 0;
        before.forEach((v, key) => {
            const oldIdx = idx++;
            if (oldIdx >= renderedCount) return;
            if (kept.has(key) || !v || ghosts >= 4) return;
            ghosts++;
            const g = v.item.cloneNode(true);
            g.classList.add('fx-ghost');
            g.style.cssText += `;position:fixed;left:${v.r.left}px;top:${v.r.top}px;width:${v.r.width}px;height:${v.r.height}px;margin:0;pointer-events:none;z-index:50`;
            document.body.appendChild(g);
            const a = g.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateX(28px) scale(.96)' }],
                                { duration: 320, easing: 'cubic-bezier(.4,0,.7,.2)', fill: 'forwards' });
            a.onfinish = () => g.remove();
        });
        return r;
    };
}
renderQueueList = fxWrapList(renderQueueList, (q, v) => (v || QV.np).list);
renderFavList = fxWrapList(renderFavList, (f, v) => (v || FV.home).list);
renderPlaylistList = fxWrapList(renderPlaylistList, () => el.playlistList);

// ---- 分頁滑動膠囊 ----
let fxTabPos = new WeakMap();
function fxMovePill(btn, instant) {
    const tabs = btn && btn.parentElement;
    if (!tabs) return;
    // 每個分頁群組各有一顆膠囊（寬螢幕三欄時有兩組）
    let pill = tabs._pill;
    if (!pill) {
        pill = tabs._pill = document.createElement('span');
        pill.className = 'tab-pill';
        pill.setAttribute('aria-hidden', 'true');
        tabs.prepend(pill);
        tabs.classList.add('has-pill');
        instant = true;
        if ('ResizeObserver' in window) {
            new ResizeObserver(() => {
                // ResizeObserver 回呼在排版完成後執行，此時讀取位置不會強制額外排版；一次量好所有分頁
                tabs.querySelectorAll('.tab-btn').forEach(t => fxTabPos.set(t, { x: t.offsetLeft, w: t.offsetWidth, tabs }));
                const a = tabs.querySelector('.tab-btn.active');
                if (a) fxMovePill(a, true);
            }).observe(tabs);
        }
    }
    // 分頁位置快取：點擊時不必讀 offsetLeft（避免切換分頁時強制同步排版）
    let pos = fxTabPos.get(btn);
    if (!pos || pos.tabs !== tabs) { pos = { x: btn.offsetLeft, w: btn.offsetWidth, tabs }; fxTabPos.set(btn, pos); }
    const x = pos.x, w = pos.w;
    if (!w) return;   // 面板隱藏中，等 ResizeObserver 顯示後再定位
    const prevX = parseFloat(pill.dataset.x || x), prevW = parseFloat(pill.dataset.w || w);
    pill.dataset.x = x; pill.dataset.w = w;
    if (pill.style.width !== `${w}px`) pill.style.width = `${w}px`;
    pill.style.transform = `translateX(${x}px)`;
    if (!instant && !fxOff() && (prevX !== x || prevW !== w)) {
        pill.animate([
            { transform: `translateX(${prevX}px) scaleX(${prevW / w})` },
            { transform: `translateX(${x}px)` }
        ], { duration: 480, easing: FX_SPRING });
    }
}

// ---- 按鈕：點擊漣漪 + 彈跳 ----
function fxRipple(btn, cx, cy) {
    if (!btn.isConnected) return;
    const r = btn.getBoundingClientRect();
    const size = Math.max(r.width, r.height) * 2.2;
    const rip = document.createElement('span');
    rip.className = 'fx-ripple';
    rip.style.cssText = `width:${size}px;height:${size}px;left:${cx - r.left - size / 2}px;top:${cy - r.top - size / 2}px`;
    btn.appendChild(rip);
    const a = rip.animate([{ transform: 'scale(0)', opacity: .4 }, { transform: 'scale(1)', opacity: 0 }],
                          { duration: 650, easing: FX_EASE });
    a.onfinish = () => rip.remove();
}

document.addEventListener('pointerdown', (e) => {
    if (fxOff()) return;
    const btn = e.target.closest('.ctrl-play, .btn-sm, .tab-btn, .btn-search');
    if (btn) {
        // 延到下一個畫格再量尺寸：在事件當下讀 getBoundingClientRect 會強制同步排版，
        // 與點擊後的 DOM 更新疊在一起變成長畫格
        const cx = e.clientX, cy = e.clientY;
        requestAnimationFrame(() => fxRipple(btn, cx, cy));
    }
    const cb = e.target.closest('.ctrl-btn:not(.ctrl-play), .item-btn');
    if (cb && cb.animate) {
        cb.animate([{ transform: 'scale(1)' }, { transform: 'scale(.82)', offset: .35 }, { transform: 'scale(1.1)', offset: .7 }, { transform: 'scale(1)' }],
                   { duration: 420, easing: FX_EASE });
    }
}, { passive: true });

// ---- 背景漂浮光點（預先畫好的星點圖層，分頁隱藏時暫停） ----
(function fxParticles() {
    return;   // 背景漂浮光點已移除（順暢優先：不在背後跑任何動畫）
    // 少量小光點，各自是很小的合成圖層：GPU 只需畫幾個小方塊（不像整面星點圖層每幀要混合整個螢幕），
    // 動畫只有 transform / opacity，keyframes 不含 CSS 變數，可完全交給合成器。
    const host = document.querySelector('.bg-aurora');
    if (!host) return;
    const n = window.matchMedia('(max-width: 640px)').matches ? 6 : 10;
    let html = '';
    for (let k = 0; k < n; k++) {
        const size = (3 + Math.random() * 3.5).toFixed(1);
        const dur = (20 + Math.random() * 18).toFixed(1);
        const delay = (-Math.random() * 38).toFixed(1);
        const cls = (Math.random() < .5 ? 'p' : 'k') + (k % 2 ? ' d2' : '');
        html += `<i class="${cls}" style="left:${(4 + Math.random() * 92).toFixed(1)}%;width:${size}px;height:${size}px;animation-duration:${dur}s;animation-delay:${delay}s"></i>`;
    }
    const layer = document.createElement('div');
    layer.className = 'fx-particles';
    layer.innerHTML = html;
    host.after(layer);
    document.addEventListener('visibilitychange', () => {
        document.documentElement.classList.toggle('fx-hidden', document.hidden);
    });
})();

// ---- 滾輪捲動清單時暫停列 hover 效果（避免每幀重繪經過游標的列）----
(function wheelHoverGuard() {
    const root = document.documentElement;
    let timer = 0, scroller = null;
    document.addEventListener('wheel', (e) => {
        if (!root.classList.contains('wheeling')) root.classList.add('wheeling');
        // 只標記正在捲動的清單：切換 class 時只重算這一個清單，不是整頁所有列
        const sc = e.target && e.target.closest ? e.target.closest('.list-container, .shelf-row') : null;
        if (sc !== scroller) {
            if (scroller) scroller.classList.remove('is-wheeling');
            scroller = sc;
            if (sc) sc.classList.add('is-wheeling');
        }
        clearTimeout(timer);
        timer = setTimeout(() => {
            root.classList.remove('wheeling');
            if (scroller) { scroller.classList.remove('is-wheeling'); scroller = null; }
        }, 200);
    }, { passive: true, capture: true });
})();

// ---- 播放卡片離開畫面（手機往下捲）時暫停卡片內所有無限動畫 ----

// ---- 背景視差：滑鼠移動時光點層輕微位移（rAF 節流，只有桌面滑鼠）。
//      perf2：漸層背景本身不再位移（全螢幕圖層移動在大螢幕會讓整個畫面每幀重新合成） ----
(function fxParallax() {
    if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
    const pt = document.querySelector('.fx-particles');
    if (!pt) return;
    let mx = 0, my = 0, queued = false;
    window.addEventListener('mousemove', (e) => {
        if (fxDecorOff() || document.hidden) return;
        mx = e.clientX / window.innerWidth - .5;
        my = e.clientY / window.innerHeight - .5;
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => {
            queued = false;
            pt.style.transform = `translate3d(${(-mx * 44).toFixed(1)}px, ${(-my * 32).toFixed(1)}px, 0)`;
        });
    }, { passive: true });
})();

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
    const root = document.documentElement;
    if (open) { updateHeaderBottom(); _npScrollY = window.scrollY; }   // 只讀（版面是乾淨的）
    else if (window.scrollY !== _npScrollY) window.scrollTo(0, _npScrollY);   // 展開期間背後頁面被捲動過才還原
    npOpen = open;
    const instant = fxOff();
    const fps = window.__npFps;
    _npAnimating = !instant;
    // 以下都是很小的寫入：一個屬性、兩個沒有樣式規則依賴的 class、按鈕自己的 class
    npView.setAttribute('aria-hidden', open ? 'false' : 'true');
    root.classList.toggle('np-open', open);
    if (el.btnExpand) {
        el.btnExpand.setAttribute('aria-expanded', open ? 'true' : 'false');
        el.btnExpand.classList.toggle('is-open', open);
        setTip(el.btnExpand, open ? '收合播放畫面 (F / Esc)' : '展開播放畫面 (F)', open ? '收合播放畫面' : '展開播放畫面');
    }
    npView.classList.toggle('open', open);   // ← 唯一會動的東西：面板 transform
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
    homeCatchUp();
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
}
function homeCatchUp() {
    if (homeDirty.favorites && isTabShown('favorites')) { renderFavList(state.favorites || [], FV.home); homeDirty.favorites = false; }
}
function activateNpTab(name, { instant = false } = {}) {
    const btn = document.querySelector(`#npTabs .tab-btn[data-np="${name}"]`);
    if (!btn) return;
    const changed = npTab !== name;
    npTab = name;
    document.querySelectorAll('#npTabs .tab-btn').forEach(b => b.classList.toggle('active', b === btn));
    [['queue', 'npTabQueue'], ['history', 'npTabHistory']]
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
    if (open) {   // 同一時間只開一個彈出框：開快捷鍵就收起狀態框
        if (window.closeDropdowns) window.closeDropdowns();
        const sp = $('statusPop');
        if (sp && !sp.hidden) $('statusClose').click();
        if (btnShortcuts) btnShortcuts.classList.remove('tip-show');
    }
}
if (btnShortcuts) btnShortcuts.addEventListener('click', (e) => { e.stopPropagation(); setKbdHelp(kbdHelp.hidden); });
if ($('kbdHelpClose')) $('kbdHelpClose').addEventListener('click', () => setKbdHelp(false));
document.addEventListener('pointerdown', (e) => {
    if (kbdHelp && !kbdHelp.hidden && !kbdHelp.contains(e.target) && !(btnShortcuts && btnShortcuts.contains(e.target))) setKbdHelp(false);
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
    else if (k === '?') { if (!e.repeat) setKbdHelp(kbdHelp ? kbdHelp.hidden : false); }
    else handled = false;
    if (handled) e.preventDefault();
});
if (el.btnExpand) el.btnExpand.addEventListener('click', (e) => { e.stopPropagation(); setNpOpen(!npOpen); });
if (el.btnCollapse) el.btnCollapse.addEventListener('click', closeNpView);
if (el.playerBar) {
    el.playerBar.addEventListener('click', (e) => {
        // 只有點在播放列「空白處 / 封面 / 標題」才展開；按鈕、滑桿、開關照常運作
        if (e.target.closest('button, input, label, select, a, .progress-bar, .volume-row, .pb-extras')) return;
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
    if (mobileMQ.matches) { if (extras.parentElement !== npExtra) npExtra.appendChild(extras); }
    else if (extras.parentElement !== right) right.insertBefore(extras, el.btnExpand);
}
mobileMQ.addEventListener ? mobileMQ.addEventListener('change', placeExtras) : mobileMQ.addListener(placeExtras);
placeExtras();

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
    items.forEach((it, i) => {
        const card = document.createElement('div');
        card.className = 'shelf-card';
        card.style.setProperty('--i', i);
        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        const art = document.createElement('div');
        art.className = 'shelf-art';
        const list = thumbCandidates(it.url, it.thumbnail, false);
        if (list.length) {
            const img = document.createElement('img');
            img.loading = 'lazy'; img.decoding = 'async'; img.alt = '';
            img.width = 320; img.height = 180;   // 固定尺寸：載入時不改版面
            art.appendChild(img);
            loadThumb(img, list, () => img.remove());
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
            const r = await API.playUrl(it.url, it.title, null, undefined, it.duration);
            toastAdded(r, it.title);
        };
        card.addEventListener('click', add);
        card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); add(); } });
        frag.appendChild(card);
    });
    row.appendChild(frag);
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
        if (btn) {
            btn.innerHTML = t === 'light' ? '<i class="fas fa-moon"></i>' : '<i class="fas fa-sun"></i>';
            btn.dataset.tip = t === 'light' ? '切換成深色' : '切換成淺色';
            btn.setAttribute('aria-label', btn.dataset.tip);
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
        set('cpu', r.cpu == null ? '—' : `${r.cpu.toFixed(0)}%（bot ${r.proc_cpu}%）`, r.cpu > 85 ? 'st-warn' : '');
        set('mem', r.mem_total ? `${fmtBytes(r.mem_used)} / ${fmtBytes(r.mem_total)}（bot ${fmtBytes(r.proc_rss)}）` : '—', r.mem_pct > 90 ? 'st-warn' : '');
        set('guilds', `${r.guilds} 個伺服器 · ${r.voice} 個語音${r.playing ? `（${r.playing} 個播放中）` : ''}`);
        set('bitrate', state.bitrate ? `Opus ${state.bitrate} kbps` : '未在語音中');
        set('ytdlp', r.ytdlp || '—');
        const ck = r.cookies || {};
        if (!ck.configured) set('cookies', '未設定', 'st-warn');
        else if (ck.session_ok) set('cookies', '已設定（含登入工作階段）', 'st-ok');
        else set('cookies', '已設定（缺 LOGIN_INFO / SID）', 'st-warn');
        const cw = $('cookieWarn');
        if (cw) {
            cw.textContent = (ck.configured && !ck.session_ok) ? (ck.warning || '') : '';
            cw.className = 'update-msg cookie-warn' + (ck.configured && !ck.session_ok ? ' st-warn' : '');
        }
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
        if (o) { setKbdHelp(false); btn.classList.remove('tip-show'); if (window.closeDropdowns) window.closeDropdowns(); }   // 跟快捷鍵說明互斥
        btn.setAttribute('aria-expanded', String(o));
        clearInterval(timer);
        if (o) { refresh(); timer = setInterval(() => { if (!document.hidden) refresh(); }, 3000); }
    }
    btn.addEventListener('click', (e) => { e.stopPropagation(); open(pop.hidden); });
    $('statusClose') && $('statusClose').addEventListener('click', () => open(false));
    // 點外面就收起：用捕獲階段的 pointerdown，其他按鈕的 stopPropagation 擋不住
    document.addEventListener('pointerdown', (e) => { if (!pop.hidden && !pop.contains(e.target) && !btn.contains(e.target)) open(false); }, { capture: true, passive: true });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !pop.hidden) open(false); });
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


// ==================== 我的歌單（個人歌單，用自己的金鑰；與本地歌單 / 最愛完全分開） ====================
// 依登入身分（cookie）；API 走 /api/ext/lists（伺服器依身分判斷是誰，只能看 / 改自己的）
const ML = (() => {
    // 舊版把金鑰存在 localStorage；改成登入後不再需要，順手清掉
    try { localStorage.removeItem('musbot-my-token'); localStorage.removeItem('musbot-owner-token'); } catch (_) {}
    const open = new Set();          // 展開中的歌單 id
    const songsCache = new Map();    // id → songs
    let lists = [], busy = false, loaded = false;
    let rev = null, revTimer = 0, inflight = null;

    async function call(method, path, body) {
        try {
            const r = await fetch('/api/ext/lists' + path, { method, cache: 'no-store',
                headers: { 'Content-Type': 'application/json' },
                body: body ? JSON.stringify(body) : undefined });
            let j = {}; try { j = await r.json(); } catch (_) {}
            if (r.status === 401) { goLogin(); return { status: 'error', message: j.message, auth: true }; }
            if (!r.ok && j.status !== 'error') j.status = 'error';
            if (!j.message && !r.ok) j.message = '錯誤 ' + r.status;
            return j;
        } catch (e) { return { status: 'error', message: '連線失敗' }; }
    }
    // 同時只跑一個 refresh；期間又被要求 → 結束後再補一次（連續變更只會多抓一次）
    let again = false;
    function refresh() {
        if (inflight) { again = true; return inflight; }
        inflight = doRefresh().finally(() => { inflight = null; if (again) { again = false; refresh(); } });
        return inflight;
    }
    async function doRefresh() {
        const j = await call('GET', '');
        if (j.status !== 'ok') { if (j.message && !j.auth) showToast(j.message, 'error'); return; }
        lists = j.lists || []; loaded = true;
        for (const id of [...songsCache.keys()]) if (!lists.some(p => p.id === id)) songsCache.delete(id);  // 展開中的歌單：draw() 先畫快取再重抓，不閃
        $('mlCount').textContent = `${j.you || ''} · ${lists.length}/${j.max_lists} 份`;
        draw();
    }
    function render() {
        $('mlActions').hidden = false;
        if (!loaded) { $('mlList').innerHTML = '<div class="empty-state"><i class="fas fa-spinner fa-pulse"></i></div>'; }
        refresh();
    }
    function draw() {
        const list = $('mlList');
        list.innerHTML = '';
        if (!lists.length) {
            list.innerHTML = '<div class="empty-state"><i class="fas fa-user"></i><p>還沒有歌單：按「新增」，或「匯入」YouTube／Spotify 歌單</p></div>';
            return;
        }
        const frag = document.createDocumentFragment();
        for (const pl of lists) frag.appendChild(row(pl));
        list.appendChild(frag);
    }
    function row(pl) {
        const wrap = document.createElement('div');
        wrap.className = 'ml-item' + (open.has(pl.id) ? ' open' : '');
        wrap.innerHTML = `
            <div class="list-item ml-head">
                <span class="item-index"><i class="fas fa-chevron-right ml-chev"></i></span>
                <span class="item-title ml-title"><span class="ml-name">${escapeHtml(pl.name)}</span><span class="item-by">${pl.count} 首</span></span>
                <div class="item-actions">
                    <button class="item-btn btn-play" data-a="play" title="加入佇列"><i class="fas fa-play"></i></button>
                    <button class="item-btn" data-a="shuffle" title="隨機加入佇列"><i class="fas fa-random"></i></button>
                    <button class="item-btn ml-opt" data-a="addcur" title="加入目前播放的歌"><i class="fas fa-plus"></i></button>
                    <button class="item-btn ml-opt" data-a="addurl" title="用網址加歌"><i class="fas fa-link"></i></button>
                    <button class="item-btn btn-edit ml-opt" data-a="rename" title="改名"><i class="fas fa-pen"></i></button>
                    <button class="item-btn btn-remove ml-opt" data-a="delete" title="刪除歌單"><i class="fas fa-trash-alt"></i></button>
                    <button class="item-btn ml-more" data-a="more" title="更多" aria-label="更多"><i class="fas fa-ellipsis-h"></i></button>
                </div>
            </div>
            <div class="ml-menu">
                <button class="ml-menu-btn" data-a="addcur"><i class="fas fa-plus"></i> 加入目前播放的歌</button>
                <button class="ml-menu-btn" data-a="addurl"><i class="fas fa-link"></i> 用網址加歌</button>
                <button class="ml-menu-btn" data-a="rename"><i class="fas fa-pen"></i> 改名</button>
                <button class="ml-menu-btn danger" data-a="delete"><i class="fas fa-trash-alt"></i> 刪除歌單</button>
            </div>
            <div class="ml-songs"></div>`;
        const head = wrap.firstElementChild;
        head.addEventListener('click', (e) => {
            const b = e.target.closest('[data-a]');
            if (b && b.dataset.a === 'more') { e.stopPropagation(); toggleMenu(wrap); return; }
            if (b) { e.stopPropagation(); act(pl, b.dataset.a, b); return; }
            toggle(pl, wrap);
        });
        // 手機版「⋯」選單（改名 / 刪除 / 加入目前播放）：純顯示切換，沒有動畫
        wrap.querySelector('.ml-menu').addEventListener('click', (e) => {
            const b = e.target.closest('[data-a]'); if (!b) return;
            e.stopPropagation(); wrap.classList.remove('menu-open'); act(pl, b.dataset.a, b);
        });
        if (open.has(pl.id)) fillSongs(pl, wrap);
        return wrap;
    }
    // 點「⋯」選單以外的地方就收起（手機上不會一直開著擋住清單）
    const closeMenus = () => document.querySelectorAll('#mlList .ml-item.menu-open').forEach(x => x.classList.remove('menu-open'));
    document.addEventListener('pointerdown', (e) => { if (!e.target.closest('.ml-menu, .ml-more')) closeMenus(); }, { capture: true, passive: true });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenus(); });
    function toggleMenu(wrap) {
        const was = wrap.classList.contains('menu-open');
        document.querySelectorAll('#mlList .ml-item.menu-open').forEach(x => x.classList.remove('menu-open'));
        if (!was) wrap.classList.add('menu-open');
    }
    async function toggle(pl, wrap) {
        if (open.has(pl.id)) { open.delete(pl.id); wrap.classList.remove('open'); return; }
        open.add(pl.id); wrap.classList.add('open');
        fillSongs(pl, wrap);
    }
    async function fillSongs(pl, wrap) {
        const box = wrap.querySelector('.ml-songs');
        const cached = songsCache.get(pl.id);
        if (cached) drawSongs(pl, box, cached);
        else box.innerHTML = '<div class="ml-song muted"><i class="fas fa-spinner fa-pulse"></i></div>';
        const j = await call('GET', '/' + pl.id);
        if (j.status !== 'ok') { box.innerHTML = `<div class="ml-song muted">${escapeHtml(j.message || '讀取失敗')}</div>`; return; }
        songsCache.set(pl.id, j.list.songs);
        syncHead(pl, wrap, j.list);     // 標頭首數以剛抓到的歌單為準（清單摘要可能比歌曲晚一步）
        drawSongs(pl, box, j.list.songs);
    }
    // 歌單標頭（名稱 / 首數）跟著最新資料更新；lists 摘要也同步，下次 draw() 不會畫回舊值
    function syncHead(pl, wrap, fresh) {
        const n = fresh.songs ? fresh.songs.length : fresh.count;
        pl.count = n; if (fresh.name) pl.name = fresh.name;
        const s = lists.find(x => x.id === pl.id); if (s) { s.count = n; if (fresh.name) s.name = fresh.name; }
        const by = wrap.querySelector('.ml-head .item-by'); if (by) by.textContent = `${n} 首`;
    }
    function drawSongs(pl, box, songs) {
        cancelChunks(box);
        box.innerHTML = '';
        if (!songs.length) { box.innerHTML = '<div class="ml-song muted">空的歌單</div>'; return; }
        // 長歌單分批畫（500 首也不卡）
        appendChunked(box, songs.length, (i) => {
            const s = songs[i];
            const d = document.createElement('div');
            d.className = 'ml-song';
            d.innerHTML = `<span class="ml-n">${i + 1}</span><span class="ml-t"></span>
                <button class="item-btn btn-play" title="加入佇列"><i class="fas fa-play"></i></button>
                <button class="item-btn btn-remove" title="從歌單移除"><i class="fas fa-times"></i></button>`;
            d.querySelector('.ml-t').textContent = s.title;
            d.querySelector('.btn-play').onclick = async () => { toastAdded(await API.playUrl(s.url, s.title, null, true), s.title); };
            d.querySelector('.btn-remove').onclick = async (ev) => {
                ev.currentTarget.disabled = true;
                const j = await call('POST', '/' + pl.id, { action: 'remove', url: s.url });
                if (j.status === 'error') { showToast(j.message, 'error'); return; }
                refresh();
            };
            return d;
        });
    }
    async function act(pl, a, btn) {
        if (busy) return;
        if (a === 'rename') {
            const n = prompt('新的歌單名稱', pl.name); if (!n || n.trim() === pl.name) return;
            const j = await call('POST', '/' + pl.id, { action: 'rename', name: n });
            if (j.status === 'error') showToast(j.message, 'error'); else refresh();
            return;
        }
        if (a === 'delete') {
            if (!confirm(`刪除歌單「${pl.name}」（${pl.count} 首）？`)) return;
            const j = await call('DELETE', '/' + pl.id);
            if (j.status === 'error') showToast(j.message, 'error');
            else { open.delete(pl.id); songsCache.delete(pl.id); showToast('🗑️ 已刪除歌單', 'warning'); refresh(); }
            return;
        }
        if (a === 'addcur' || a === 'addurl') {
            let body;
            if (a === 'addurl') {
                const u = prompt(`貼上要加入「${pl.name}」的 YouTube 網址`); if (!u || !u.trim()) return;
                body = { action: 'add', url: u.trim() };
            } else {
                if (!state.currentUrl) { showToast('目前沒有正在播放的歌', 'warning'); return; }
                body = { action: 'add', url: state.currentUrl, title: state.currentTitle };
            }
            const j = await call('POST', '/' + pl.id, body);
            if (j.status === 'error') showToast(j.message, 'error');
            else if (j.status === 'duplicate') showToast('歌單裡已經有這首歌', 'info');
            else { showToast(`✅ 已加入「${pl.name}」`, 'success'); refresh(); }
            return;
        }
        busy = true; btn.disabled = true;
        try {
            const j = await call('POST', '/' + pl.id, { action: 'play', shuffle: a === 'shuffle', guild_id: gid() });
            showToast(j.message || (j.status === 'ok' ? '已加入' : '失敗'), j.status === 'ok' ? 'success' : 'error');
        } finally { busy = false; btn.disabled = false; }
    }
    async function create() {
        const n = prompt('新歌單名稱'); if (!n || !n.trim()) return;
        const j = await call('POST', '', { name: n });
        if (j.status === 'error') showToast(j.message, 'error'); else { showToast('✅ 已建立', 'success'); refresh(); }
    }
    async function importList() {
        const url = prompt('貼上 YouTube 播放清單（含 list=）或 Spotify 歌單／專輯網址'); if (!url || !url.trim()) return;
        const j = await call('POST', '/import', { url: url.trim() });
        if (j.status === 'error') { showToast(j.message, 'error'); return; }
        showToast('📥 匯入中…（不會加入佇列）', 'info');
        for (let i = 0; i < 300; i++) {
            await new Promise(r => setTimeout(r, 2000));
            const p = await call('GET', '/import/' + j.job);
            const st = p.job && p.job.status;
            if (st === 'done') { showToast(`✅ 已匯入「${p.job.name}」${p.job.added} 首` + (p.job.skipped ? `（${p.job.skipped} 首找不到，已略過）` : ''), 'success'); refresh(); return; }
            if (st === 'error' || p.status === 'error') { showToast(`匯入失敗：${(p.job && p.job.message) || p.message}`, 'error'); return; }
            if (p.job && p.job.total) $('mlCount').textContent = `匯入中 ${p.job.done}/${p.job.total}`;
        }
    }
    $('btnMlNew').addEventListener('click', create);
    $('btnMlImport').addEventListener('click', importList);
    // SSE 版本號變了：分頁開著且已登入才重抓（沒開的分頁切過去時 render() 本來就會抓）
    function onRev(v) {
        if (rev === null) { rev = v; return; }
        if (v === rev) return;
        rev = v;
        if (!loaded || !isTabShown('mylists')) return;   // 沒開的分頁：切過去時 render() 會重抓
        clearTimeout(revTimer);
        revTimer = setTimeout(refresh, 120);
    }
    return { render, onRev };
})();


// ==================== 自訂下拉選單（伺服器 / 語音頻道） ====================
// 隱藏的 <select> 仍是唯一資料來源（原本的 change 事件與 API 呼叫完全不變）；這裡只負責畫面與操作。
(function customDropdowns() {
    const all = [];
    const OPT_RE = /^(\S+)\s+(.*?)\s*\((\d+)\)\s*$/u;
    function parse(opt) {
        const m = OPT_RE.exec(opt.textContent || '');
        if (!m) return { icon: '', name: (opt.textContent || '').trim(), count: null };
        return { icon: m[1], name: m[2], count: +m[3] };
    }
    function closeOthers(except) {
        all.forEach(d => { if (d !== except) d.close(false); });
        if (typeof setKbdHelp === 'function') setKbdHelp(false);
        const sp = $('statusPop'); if (sp && !sp.hidden && $('statusClose')) $('statusClose').click();
        document.querySelectorAll('#mlList .ml-item.menu-open').forEach(x => x.classList.remove('menu-open'));
    }
    window.closeDropdowns = () => all.forEach(d => d.close(false));

    // 名稱最多顯示 7 個字（以字素計，中日韓字 / emoji 不會被切半），超過加「…」；完整名稱放 title
    const _seg = (typeof Intl !== 'undefined' && Intl.Segmenter) ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null;
    function shortName(name, max = 7) {
        const g = _seg ? Array.from(_seg.segment(name), x => x.segment) : Array.from(name);
        return g.length > max ? g.slice(0, max).join('') + '…' : name;
    }
    window.cddShortName = shortName;

    function enhance(sel, kind) {
        if (!sel || sel._cdd) return;
        const wrap = sel.closest('.sel-wrap') || sel.parentNode;
        wrap.classList.add('cdd');
        sel.classList.add('cdd-native'); sel.tabIndex = -1; sel.setAttribute('aria-hidden', 'true');
        const btn = document.createElement('button');
        btn.type = 'button'; btn.className = 'sel cdd-btn';
        btn.setAttribute('aria-haspopup', 'listbox'); btn.setAttribute('aria-expanded', 'false');
        btn.setAttribute('aria-label', sel.getAttribute('aria-label') || (kind === 'voice' ? '語音頻道' : '伺服器'));
        const label = document.createElement('span'); label.className = 'cdd-label'; btn.appendChild(label);
        const list = document.createElement('div');
        list.className = 'cdd-list'; list.setAttribute('role', 'listbox'); list.hidden = true;
        list.id = sel.id + 'List'; btn.setAttribute('aria-controls', list.id);
        wrap.appendChild(btn); wrap.appendChild(list);
        let active = -1, items = [];

        function rowHtml(opt, i) {
            const p = parse(opt);
            const isBot = kind === 'voice' && p.icon === '\u{1F7E2}';
            const guildOn = kind === 'guild' && p.icon === '🟢';
            const icon = kind === 'voice' ? (isBot ? 'fa-robot' : 'fa-volume-high') : 'fa-server';
            const cls = ['cdd-opt'];
            if (opt.value === sel.value && opt.value) cls.push('selected');
            if (p.count) cls.push('busy');
            if (isBot || guildOn) cls.push('bot-here');
            const badge = p.count == null ? '' :
                `<span class="cdd-badge" title="${kind === 'voice' ? '頻道內人數' : '佇列歌曲數'}">${kind === 'voice' ? '<i class="fas fa-user"></i>' : '<i class="fas fa-music"></i>'}${p.count}</span>`;
            const tag = isBot ? '<span class="cdd-tag">機器人在這</span>' : (guildOn ? '<span class="cdd-tag">使用中</span>' : '');
            return `<div class="${cls.join(' ')}" role="option" data-i="${i}" aria-selected="${opt.value === sel.value}"><i class="fas ${icon} cdd-ic"></i><span class="cdd-main"><span class="cdd-name" title="${escapeHtml(p.name)}">${escapeHtml(shortName(p.name))}</span>${tag}</span>${badge}</div>`;
        }
        function render() {
            const opts = Array.from(sel.options);
            const cur = sel.options[sel.selectedIndex];
            const p = cur && cur.value ? parse(cur) : null;
            const ph = kind === 'voice' ? '選擇語音頻道' : '選擇伺服器';
            setText(label, p ? shortName(p.name) : ph);
            if (p) label.title = p.name; else label.removeAttribute('title');
            btn.classList.toggle('placeholder', !p);
            btn.classList.toggle('bot-here', !!(p && (p.icon === '\u{1F7E2}' || p.icon === '🟢')));
            btn.disabled = sel.disabled;
            if (list.hidden) return;
            items = [];
            let html = '';
            opts.forEach((o, i) => {
                if (!o.value) { if (o.disabled) html += `<div class="cdd-note">${escapeHtml(o.textContent)}</div>`; return; }
                items.push(i); html += rowHtml(o, i);
            });
            if (!items.length && !html) html = `<div class="cdd-note">${kind === 'voice' ? '請先選擇伺服器' : '尚未連線到任何伺服器'}</div>`;
            list.innerHTML = html;
            mark(); clamp();
        }
        function mark() {
            list.querySelectorAll('.cdd-opt').forEach((n, k) => n.classList.toggle('active', k === active));
            const n = list.querySelectorAll('.cdd-opt')[active];
            if (n) n.scrollIntoView({ block: 'nearest' });
        }
        function open() {
            if (btn.disabled) return;
            closeOthers(api_);
            list.hidden = false; btn.setAttribute('aria-expanded', 'true'); btn.classList.remove('tip-show');
            if (kind === 'voice' && typeof refreshVoiceOptions === 'function') refreshVoiceOptions();   // 人數即時
            render();
            const opts = Array.from(sel.options);
            active = Math.max(0, items.findIndex(i => opts[i].value === sel.value));
            mark();
            clamp();
        }
        // 選單寬度依內容而定：超出視窗左右邊就往內推（手機上語音選單靠右對齊時，長頻道名會把左半邊推出畫面）
        function clamp() {
            // 選單左右邊緣對齊觸發按鈕（同寬）；長名稱在選單內截斷，不撐寬
            list.style.removeProperty('transform');
            const base = list.offsetParent || list.parentElement;
            const br = btn.getBoundingClientRect(), pr = base.getBoundingClientRect();
            list.style.left = `${br.left - pr.left - (base.clientLeft || 0)}px`;
            list.style.right = 'auto';
            list.style.width = `${br.width}px`;
        }
        window.addEventListener('resize', () => { if (!list.hidden) clamp(); });
        function close(focus) {
            if (list.hidden) return;
            list.hidden = true; btn.setAttribute('aria-expanded', 'false');
            if (focus) btn.focus();
        }
        function pick(k) {
            const i = items[k]; if (i == null) return;
            const v = sel.options[i].value;
            close(true);
            if (v === sel.value && kind === 'guild') return;
            nativeSet.call(sel, v); render();
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
        const api_ = { close, render };
        all.push(api_);
        btn.addEventListener('click', (e) => { e.stopPropagation(); list.hidden ? open() : close(false); });
        btn.addEventListener('keydown', (e) => {
            if (list.hidden) { if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); e.stopPropagation(); open(); } return; }
            if (e.key === 'ArrowDown') { active = Math.min(items.length - 1, active + 1); mark(); }
            else if (e.key === 'ArrowUp') { active = Math.max(0, active - 1); mark(); }
            else if (e.key === 'Home') { active = 0; mark(); }
            else if (e.key === 'End') { active = items.length - 1; mark(); }
            else if (e.key === 'Enter' || e.key === ' ') pick(active);
            else if (e.key === 'Escape') close(true);
            else if (e.key === 'Tab') { close(false); return; }
            else return;
            e.preventDefault(); e.stopPropagation();
        });
        list.addEventListener('click', (e) => {
            const n = e.target.closest('.cdd-opt'); if (!n) return;
            e.stopPropagation();
            pick(Array.from(list.querySelectorAll('.cdd-opt')).indexOf(n));
        });
        list.addEventListener('pointermove', (e) => {
            const n = e.target.closest('.cdd-opt'); if (!n) return;
            const k = Array.from(list.querySelectorAll('.cdd-opt')).indexOf(n);
            if (k !== active) { active = k; list.querySelectorAll('.cdd-opt').forEach((x, j) => x.classList.toggle('active', j === active)); }
        }, { passive: true });
        document.addEventListener('pointerdown', (e) => { if (!list.hidden && !wrap.contains(e.target)) close(false); }, { capture: true, passive: true });
        // 程式改 sel.value / 重建選項 / 停用 → 同步畫面（每秒的狀態更新只在真的有變化時重畫）
        const nativeDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
        const nativeSet = nativeDesc.set;
        let queued = false;
        const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; render(); }); } };
        Object.defineProperty(sel, 'value', { configurable: true, get() { return nativeDesc.get.call(this); }, set(v) { nativeSet.call(this, v); schedule(); } });
        new MutationObserver(schedule).observe(sel, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled'] });
        sel.addEventListener('change', schedule);
        sel._cdd = api_;
        render();
    }
    enhance(el.guildSelect, 'guild');
    enhance(el.voiceChannelSelect, 'voice');
})();

// 搜尋結果：按 Esc 收起（輸入框裡按 Esc 也會收）
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !el.searchResults || !el.searchResults.firstChild) return;
    if (document.querySelector('.modal-overlay.active')) return;
    el.searchResults.innerHTML = '';
});
