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
    resetListCaches();
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

// 連點「跳過」只跳一首：按下後 700ms 內、或伺服器還沒回應（最多等 5 秒）時，再按都不送
let _skipBusy = false, _skipHoldUntil = 0;
function skipSong() {
    if (!state.guildId) return;
    const now = performance.now();
    if (_skipBusy || now < _skipHoldUntil) return;
    _skipBusy = true;
    _skipHoldUntil = now + 700;
    const release = setTimeout(() => { _skipBusy = false; }, 5000);
    // 樂觀更新
    setProgress(0);
    el.currentTime.textContent = '0:00';
    showToast('⏭️ 已跳過', 'success');
    API.skip().then(reportActionResult)  // SSE 會推送新歌資訊
        .finally(() => { clearTimeout(release); _skipBusy = false; });
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

