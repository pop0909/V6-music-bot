// ==================== 佇列 UI（含拖曳排序） ====================
// 佇列只在展開畫面：開著 → 立即重畫；收合中 → 標記待更新，閒置時補建（開啟時就不必現畫）
function renderQueueFromCache() {
    if (!state.guildId) { QV.np.list.innerHTML = '<div class="empty-state"><i class="fas fa-list"></i><p>選擇伺服器以檢視佇列</p></div>'; return; }
    if (stillLoading('_queueKey') && !(state.queue || []).length) { showSkeleton(QV.np.list, 'row', 6); return; }
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
        if (e.pointerType === 'touch' && navigator.vibrate) { try { navigator.vibrate(8); } catch (_) {} }   // 手機：拿起來時輕震一下
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
    // 鍵盤：把手有焦點時 ↑ ↓ 上下移一格（Home / End 移到最前 / 最後）；不會觸發全域的音量快捷鍵
    handle.addEventListener('keydown', (e) => {
        if (_qDrag || e.altKey || e.ctrlKey || e.metaKey) return;
        const items = [...list.querySelectorAll('.list-item')];
        const from = items.indexOf(div);
        if (from < 0) return;
        const to = e.key === 'ArrowUp' ? from - 1 : e.key === 'ArrowDown' ? from + 1
                 : e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : null;
        if (to === null) return;
        e.preventDefault(); e.stopPropagation();
        if (to < 0 || to >= items.length || to === from || e.repeat && _qKeyBusy) return;
        commitQueueMove(list, div, items, from, to, true);
    });
}
let _qKeyBusy = false, _qMoving = 0;
function dragFrame(d) {
    // 靠近清單上下緣時自動捲動
    // 手指停在邊緣不動也會一直捲（越靠邊越快）
    const lr = d.list.getBoundingClientRect(), EDGE = 48;
    let speed = 0;
    if (d.y < lr.top + EDGE) speed = -Math.ceil((lr.top + EDGE - d.y) / 4);
    else if (d.y > lr.bottom - EDGE) speed = Math.ceil((d.y - (lr.bottom - EDGE)) / 4);
    if (speed) {
        const before = d.list.scrollTop;
        d.list.scrollTop += Math.max(-18, Math.min(18, speed));
        if (d.list.scrollTop !== before && !d.raf) d.raf = requestAnimationFrame(() => { d.raf = 0; if (_qDrag === d) dragFrame(d); });
    }
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
    await commitQueueMove(list, div, items, from, to, false);
}
// 真正搬移：先在畫面上搬（樂觀更新），再送 API（帶上原本那首的網址，佇列被別人改過就 409 → 重畫成伺服器的版本）
async function commitQueueMove(list, div, items, from, to, byKey) {
    const song = (state.queue || [])[from];
    if (byKey) _qKeyBusy = true;
    // 搬 DOM（transform 已清掉，畫面直接落在新位置）＋重新編號
    if (to > from) list.insertBefore(div, items[to].nextSibling); else list.insertBefore(div, items[to]);
    list.querySelectorAll('.list-item').forEach((it, idx) => {
        it.dataset.index = idx;
        const n = it.querySelector('.item-index-num'); if (n) n.textContent = idx + 1;
    });
    div.classList.add('fx-dropped');
    setTimeout(() => div.classList.remove('fx-dropped'), 450);
    if (byKey) { const h = div.querySelector('.drag-handle'); if (h) h.focus({ preventScroll: false }); }
    if (state.queue && song) { state.queue.splice(from, 1); state.queue.splice(to, 0, song); }
    state._queueKey = JSON.stringify((state.queue || []).map(s => [s.url, s.title]));   // SSE 帶回同樣順序時不再重畫
    _qMoving++;
    let r;
    try { r = await API.queueReorder(from, to, undefined, song && song.url); } finally { _qMoving--; }
    if (byKey) _qKeyBusy = false;
    if (!r || r.status !== 'ok') {
        showToast((r && r.message) || '排序失敗', 'error', { key: 'reorder' });
        if (state.queue && song) { state.queue.splice(to, 1); state.queue.splice(from, 0, song); }
        state._queueKey = undefined;   // 下一次 SSE 一定照伺服器的順序重畫（可能是別人改過佇列）
        renderQueueFromCache();
    } else if (_qRenderPending) { _qRenderPending = false; renderQueueFromCache(); }
}

let _qRenderPending = false;
function renderQueueList(q, view = QV.np) {
    if (typeof syncQueueClearBtn === 'function') syncQueueClearBtn();
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
            <span class="drag-handle" tabindex="0" role="button" aria-roledescription="拖曳把手" aria-label="移動（拖曳，或按 ↑ ↓）"><i class="fas fa-grip-vertical" aria-hidden="true"></i></span>
            <span class="item-index-num">${i + 1}</span>
            <span class="item-title">${escapeHtml(song.title)}${song.by ? `<span class="item-by">👤 ${escapeHtml(song.by)}</span>` : ''}</span>
            <div class="item-actions">
                <button class="item-btn btn-play" data-action="play" title="現在播放這首" aria-label="現在播放這首"><i class="fas fa-play" aria-hidden="true"></i></button>
                <button class="item-btn btn-remove" data-action="remove" title="從佇列移除" aria-label="從佇列移除"><i class="fas fa-times" aria-hidden="true"></i></button>
            </div>`;
        div.querySelector('.item-title').title = song.title || '';   // 完整歌名（屬性用 property 設定，不拼 HTML）

        bindQueueDrag(list, div);

        div.querySelector('[data-action="play"]').addEventListener('click', (ev) => {
            const b = ev.currentTarget; if (b.disabled) return; b.disabled = true;   // 連點兩下不會連跳兩首
            // 先顯示「切換中」，等真正開始播放（SSE 更新 currentUrl）再確認，避免提示比換歌早 5–8 秒
            showToast(`切換中… ${song.title}`, 'success', { key: 'queue-play', ms: 30000 });
            setProgress(0);
            setText(el.currentTime, '0:00');
            // 用目前位置（拖曳 / 鍵盤移動後會變）＋網址：位置過時（上一首剛播完佇列前移）時伺服器以網址為準
            API.queuePlay(+div.dataset.index, null, song.url).then(async (r) => {
                if (r && r.code === 'stale') { b.disabled = false; resyncSoon(150); return; }   // 這首已經在播 / 被刪：不必再提示
                if (actionFailed(r)) { b.disabled = false; reportActionResult(r); return; }
                const ok = await waitUntilCurrent(song.url, 25000);
                b.disabled = false;
                if (ok) showToast(`▶ 正在播放：${song.title}`, 'success', { key: 'queue-play' });
                else showToast(`已送出切換：${song.title}`, 'warning', { key: 'queue-play', ms: 4000 });
                resyncSoon();
            });
        });
        div.querySelector('[data-action="remove"]').addEventListener('click', async (ev) => {
            const b = ev.currentTarget; if (b.disabled) return; b.disabled = true;   // 連點兩下不會刪到下一首
            await API.queueRemove(+div.dataset.index, null, song.url);
            b.disabled = false;
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

$('modalConfirm').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    if (btn.disabled) return;
    const url = $('editUrl').value.trim(), title = $('editTitle').value.trim();
    if (!url) { showToast('請輸入網址', 'error'); return; }
    if (!title) { showToast('請輸入標題', 'error'); return; }
    btn.disabled = true;                         // 送出中不能再按（連點兩下會新增兩首）
    try { await saveModal(url, title); } finally { btn.disabled = false; }
});
async function saveModal(url, title) {
    const urlErr = songUrlError(url);
    if (urlErr) { showToast(urlErr, 'error'); return; }   // 出錯不關視窗，保留已輸入的內容
    if (modalMode === 'edit' && editingIndex >= 0) {
        const r = await API.playlistEdit(editingIndex, url, title, editingUrl);
        if (!r || r.status === 'error') { showToast((r && r.message) || '更新失敗', 'error'); return; }
        showToast('✅ 已更新', 'success');
    } else {
        const r = await API.playlistAdd(url, title);
        if (!r || r.status === 'error') { showToast((r && r.message) || '新增失敗', 'error'); return; }
        showToast(`✅ 已新增：${title}`, 'success');
    }
    $('editModal').classList.remove('active');
    renderPlaylistFromCache();
}

$('btnPlaylistAdd').addEventListener('click', () => {
    modalMode = 'add'; $('modalTitle').textContent = '新增到本地歌單';
    $('editUrl').value = ''; $('editTitle').value = '';
    $('editModal').classList.add('active'); $('editUrl').focus();
});

$('editUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('editTitle').focus(); });
$('editTitle').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('modalConfirm').click(); });

function renderPlaylistFromCache() {
    if (!state.guildId) { el.playlistList.innerHTML = '<div class="empty-state"><i class="fas fa-book"></i><p>選擇伺服器以檢視歌單</p></div>'; if (!uiReady) el.playlistCount.textContent = '載入中…'; return; }
    if (stillLoading('_plKey') || !uiReady) { showSkeleton(el.playlistList, 'row', 6); el.playlistCount.textContent = '載入中…'; return; }
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
                <button class="item-btn btn-play" data-action="play" title="加入佇列" aria-label="加入佇列"><i class="fas fa-play" aria-hidden="true"></i></button>
                <button class="item-btn btn-edit" data-action="edit" title="編輯" aria-label="編輯"><i class="fas fa-pen" aria-hidden="true"></i></button>
                <button class="item-btn btn-remove" data-action="delete" title="從歌單刪除" aria-label="從歌單刪除"><i class="fas fa-trash-alt" aria-hidden="true"></i></button>
            </div>`;
        div.querySelector('.item-title').title = song.title || '';
        div.querySelector('[data-action="play"]').addEventListener('click', async (ev) => {
            const b = ev.currentTarget; if (b.disabled) return; b.disabled = true;   // 連點兩下只加一次
            try {
                const r = await API.playlistPlay(i, null, song.url);
                toastAdded(r, song.title);
            } finally { b.disabled = false; }
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
const SEARCH_PLACEHOLDER = {
    youtube: '搜尋 YouTube，或貼上 YouTube／B 站／Spotify 網址…',
    bilibili: '搜尋 B 站，或貼上 B 站／YouTube／Spotify 網址…',
};
let searchSource = 'youtube';
try { const s = localStorage.getItem('mb_search_src'); if (s === 'bilibili' || s === 'youtube') searchSource = s; } catch (e) {}
function syncSearchSource() {
    document.querySelectorAll('.search-src-btn').forEach((b) => {
        const on = b.dataset.src === searchSource;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (el.searchInput) el.searchInput.placeholder = SEARCH_PLACEHOLDER[searchSource] || SEARCH_PLACEHOLDER.youtube;
}
document.querySelectorAll('.search-src-btn').forEach((b) => {
    b.addEventListener('click', (e) => {
        e.preventDefault(); e.stopPropagation();
        searchSource = b.dataset.src === 'bilibili' ? 'bilibili' : 'youtube';
        try { localStorage.setItem('mb_search_src', searchSource); } catch (err) {}
        syncSearchSource();
        if (el.searchInput && el.searchInput.value.trim() && !/^https?:\/\//.test(el.searchInput.value.trim())) doSearch();
    });
});
syncSearchSource();
el.btnSearch.addEventListener('click', doSearch);
el.searchInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
// 點到搜尋框外框的空白（圖示旁、上下邊）也能開始打字
if (el.searchInput.closest('.search-row')) el.searchInput.closest('.search-row').addEventListener('click', (e) => {
    if (!e.target.closest('button, input, a')) el.searchInput.focus();
});
// 搜尋框清空（刪字、剪下、手機鍵盤、瀏覽器的 ✕）→ 結果一起收起來；還在搜的那次也作廢，回來不會再冒出來
let _searchSeq = 0;
function clearSearchResults() {
    _searchSeq++;
    el.searchResults.innerHTML = '';
    el.btnSearch.disabled = false;
}
const _onSearchEmptied = () => { if (!el.searchInput.value.trim() && el.searchResults.firstChild) clearSearchResults(); };
el.searchInput.addEventListener('input', _onSearchEmptied);
el.searchInput.addEventListener('search', _onSearchEmptied);

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
                const n = Number(j.added);
                const added = Number.isFinite(n) ? n : 0;
                const extra = (j.skipped ? `（${j.skipped} 首找不到）` : '') + (j.full_skipped ? `（佇列已滿，另有 ${j.full_skipped} 首沒加入）` : '');
                showToast(`✅ 已從「${j.name}」加入 ${added} 首${extra}`, 'success', { key: 'import', ms: 3500 });
                return;
            }
            if (j.total) showToast(`🔎 搜尋中 ${j.done}/${j.total}…`, 'success', { key: 'import', ms: 60000 });
        }
        showToast('匯入逾時', 'error', { key: 'import' });
    } finally { _importing = false; }
}

// 貼網址：解析 → 整批加入佇列（_resolving 擋住解析中又按 Enter 造成的重複加入）
let _resolving = false;
async function addFromUrl(query) {
    el.searchResults.innerHTML = '<div style="padding:12px;text-align:center;color:var(--sub)"><i class="fas fa-spinner fa-pulse"></i> 正在解析網址...</div>';
    el.btnSearch.disabled = true;

    const data = await API.resolveUrl(query);
    el.btnSearch.disabled = false;

    if (!data || data.status === 'error') {
        const msg = (data && data.message) || '無法解析這個網址';
        el.searchResults.innerHTML = `<div style="padding:12px;text-align:center;color:var(--sub)">${escapeHtml(msg)}</div>`;
        showToast(msg, 'error');
        return;   // 保留輸入的網址，不關結果區
    }

    if (!data.songs || data.songs.length === 0) {
        el.searchResults.innerHTML = '<div style="padding:12px;text-align:center;color:var(--sub)">這個網址找不到歌曲</div>';
        showToast('這個網址找不到歌曲', 'error');
        return;
    }

    // 將所有歌曲加入佇列（不自動播放）；guild 未選時後端自動 fallback
    el.searchResults.innerHTML = '';
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
}

async function doSearch() {
    const query = el.searchInput.value.trim();
    if (!query) return;
    if (!/^https?:\/\//i.test(query) && [...query].length > SEARCH_QUERY_MAX) {
        showToast(`關鍵字最多 ${SEARCH_QUERY_MAX} 個字`, 'error');
        return;
    }
    closeNpView();   // 搜尋結果顯示在主畫面

    // === 匯入歌單：YouTube 播放清單 / Spotify 歌單或專輯（背景解析，toast 顯示進度） ===
    if (/^https?:\/\//.test(query) && (/open\.spotify\.com\//.test(query) || /[?&]list=|\/playlist/.test(query))) {
        importPlaylist(query);
        return;
    }

    // === 處理網址（支援播放清單） ===
    if (query.match(/^https?:\/\//)) {
        if (_resolving) return;                  // 解析中又按一次 Enter：不要加兩次
        _resolving = true;
        try { await addFromUrl(query); } finally { _resolving = false; }
        return;
    }

    // === 關鍵字搜尋 ===
    const seq = ++_searchSeq;
    el.searchResults.innerHTML = '<div class="search-loading" role="status" aria-live="polite" aria-busy="true">'
        + '<div class="search-loading-text"><i class="fas fa-spinner fa-pulse" aria-hidden="true"></i> 搜尋中…</div>'
        + skelHTML('result', 5).replace('aria-label="載入中"', 'aria-label="搜尋中"')
        + '</div>';
    el.btnSearch.disabled = true;
    const data = await API.search(query, searchSource);
    if (seq !== _searchSeq) return;   // 等結果時搜尋框被清空 / 按了 Esc / 又搜了別的
    el.btnSearch.disabled = false;
    el.searchResults.innerHTML = '';

    if (!data || !data.results || data.results.length === 0) {
        el.searchResults.innerHTML = '<div style="padding:12px;text-align:center;color:var(--sub)">找不到結果</div>';
        return;
    }

    data.results.forEach(r => {
        const div = document.createElement('div');
        div.className = 'search-result-item';
        const channel = r.channel || r.author || r.uploader || '';
        div.innerHTML = `
            <img alt="" loading="lazy" decoding="async" width="64" height="36">
            <span class="result-meta">
                <span class="result-title">${escapeHtml(r.title)}</span>
                ${channel ? `<span class="result-channel">${escapeHtml(channel)}</span>` : ''}
            </span>
            <span class="result-duration">${r.duration ? formatTime(r.duration) : ''}</span>
            <span class="result-add"><i class="fas fa-plus-circle"></i></span>`;
        const titleEl = div.querySelector('.result-title');
        titleEl.title = r.title || '';
        const chEl = div.querySelector('.result-channel');
        if (chEl) chEl.title = channel;
        // 64x36 的小圖：mqdefault（320x180、16:9）就夠，伺服器給的 hqdefault（480x360、4:3 有黑邊）太大
        const rimg = div.querySelector('img');
        loadThumb(rimg, thumbCandidates(r.url, r.thumbnail, thumbNeed(64)), () => { rimg.style.display = 'none'; });
        div.addEventListener('click', async () => {
            if (div.classList.contains('is-adding')) return;   // 連點兩下只加一次
            div.classList.add('is-adding');                    // 按下立刻有反應：整列變淡、＋ 換成轉圈
            div.setAttribute('aria-busy', 'true');
            const ic = div.querySelector('.result-add i');
            if (ic) ic.className = 'fas fa-spinner fa-pulse';
            const res = await API.playUrl(r.url, r.title, null, true, r.duration);  // no_play=true；guild 未選時後端自動 fallback
            div.classList.remove('is-adding'); div.removeAttribute('aria-busy');
            if (ic) ic.className = 'fas fa-plus-circle';
            toastAdded(res, r.title);
            setTimeout(refreshAll, 500);   // 加歌後保留搜尋清單，方便繼續加下一首
        });
        el.searchResults.appendChild(div);
    });
}

