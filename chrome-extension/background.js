importScripts('api.js');

const YT_LINKS = ['*://*.youtube.com/watch*', '*://youtu.be/*', '*://*.youtube.com/shorts/*'];
const BILI_LINKS = ['*://*.bilibili.com/video/*', '*://m.bilibili.com/video/*', '*://b23.tv/*', '*://www.b23.tv/*'];
const PAGE_LINKS = YT_LINKS.concat(BILI_LINKS);
const LIST_LINKS = [
  '*://*.youtube.com/playlist?list=*',
  '*://music.youtube.com/playlist?list=*',
  '*://*.bilibili.com/*collectiondetail*',
  '*://*.bilibili.com/*/lists/*',
  '*://*.bilibili.com/medialist/*',
  '*://*.bilibili.com/list/*',
  '*://space.bilibili.com/*collectiondetail*',
  '*://space.bilibili.com/*/lists/*',
];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const [id, title] of [['add', '加入音樂機器人'], ['next', '下一首播']]) {
      chrome.contextMenus.create({ id: 'link-' + id, title, contexts: ['link'], targetUrlPatterns: PAGE_LINKS });
      chrome.contextMenus.create({ id: 'page-' + id, title, contexts: ['page', 'video'], documentUrlPatterns: PAGE_LINKS });
    }
    chrome.contextMenus.create({ id: 'link-list', title: '加入整個清單', contexts: ['link'], targetUrlPatterns: LIST_LINKS });
    chrome.contextMenus.create({ id: 'page-list', title: '加入整個清單', contexts: ['page'], documentUrlPatterns: LIST_LINKS });
  });
  chrome.storage.local.get('extToken').then(({ extToken }) => { if (!extToken) chrome.runtime.openOptionsPage(); });
});

async function addSong(url, title, next) {
  const body = { url, next: !!next };
  if (title) body.title = title;
  return botApi('/api/ext/add', body);
}

async function importList(url) {
  return botApi('/api/ext/import', { url });
}

async function toastInTab(tabId, res) {
  if (tabId == null || tabId < 0) return;
  const ok = res.status !== 'error';
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'toast', ok, text: res.message || (ok ? '已加入' : '失敗') });
  } catch (_) {
    // 不是影片頁（沒有 content script）→ 用 activeTab 臨時注入一個提示
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

async function pollImport(tabId, jobId) {
  for (let i = 0; i < 90; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (!/^[0-9a-f]{8,32}$/.test(jobId || '')) return;
    const s = await botApi('/api/ext/import/' + jobId);
    const job = s.job || {};
    if (s.status === 'error') { await toastInTab(tabId, s); return; }
    if (job.status === 'error') { await toastInTab(tabId, { status: 'error', message: job.message || '匯入失敗' }); return; }
    if (job.status === 'done') {
      const n = job.added || 0;
      const extra = job.full_skipped ? `（${job.full_skipped} 首佇列已滿沒加進去）` : '';
      await toastInTab(tabId, { status: 'ok', message: `已加入 ${n} 首${job.name ? '：「' + job.name + '」' : ''}${extra}` });
      return;
    }
    if (i === 0 || i % 3 === 2) {
      const d = job.done, t = job.total;
      const prog = t ? `（${d}/${t}）` : '';
      await toastInTab(tabId, { status: 'ok', message: '正在讀取清單…' + prog });
    }
  }
  await toastInTab(tabId, { status: 'error', message: '讀取清單逾時，請到網頁面板查看佇列' });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const isList = info.menuItemId.endsWith('list');
  const isLink = info.menuItemId.startsWith('link-');
  const url = isLink ? info.linkUrl : (info.pageUrl || (tab && tab.url));
  if (isList) {
    const res = await importList(url);
    if (res.status === 'error' || !res.job) { await toastInTab(tab && tab.id, res); return; }
    await toastInTab(tab && tab.id, { status: 'ok', message: '正在讀取清單…' });
    pollImport(tab && tab.id, res.job);
    return;
  }
  const next = info.menuItemId.endsWith('next');
  const title = isLink ? '' : cleanTitle(tab && tab.title);
  const res = await addSong(url, title, next);
  toastInTab(tab && tab.id, res);
});

chrome.commands.onCommand.addListener(async (command) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (command === 'add-current') {
    const v = tab && videoFrom(tab.url);
    if (!v) {
      await toastInTab(tab && tab.id, { status: 'error', message: '目前分頁不是 YouTube 或 B 站影片' });
      return;
    }
    const res = await addSong(v.url, cleanTitle(tab && tab.title), false);
    await toastInTab(tab && tab.id, res);
  } else if (command === 'skip-song') {
    const res = await botApi('/api/ext/control', { action: 'skip' });
    if (!res.message) res.message = res.status === 'error' ? '跳過失敗' : '已跳過';
    await toastInTab(tab && tab.id, res);
  }
});

// content script（頁面上的按鈕）和 popup 透過這裡呼叫 API：擴充功能有 host 權限，不受頁面 CORS 限制
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === 'add') { addSong(msg.url, msg.title, msg.next).then(reply); return true; }
  if (msg && msg.type === 'status') { botApi('/api/ext/status').then(reply); return true; }
  if (msg && msg.type === 'control') { botApi('/api/ext/control', msg.body || {}).then(reply); return true; }
  if (msg && msg.type === 'import') { importList(msg.url).then(reply); return true; }
  if (msg && msg.type === 'importStatus' && /^[0-9a-f]{8,32}$/.test(msg.id || '')) {
    botApi('/api/ext/import/' + msg.id).then(reply); return true;
  }
  // 個人歌單（金鑰決定是誰的歌單）
  if (msg && msg.type === 'lists') { botApi('/api/ext/lists').then(reply); return true; }
  if (msg && msg.type === 'listCreate') { botApi('/api/ext/lists', { name: msg.name }).then(reply); return true; }
  if (msg && msg.type === 'listAdd' && /^[0-9a-f]{6,20}$/.test(msg.id || '')) {
    botApi('/api/ext/lists/' + msg.id, { action: 'add', url: msg.url, title: msg.title }).then(reply); return true;
  }
  return false;
});
