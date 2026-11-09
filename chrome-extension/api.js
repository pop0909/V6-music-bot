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
    if (!r.ok && !j.message) j.message = `伺服器回應 ${r.status}`;
    if (!r.ok) j.status = 'error';
    return j;
  } catch (e) {
    return { status: 'error', message: '連不到機器人（' + (e.message || e) + '）' };
  }
}
