// ==================== 面板內詢問對話框（取代瀏覽器的 prompt / confirm） ====================
// askDialog({ title, message?, input?: { label?, value?, placeholder?, inputMode?, maxLength?, required?, validate?(v) → 錯誤文字 },
//             okText?, cancelText?, danger? }) → Promise：有輸入框 → 輸入的文字（已去頭尾空白）；沒有 → true；取消 → null
// 鍵盤：Enter 確定、Esc 取消、Tab 只在對話框裡循環；點外面也算取消；關掉後焦點回到原本的按鈕
const askDialog = (() => {
    const ov = $('askModal');
    if (!ov) return async () => null;
    const box = ov.querySelector('.ask-modal');
    const title = $('askTitle'), msg = $('askMsg'), field = $('askField'), label = $('askLabel');
    const input = $('askInput'), err = $('askErr'), ok = $('askOk'), cancel = $('askCancel'), close = $('askClose');
    let done = null, opts = null, back = null;
    function finish(v) {
        if (!done) return;
        const d = done; done = null; opts = null;
        ov.classList.remove('active');
        if (back && back.isConnected && typeof back.focus === 'function') { try { back.focus({ preventScroll: true }); } catch (_) {} }
        back = null;
        d(v);
    }
    function setErr(t) {
        err.textContent = t || '';
        if (t) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    }
    function submit() {
        if (!opts) return;
        if (!opts.input) { finish(true); return; }
        const v = input.value.trim();
        const e = !v ? (opts.input.required || '請輸入內容') : (opts.input.validate ? opts.input.validate(v) : '');
        if (e) { setErr(e); input.focus(); return; }
        finish(v);
    }
    function focusables() {
        return [close, opts && opts.input ? input : null, cancel, ok].filter(Boolean);
    }
    ok.addEventListener('click', submit);
    cancel.addEventListener('click', () => finish(null));
    close.addEventListener('click', () => finish(null));
    ov.addEventListener('click', (e) => { if (e.target === ov) finish(null); });
    input.addEventListener('input', () => { if (err.textContent) setErr(''); });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); }   // 注音選字中的 Enter 不算
    });
    // 捕獲階段處理：Esc 不會同時觸發其他「按 Esc 收起」的功能
    document.addEventListener('keydown', (e) => {
        if (!done) return;
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(null); return; }
        if (e.key === 'Tab') {
            const f = focusables(), i = f.indexOf(document.activeElement);
            e.preventDefault();
            const n = i < 0 ? (e.shiftKey ? f.length - 1 : 0) : (i + (e.shiftKey ? -1 : 1) + f.length) % f.length;
            f[n].focus();
        }
    }, true);
    // 焦點跑出對話框（例如點到背景）→ 拉回來
    document.addEventListener('focusin', (e) => {
        if (done && !box.contains(e.target)) { const f = focusables(); (opts && opts.input ? input : f[f.length - 1]).focus(); }
    }, true);
    return function askDialog(o) {
        if (done) finish(null);
        opts = o || {};
        back = document.activeElement;
        title.textContent = opts.title || '';
        msg.textContent = opts.message || '';
        msg.hidden = !opts.message;
        field.hidden = !opts.input;
        setErr('');
        if (opts.input) {
            const c = opts.input;
            label.textContent = c.label || '';
            label.hidden = !c.label;
            input.value = c.value || '';
            input.placeholder = c.placeholder || '';
            input.inputMode = c.inputMode || 'text';
            if (c.maxLength) input.maxLength = c.maxLength; else input.removeAttribute('maxlength');
            if (c.label) input.removeAttribute('aria-label'); else input.setAttribute('aria-label', opts.title || '');
        }
        ok.textContent = opts.okText || '確定';
        ok.className = 'btn-sm ' + (opts.danger ? 'danger' : 'gold');
        cancel.textContent = opts.cancelText || '取消';
        const p = new Promise((r) => { done = r; });
        ov.classList.add('active');
        if (opts.input) { input.focus(); input.select(); } else ok.focus();
        return p;
    };
})();

// ==================== 我的歌單（個人歌單，用自己的金鑰；與本地歌單完全分開） ====================
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
        if (!loaded) showSkeleton($('mlList'), 'row', 4);
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
            const n = await askDialog({ title: '重新命名歌單', input: { label: '新的歌單名稱', value: pl.name, maxLength: 40, required: '請輸入歌單名稱' }, okText: '儲存' });
            if (!n || n === pl.name) return;
            const j = await call('POST', '/' + pl.id, { action: 'rename', name: n });
            if (j.status === 'error') showToast(j.message, 'error'); else refresh();
            return;
        }
        if (a === 'delete') {
            if (!await askDialog({ title: '刪除歌單', message: `確定要刪除「${pl.name}」（${pl.count} 首）嗎？刪除後無法復原。`, okText: '刪除', danger: true })) return;
            const j = await call('DELETE', '/' + pl.id);
            if (j.status === 'error') showToast(j.message, 'error');
            else { open.delete(pl.id); songsCache.delete(pl.id); showToast('🗑️ 已刪除歌單', 'warning'); refresh(); }
            return;
        }
        if (a === 'addcur' || a === 'addurl') {
            let body;
            if (a === 'addurl') {
                const u = await askDialog({ title: `加入歌曲到「${pl.name}」`, input: { label: 'YouTube 網址', placeholder: 'https://www.youtube.com/watch?v=…', inputMode: 'url', required: '請貼上 YouTube 網址' }, okText: '加入' });
                if (!u) return;
                body = { action: 'add', url: u };
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
        const n = await askDialog({ title: '新增歌單', input: { label: '歌單名稱', placeholder: '例如：開車聽', maxLength: 40, required: '請輸入歌單名稱' }, okText: '建立' });
        if (!n) return;
        const j = await call('POST', '', { name: n });
        if (j.status === 'error') showToast(j.message, 'error'); else { showToast('✅ 已建立', 'success'); refresh(); }
    }
    async function importList() {
        const url = await askDialog({ title: '匯入歌單', message: '貼上 YouTube 播放清單（網址含 list=）或 Spotify 歌單／專輯網址，會建立一份新的個人歌單（不會加入佇列）。',
            input: { label: '歌單網址', placeholder: 'https://www.youtube.com/playlist?list=…', inputMode: 'url', required: '請貼上歌單網址' }, okText: '匯入' });
        if (!url) return;
        const j = await call('POST', '/import', { url });
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
if (isTabShown('mylists')) ML.render();     // 寬螢幕：右側面板一開始就是我的歌單


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
        if (window.setStatusOpen) window.setStatusOpen(false);
        if (window.closeSettingsMenu) window.closeSettingsMenu();
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
    clearSearchResults();
});
