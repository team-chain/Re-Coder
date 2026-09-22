const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {layoutProject,layoutGrid} = require('../out/webview-test/components/CodeMap.js');
const {analyzeFile,analyzeProject} = require('../out/codemap/analyzer.js');
const node = (id,layer='service')=>({id,name:id,module:id,layer,in_degree:0,out_degree:0,flags:[]});

function spaced(layout,width){
 const positions=[...layout.pos.values()];
 for(const p of positions){assert.ok(p.x>=85&&p.x<=width-85);assert.ok(p.y>=30&&p.y+30<=layout.height);}
 for(let i=0;i<positions.length;i++)for(let j=i+1;j<positions.length;j++){
  const a=positions[i],b=positions[j];assert.ok(Math.abs(a.x-b.x)>=185||Math.abs(a.y-b.y)>=78,`overlap: ${JSON.stringify([a,b])}`);
 }
}
for(const width of [320,540,960]) test(`project nodes wrap without overlap at width ${width}`,()=>{
 const nodes=[...Array.from({length:18},(_,i)=>node(`service${i}`)),node('entry','entry'),node('data','data'),node('unknown','custom')];
 const layout=layoutProject(nodes,width);
 assert.equal(layout.pos.size,nodes.length);spaced(layout,width);
 assert.ok(new Set(nodes.filter(n=>n.layer==='service').map(n=>layout.pos.get(n.id).y)).size>1);
 assert.ok(layout.pos.get('data').y>Math.max(...nodes.filter(n=>n.layer==='service').map(n=>layout.pos.get(n.id).y)));
 assert.equal(layout.bands.length,4);
});
test('function grid reserves a full chip width even in a narrow sidebar',()=>{
 spaced(layoutGrid(Array.from({length:15},(_,i)=>node(String(i))),320),320);
 assert.ok(layoutProject([],320).height>=180);
});
function analyze(src){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-map-'));try{const file=path.join(dir,'app.js');fs.writeFileSync(file,src);return analyzeFile(file);}finally{fs.rmSync(dir,{recursive:true,force:true});}}
function project(manifest, files){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'recoder-map-entry-'));
 try{
  if(manifest!==undefined)fs.writeFileSync(path.join(dir,'package.json'),typeof manifest==='string'?manifest:JSON.stringify(manifest));
  for(const [rel,content] of Object.entries(files)){const target=path.join(dir,rel);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,content);}
  return analyzeProject(dir);
 }finally{fs.rmSync(dir,{recursive:true,force:true});}
}
test('declared sample app entry is not orphaned, while unrelated fixtures remain visible',()=>{
 const graph=project({main:'./src/app.js',scripts:{start:'node src/app.js'}},{'src/app.js':"require('./helper');",'src/helper.js':'module.exports = 1;','a4-warning-test.js':'module.exports = {};'});
 const app=graph.nodes.find(n=>n.id==='src/app.js');
 assert.equal(app.layer,'entry');assert.ok(!app.flags.includes('orphan'));
 assert.ok(graph.edges.some(e=>e.from==='src/app.js'&&e.to==='src/helper.js'));
 assert.ok(graph.nodes.find(n=>n.id==='a4-warning-test.js').flags.includes('orphan'));
 assert.ok(!graph.findings.some(f=>f.node==='src/app.js'));
 assert.ok(graph.findings.every(f=>!f.fix.includes('삭제')));
});
test('direct quoted scripts and package bin are entries without executing scripts',()=>{
 const graph=project({scripts:{start:'node "src/server app.js"',dev:'tsx src/dev.ts',lint:'node --require ./helper.js tool.js'},bin:{sample:'./cli.js'}},{'src/server app.js':'','src/dev.ts':'','cli.js':'','helper.js':'','other.js':''});
 for(const id of ['src/server app.js','src/dev.ts','cli.js'])assert.equal(graph.nodes.find(n=>n.id===id).layer,'entry');
 assert.ok(graph.nodes.find(n=>n.id==='helper.js').flags.includes('orphan'));
});
test('malformed metadata and unsafe paths do not invent entrypoints or drop files',()=>{
 for(const manifest of ['{broken',null,{main:'../src/app.js',bin:'/src/app.js',scripts:{start:'echo node src/app.js',bad:42}}]){
  const graph=project(manifest,{'src/app.js':'','other.js':''});
  assert.equal(graph.files_scanned,2);
  assert.ok(graph.nodes.find(n=>n.id==='src/app.js').flags.includes('orphan'));
 }
 const graph=project({name:'default-entry'},{'index.js':'','other.js':''});
 assert.equal(graph.nodes.find(n=>n.id==='index.js').layer,'entry');
});
test('Express anonymous routes are visible and link to named helper calls',()=>{
 const graph=analyze(`function listTodos(){ return []; }
app.get('/todos', (req,res) => { res.json(listTodos()); });
app.post('/todos', async function(req,res) { res.json(listTodos()); });
app.get('/health', (req,res) => res.json({ok:true}));`);
 assert.equal(graph.functions_scanned,4);
 assert.ok(graph.nodes.some(n=>n.name==='GET /health · 익명'));
 for(const method of ['GET /todos · 익명','POST /todos · 익명']){
  const route=graph.nodes.find(n=>n.name===method);assert.ok(route);assert.ok(graph.edges.some(e=>e.from===route.id&&e.to==='listTodos'));
 }
});
test('named arrows are not counted twice; same-line anonymous callbacks have distinct identities',()=>{
 const graph=analyze(`const named = (x) => x; const other = function(x) { return x; }; items.map(x => named(x)); items.map(x => named(x));`);
 assert.equal(graph.functions_scanned,4);
 assert.equal(graph.nodes.filter(n=>n.name==='named').length,1);
 assert.equal(graph.nodes.filter(n=>n.name==='other').length,1);
 const anonymous=graph.nodes.filter(n=>n.id.startsWith('anonymous@'));
 assert.equal(anonymous.length,2);assert.notEqual(anonymous[0].id,anonymous[1].id);
});
test('comments and strings cannot invent callbacks, and expression bodies do not consume the next route',()=>{
 const graph=analyze(`// app.get('/fake', (x) => missing())
const text = "(req,res)=>missing()";
function one(){} function two(){}
app.get('/a', x => one());
app.get('/b', x => two());`);
 const a=graph.nodes.find(n=>n.name==='GET /a · 익명');const b=graph.nodes.find(n=>n.name==='GET /b · 익명');
 assert.equal(graph.functions_scanned,4);
 assert.deepEqual(graph.edges.filter(e=>e.from===a.id).map(e=>e.to),['one']);
 assert.deepEqual(graph.edges.filter(e=>e.from===b.id).map(e=>e.to),['two']);
});
