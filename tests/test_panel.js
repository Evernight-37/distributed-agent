// Minimal vm smoke test for the browser console. No browser or network is used.
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(require('path').join(__dirname, '..', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const ids = ['error','health','nodes','projects','jobs','detail','refresh','submit','taskProject','taskStage','taskText','allowed','acceptance','validation','cfgProject','cfgModel','cfgEffort','cfgTimeout','configure','regNode','regName','regLocal','regRemote','regModel','regEffort','regTimeout','register','msgProject','msgText','message','memProject','memKind','memText','memEvidence','remember'];
class Element {
  constructor(tag='div') { this.tagName=tag; this.value=''; this.textContent=''; this.children=[]; this.firstChild=null; this.className=''; this.onclick=null; this.parentNode=null; }
  appendChild(x) { this.children.push(x); this.firstChild=this.children[0]; x.parentNode=this; return x; }
  append(...xs) { xs.forEach(x=>this.appendChild(x)); }
  removeChild(x) { this.children=this.children.filter(y=>y!==x); this.firstChild=this.children[0]||null; }
}
const elements = Object.fromEntries(ids.map(id=>[id,new Element(id==='cfgEffort'||id.endsWith('Project')||id.endsWith('Stage')?'select':'div')]));
const calls=[];
const document = { getElementById:id=>elements[id], createElement:tag=>new Element(tag), createTextNode:text=>new Element('text') };
let fetchCount=0;
async function fetch(path, options={}) {
  fetchCount++;
  if (path === '/api/session') return {ok:true, json:async()=>({token:'test-token'})};
  if (path === '/api/snapshot') return {ok:true, json:async()=>({nodes:[{name:'lab',host:'100.0.0.1'}],projects:[{name:'demo',model:'m',effort:'medium'}],jobs:[],capabilities:{}})};
  if (path === '/api/job?id=job-1') return {ok:true, json:async()=>({status:'needs_review'})};
  calls.push({path, options});
  return {ok:true, json:async()=>({message:'ok'})};
}
const context={document,fetch,setInterval:()=>0,setTimeout:()=>0,encodeURIComponent,console};
vm.runInNewContext(script, context, {filename:'index.html'});
setImmediate(async()=>{
  await new Promise(r=>setImmediate(r));
  assert(fetchCount >= 2, 'session and snapshot should initialize');
  assert.strictEqual(typeof elements.submit.onclick, 'function');
  elements.taskText.value='task'; elements.acceptance.value='accept'; elements.validation.value='python -V';
  await elements.submit.onclick();
  assert(calls.some(c=>c.path==='/api/action' && JSON.parse(c.options.body).action==='submit'));
  const probe = elements.nodes.children.find(x=>x.children.some(y=>y.textContent==='探测'));
  assert(probe, 'render should create probe button');
  const probeButton = probe.children.find(x=>typeof x.onclick==='function');
  await probeButton.onclick({stopPropagation(){}});
  assert(calls.some(c=>c.path==='/api/action' && JSON.parse(c.options.body).action==='probe'));
  console.log('panel vm smoke: ok');
});
