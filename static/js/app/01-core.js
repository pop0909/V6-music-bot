/**
 * 🎵 Music Bot V6 — 前端控制面板 JS
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
const npDirty = { queue: true, history: true, actions: true };
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
    // 錯誤停久一點（5 秒），來得及看完；一般提示 2.5 秒、連續更新的提示 1.4 秒
    const ms = (opts && opts.ms) || (type === 'error' ? 5000 : key ? 1400 : 2500);
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
    // 同時最多留 3 則（手機）/ 4 則（桌面）：連續操作時不會疊滿半個畫面；多的從最舊的開始收
    const maxShown = innerWidth < 600 ? 3 : 4;
    const live = [...c.children].filter(x => !x._leaving);
    for (const old of live.slice(0, Math.max(0, live.length - maxShown))) {
        old._leaving = true;
        for (const [k, v] of _toastByKey) if (v.t === old) { clearTimeout(v.timer); _toastByKey.delete(k); }
        old.style.transition = 'opacity .18s ease'; old.style.opacity = '0';
        setTimeout(() => old.remove(), 180);
    }
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
    setTip(el.btnShuffle, on ? '隨機播放：開（S）' : '隨機播放：關（S）', on ? '隨機播放：開' : '隨機播放：關');
    scheduleModeSync();
}
const TOGGLE_TIPS = {
    autoplayToggle: ['自動推薦接歌', '佇列播完自動接相關歌曲'],
    normalizeToggle: ['音量平衡', '讓每首歌音量差不多，切換立即生效'],
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
    scheduleModeSync();
}

// ==================== 播放模式 ⋮：選單裡每列的狀態文字 + ⋮ 上的小圓點 ====================
// 各模式原本的更新函式（含樂觀更新 / 失敗退回）改完按鈕後呼叫這裡；同一輪多次呼叫只算一次
var _modeSyncQueued = false, _sleepShort = '';   // var：初始化前就可能被呼叫（不會有 TDZ 錯誤）
function scheduleModeSync() {
    if (_modeSyncQueued) return;
    _modeSyncQueued = true;
    queueMicrotask(() => { _modeSyncQueued = false; syncModeMenu(); });
}
function syncModeMenu() {
    const btn = document.getElementById('btnModes');
    if (!btn) return;
    const put = (id, txt) => { const n = document.getElementById(id); if (n && n.textContent !== txt) n.textContent = txt; };
    const lp = el.btnLoop, isOn = (input) => !!(input && input.closest('.toggle-row') && input.closest('.toggle-row').classList.contains('on'));
    const loop = lp && lp.classList.contains('loop-one') ? '單曲' : lp && lp.classList.contains('loop-all') ? '全部' : '';
    const shuffle = !!(el.btnShuffle && el.btnShuffle.classList.contains('active'));
    const auto = isOn(el.autoplayToggle), norm = isOn(el.normalizeToggle);
    put('loopState', loop || '關');
    put('shuffleState', shuffle ? '開' : '關');
    put('autoplayState', auto ? '開' : '關');
    put('normalizeState', norm ? '開' : '關');
    put('sleepState', _sleepShort || '關');
    const on = [];
    if (loop) on.push(`循環（${loop}）`);
    if (shuffle) on.push('隨機');
    if (auto) on.push('推薦');
    if (norm) on.push('平衡');
    if (_sleepShort) on.push(`睡眠（${_sleepShort}）`);
    btn.classList.toggle('has-on', on.length > 0);
    const tip = on.length ? `播放模式：已開 ${on.join('、')}` : '播放模式：全部關閉';
    setTip(btn, tip, tip);
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
function httpErrorText(code) {
    if (code === 413) return '送出的內容太大';
    if (code === 429) return '操作太頻繁，請稍等一下再試';
    if (code >= 500) return '機器人暫時沒有回應，請稍後再試';
    return '操作失敗，請重新整理頁面再試';
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
        try {
            return await res.json();
        } catch (_) {   // 不是 JSON（例如 Cloudflare 的錯誤頁、內容太大被擋）：不要把英文解析錯誤丟給使用者
            return { status: 'error', message: httpErrorText(res.status) };
        }
    } catch (err) {
        apiNoteFail(key);
        console.error(`API Error [${method} ${path}]:`, err);
        return { status: 'error', message: '連不到機器人，請檢查網路後再試', network: true };
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
    queueRemove: (i, g, u) => api('POST', '/api/queue/remove', { guild_id: g || gid(), index: i, url: u }),
    queueReorder: (f, t, g, u) => api('POST', '/api/queue/reorder', { guild_id: g || gid(), from: f, to: t, url: u }),
    queuePlay: (i, g, u) => api('POST', '/api/queue/play', { guild_id: g || gid(), index: i, url: u }),
    queueClear: (g) => api('POST', '/api/queue/clear', { guild_id: g || gid() }),
    getPlaylist: () => api('GET', '/api/playlist'),
    playlistAdd: (u, t) => api('POST', '/api/playlist/add', { url: u, title: t }),
    playlistEdit: (i, u, t, ex) => api('POST', '/api/playlist/edit', { index: i, url: u, title: t, expect_url: ex }),
    playlistDelete: (i, ex) => api('POST', '/api/playlist/delete', { index: i, expect_url: ex }),
    playlistPlay: (i, g, ex) => api('POST', '/api/playlist/play', { guild_id: g || gid(), index: i, expect_url: ex }),
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
    actions: (g) => api('GET', `/api/actions?guild_id=${g || gid()}&limit=300`),
    sleepTimer: (mode, minutes) => api('POST', '/api/sleep-timer', { guild_id: gid() || 0, mode, minutes }),
};

