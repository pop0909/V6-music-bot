// node test_content.js：用假 DOM 跑 content.js（watch / shorts / music / 非影片頁）
const assert = require('assert'); const vm = require('vm'); const fs = require('fs');
const src = fs.readFileSync(__dirname + '/content.js', 'utf8');
function el(tag) {
  return { tag, children: [], classList: { _s: new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)}, contains(c){return this._s.has(c)} },
    set className(v){ v.split(' ').filter(Boolean).forEach(c=>this.classList.add(c)); this._cn=v }, get className(){return this._cn},
    append(...k){ k.forEach(x=>{x.parent=this; this.children.push(x)}) }, appendChild(x){ this.append(x) },
    contains(x){ let p=x; while(p){ if(p===this) return true; p=p.parent } return false },
    remove(){ if(this.parent){ this.parent.children=this.parent.children.filter(c=>c!==this); this.parent=null } },
    _l: {}, addEventListener(t, f){ (this._l[t] = this._l[t] || []).push(f) }, click(){ (this._l.click || []).forEach(f => f({ stopPropagation(){}, shiftKey: false })) }, textContent:'' };
}
function run(href, opts = {}, send = async () => ({})) {
  const body = el('body'), root = el('html'); root.append(body);
  const h1 = el('h1'); h1.textContent = opts.h1 || ''; const title = el('div');
  const all = () => { const out=[]; (function w(n){ out.push(n); n.children.forEach(w) })(root); return out };
  if (opts.titleRow) body.append(title);
  const document = { documentElement: root, body, title: opts.docTitle || '',
    getElementById: id => all().find(n => n.id === id) || null,
    querySelector: q => (q.includes('h1') ? (opts.h1 ? h1 : null) : (q.includes('#title') && opts.titleRow ? title : null)),
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
