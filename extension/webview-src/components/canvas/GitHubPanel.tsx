import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useVSCodeApi } from '../../hooks/useVSCodeApi';
import { Snapshot } from './model';

export function GitHubPanel({ workspace, git, onChanged, onReview, onWorkflow, disabled }: {
  workspace:string; git:Snapshot['git']; onChanged:(git:Snapshot['git'])=>void;
  onReview:()=>void; onWorkflow:()=>void; disabled:boolean;
}) {
  const {postMessage,useMessage}=useVSCodeApi();
  const [auth,setAuth]=useState<{status:string;user?:string;message?:string}|null>(null);
  const [repos,setRepos]=useState<Array<{name:string;private:boolean}>>([]);
  const [repository,setRepository]=useState(''),[create,setCreate]=useState(false);
  const [changing,setChanging]=useState(false);
  const [pending,setPending]=useState(''),[error,setError]=useState(''),[message,setMessage]=useState('');
  const [review,setReview]=useState<{id:string;files:Array<{path:string;status:string;blocked:string}>}|null>(null);
  const [selected,setSelected]=useState<string[]>([]),[commitMessage,setCommitMessage]=useState('프로젝트 변경 저장');
  const [author,setAuthor]=useState(''),[email,setEmail]=useState('');
  const request=useRef(''),timer=useRef<ReturnType<typeof setTimeout>>();
  const send=useCallback((action:string,extra:Record<string,unknown>={})=>{
    clearTimeout(timer.current);
    const id=`github-${Date.now()}-${Math.random()}`;request.current=id;
    setPending(action);setError('');setMessage('');
    timer.current=setTimeout(()=>{if(request.current!==id)return;request.current='';setPending('');setError('응답이 지연되고 있습니다. VS Code 로그인 알림과 Core 연결을 확인한 뒤 상태를 새로고침하세요.');},action==='login'?120000:45000);
    postMessage(`canvas.github.${action}`,{requestId:id,workspace,...extra});
  },[postMessage,workspace]);
  useEffect(()=>{setAuth(null);setRepos([]);setRepository('');setChanging(false);send('status');return()=>{clearTimeout(timer.current);request.current='';};},[send]);
  useMessage(event=>{
    const p=event.payload as any;
    if(p?.requestId!==request.current)return;
    if(event.type!=='canvas.github.result'&&event.type!=='canvas.error')return;
    clearTimeout(timer.current);request.current='';setPending('');
    if(event.type==='canvas.error'){setError(p.message);return;}
    if(p.auth)setAuth(p.auth);
    if(p.repos)setRepos(p.repos);
    if(p.git){onChanged(p.git);if(pending==='connect')setChanging(false);}
    if(p.commitPreview){setReview(p.commitPreview);setSelected(p.commitPreview.files.filter((f:any)=>!f.blocked).map((f:any)=>f.path));setAuthor(p.commitPreview.name||auth?.user||'');setEmail(p.commitPreview.email||(auth?.user?`${auth.user}@users.noreply.github.com`:''));}
    else setReview(null);
    setMessage(p.message||'');
  });
  const busy=disabled||Boolean(pending), signedIn=auth?.status==='authenticated';
  return <section aria-label="GitHub 연결">
    <header><h3>{signedIn?`GitHub · ${auth.user}`:'GitHub 연결'}</h3><button disabled={busy} onClick={()=>send('status')}>상태 새로고침</button></header>
    {pending&&<p role="status" className="rc-note">{pending==='login'?'VS Code에서 GitHub 로그인을 완료해 주세요…':pending==='connect'?'저장소 연결 중…':pending==='commit'?'선택한 파일을 커밋하고 있습니다…':'GitHub 상태 확인 중…'}</p>}
    {(error||git.error||auth?.status==='error')&&<p className="rc-note rc-error" role="alert">{error||git.error||auth?.message}</p>}
    {message&&<p className="rc-note" role="status">{message}</p>}
    <button disabled={busy} onClick={()=>send('login')}>{signedIn?'GitHub 계정 다시 연결':'GitHub 로그인'}</button>
    {git.connected&&<p><b>{git.repository}</b><br/>{git.branch||'브랜치 미선택'} · {git.head?'커밋된 변경만 푸시':'첫 커밋 필요'}<br/><button disabled={busy} onClick={()=>{setChanging(v=>!v);setRepository('');setCreate(false);}}>{changing?'변경 취소':'저장소 변경·새로 만들기'}</button></p>}
    {(!git.connected||changing)&&<>
      <p className="rc-muted">로그인 후 이 프로젝트를 올릴 저장소를 연결하세요.</p>
      <div className="rc-fields">
        <label>연결 방식<select disabled={busy||!signedIn} value={create?'new':'existing'} onChange={e=>{setCreate(e.target.value==='new');setRepository('');}}><option value="existing">기존 저장소</option><option value="new">새 비공개 저장소</option></select></label>
        {!create&&repos.length>0&&<label>내 저장소<select disabled={busy||!signedIn} value={repos.some(r=>r.name===repository)?repository:''} onChange={e=>setRepository(e.target.value)}><option value="">저장소 선택 또는 주소 입력</option>{repos.map(repo=><option key={repo.name} value={repo.name}>{repo.name}{repo.private?' · 비공개':''}</option>)}</select></label>}
        <label>{create?'저장소 이름':'저장소 주소'}<input disabled={busy||!signedIn} value={repository} onChange={e=>setRepository(e.target.value)} placeholder={create?`${auth?.user||'owner'}/my-project`:'owner/repository 또는 GitHub URL'}/></label>
      </div>
      <button disabled={busy||!signedIn||!repository.trim()} onClick={()=>send('connect',{repository:create&&!repository.includes('/')?`${auth?.user}/${repository}`:repository,create,replace:Boolean(git.hasOrigin||git.connected),previousRepository:git.repository})}>{create?'비공개 저장소 만들고 연결':git.hasOrigin||git.connected?'이 저장소로 변경':'이 저장소 연결'}</button>
      <p className="rc-muted">{git.hasOrigin||git.connected?'프로젝트의 연결 대상을 바꿉니다. 기존 저장소와 커밋은 보존됩니다. ':''}파일 업로드는 별도의 푸시 승인 후 진행합니다.</p>
    </>}
    {git.connected&&(!git.head||git.dirty)&&<div className="rc-note"><p>{!git.head?'올릴 파일을 확인하고 첫 커밋을 만드세요.':'새 변경 사항이 있습니다.'}</p><button disabled={busy} onClick={()=>send('changes')}>변경 파일 확인</button></div>}
    {review&&<div className="rc-fields" aria-label="커밋할 파일">
      <b>커밋할 파일 · {selected.length}개 선택</b>
      <div style={{maxHeight:240,overflow:'auto'}}>{review.files.map(file=><label key={file.path} style={{display:'flex',alignItems:'start',gap:6,marginBottom:6}}><input type="checkbox" disabled={busy||Boolean(file.blocked)} checked={selected.includes(file.path)} onChange={e=>setSelected(items=>e.target.checked?[...items,file.path]:items.filter(p=>p!==file.path))}/><span><code>{file.status.trim()}</code> {file.path}{file.blocked&&<small> · 제외: {file.blocked}</small>}</span></label>)}</div>
      <label>커밋 메시지<input value={commitMessage} disabled={busy} onChange={e=>setCommitMessage(e.target.value)}/></label>
      <details><summary>커밋 작성자 · {author||'입력 필요'}</summary><label>이름<input value={author} onChange={e=>setAuthor(e.target.value)}/></label><label>이메일<input value={email} onChange={e=>setEmail(e.target.value)}/></label></details>
      <button disabled={busy||!selected.length||!commitMessage.trim()||!author||!email} onClick={()=>send('commit',{previewId:review.id,files:selected,message:commitMessage,name:author,email,approved:true})}>선택한 파일 커밋</button>
      <p className="rc-muted">컴퓨터에 변경 이력을 저장합니다. 다음 단계에서 승인하면 GitHub로 푸시합니다.</p>
    </div>}
    <button className="rc-primary rc-review-button" disabled={busy||changing||!git.connected||!git.head||!git.branch} onClick={onReview}>푸시 내용 확인</button>
    <p className="rc-muted">커밋을 확인하고 승인하면 시크릿 검사 후 푸시합니다.</p>
    <details className="rc-details"><summary>워크플로우 설정</summary><button disabled={busy} onClick={onWorkflow}>Actions 워크플로우 생성</button></details>
  </section>;
}
