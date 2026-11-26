// YouTube / YouTube Music / Bilibili：在標題旁放「加入機器人」按鈕（這些站都是單頁應用，換影片時重新放）
(() => {
  const BTN_ID = 'musbot-add-btn';
  const isMusic = location.hostname === 'music.youtube.com';
  const isBili = /(^|\.)(bilibili\.com|b23\.tv)$/.test(location.hostname);
  const isBiliMobile = location.hostname === 'm.bilibili.com';

  function toast(text, ok = true) {
    const d = document.createElement('div');
    d.className = 'musbot-toast ' + (ok ? 'ok' : 'err');
    d.textContent = (ok ? '🎵 ' : '⚠️ ') + text;
    document.documentElement.appendChild(d);
    requestAnimationFrame(() => d.classList.add('show'));
    setTimeout(() => { d.classList.remove('show'); setTimeout(() => d.remove(), 300); }, 3200);
  }

  // YouTube：/watch?v=ID 或 /shorts/ID；B 站：/video/BVxxx 或 /video/av123（含 ?p=）
  function videoId() {
    if (isBili) {
      const m = location.pathname.match(/\/video\/(BV[1-9A-HJ-NP-Za-km-z]{10}|av\d+)/i);
      return m ? m[1] : null;
    }
    if (location.pathname === '/watch') return new URL(location.href).searchParams.get('v');
    const m = location.pathname.match(/^\/shorts\/([A-Za-z0-9_-]{11})/);
    return m ? m[1] : null;
  }
  const isShorts = () => location.pathname.startsWith('/shorts/');

  function currentVideo() {
    const v = videoId();
    if (!v) return null;
    let title = '';
    let url;
    if (isBili) {
      const h = document.querySelector('h1.video-title, .video-title, h1[class*="video-title"], .video-info-title-inner h1');
      title = (h && h.textContent.trim()) || '';
      if (!title) title = document.title.replace(/[\s_]*哔哩哔哩.*$/, '').replace(/\s*-\s*bilibili$/i, '').trim();
      url = 'https://www.bilibili.com/video/' + v;
      const part = new URL(location.href).searchParams.get('p');
      if (part && part !== '1') url += '?p=' + part;
      return { url, title };
    }
    if (isMusic) {
      const t = document.querySelector('ytmusic-player-bar .title');
      const a = document.querySelector('ytmusic-player-bar .byline a');
      title = (t && t.textContent.trim()) || '';
      if (title && a) title = `${a.textContent.trim()} - ${title}`;
    } else if (!isShorts()) {
      const h = document.querySelector('ytd-watch-metadata #title h1, h1.ytd-watch-metadata');
      title = (h && h.textContent.trim()) || '';
    }
    if (!title) title = document.title.replace(/^\(\d+\)\s*/, '').replace(/\s*-\s*YouTube( Music)?$/, '').trim();
    return { url: `https://www.youtube.com/watch?v=${v}`, title };
  }

  async function send(next, btn) {
    const v = currentVideo();
    if (!v) { toast('這頁沒有影片', false); return; }
    btn.disabled = true;
    try {
      const res = await chrome.runtime.sendMessage({ type: 'add', url: v.url, title: v.title, next });
      toast(res && res.message ? res.message : '已送出', !res || res.status !== 'error');
    } catch (e) {
      toast('擴充功能已更新，請重新整理頁面', false);
    } finally { btn.disabled = false; }
  }

  function anchor() {
    // Shorts / 行動版沒有桌面版標題列 → 用右下角浮動按鈕
    if (isBili) {
      if (isBiliMobile) return document.body;
      return document.querySelector('h1.video-title, .video-title, .video-info-title, #viewbox_report') || document.body;
    }
    if (isShorts() || location.hostname === 'm.youtube.com') return document.body;
    if (isMusic) return document.querySelector('ytmusic-player-bar .middle-controls .content-info-wrapper');
    return document.querySelector('ytd-watch-metadata #title, #above-the-fold #title');
  }

  function ensureButton() {
    const onWatch = !!videoId();
    const old = document.getElementById(BTN_ID);
    if (!onWatch) { if (old) old.remove(); return; }
    const a = anchor();
    if (!a) return;
    if (old && a.contains(old) && old.classList.contains('floating') === (a === document.body)) return;
    if (old) old.remove();
    const wrap = document.createElement('span');
    wrap.id = BTN_ID;
    wrap.className = 'musbot-wrap' + (isMusic ? ' music' : '') + (a === document.body ? ' floating' : '');
    const add = document.createElement('button');
    add.type = 'button'; add.className = 'musbot-btn'; add.textContent = '🎵 加入機器人';
    add.title = '加入 Discord 音樂機器人的佇列（Shift+點 = 下一首播）';
    add.addEventListener('click', (e) => { e.stopPropagation(); send(e.shiftKey, add); });
    const nx = document.createElement('button');
    nx.type = 'button'; nx.className = 'musbot-btn ghost'; nx.textContent = '下一首播';
    nx.addEventListener('click', (e) => { e.stopPropagation(); send(true, nx); });
    const pk = document.createElement('button');
    pk.type = 'button'; pk.className = 'musbot-btn ghost'; pk.textContent = '📁 歌單';
    pk.title = '加到我的個人歌單';
    pk.addEventListener('click', (e) => { e.stopPropagation(); togglePicker(wrap, pk); });
    wrap.append(add, nx, pk);
    a.appendChild(wrap);
  }

  // ---------- 加到我的歌單：列出自己的歌單（用自己的金鑰）→ 點一份就加入目前影片 ----------
  const PICK_ID = 'musbot-picker';
  function closePicker() { const p = document.getElementById(PICK_ID); if (p) p.remove(); }
  function pickItem(text, cls, onClick) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'musbot-pick-item' + (cls ? ' ' + cls : ''); b.textContent = text;
    b.addEventListener('click', (e) => { e.stopPropagation(); onClick(b); });
    return b;
  }
  async function addToList(id, name) {
    const v = currentVideo();
    if (!v) { toast('這頁沒有影片', false); return; }
    const r = await chrome.runtime.sendMessage({ type: 'listAdd', id, url: v.url, title: v.title });
    if (r && r.status === 'duplicate') toast(`「${name}」裡已經有這首`, true);
    else toast(r && r.status === 'ok' ? `已加到「${name}」` : ((r && r.message) || '加入失敗'), !!(r && r.status === 'ok'));
  }
  async function togglePicker(wrap, btn) {
    if (document.getElementById(PICK_ID)) { closePicker(); return; }
    const box = document.createElement('div');
    box.id = PICK_ID; box.className = 'musbot-picker' + (wrap.classList.contains('floating') ? ' up' : '');
    box.addEventListener('click', (e) => e.stopPropagation());
    box.textContent = '讀取中…';
    wrap.appendChild(box);
    let r;
    try { r = await chrome.runtime.sendMessage({ type: 'lists' }); }
    catch (_) { box.textContent = '擴充功能已更新，請重新整理頁面'; return; }
    if (!document.getElementById(PICK_ID)) return;     // 等待中已關閉
    box.textContent = '';
    if (!r || r.status !== 'ok') { box.textContent = (r && r.message) || '讀取失敗'; return; }
    for (const pl of r.lists || []) {
      box.appendChild(pickItem(`${pl.name}（${pl.count}）`, '', async (b) => { b.disabled = true; closePicker(); await addToList(pl.id, pl.name); }));
    }
    if (!(r.lists || []).length) { const d = document.createElement('div'); d.className = 'musbot-pick-empty'; d.textContent = '還沒有歌單'; box.appendChild(d); }
    box.appendChild(pickItem('＋ 新增歌單並加入', 'new', async () => {
      const name = prompt('新歌單名稱'); if (!name || !name.trim()) return;
      closePicker();
      const c = await chrome.runtime.sendMessage({ type: 'listCreate', name: name.trim() });
      if (!c || c.status !== 'ok') { toast((c && c.message) || '建立失敗', false); return; }
      await addToList(c.list.id, c.list.name);
    }));
  }
  document.addEventListener('click', closePicker);
  document.addEventListener('yt-navigate-start', closePicker);

  chrome.runtime.onMessage.addListener((msg) => { if (msg && msg.type === 'toast') toast(msg.text, msg.ok); });
  document.addEventListener('yt-navigate-finish', () => setTimeout(ensureButton, 300));
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    setTimeout(() => { pending = false; ensureButton(); }, 800);
  }).observe(document.documentElement, { childList: true, subtree: true });
  ensureButton();
  if (typeof module !== 'undefined') module.exports = { videoId, currentVideo, ensureButton, togglePicker, closePicker };
})();
