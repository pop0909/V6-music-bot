// ==================== 縮圖：依顯示大小挑解析度 + 裁掉內嵌黑邊 ====================
// 一律用 i.ytimg.com/vi/<id>/*.jpg：有 CORS（量黑邊、預先模糊要讀像素）。yt-dlp 給的 vi_webp/maxresdefault.webp
// 沒有 CORS，加了 crossorigin 會載入失敗 → 舊版大封面其實一直退到 hqdefault（480x360）
// 尺寸階梯（寬）：maxresdefault / hq720 1280 → sddefault 640（4:3）→ hqdefault 480（4:3）→ mqdefault 320（16:9、每支都有）
// 依「實際顯示寬 × 螢幕倍率」從夠用的那階開始往下退；小圖不浪費流量
const THUMB_TIERS = [
    [1280, 'maxresdefault'], [1280, 'hq720'], [640, 'sddefault'], [480, 'hqdefault'], [320, 'mqdefault'],
];
function ytId(url) {
    const m = String(url || '').match(/(?:v=|youtu\.be\/|shorts\/|\/vi(?:_webp)?\/)([A-Za-z0-9_-]{11})/);
    return m ? m[1] : '';
}
// 已知不存在的尺寸（YouTube 回 404 + 120x90 灰色預設圖）：記住，同一支影片之後直接跳過
const _thumbMissing = new Set();
// need：需要的像素寬（CSS 寬 × devicePixelRatio）。舊呼叫法傳 true / false = 大封面 / 小圖
function thumbCandidates(url, fallback, need) {
    if (need === true) need = 1280;
    else if (!need) need = 320;
    const id = ytId(url) || ytId(fallback);
    const list = [];
    if (id) {
        // 從「第一個 >= need 的最小那階」開始；need 比最大還大就從最大開始
        let start = 0;
        for (let i = THUMB_TIERS.length - 1; i >= 0; i--) { if (THUMB_TIERS[i][0] >= need) { start = i; break; } }
        while (start > 0 && THUMB_TIERS[start - 1][0] === THUMB_TIERS[start][0]) start--;   // 同寬的幾階都要試（maxres → hq720）
        for (let i = start; i < THUMB_TIERS.length; i++) {
            const u = `https://i.ytimg.com/vi/${id}/${THUMB_TIERS[i][1]}.jpg`;
            if (!_thumbMissing.has(u)) list.push(u);
        }
    }
    // 不是 YouTube 的縮圖（或抓不到 id）才用伺服器給的原網址；YouTube 的 webp 沒 CORS，不用
    if (fallback && !id) list.push(fallback);
    return list;
}
function thumbNeed(elOrWidth) {
    const w = typeof elOrWidth === 'number' ? elOrWidth : (elOrWidth && elOrWidth.getBoundingClientRect().width) || 0;
    return Math.round(w * Math.min(3, window.devicePixelRatio || 1));
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
        if (i >= candidates.length) { img.onerror = img.onload = null; if (onFail) onFail(); return; }
        const src = candidates[i++];
        const miss = () => { if (img.src === src) { _thumbMissing.add(src); next(); } };
        img.onerror = miss;
        // 不存在的尺寸會回 404 + 120x90 灰圖，有的瀏覽器照樣觸發 load → 看實際大小判斷
        img.onload = () => { if (img.src === src && i < candidates.length && img.naturalWidth > 0 && img.naturalWidth <= 120) miss(); };
        autoCrop(img, src);
        img.src = src;
    };
    next();
}
// 影片本身內嵌的邊（4:3 / 方形內容放在 16:9 畫面裡的左右色邊、黑邊）：載入後縮成 128x72 量一次，
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
    // 同一張圖放在方形（底部播放列）和 16:9（卡片）要裁的量不同 → 快取依「網址 + 顯示比例」分開
    const key = () => { const r = img.getBoundingClientRect(); return src + '|' + (r.width && r.height ? (r.width / r.height).toFixed(1) : '?'); };
    const k0 = key();
    if (_cropCache.has(k0)) { apply(_cropCache.get(k0)); return; }
    img.addEventListener('load', function onLoad() {
        if (img.src !== src) return;
        if (img.naturalWidth <= 120) return;   // 404 的 120x90 灰圖，loadThumb 會換下一張
        const s = measureBars(img);
        _cropCache.set(key(), s);
        apply(s);
    }, { once: true });
}
function measureBars(img) {
    try {
        // 128x72（每格約 10px）：64x36 時縮圖混色會少算一格，邊沒裁乾淨（maxresdefault 上看得出一條）
        const W = 128, H = 72;
        if (!_cropCanvas) { _cropCanvas = document.createElement('canvas'); _cropCanvas.width = W; _cropCanvas.height = H; }
        const ctx = _cropCanvas.getContext('2d', { willReadFrequently: true });
        // 只量「object-fit: cover 之後看得到的那塊」：4:3 的 sddefault / hqdefault 上下有黑邊，
        // 整欄一起量會因為黑邊跟色邊不同色而判斷成「沒有邊」，左右色邊就裁不掉
        const nw = img.naturalWidth || W, nh = img.naturalHeight || H;
        const r = img.getBoundingClientRect();
        const ba = r.width && r.height ? r.width / r.height : 16 / 9;
        let sw = nw, sh = nh;
        if (nw / nh > ba) sw = nh * ba; else sh = nw / ba;
        ctx.drawImage(img, (nw - sw) / 2, (nh - sh) / 2, sw, sh, 0, 0, W, H);
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
        // 多裁一格保險（縮小取樣時邊界那格可能是混色）
        const cw = 1 - 2 * (bx ? bx + 1 : 0) / W, ch = 1 - 2 * (by ? by + 1 : 0) / H;
        const s = Math.max(1, 1 / cw, 1 / ch);   // 量的已經是看得到的那塊，直接放大到邊被推出去
        return s > 1 ? Math.min(2, s * 1.02) : 1;
    } catch (_) { return 1; }   // 讀不到像素（CORS）就不裁
}

// ==================== 跑馬燈（歌名放不下才捲動） ====================
// 量實際寬度：放得下就是普通文字；放不下才換成兩段相同文字無縫循環（translateX(-50%) 剛好一段）。
// 速度固定約 38px/秒（長歌名不會飛很快）；滑鼠移上去暫停；系統「減少動態效果」時不捲動、用 … 截斷。
// 元素大小改變（視窗縮放、展開畫面從隱藏變顯示）由 ResizeObserver 重新判斷。
const MQ_SPEED = 38;
const _mqReduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
const _mqRO = window.ResizeObserver ? new ResizeObserver((ents) => ents.forEach(en => checkMarquee(en.target))) : null;
function setMarquee(node, text) {
    if (!node) return;
    text = String(text || '');
    if (node._mqText === text) return;
    node._mqText = text;
    node.classList.remove('marquee');
    node.style.removeProperty('--mq-dur');
    node.textContent = text;
    if (_mqRO && !node._mqObserved) { node._mqObserved = true; _mqRO.observe(node); }
    checkMarquee(node);
}
function checkMarquee(node) {
    const text = node._mqText;
    if (text === undefined) return;
    const w = node.clientWidth;
    if (!w) return;   // 看不到（收合中）：之後變大小時 ResizeObserver 會再叫
    const on = node.classList.contains('marquee');
    const seg = on ? node.querySelector('.mq-seg') : null;
    const need = on && seg ? seg.offsetWidth - mqGap(seg) : node.scrollWidth;   // 捲動中：量其中一段（扣掉段落間距）
    const over = need > w + 1 && !_mqReduce.matches;
    if (over && !on) {
        node.classList.add('marquee');
        node.textContent = '';
        const inner = document.createElement('span');
        inner.className = 'marquee-inner';
        for (let k = 0; k < 2; k++) {
            const sp = document.createElement('span');
            sp.className = 'mq-seg';
            sp.textContent = text;
            if (k) sp.setAttribute('aria-hidden', 'true');
            inner.appendChild(sp);
        }
        node.appendChild(inner);
        const segW = inner.firstChild.offsetWidth || need;
        node.style.setProperty('--mq-dur', `${Math.max(6, Math.min(60, segW / MQ_SPEED)).toFixed(1)}s`);
    } else if (!over && on) {
        node.classList.remove('marquee');
        node.style.removeProperty('--mq-dur');
        node.textContent = text;
    }
}
function mqGap(seg) { return parseFloat(getComputedStyle(seg).paddingRight) || 0; }
if (_mqReduce.addEventListener) _mqReduce.addEventListener('change', () => document.querySelectorAll('.song-title, .pb-title').forEach(n => { const t = n._mqText; n._mqText = undefined; setMarquee(n, t); }));

let _songInfoKey = null;
function updateSongInfo(title, url, thumbnail) {
    // 內容沒變就什麼都不做：舊版每秒重設 innerHTML，會讓跑馬燈每秒跳回開頭、封面與模糊背景重繪
    const key = `${title}\u0000${url}\u0000${thumbnail}`;
    if (key === _songInfoKey) return;
    const prevKey = _songInfoKey;
    _songInfoKey = key;
    if (prevKey !== null && title) fxSongSwap();
    // 跑馬燈：只有歌名真的放不下（量實際寬度）才捲動
    const titleEl = el.songTitle;
    setMarquee(titleEl, title || '尚未播放');
    titleEl.title = title || '';
    el.songUrl.textContent = url || '';
    el.songUrl.title = url || '';
    setClass(el.albumArt, `album-art${title ? ' playing' : ''}`);

    // 更新封面縮圖
    // 重點！不重建 DOM，只換 src 屬性，避免打斷瀏覽器載入
    const coverList = thumbCandidates(url, thumbnail, Math.max(640, thumbNeed(el.albumArt) || 1280));   // 大封面：通常是 maxresdefault（1280x720）
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
    updateNowPlayingBackdrop(thumbnail, url);
    updateBarSong(title, thumbnail, url);
}

// 底部播放列的小封面 + 標題（textContent / 屬性設定，不拼 HTML）
let _barArtSrc = null;
function updateBarSong(title, thumbnail, url) {
    if (!el.barTitle) return;
    setMarquee(el.barTitle, title || '尚未播放');
    el.barTitle.title = title || '';
    if (el.playerBar) el.playerBar.classList.toggle('has-song', !!title);
    const list = thumbCandidates(url, thumbnail, thumbNeed(el.barArt));   // 小圖：50~76px 方塊，mqdefault（320x180）就夠
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
function updateNowPlayingBackdrop(thumbnail, url) {
    const card = el.albumArt.closest('.np-card');
    if (!card) return;
    // 背景會被模糊，用 mqdefault（320x180、16:9、有 CORS）就好；YouTube 的 webp 沒 CORS，預先模糊會失敗、退回很吃 GPU 的即時模糊
    thumbnail = thumbCandidates(url, thumbnail, 320)[0] || '';
    if (thumbnail === _npArtSrc) return;
    _npArtSrc = thumbnail;
    const safe = thumbnail ? String(thumbnail).replace(/["\\\n\r()]/g, encodeURIComponent) : '';
    // 先用原圖 + CSS 即時模糊（舊行為，當作備援）
    card.classList.remove('np-preblur');
    card.style.setProperty('--np-art', safe ? `url("${safe}")` : 'none');
    card.style.setProperty('--np-glow', 'none');
    if (!safe) { applyCoverTint(null); return; }
    // 效能：把封面「預先模糊」成一張小圖（只算一次），取代每幀由 GPU 重算的 filter: blur(50px)
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.decoding = 'async';
    img.onload = () => {
        if (_npArtSrc !== thumbnail) return;
        applyCoverTint(sampleCoverTint(img));
        const data = preblurImage(img);
        if (!data) return;
        card.style.setProperty('--np-art', `url("${data}")`);
        card.classList.add('np-preblur');
        const glow = makeGlowImage(img);
        if (glow) card.style.setProperty('--np-glow', `url("${glow}")`);
    };
    img.onerror = () => { if (_npArtSrc === thumbnail) applyCoverTint(null); };
    img.src = thumbnail;
}
let _npArtSrc;

// ==================== 封面主色 → 播放器強調色（輕微） ====================
// 把封面縮成 32x18 取樣，找「最多、又夠鮮豔」的色相；灰階 / 太暗太亮的封面就用主題原本的顏色。
// 深色、淺色主題各算一個亮度（跟播放鍵圖示的對比都 ≥ 4.5），CSS 再跟主題色混合，只是輕輕帶一點封面的味道。
function sampleCoverTint(img) {
    try {
        const W = 32, H = 18;
        const c = document.createElement('canvas'); c.width = W; c.height = H;
        const ctx = c.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, W, H);
        return pickTint(ctx.getImageData(0, 0, W, H).data);
    } catch (_) { return null; }   // 沒有 CORS → 不上色
}
function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn;
    if (!d) return [0, 0, l];
    const s = d / (1 - Math.abs(2 * l - 1));
    let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h *= 60; if (h < 0) h += 360;
    return [h, s, l];
}
function hslToRgb(h, s, l) {
    const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
}
function relLum([r, g, b]) {
    const ch = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
}
// 在固定色相 / 飽和度下找「最亮、但白色播放鍵圖示還看得清楚」的亮度：
// 深色主題比照原本的粉紅（白字對比約 3.3，圖示屬大型元件 ≥ 3:1），淺色主題要 ≥ 4.6（跟原本的 #d6006f 一樣清楚）
function tintForTheme(h, s, theme) {
    const need = theme === 'light' ? 4.6 : 3.3;
    const ok = (l) => 1.05 / (relLum(hslToRgb(h, s, l)) + 0.05) >= need;
    let lo = 0.22, hi = 0.8;
    if (!ok(lo)) return hslToRgb(h, s, lo);
    for (let i = 0; i < 18; i++) { const mid = (lo + hi) / 2; if (ok(mid)) lo = mid; else hi = mid; }
    return hslToRgb(h, s, lo);
}
// data：RGBA 位元組。回傳 { h, s, dark: [r,g,b], light: [r,g,b] } 或 null（封面太灰 / 色彩太雜）
function pickTint(data) {
    const BINS = 24, bins = new Array(BINS).fill(0), hx = new Array(BINS).fill(0), hy = new Array(BINS).fill(0), sat = new Array(BINS).fill(0);
    let n = 0;
    for (let i = 0; i + 3 < data.length; i += 4) {
        if (data[i + 3] < 128) continue;
        n++;
        const [h, s, l] = rgbToHsl(data[i], data[i + 1], data[i + 2]);
        if (s < 0.22 || l < 0.12 || l > 0.9) continue;
        const w = s * (1 - Math.abs(l - 0.5) * 1.4);
        if (w <= 0) continue;
        const b = Math.floor(h / (360 / BINS)) % BINS;
        bins[b] += w; sat[b] += s * w;
        hx[b] += Math.cos(h * Math.PI / 180) * w; hy[b] += Math.sin(h * Math.PI / 180) * w;
    }
    if (!n) return null;
    // 相鄰三格一起算（色相落在格線上不會被拆散）
    let best = -1, bestW = 0;
    for (let b = 0; b < BINS; b++) {
        const w = bins[b] + 0.5 * (bins[(b + 1) % BINS] + bins[(b + BINS - 1) % BINS]);
        if (w > bestW) { bestW = w; best = b; }
    }
    if (best < 0 || bestW / n < 0.06) return null;   // 鮮豔的像素太少：灰階 / 黑白封面
    const ids = [(best + BINS - 1) % BINS, best, (best + 1) % BINS];
    let x = 0, y = 0, ws = 0, ss = 0;
    ids.forEach(b => { x += hx[b]; y += hy[b]; ws += bins[b]; ss += sat[b]; });
    let h = Math.atan2(y, x) * 180 / Math.PI; if (h < 0) h += 360;
    const s = Math.max(0.45, Math.min(0.85, ss / ws));
    return { h: Math.round(h), s: +s.toFixed(2), dark: tintForTheme(h, s, 'dark'), light: tintForTheme(h, s, 'light') };
}
function applyCoverTint(t) {
    const root = document.documentElement;
    if (!t) { root.classList.remove('has-tint'); return; }
    root.style.setProperty('--tint-dark', `rgb(${t.dark.join(' ')})`);
    root.style.setProperty('--tint-light', `rgb(${t.light.join(' ')})`);
    root.classList.add('has-tint');
}
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

// 佇列標頭：「12 首・48 分鐘」（加總已知長度；有未知長度的歌 → 尾端加「以上」）
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
    return `${n} 首・${fmtTotal(sum)}${unknown ? '以上' : ''}`;   // 有不知道長度的歌（直播等）→「23 分鐘以上」
}

// 總長度文字：直播沒有長度 → 顯示「直播」，不要顯示「/ 0:00」
function totalText(data) {
    // SSE 精簡進度事件可能只帶 elapsed：其餘沿用畫面上一次的完整狀態
    const title = ('current_title' in data) ? data.current_title : state.currentTitle;
    const dur = ('duration' in data) ? data.duration : state.duration;
    if (title && !(dur > 0)) return '直播';
    if ('duration_str' in data) return data.duration_str || '0:00';
    return (el.totalTime && el.totalTime.textContent) || '0:00';
}

