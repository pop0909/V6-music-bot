importScripts('api.js');

const YT_LINKS = ['*://*.youtube.com/watch*', '*://youtu.be/*', '*://*.youtube.com/shorts/*'];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const [id, title] of [['add', '加入音樂機器人'], ['next', '下一首播']]) {
      chrome.contextMenus.create({ id: 'link-' + id, title, contexts: ['link'], targetUrlPatterns: YT_LINKS });
      chrome.contextMenus.create({ id: 'page-' + id, title, contexts: ['page', 'video'], documentUrlPatterns: YT_LINKS });
    }
  });
  chrome.storage.local.get('extToken').then(({ extToken }) => { if (!extToken) chrome.runtime.openOptionsPage(); });
});

async function addSong(url, title, next) {
  const body = { url, next: !!next };
  if (title) body.title = title;
  return botApi('/api/ext/add', body);
}

function cleanTitle(t) {
  return (t || '').replace(/^\(\d+\)\s*/, '').replace(/\s*-\s*YouTube( Music)?$/, '').trim();
}

async function toastInTab(tabId, res) {
  if (tabId == null || tabId < 0) return;
  const ok = res.status !== 'error';
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'toast', ok, text: res.message || (ok ? '已加入' : '失敗') });
  } catch (_) {
    // 不是 YouTube 頁（沒有 content script）→ 用 activeTab 臨時注入一個提示
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        args: [ok, res.message || (ok ? '已加入' : '失敗')],
        func: (ok, text) => {
          const d = document.createElement('div');
          d.textContent = (ok ? '🎵 ' : '⚠️ ') + text;
          d.style.cssText = 'position:fixed;z-index:2147483647;right:20px;bottom:20px;padding:10px 14px;border-radius:10px;' +
            'font:14px/1.4 system-ui,sans-serif;color:#fff;box-shadow:0 4px 16px rgba(0,0,0,.35);background:' + (ok ? '#6c5ce7' : '#d63031');
          document.documentElement.appendChild(d);
          setTimeout(() => d.remove(), 3500);
        },
      });
    } catch (_) {}
  }
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const next = info.menuItemId.endsWith('next');
  const isLink = info.menuItemId.startsWith('link-');
  const url = isLink ? info.linkUrl : (info.pageUrl || (tab && tab.url));
  const title = isLink ? '' : cleanTitle(tab && tab.title);
  const res = await addSong(url, title, next);
  toastInTab(tab && tab.id, res);
});

// content script（頁面上的按鈕）和 popup 透過這裡呼叫 API：擴充功能有 host 權限，不受頁面 CORS 限制
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === 'add') { addSong(msg.url, msg.title, msg.next).then(reply); return true; }
  if (msg && msg.type === 'status') { botApi('/api/ext/status').then(reply); return true; }
  // 個人歌單（金鑰決定是誰的歌單）
  if (msg && msg.type === 'lists') { botApi('/api/ext/lists').then(reply); return true; }
  if (msg && msg.type === 'listCreate') { botApi('/api/ext/lists', { name: msg.name }).then(reply); return true; }
  if (msg && msg.type === 'listAdd' && /^[0-9a-f]{6,20}$/.test(msg.id || '')) {
    botApi('/api/ext/lists/' + msg.id, { action: 'add', url: msg.url, title: msg.title }).then(reply); return true;
  }
  return false;
});
