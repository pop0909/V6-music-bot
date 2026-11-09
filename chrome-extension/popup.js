const $ = (id) => document.getElementById(id);
let tabVideo = null;

function videoFrom(url) {
  try {
    const u = new URL(url);
    if (u.hostname === 'youtu.be') return u.pathname.slice(1, 12) || null;
    if (/(^|\.)youtube\.com$/.test(u.hostname)) {
      if (u.pathname === '/watch') return u.searchParams.get('v');
      const m = u.pathname.match(/^\/shorts\/([\w-]{11})/); if (m) return m[1];
    }
  } catch (_) {}
  return null;
}

async function refresh() {
  const s = await botApi('/api/ext/status');
  if (s.status === 'error') { $('np').textContent = s.message; $('np').className = 'np err'; $('meta').textContent = ''; return; }
  $('np').className = 'np' + (s.current_title ? '' : ' muted');
  $('np').textContent = s.current_title ? (s.is_paused ? '⏸️ ' : '🎶 ') + s.current_title : (s.connected ? '⏹️ 待機中' : '💤 休眠中');
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
    tabVideo = { url: `https://www.youtube.com/watch?v=${v}`, title: (tab.title || '').replace(/^\(\d+\)\s*/, '').replace(/\s*-\s*YouTube( Music)?$/, '') };
    $('add').disabled = $('next').disabled = false;
  } else {
    $('add').title = $('next').title = '目前分頁不是 YouTube 影片';
  }
  $('add').onclick = () => add(false);
  $('next').onclick = () => add(true);
  $('opt').onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
  refresh();
})();
