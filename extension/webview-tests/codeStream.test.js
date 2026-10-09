const test=require('node:test'),assert=require('node:assert/strict');
const {readCodeStream,GenerationPausedError,compactCodeEvent}=require('../out/core/codeStream');
const {ApiClient}=require('../out/core/ApiClient');
function response(text,{close=true}={}){const bytes=new TextEncoder().encode(text);return new Response(new ReadableStream({start(c){for(const b of bytes)c.enqueue(Uint8Array.of(b));if(close)c.close();}}));}

test('대규모 생성 스트림: 진행 이벤트(한글 바이트 쪼개짐·심장박동) 뒤 결과로 끝난다',async()=>{
 const events=[];
 const result=await readCodeStream(response(': heartbeat\n\ndata: {"step":"planned","job_id":"abcdef123456","total":3,"files":[{"file":"a.js","layer":1}]}\n\ndata: {"step":"file_done","agent":"agent-2","file":"a.js","done_count":1,"total":3}\r\n\r\ndata: {"step":"done","result":{"summary":"완료","ops":[]}}\n\n'),e=>events.push(e),()=>{});
 assert.equal(result.summary,'완료');assert.deepEqual(events.map(e=>e.step),['planned','file_done','done']);assert.equal(events[1].agent,'agent-2');
});

test('멈춘 생성은 작업 ID 와 진행 정도를 담은 일시 정지 오류가 된다',async()=>{
 await assert.rejects(readCodeStream(response('data: {"step":"planned","job_id":"abcdef123456"}\n\ndata: {"step":"error","message":"AI 사용 한도","resumable":true,"resume_job":"abcdef123456","done_count":4,"total":9}\n\n'),()=>{},()=>{}),
  e=>e instanceof GenerationPausedError&&e.jobId==='abcdef123456'&&e.done===4&&e.total===9&&/AI 사용 한도/.test(e.message));
 await assert.rejects(readCodeStream(response('data: {"step":"error","message":"승인 없음"}\n\n'),()=>{},()=>{}),e=>!(e instanceof GenerationPausedError)&&/승인 없음/.test(e.message));
});

test('작업이 시작된 뒤 연결이 끊기면 실패가 아니라 이어서 받을 수 있는 일시 정지',async()=>{
 await assert.rejects(readCodeStream(response('data: {"step":"file_done","job_id":"abcdef123456"}\n\n'),()=>{},()=>{}),
  e=>e instanceof GenerationPausedError&&e.jobId==='abcdef123456');
 await assert.rejects(readCodeStream(response(''),()=>{},()=>{}),e=>!(e instanceof GenerationPausedError));
});

test('아무 데이터도 없이 오래 멈추면 끊고 이어서 받게 한다(전체 시간 상한은 없음)',async()=>{
 let abort;const stream=new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('data: {"step":"planned","job_id":"abcdef123456"}\n\n'));abort=()=>c.error(new Error('aborted'));}});
 await assert.rejects(readCodeStream(new Response(stream),()=>{},()=>abort(),30),e=>e instanceof GenerationPausedError);
});

test('ApiClient: 스트림 경로로 mode·이어 만들기 작업을 보내고, 구버전 코어(404)면 예전 경로로 물러선다',async t=>{
 const old=global.fetch;t.after(()=>global.fetch=old);const seen=[];
 const api=new ApiClient({getSessionToken:()=>'tok',getPort:()=>12345,refreshToken:async()=>{}});
 global.fetch=async(url,options)=>{seen.push([url,JSON.parse(options.body||'{}')]);return response('data: {"step":"done","result":{"summary":"ok","ops":[],"model":"m"}}\n\n');};
 const r=await api.generateCodeStream('쇼핑몰',{mode:'team',resumeJob:'abcdef123456',agents:4,decisions:[]},()=>{});
 assert.equal(r.summary,'ok');assert.match(seen[0][0],/\/api\/code\/generate\/stream$/);
 assert.equal(seen[0][1].mode,'team');assert.equal(seen[0][1].resume_job,'abcdef123456');assert.equal(seen[0][1].agents,4);
 global.fetch=async()=>new Response('',{status:404});
 api.generateCode=async(instruction)=>({summary:'legacy:'+instruction,ops:[],model:'m'});
 const legacy=await api.generateCodeStream('쇼핑몰',{},()=>{});
 assert.equal(legacy.summary,'legacy:쇼핑몰');
});

test('웹뷰로는 파일 내용 전체(result)를 빼고 보낸다',()=>{
 const c=compactCodeEvent({step:'done',result:{ops:[{content:'x'.repeat(10)}]},message:'끝'});
 assert.equal(c.result,undefined);assert.equal(c.message,'끝');
});
