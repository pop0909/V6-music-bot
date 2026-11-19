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
