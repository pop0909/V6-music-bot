const $ = (id) => document.getElementById(id);
let tabVideo = null;
let tabList = null;
let lastStatus = null;
let pollTimer = null;

function setMsg(text, cls) {
  $('msg').textContent = text || '';
  $('msg').className = cls || '';
}

function render(s) {
  lastStatus = s;
  if (!s || s.status === 'error') {
    $('np').textContent = (s && s.message) || '讀取失敗';
    $('np').className = 'np err';
    $('meta').textContent = '';
    $('art').style.display = 'none';
    $('toggle').disabled = $('skip').disabled = $('vol').disabled = true;
    return;
  }
  $('np').className = 'np' + (s.current_title ? '' : ' muted');
  $('np').textContent = s.current_title ? ((s.is_paused ? '⏸️ ' : '🎶 ') + s.current_title) : (s.connected ? '⏹️ 待機中' : (s.asleep ? '💤 休眠中' : '💤 不在語音頻道'));
  $('meta').textContent = `佇列 ${s.queue_length} 首` + (s.voice_channel ? ` · 🔊 ${s.voice_channel}` : '') + (s.guild_name ? ` · ${s.guild_name}` : '') + (s.you ? ` · 我是 ${s.you}` : '');
  const has = !!(s.thumbnail && s.current_title);
  $('art').style.display = has ? 'block' : 'none';
  if (has) $('art').src = s.thumbnail;
  const el = Number(s.elapsed) || 0, dur = Number(s.duration) || 0;
  $('elapsed').textContent = s.elapsed_str || '0:00';
  $('duration').textContent = dur ? (s.duration_str || '') : '';
  $('barFill').style.width = dur ? Math.min(100, (el / dur) * 100) + '%' : '0';
  const live = !!(s.connected && (s.is_playing || s.is_paused || s.current_title));
  $('toggle').disabled = !s.connected;
  $('skip').disabled = !live;
  $('vol').disabled = false;
  if (document.activeElement !== $('vol')) $('vol').value = String(s.volume != null ? s.volume : 100);
  $('toggle').textContent = s.is_paused || !s.is_playing ? '▶️' : '⏸️';
}

async function refresh() {
  const s = await botApi('/api/ext/status');
  render(s);
}

async function add(next) {
  if (!tabVideo) return;
  $('add').disabled = $('next').disabled = true;
  const res = await chrome.runtime.sendMessage({ type: 'add', url: tabVideo.url, title: tabVideo.title, next });
  setMsg(res.message || (res.status === 'error' ? '失敗' : '已加入'), res.status === 'error' ? 'err' : 'ok');
  $('add').disabled = $('next').disabled = false;
  refresh();
}

async function addList() {
  if (!tabList) return;
  $('addList').disabled = true;
  setMsg('正在讀取清單…', 'muted');
  const res = await chrome.runtime.sendMessage({ type: 'import', url: tabList.url });
  if (res.status === 'error' || !res.job) {
    setMsg(res.message || '匯入失敗', 'err');
    $('addList').disabled = false;
    return;
  }
  for (let i = 0; i < 90; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    const s = await chrome.runtime.sendMessage({ type: 'importStatus', id: res.job });
    const job = (s && s.job) || {};
    if (!s || s.status === 'error' || job.status === 'error') {
      setMsg((s && s.message) || job.message || '匯入失敗', 'err');
      break;
    }
    if (job.status === 'done') {
      const extra = job.full_skipped ? `（${job.full_skipped} 首佇列已滿沒加進去）` : '';
      setMsg(`已加入 ${job.added || 0} 首${job.name ? '：「' + job.name + '」' : ''}${extra}`, 'ok');
      break;
    }
    if (job.total) setMsg(`正在讀取清單…（${job.done || 0}/${job.total}）`, 'muted');
  }
  $('addList').disabled = false;
  refresh();
}

async function control(action, extra) {
  const body = Object.assign({ action }, extra || {});
  const res = await chrome.runtime.sendMessage({ type: 'control', body });
  if (res && res.status === 'error') setMsg(res.message || '操作失敗', 'err');
  await refresh();
}

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const v = tab && videoFrom(tab.url);
  const lst = tab && listFrom(tab.url);
  if (v) {
    tabVideo = { url: v.url, title: cleanTitle(tab.title || '') };
    $('add').disabled = $('next').disabled = false;
  } else if (lst) {
    tabList = lst;
    $('listRow').style.display = 'flex';
    setMsg('這個分頁是播放清單，可以整份加入佇列', 'muted');
  } else {
    $('add').title = $('next').title = '目前分頁不是 YouTube 或 Bilibili 影片';
    setMsg('打開 YouTube 或 B 站影片頁面後就能加入', 'muted');
  }
  if (v && lst) {
    tabList = lst;
    $('listRow').style.display = 'flex';
  }
  $('add').onclick = () => add(false);
  $('next').onclick = () => add(true);
  $('addList').onclick = () => addList();
  $('toggle').onclick = () => control('toggle');
  $('skip').onclick = () => control('skip');
  $('vol').addEventListener('change', () => control('volume', { level: Number($('vol').value) }));
  $('opt').onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
  await refresh();
  pollTimer = setInterval(refresh, 1000);
  window.addEventListener('unload', () => clearInterval(pollTimer));
})();
