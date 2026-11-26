const $ = (id) => document.getElementById(id);
let tabVideo = null;

function videoFrom(url) {
  try {
    const u = new URL(url);
    if (u.hostname === 'youtu.be') {
      const id = u.pathname.slice(1).split('/')[0];
      return id ? { url: 'https://www.youtube.com/watch?v=' + id, kind: 'youtube' } : null;
    }
    if (/(^|\.)youtube\.com$/.test(u.hostname)) {
      let id = null;
      if (u.pathname === '/watch') id = u.searchParams.get('v');
      const m = u.pathname.match(/^\/shorts\/([\w-]{11})/);
      if (m) id = m[1];
      return id ? { url: 'https://www.youtube.com/watch?v=' + id, kind: 'youtube' } : null;
    }
    if (/(^|\.)(bilibili\.com|b23\.tv)$/.test(u.hostname)) {
      const m = u.pathname.match(/\/video\/(BV[1-9A-HJ-NP-Za-km-z]{10}|av\d+)/i);
      if (!m && /(^|\.)b23\.tv$/.test(u.hostname)) return { url: u.href, kind: 'bilibili' };
      if (!m) return null;
      let href = 'https://www.bilibili.com/video/' + m[1];
      const part = u.searchParams.get('p');
      if (part && part !== '1') href += '?p=' + part;
      return { url: href, kind: 'bilibili' };
    }
  } catch (_) {}
  return null;
}

async function refresh() {
  const s = await botApi('/api/ext/status');
  if (s.status === 'error') { $('np').textContent = s.message; $('np').className = 'np err'; $('meta').textContent = ''; return; }
  $('np').className = 'np' + (s.current_title ? '' : ' muted');
  $('np').textContent = s.current_title ? (s.is_paused ? '⏸️ ' : '🎶 ') + s.current_title : (s.connected ? '⏹️ 待機中' : (s.asleep ? '💤 休眠中' : '💤 不在語音頻道'));
  $('meta').textContent = `佇列 ${s.queue_length} 首` + (s.voice_channel ? ` · 🔊 ${s.voice_channel}` : '') + (s.guild_name ? ` · ${s.guild_name}` : '') + (s.you ? ` · 我是 ${s.you}` : '');
}

async function add(next) {
  if (!tabVideo) return;
  $('add').disabled = $('next').disabled = true;
  const res = await chrome.runtime.sendMessage({ type: 'add', url: tabVideo.url, title: tabVideo.title, next });
  $('msg').textContent = res.message || (res.status === 'error' ? '失敗' : '已加入');
  $('msg').className = res.status === 'error' ? 'err' : 'ok';
  $('add').disabled = $('next').disabled = false;
  refresh();
}

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const v = tab && videoFrom(tab.url);
  if (v) {
    let title = tab.title || '';
    title = title.replace(/^\(\d+\)\s*/, '').replace(/\s*-\s*YouTube( Music)?$/, '').replace(/[\s_]*哔哩哔哩.*$/, '').replace(/\s*-\s*bilibili$/i, '').trim();
    tabVideo = { url: v.url, title };
    $('add').disabled = $('next').disabled = false;
  } else {
    $('add').title = $('next').title = '目前分頁不是 YouTube 或 Bilibili 影片';
    $('msg').textContent = '打開 YouTube 或 B 站影片頁面後就能加入';   // 按鈕灰掉時說明原因（title 要滑過才看得到）
    $('msg').className = 'muted';
  }
  $('add').onclick = () => add(false);
  $('next').onclick = () => add(true);
  $('opt').onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
  refresh();
})();
