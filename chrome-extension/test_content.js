// node test_content.js：用假 DOM 跑 content.js（watch / shorts / music / 非影片頁）
const assert = require('assert'); const vm = require('vm'); const fs = require('fs');
const src = fs.readFileSync(__dirname + '/content.js', 'utf8');
function el(tag) {
  return { tag, children: [], classList: { _s: new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)}, contains(c){return this._s.has(c)} },
    set className(v){ this._s && 0; v.split(' ').filter(Boolean).forEach(c=>this.classList.add(c)); this._cn=v }, get className(){return this._cn},
    get parentNode(){ return this.parent }, get firstChild(){ return this.children[0] || null },
    get previousSibling(){ if(!this.parent) return null; const i=this.parent.children.indexOf(this); return i>0?this.parent.children[i-1]:null },
    get previousElementSibling(){ return this.previousSibling },
    append(...k){ k.forEach(x=>{x.parent=this; this.children.push(x)}) }, appendChild(x){ this.append(x) },
    insertBefore(x, ref){ x.parent=this; const i=(ref==null)?this.children.length:this.children.indexOf(ref); this.children.splice(i<0?this.children.length:i, 0, x); return x },
    insertAdjacentElement(pos, x){ if(pos!=='afterend'||!this.parent) return null; this.parent.insertBefore(x, this.parent.children[this.parent.children.indexOf(this)+1]||null); if(!x.parent){ x.parent=this.parent; this.parent.children.push(x) } return x },
    contains(x){ let p=x; while(p){ if(p===this) return true; p=p.parent } return false },
    remove(){ if(this.parent){ this.parent.children=this.parent.children.filter(c=>c!==this); this.parent=null } },
    _l: {}, addEventListener(t, f){ (this._l[t] = this._l[t] || []).push(f) }, click(){ (this._l.click || []).forEach(f => f({ stopPropagation(){}, shiftKey: false })) }, textContent:'', getAttribute(n){ return (this._attrs||{})[n] || null }, setAttribute(n,v){ (this._attrs||(this._attrs={}))[n]=v } };
}
function matchSel(node, sel) {
  sel = sel.trim();
  if (!sel || !node) return false;
  if (sel.startsWith('#')) return node.id === sel.slice(1);
  const m = sel.match(/^([a-z0-9-]*)(\.[A-Za-z0-9_-]+)?(\[[^\]]+\])?(?:\s+(.+))?$/i);
  if (!m) return false;
  const tag = (m[1]||'').toLowerCase(), cls = m[2] ? m[2].slice(1) : '', rest = m[4];
  if (tag && node.tag !== tag) return false;
  if (cls && !(node.classList.contains(cls) || (node.className||'').split(' ').includes(cls))) return false;
  if (rest) return (node.children||[]).some(c => matchSel(c, rest));
  return true;
}
function qs(root, selector, fallbacks) {
  const parts = selector.split(',').map(s => s.trim()).filter(Boolean);
  const all = []; (function w(n){ all.push(n); (n.children||[]).forEach(w) })(root);
  for (const p of parts) {
    if (fallbacks) {
      if (p.includes('#title') && !p.includes('h1') && fallbacks.title) return fallbacks.title;
      if ((p.includes('h1.ytd') || p.includes('#title h1')) && fallbacks.h1) return fallbacks.h1;
    }
    const hit = all.find(n => matchSel(n, p.split(/\s+/).pop() === p ? p : p) || matchSel(n, p));
    if (hit) return hit;
    // last simple token (e.g. "ytd-watch-metadata #title" → #title id or .title)
    const last = p.split(/\s+/).pop();
    const hit2 = all.find(n => matchSel(n, last));
    if (hit2) return hit2;
  }
  return null;
}
function run(href, opts = {}, send = async () => ({})) {
  const body = el('body'), root = el('html'); root.append(body);
  const h1 = el('h1'); h1.textContent = opts.h1 || ''; h1.className = 'video-title';
  const title = el('div'); title.id = 'title';
  const toolbar = el('div'); toolbar.id = 'arc_toolbar_report'; toolbar.className = 'video-toolbar-container';
  const info = el('div'); info.className = 'video-info-container';
  const all = () => { const out=[]; (function w(n){ out.push(n); n.children.forEach(w) })(root); return out };
  if (opts.info) { if (opts.h1) info.append(h1); if (opts.toolbar) info.append(toolbar); body.append(info); }
  else {
    if (opts.h1) body.append(h1);
    if (opts.titleRow) body.append(title);
    if (opts.toolbar) body.append(toolbar);
  }
  const fallbacks = { h1: opts.h1 ? h1 : null, title: opts.titleRow ? title : null };
  const document = { documentElement: root, body, title: opts.docTitle || '',
    getElementById: id => all().find(n => n.id === id) || null,
    querySelector: q => qs(root, q, fallbacks),
    createElement: el, addEventListener(){} };
  const u = new URL(href);
  const ctx = { document, location: { href, hostname: u.hostname, pathname: u.pathname }, URL, setTimeout(){}, requestAnimationFrame(){},
    MutationObserver: class { observe(){} }, chrome: { runtime: { onMessage: { addListener(){} }, sendMessage: send } }, prompt: () => opts.prompt || null, module: {} };
  vm.runInNewContext(src, ctx);
  return { api: ctx.module.exports, btn: document.getElementById('musbot-add-btn'), body, title };
}
let r = run('https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1', { titleRow: true, h1: 'Song' });
assert.ok(r.btn && r.title.contains(r.btn)); assert.strictEqual(JSON.stringify(r.api.currentVideo()), JSON.stringify({ url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', title: "Song" }));
r = run('https://www.youtube.com/shorts/abcdefghijk', { docTitle: '(3) Funny clip - YouTube', h1: 'Stale old video title' });
assert.ok(r.btn && r.btn.classList.contains('floating'), 'shorts 要有浮動按鈕');
assert.strictEqual(JSON.stringify(r.api.currentVideo()), JSON.stringify({ url: 'https://www.youtube.com/watch?v=abcdefghijk', title: "Funny clip" }));
r = run('https://m.youtube.com/watch?v=dQw4w9WgXcQ', { docTitle: 'X - YouTube' });
assert.ok(r.btn && r.btn.classList.contains('floating'), '行動版要有浮動按鈕');
r = run('https://www.youtube.com/results?search_query=a', { titleRow: true });
assert.strictEqual(r.btn, null); assert.strictEqual(r.api.currentVideo(), null);

// Bilibili 影片頁：按鈕放工具列，不要插進標題 h1；網址含 BV 與指定分 P
r = run('https://www.bilibili.com/video/BV1rTy2zFE7f?spm_id_from=x', { h1: '洛天依', toolbar: true });
assert.ok(r.btn, 'B 站影片頁要有加入按鈕');
assert.ok(r.btn.classList.contains('bili-bar'), '桌面版要放在工具列');
assert.ok(!r.btn.classList.contains('floating'), '桌面版不要用右下角浮動');
assert.ok(!r.body.children.find(c => c.tag==='h1' && c.contains(r.btn)), '不可以插進標題 h1');
assert.strictEqual(JSON.stringify(r.api.currentVideo()), JSON.stringify({ url: 'https://www.bilibili.com/video/BV1rTy2zFE7f', title: '洛天依' }));
r = run('https://www.bilibili.com/video/BV1rTy2zFE7f?p=3', { h1: '第三集', toolbar: true });
assert.strictEqual(r.api.currentVideo().url, 'https://www.bilibili.com/video/BV1rTy2zFE7f?p=3');
r = run('https://www.bilibili.com/video/BV1rTy2zFE7f', { h1: '只有標題' });
assert.ok(r.btn && r.btn.classList.contains('bili-row'), '沒有工具列時要插在標題後面獨立一列');
assert.ok(!r.btn.classList.contains('floating'));
assert.strictEqual(r.btn.previousSibling && r.btn.previousSibling.textContent, '只有標題');
r = run('https://m.bilibili.com/video/BV1rTy2zFE7f', { h1: '行動版', docTitle: '行動版_哔哩哔哩_bilibili' });
assert.ok(r.btn && r.btn.classList.contains('floating'), 'B 站行動版要有浮動按鈕');
r = run('https://www.bilibili.com/video/av170001', { h1: '舊版av', toolbar: true });
assert.strictEqual(r.api.currentVideo().url, 'https://www.bilibili.com/video/av170001');
r = run('https://www.bilibili.com/', { h1: '首頁', toolbar: true });
assert.strictEqual(r.btn, null); assert.strictEqual(r.api.currentVideo(), null);

// YouTube 播放清單頁：只顯示「加入整個清單」，不是單曲按鈕
r = run('https://www.youtube.com/playlist?list=PLtest123', { h1: '我的歌單' });
assert.ok(r.btn && r.btn.classList.contains('musbot-list-only'), '清單頁要有清單按鈕');
assert.ok(r.btn.children.some(c => c.textContent.includes('加入整個清單')));
assert.strictEqual(r.api.currentList().url, 'https://www.youtube.com/playlist?list=PLtest123');
assert.strictEqual(r.api.currentVideo(), null);
r = run('https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLtest123', { titleRow: true, h1: 'Song' });
assert.ok(r.btn && !r.btn.classList.contains('musbot-list-only'), '影片頁即使帶 list= 仍是單曲按鈕');
assert.strictEqual(r.api.currentList(), null);
r = run('https://space.bilibili.com/123/channel/collectiondetail?sid=9', { h1: '合集' });
assert.ok(r.btn && r.btn.classList.contains('musbot-list-only'), 'B 站合集頁要有清單按鈕');
assert.ok(r.api.currentList() && r.api.currentList().url.includes('collectiondetail'));

(async () => {
  const sent = [];
  const send = async (m) => { sent.push(m); if (m.type === 'lists') return { status: 'ok', lists: [{ id: 'abc123def0', name: 'Chill', count: 3 }] };
    if (m.type === 'listCreate') return { status: 'ok', list: { id: 'fff000aaa1', name: m.name } }; return { status: 'ok' }; };
  const r = run('https://www.youtube.com/watch?v=dQw4w9WgXcQ', { titleRow: true, h1: 'Song', prompt: 'New one' }, send);
  const pickBtn = r.btn.children.find(c => c.textContent.includes('歌單'));
  assert.ok(pickBtn, '要有「歌單」按鈕');
  pickBtn.click(); await new Promise(z => setTimeout(z, 0)); await new Promise(z => setImmediate(z));
  const picker = r.btn.children.find(c => c.id === 'musbot-picker');
  assert.ok(picker, '點了要出現歌單選單');
  const items = picker.children;
  assert.ok(items[0].textContent.startsWith('Chill'));
  items[0].click(); await new Promise(z => setImmediate(z)); await new Promise(z => setImmediate(z));
  const add = sent.find(m => m.type === 'listAdd');
  assert.strictEqual(add.id, 'abc123def0'); assert.strictEqual(add.url, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'); assert.strictEqual(add.title, 'Song');
  // 新增歌單並加入
  pickBtn.click(); await new Promise(z => setImmediate(z)); await new Promise(z => setImmediate(z));
  const p2 = r.btn.children.find(c => c.id === 'musbot-picker');
  p2.children[p2.children.length - 1].click();
  for (let i = 0; i < 4; i++) await new Promise(z => setImmediate(z));
  assert.ok(sent.some(m => m.type === 'listCreate' && m.name === 'New one'));
  assert.strictEqual(sent.filter(m => m.type === 'listAdd').pop().id, 'fff000aaa1');
  // Shorts 也有歌單按鈕
  const s2 = run('https://www.youtube.com/shorts/abcdefghijk', { docTitle: 'Clip - YouTube' }, send);
  assert.ok(s2.btn.children.some(c => c.textContent.includes('歌單')));
  console.log('content.js OK');
})().catch(e => { console.error(e); process.exit(1); });
