// YouTube / YouTube Music / Bilibili：在標題旁放「加入機器人」按鈕（這些站都是單頁應用，換影片時重新放）
(() => {
  const BTN_ID = 'musbot-add-btn';
  // data-musbot-host / path / href：給模擬頁或測試覆寫網址（正式站沒有這些屬性）
  function loc() {
    const d = document.documentElement;
    const host = d.getAttribute && d.getAttribute('data-musbot-host');
    if (host) {
      const path = (d.getAttribute('data-musbot-path') || '/');
      return { hostname: host, pathname: path, href: d.getAttribute('data-musbot-href') || ('https://' + host + path) };
    }
    return { hostname: location.hostname, pathname: location.pathname, href: location.href };
  }
  const isMusic = loc().hostname === 'music.youtube.com';
  const isBili = /(^|\.)(bilibili\.com|b23\.tv)$/.test(loc().hostname);
  const isBiliMobile = loc().hostname === 'm.bilibili.com';

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
    const L = loc();
    if (isBili) {
      const m = L.pathname.match(/\/video\/(BV[1-9A-HJ-NP-Za-km-z]{10}|av\d+)/i);
      return m ? m[1] : null;
    }
    if (L.pathname === '/watch') return new URL(L.href).searchParams.get('v');
    const m = L.pathname.match(/^\/shorts\/([A-Za-z0-9_-]{11})/);
    return m ? m[1] : null;
  }
  const isShorts = () => loc().pathname.startsWith('/shorts/');

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
      const part = new URL(loc().href).searchParams.get('p');
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

  function currentList() {
    const L = loc();
    try {
      const u = new URL(L.href);
      const list = u.searchParams.get('list');
      if (/(^|\.)youtube\.com$/.test(u.hostname) && u.pathname === '/playlist' && list) {
        const base = u.hostname === 'music.youtube.com' ? 'https://music.youtube.com/playlist?list=' : 'https://www.youtube.com/playlist?list=';
        return { url: base + list };
      }
      if (isBili) {
        const p = u.pathname;
        if (/channel\/collectiondetail/i.test(p) || /\/lists\/\d+/.test(p) || /\/medialist\//.test(p) || /\/favlist/.test(p) || /(?:^|\/)ml\d+/i.test(p) || /\/list\//.test(p)) {
          return { url: u.href };
        }
      }
    } catch (_) {}
    return null;
  }

  async function send(next, btn) {
    const v = currentVideo();
    if (!v) { toast('這頁沒有影片', false); return; }
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '加入中…';
    try {
      const res = await chrome.runtime.sendMessage({ type: 'add', url: v.url, title: v.title, next });
      const ok = !res || res.status !== 'error';
      toast(res && res.message ? res.message : '已送出', ok);
      if (ok) {
        btn.textContent = next ? '已設為下一首' : '已加入';
        setTimeout(() => { if (btn.textContent === '已加入' || btn.textContent === '已設為下一首') btn.textContent = orig; }, 2000);
      } else btn.textContent = orig;
    } catch (e) {
      toast('擴充功能已更新，請重新整理頁面', false);
      btn.textContent = orig;
    } finally { btn.disabled = false; }
  }

  async function sendList(btn) {
    const lst = currentList();
    if (!lst) { toast('這頁沒有播放清單', false); return; }
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '讀取中…';
    try {
      const res = await chrome.runtime.sendMessage({ type: 'import', url: lst.url });
      if (!res || res.status === 'error' || !res.job) {
        toast((res && res.message) || '匯入失敗', false);
        return;
      }
      toast('正在讀取清單…', true);
      for (let i = 0; i < 90; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const s = await chrome.runtime.sendMessage({ type: 'importStatus', id: res.job });
        const job = (s && s.job) || {};
        if (!s || s.status === 'error' || job.status === 'error') {
          toast((s && s.message) || job.message || '匯入失敗', false);
          return;
        }
        if (job.status === 'done') {
          const extra = job.full_skipped ? `（${job.full_skipped} 首佇列已滿沒加進去）` : '';
          toast(`已加入 ${job.added || 0} 首${job.name ? '：「' + job.name + '」' : ''}${extra}`, true);
          btn.textContent = '已加入清單';
          setTimeout(() => { if (btn.textContent === '已加入清單') btn.textContent = orig; }, 2000);
          return;
        }
      }
      toast('讀取清單逾時，請到網頁面板查看佇列', false);
    } catch (e) {
      toast('擴充功能已更新，請重新整理頁面', false);
    } finally {
      if (btn.textContent === '讀取中…') btn.textContent = orig;
      btn.disabled = false;
    }
  }

  function anchor() {
    // Shorts / 行動版沒有桌面版標題列 → 用右下角浮動按鈕
    if (isBili) {
      if (isBiliMobile) return document.body;
      const mount = biliDesktopMount();
      return mount.host;
    }
    if (isShorts() || loc().hostname === 'm.youtube.com') return document.body;
    if (isMusic) return document.querySelector('ytmusic-player-bar .middle-controls .content-info-wrapper');
    return document.querySelector('ytd-watch-metadata #title, #above-the-fold #title');
  }

  // 桌面版 B 站：不要插進標題 h1（會折到標題下方、被播放器蓋住）。
  // 優先放進工具列（點讚／投幣那一排），否則插在標題後面當獨立一列，再不行才浮動。
  function biliDesktopMount() {
    const bar = document.querySelector('#arc_toolbar_report, .video-toolbar-container, .video-toolbar-left, .video-toolbar');
    if (bar) return { host: bar, mode: 'bar' };
    const title = document.querySelector('h1.video-title, h1[class*="video-title"], .video-info-title-inner h1, .video-info-title h1');
    if (title && title.parentNode) return { host: title, mode: 'after-title' };
    const info = document.querySelector('.video-info-container, .video-info, #viewbox_report');
    if (info) return { host: info, mode: 'info' };
    return { host: document.body, mode: 'floating' };
  }

  function biliStillMounted(old, mount) {
    if (!old) return false;
    const floating = old.classList.contains('floating');
    if (mount.mode === 'floating') return floating && (document.body.contains(old) || old.parent === document.body);
    if (floating) return false;
    if (mount.mode === 'after-title') return old.previousElementSibling === mount.host || old.previousSibling === mount.host;
    return mount.host.contains(old);
  }

  function placeWrap(wrap, mount) {
    const bits = ['musbot-wrap'];
    if (isMusic) bits.push('music');
    if (mount.mode === 'floating') bits.push('floating');
    if (mount.mode === 'bar') bits.push('bili-bar');
    if (mount.mode === 'after-title' || mount.mode === 'info') bits.push('bili-row');
    wrap.className = bits.join(' ');
    if (mount.mode === 'after-title') {
      mount.host.insertAdjacentElement('afterend', wrap);
    } else if (mount.mode === 'bar' && mount.host.firstChild) {
      mount.host.insertBefore(wrap, mount.host.firstChild);
    } else {
      mount.host.appendChild(wrap);
    }
  }

  function mountListButton(old) {
    const mobile = isBiliMobile || loc().hostname === 'm.youtube.com';
    const a = mobile ? document.body : (
      document.querySelector('ytd-playlist-header-renderer #title, ytmusic-detail-header-renderer, .playlist-info, .favlist-info, h1') || document.body
    );
    if (!a) return;
    const floating = a === document.body;
    if (old && a.contains(old) && old.classList.contains('musbot-list-only') && old.classList.contains('floating') === floating) return;
    if (old) old.remove();
    const wrap = document.createElement('span');
    wrap.id = BTN_ID;
    wrap.className = 'musbot-wrap musbot-list-only' + (floating ? ' floating' : '') + (isMusic ? ' music' : '');
    const add = document.createElement('button');
    add.type = 'button'; add.className = 'musbot-btn'; add.textContent = '📋 加入整個清單';
    add.title = '把這份播放清單或合集全部加入 Discord 音樂機器人的佇列';
    add.addEventListener('click', (e) => { e.stopPropagation(); sendList(add); });
    wrap.append(add);
    if (a !== document.body && a.parentNode && a.tagName === 'H1') a.insertAdjacentElement('afterend', wrap);
    else a.appendChild(wrap);
  }

  function ensureButton() {
    const onWatch = !!videoId();
    const lst = currentList();
    const old = document.getElementById(BTN_ID);
    if (!onWatch && !lst) { if (old) old.remove(); return; }
    if (!onWatch && lst) { mountListButton(old); return; }
    const biliDesk = isBili && !isBiliMobile;
    const mount = biliDesk ? biliDesktopMount() : null;
    const a = biliDesk ? mount.host : anchor();
    if (!a) return;
    if (biliDesk) {
      if (biliStillMounted(old, mount)) return;
    } else if (old && a.contains(old) && old.classList.contains('floating') === (a === document.body)) {
      return;
    }
    if (old) old.remove();
    const wrap = document.createElement('span');
    wrap.id = BTN_ID;
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
    if (biliDesk) placeWrap(wrap, mount);
    else {
      wrap.className = 'musbot-wrap' + (isMusic ? ' music' : '') + (a === document.body ? ' floating' : '');
      a.appendChild(wrap);
    }
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
  if (typeof module !== 'undefined') module.exports = { videoId, currentVideo, currentList, ensureButton, togglePicker, closePicker, send, sendList };
})();
