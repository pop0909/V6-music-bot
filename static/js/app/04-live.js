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
        _npTimeText = `${data.elapsed_str || '0:00'} / ${totalText(data)}`;
        if (npOpen && el.npTime) setText(el.npTime, _npTimeText);   // 收合時不碰展開畫面的 DOM
    }
    setText(el.totalTime, totalText(data));
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
        // 排序請求還沒回來時，SSE 可能還帶著舊順序：先不理，等請求回來再照下一次推送比對（不然會閃回舊順序、鍵盤焦點也會掉）
        if (queueKey !== state._queueKey && !_qMoving) {
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
    // === 操作紀錄版本號：有人點歌 / 跳過 / 刪歌 → 正在看操作紀錄就重抓，沒在看只標記 ===
    if (data.act_rev !== undefined && data.act_rev !== state._actRev) {
        const first = state._actRev === undefined;
        state._actRev = data.act_rev;
        if (!first) {
            npDirty.actions = true;
            if (actionsVisible()) scheduleActionsReload(400);
        }
    }

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
    if (data.shuffle_mode !== undefined && performance.now() > _shuffleHoldUntil) {   // 剛按過：等伺服器確認前不讓 SSE 舊值把按鈕彈回去
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
        showToast((r && r.message) || '操作失敗（機器人可能不在語音頻道）', 'error');
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
    resetListCaches();
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
            el.songTitle.textContent = '⚠️ 機器人尚未連線到任何伺服器';
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

// ==================== 載入中骨架畫面 ====================
// 資料還沒到之前先畫灰色佔位框（跟真的列表 / 卡片同大小），資料到了直接換掉，不會先閃「清單為空」。
// 外層 .skel-wrap 用 display: contents，放進 grid（再聽一次）也不影響排版；讀屏只會念一次「載入中」。
const SKEL_W = [72, 54, 86, 61, 47, 79, 58, 67];
function skelHTML(kind, n) {
    let h = '';
    for (let i = 0; i < n; i++) {
        const w = SKEL_W[i % SKEL_W.length];
        if (kind === 'card') h += `<div class="skel-card"><span class="skel skel-art"></span><span class="skel skel-line" style="width:${w}%"></span><span class="skel skel-line skel-sm" style="width:38%"></span></div>`;
        else if (kind === 'result') h += `<div class="skel-result"><span class="skel skel-thumb"></span><span class="skel skel-line" style="width:${w}%"></span><span class="skel skel-dot"></span></div>`;
        else h += `<div class="skel-row"><span class="skel skel-num"></span><span class="skel skel-line" style="width:${w}%"></span></div>`;
    }
    return `<div class="skel-wrap" role="status" aria-busy="true" aria-label="載入中">${h}</div>`;
}
function showSkeleton(container, kind, n) {
    if (!container) return;
    if (container.firstElementChild && container.firstElementChild.classList.contains('skel-wrap')) return;   // 已經在轉：不重來
    if (typeof cancelChunks === 'function') cancelChunks(container);
    container.innerHTML = skelHTML(kind, n);
}
// 換伺服器 / 剛打開：SSE 第一次推送前（最多 8 秒）清單顯示骨架；逾時就照現有資料畫（避免永遠在轉）
state._skelUntil = performance.now() + 8000;
function stillLoading(key) { return state[key] === undefined && performance.now() < state._skelUntil; }
let _skelTimer = 0;
function resetListCaches() {
    state._plKey = state._favKey = state._queueKey = undefined;
    state._skelUntil = performance.now() + 8000;
    clearTimeout(_skelTimer);
    _skelTimer = setTimeout(() => { renderPlaylistFromCache(); renderFavoritesFromCache(); renderQueueFromCache(); }, 8100);
}

