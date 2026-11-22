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
    let clip = btn.querySelector(':scope > .fx-ripple-clip');
    if (!clip) { clip = document.createElement('span'); clip.className = 'fx-ripple-clip'; clip.setAttribute('aria-hidden', 'true'); btn.appendChild(clip); }
    clip.appendChild(rip);
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

