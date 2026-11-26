// 共用：呼叫機器人的擴充功能 API（金鑰存在 chrome.storage.local，不同步到雲端）
const BOT_BASE = 'https://musbot.kdns.fr';

async function botApi(path, body) {
  const { extToken } = await chrome.storage.local.get('extToken');
  if (!extToken) return { status: 'error', message: '尚未設定金鑰：請到擴充功能「選項」貼上擁有者給你的金鑰' };
  try {
    const r = await fetch(BOT_BASE + path, {
      method: body ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json', 'X-Ext-Token': extToken },
      body: body ? JSON.stringify(body) : undefined,
    });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (!r.ok && !j.message) j.message = r.status >= 500 ? '機器人暫時沒有回應，請稍後再試' : (r.status === 429 ? '操作太頻繁，請稍等一下再試' : '操作失敗，請稍後再試');
    if (!r.ok) j.status = 'error';
    return j;
  } catch (e) {
    return { status: 'error', message: '連不到機器人，請檢查網路後再試' };   // 不顯示英文的 Failed to fetch
  }
}

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

// 播放清單／合集頁（不含「影片頁附帶的 list=」：那種仍算單曲）
function listFrom(url) {
  try {
    const u = new URL(url);
    const host = u.hostname;
    const list = u.searchParams.get('list');
    if (/(^|\.)youtube\.com$/.test(host) && u.pathname === '/playlist' && list) {
      const base = host === 'music.youtube.com' ? 'https://music.youtube.com/playlist?list=' : 'https://www.youtube.com/playlist?list=';
      return { url: base + list, kind: 'youtube-list' };
    }
    if (/(^|\.)bilibili\.com$/.test(host)) {
      const p = u.pathname;
      if (/channel\/collectiondetail/i.test(p) || /\/lists\/\d+/.test(p) || /\/medialist\//.test(p) || /\/favlist/.test(p) || /(?:^|\/)ml\d+/i.test(p) || /\/list\//.test(p)) {
        return { url: u.href, kind: 'bili-list' };
      }
    }
  } catch (_) {}
  return null;
}

function cleanTitle(t) {
  return (t || '').replace(/^\(\d+\)\s*/, '').replace(/\s*-\s*YouTube( Music)?$/, '').replace(/[\s_]*哔哩哔哩.*$/, '').replace(/\s*-\s*bilibili$/i, '').trim();
}
