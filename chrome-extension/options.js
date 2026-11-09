const tok = document.getElementById('tok'), msg = document.getElementById('msg');
chrome.storage.local.get('extToken').then(({ extToken }) => { if (extToken) tok.value = extToken; });
document.getElementById('save').onclick = async () => {
  await chrome.storage.local.set({ extToken: tok.value.trim() });
  msg.textContent = '測試連線中…'; msg.className = '';
  const s = await botApi('/api/ext/status');
  if (s.status === 'error') { msg.textContent = '❌ ' + s.message; msg.className = 'err'; }
  else { msg.textContent = `✅ 已連線${s.guild_name ? '：' + s.guild_name : ''}（佇列 ${s.queue_length} 首）${s.you ? '，身分：' + s.you : ''}`; msg.className = 'ok'; }
};
