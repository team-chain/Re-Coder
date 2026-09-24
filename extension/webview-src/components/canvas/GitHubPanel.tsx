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
  const [pending,setPending]=useState(''),[error,setError]=useState(''),[message,setMessage]=useState('');
  const request=useRef(''),timer=useRef<ReturnType<typeof setTimeout>>();
  const send=useCallback((action:string,extra:Record<string,unknown>={})=>{
    clearTimeout(timer.current);
    const id=`github-${Date.now()}-${Math.random()}`;request.current=id;
    setPending(action);setError('');setMessage('');
    timer.current=setTimeout(()=>{if(request.current!==id)return;request.current='';setPending('');setError('응답이 지연되고 있습니다. VS Code 로그인 알림과 Core 연결을 확인한 뒤 상태를 새로고침하세요.');},action==='login'?120000:45000);
    postMessage(`canvas.github.${action}`,{requestId:id,workspace,...extra});
  },[postMessage,workspace]);
  useEffect(()=>{setAuth(null);setRepos([]);setRepository('');send('status');return()=>{clearTimeout(timer.current);request.current='';};},[send]);
  useMessage(event=>{
    const p=event.payload as any;
    if(p?.requestId!==request.current)return;
    if(event.type!=='canvas.github.result'&&event.type!=='canvas.error')return;
    clearTimeout(timer.current);request.current='';setPending('');
    if(event.type==='canvas.error'){setError(p.message);return;}
    if(p.auth)setAuth(p.auth);
    if(p.repos)setRepos(p.repos);
    if(p.git)onChanged(p.git);
    setMessage(p.message||'');
  });
  const busy=disabled||Boolean(pending), signedIn=auth?.status==='authenticated';
  return <section aria-label="GitHub 연결">
    <header><h3>{signedIn?`GitHub · ${auth.user}`:'GitHub 연결'}</h3><button disabled={busy} onClick={()=>send('status')}>상태 새로고침</button></header>
    {pending&&<p role="status" className="rc-note">{pending==='login'?'VS Code에서 GitHub 로그인을 완료해 주세요…':pending==='connect'?'저장소 연결 중…':'GitHub 상태 확인 중…'}</p>}
    {(error||git.error||auth?.status==='error')&&<p className="rc-note rc-error" role="alert">{error||git.error||auth?.message}</p>}
    {message&&<p className="rc-note" role="status">{message}</p>}
    <button disabled={busy} onClick={()=>send('login')}>{signedIn?'GitHub 계정 다시 연결':'GitHub 로그인'}</button>
    {git.connected?<p><b>{git.repository}</b><br/>{git.branch||'브랜치 미선택'} · {git.head?'커밋된 변경만 푸시':'첫 커밋 필요'}</p>:<>
      <p className="rc-muted">로그인 후 이 프로젝트를 올릴 저장소를 연결하세요.</p>
      <div className="rc-fields">
        <label>연결 방식<select disabled={busy||!signedIn} value={create?'new':'existing'} onChange={e=>{setCreate(e.target.value==='new');setRepository('');}}><option value="existing">기존 저장소</option><option value="new">새 비공개 저장소</option></select></label>
        {!create&&repos.length>0&&<label>내 저장소<select disabled={busy||!signedIn} value={repos.some(r=>r.name===repository)?repository:''} onChange={e=>setRepository(e.target.value)}><option value="">저장소 선택 또는 주소 입력</option>{repos.map(repo=><option key={repo.name} value={repo.name}>{repo.name}{repo.private?' · 비공개':''}</option>)}</select></label>}
        <label>{create?'저장소 이름':'저장소 주소'}<input disabled={busy||!signedIn} value={repository} onChange={e=>setRepository(e.target.value)} placeholder={create?`${auth?.user||'owner'}/my-project`:'owner/repository 또는 GitHub URL'}/></label>
      </div>
      <button disabled={busy||!signedIn||!repository.trim()} onClick={()=>send('connect',{repository:create&&!repository.includes('/')?`${auth?.user}/${repository}`:repository,create})}>{create?'비공개 저장소 만들고 연결':'이 저장소 연결'}</button>
      <p className="rc-muted">연결할 때 파일은 업로드되지 않습니다.</p>
    </>}
    {git.connected&&(!git.head||git.dirty)&&<div className="rc-note"><p>{!git.head?'아직 커밋이 없습니다. 소스 제어에서 올릴 파일을 선택하고 첫 커밋을 만드세요.':'미커밋 변경은 이번 푸시에 포함되지 않습니다. 포함하려면 소스 제어에서 먼저 커밋하세요.'}</p><button disabled={busy} onClick={()=>send('sourceControl')}>소스 제어에서 커밋</button></div>}
    <button className="rc-primary rc-review-button" disabled={busy||!git.connected||!git.head||!git.branch} onClick={onReview}>푸시 내용 확인</button>
    <p className="rc-muted">커밋을 확인하고 승인하면 시크릿 검사 후 푸시합니다.</p>
    <details className="rc-details"><summary>워크플로우 설정</summary><button disabled={busy} onClick={onWorkflow}>Actions 워크플로우 생성</button></details>
  </section>;
}
