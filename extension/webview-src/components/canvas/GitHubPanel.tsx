import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Checkbox } from "../Check";
import { Box, Button, Collapse, Line, Section, Segmented, SheetHead } from "../ui/kit";
import { Icon } from "../ui/icons";
import { Logo } from "./Scene";

/** `git status --porcelain` 두 글자 → 사람이 읽는 말. */
export function gitStatusText(status: string): string {
  const s = (status || "").trim();
  if (s === "??" || s.includes("A")) { return "새 파일"; }
  if (s.includes("D")) { return "삭제"; }
  if (s.includes("R")) { return "이름 변경"; }
  if (s.includes("M")) { return "수정"; }
  return s || "변경";
}
import { useVSCodeApi } from '../../hooks/useVSCodeApi';
import { Snapshot } from './model';
import { defaultRepoName, previewRepoName } from './githubName';

export function GitHubPanel({ workspace, git, onChanged, onReview, onWorkflow, disabled }: {
  workspace:string; git:Snapshot['git']; onChanged:(git:Snapshot['git'])=>void;
  onReview:()=>void; onWorkflow:()=>void; disabled:boolean;
}) {
  const {postMessage,useMessage}=useVSCodeApi();
  const [auth,setAuth]=useState<{status:string;user?:string;message?:string}|null>(null);
  const [repos,setRepos]=useState<Array<{name:string;private:boolean}>>([]);
  const [repository,setRepository]=useState(''),[create,setCreate]=useState(false),[visibility,setVisibility]=useState<'private'|'public'>('private');
  const [changing,setChanging]=useState(false);
  const [pending,setPending]=useState(''),[error,setError]=useState(''),[message,setMessage]=useState('');
  const [review,setReview]=useState<{id:string;files:Array<{path:string;status:string;blocked:string}>}|null>(null);
  const [selected,setSelected]=useState<string[]>([]),[commitMessage,setCommitMessage]=useState('프로젝트 변경 저장');
  const [author,setAuthor]=useState(''),[email,setEmail]=useState('');
  const request=useRef(''),timer=useRef<ReturnType<typeof setTimeout>>();
  //: 2.0.46 「커밋하고 푸시」 — 커밋이 끝나면 곧바로 푸시 승인 단계(onReview)를 연다. 푸시는 그 승인 뒤에만 한다.
  const pushAfterCommit=useRef(false),askedChanges=useRef('');
  const [query,setQuery]=useState(''),[pickFiles,setPickFiles]=useState(false),[missing,setMissing]=useState('');
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
    if(event.type==='canvas.error'){pushAfterCommit.current=false;setError(p.message);return;}
    if(p.auth)setAuth(p.auth);
    if(p.repos)setRepos(p.repos);
    if(p.git){onChanged(p.git);if(pending==='connect')setChanging(false);}
    if(p.commitPreview){setReview(p.commitPreview);setSelected(p.commitPreview.files.filter((f:any)=>!f.blocked).map((f:any)=>f.path));setAuthor(p.commitPreview.name||auth?.user||'');setEmail(p.commitPreview.email||(auth?.user?`${auth.user}@users.noreply.github.com`:''));}
    else setReview(null);
    setMessage(p.message||'');
    if(pushAfterCommit.current&&!p.commitPreview&&p.git?.head){pushAfterCommit.current=false;onReview();}
  });
  const busy=disabled||Boolean(pending), signedIn=auth?.status==='authenticated';
  //: 새 저장소: "이름", "owner/이름", GitHub 주소 모두 받는다. owner 가 없으면 로그인한 계정.
  const newName=previewRepoName(repository);
  const typedOwner=repository.trim().replace(/^(?:https?:\/\/)?(?:www\.)?github\.com\//i,'').split('/').filter(Boolean);
  const newOwner=typedOwner.length>1?typedOwner[0]:(auth?.user||'');
  const picking=!git.connected||changing;
  //: 연결된 저장소에 올릴 변경이 있으면 목록을 먼저 받아 둔다(「변경 파일 확인」 단추 없이).
  useEffect(()=>{
    const key=`${git.repository}|${git.head||''}|${git.dirty}`;
    if(!signedIn||!git.connected||changing||pending||review||!(git.dirty||!git.head)||askedChanges.current===key)return;
    askedChanges.current=key;send('changes');
  },[signedIn,git.connected,git.repository,git.head,git.dirty,changing,pending,review,send]);
  const q=query.trim().toLowerCase();
  const shown=repos.filter(r=>!q||r.name.toLowerCase().includes(q)).slice(0,6);
  const looksLikeRepo=/^[\w.-]+\/[\w.-]+$/.test(query.trim().replace(/^(?:https?:\/\/)?(?:www\.)?github\.com\//i,'').replace(/\.git$/,''));
  const pickRepo=(name:string)=>{setCreate(false);setRepository(name);setMissing('');};
  const connect=()=>{
    if(!repository.trim()&&!(looksLikeRepo&&!create)){setMissing(create?'만들 저장소 이름을 적어 주세요':'올릴 저장소를 하나 고르세요');return;}
    if(create&&!newName.name){setMissing(newName.error||'저장소 이름을 영문·숫자로 적어 주세요');return;}
    const target=create?`${newOwner}/${newName.name}`:(repository.trim()||query.trim());
    send('connect',{repository:target,create,visibility,replace:Boolean(git.hasOrigin||git.connected),previousRepository:git.repository});
  };
  const files=review?.files||[];
  const blocked=files.filter(f=>f.blocked);
  const base=(path:string)=>path.split('/').pop()||path;
  const canPush=git.connected&&Boolean(git.head)&&Boolean(git.branch)&&!changing;
  const primary=():{label:string;run:()=>void;hint:string}=>{
    if(!signedIn)return{label:'GitHub 로그인',run:()=>send('login'),hint:'VS Code 의 GitHub 로그인 창이 열립니다'};
    if(picking)return{label:create?`${visibility==='public'?'공개':'비공개'} 저장소 만들고 연결`:git.hasOrigin||git.connected?'이 저장소로 변경':'이 저장소 연결',run:connect,hint:'파일은 다음 단계에서 승인한 뒤에 올립니다'};
    if(review&&selected.length)return{label:'커밋하고 푸시',run:()=>{
      if(!commitMessage.trim()){setMissing('커밋 메시지를 적어 주세요');return;}
      if(!author||!email){setMissing('커밋 작성자 이름·이메일을 적어 주세요');return;}
      pushAfterCommit.current=true;send('commit',{previewId:review.id,files:selected,message:commitMessage,name:author,email,approved:true});
    },hint:'커밋한 뒤 푸시 내용을 확인하고 승인하면 시크릿 검사 후 올립니다'};
    if(canPush)return{label:'푸시 내용 확인',run:onReview,hint:'커밋을 확인하고 승인하면 시크릿 검사 후 푸시합니다'};
    return{label:'푸시 내용 확인',run:()=>setMissing(!git.head?'올릴 파일을 먼저 커밋해야 합니다':'브랜치를 확인하지 못했습니다 — 새로고침해 보세요'),hint:''};
  };
  const main=primary();
  return <section aria-label="GitHub 연결" data-testid="github-sheet">
    <SheetHead icon={<Logo kind="github"/>} title="GitHub 에 푸시"
      sub={signedIn?<>{auth?.user} 로 로그인됨 · <button type="button" className="rk-link" onClick={()=>!busy&&send('login')}>계정 변경</button> · <button type="button" className="rk-link" onClick={()=>!busy&&send('status')}>새로고침</button></>
        :auth?'로그인이 필요합니다':'GitHub 상태 확인 중…'}/>
    {pending&&<Line icon="history">{pending==='login'?'VS Code에서 GitHub 로그인을 완료해 주세요…':pending==='connect'?'저장소 연결 중…':pending==='commit'?'선택한 파일을 커밋하고 있습니다…':pending==='changes'?'바뀐 파일을 확인하는 중…':'GitHub 상태 확인 중…'}</Line>}
    {(error||git.error||auth?.status==='error')&&<Line icon="close" tone="bad">{error||git.error||auth?.message}</Line>}
    {message&&!pending&&<Line icon="check" tone="ok">{message}</Line>}

    {signedIn&&<Section title="저장소" aside={git.connected&&!changing?<button type="button" className="rk-link" onClick={()=>{if(busy)return;setChanging(true);setRepository('');setQuery('');setCreate(false);}}>변경</button>
      :git.connected?<button type="button" className="rk-link" onClick={()=>{setChanging(false);setMissing('');}}>취소</button>:undefined}>
      {!picking&&<div className="rk-list"><div className="rk-item" role="radio" aria-checked="true"><span className="av">{(git.repository.split('/').pop()||'?')[0].toUpperCase()}</span>
        <span className="n"><span>{git.repository.split('/')[0]} / </span>{git.repository.split('/').slice(1).join('/')}</span><small>{git.branch||'브랜치 미선택'}</small></div></div>}
      {picking&&<>
        {!create&&<div className={`rk-search${missing&&!repository?' err':''}`}><Icon name="search" size={14}/>
          <input aria-label="저장소 검색 또는 주소" placeholder="저장소 검색 또는 URL 붙여넣기" value={query} disabled={busy}
            onChange={e=>{setQuery(e.target.value);setRepository(repos.some(r=>r.name===e.target.value.trim())?e.target.value.trim():'');setMissing('');}}/></div>}
        {!create&&<div className="rk-list" role="radiogroup" aria-label="내 저장소">
          {shown.map(repo=><button type="button" key={repo.name} className="rk-item" role="radio" aria-checked={repository===repo.name} disabled={busy} onClick={()=>pickRepo(repo.name)}>
            <span className="av">{(repo.name.split('/').pop()||'?')[0].toUpperCase()}</span><span className="n"><span>{repo.name.split('/')[0]} / </span>{repo.name.split('/').slice(1).join('/')}</span><small>{repo.private?'비공개':'공개'}</small></button>)}
          {!shown.length&&q&&<div className="rk-line">{looksLikeRepo?<><Icon name="check" size={14}/>이 주소로 연결합니다: {query.trim()}</>:'찾는 저장소가 없습니다 — owner/이름 또는 주소를 붙여 넣으세요'}</div>}
          <button type="button" className="rk-item" role="radio" aria-checked={false} disabled={busy} onClick={()=>{setCreate(true);setRepository(defaultRepoName(workspace));setMissing('');}}>
            <span className="av"><Icon name="plus" size={13}/></span><span className="n" style={{color:'var(--rk-fg2)'}}>새 저장소 만들기</span></button>
        </div>}
        {create&&<Box><div style={{padding:'12px 0',display:'flex',flexDirection:'column',gap:10}}>
          <input className="rk-input" aria-label="새 저장소 이름" value={repository} disabled={busy} placeholder="my-project (영문·숫자)" onChange={e=>{setRepository(e.target.value);setMissing('');}}/>
          <Segmented name="repo-visibility" label="공개 범위" value={visibility} disabled={busy} onChange={setVisibility} options={[{value:'private',label:'비공개'},{value:'public',label:'공개'}]}/>
          {newName.name&&<span className="rk-note" role="status" style={{whiteSpace:'normal'}}>{newOwner}/{newName.name} 로 만듭니다{newName.name!==repository.trim().split('/').pop()?' (공백·한글은 -로 바꿉니다)':''}</span>}
          {visibility==='public'&&<span className="rk-note" style={{whiteSpace:'normal',color:'var(--rk-warn)'}} role="note">공개 저장소는 누구나 코드를 볼 수 있습니다. 푸시 전에 시크릿 검사를 하지만 .env 같은 파일이 목록에 없는지 한 번 더 보세요.</span>}
          <button type="button" className="rk-link" onClick={()=>{setCreate(false);setRepository('');}}>← 내 저장소에서 고르기</button>
        </div></Box>}
      </>}
    </Section>}

    {signedIn&&!picking&&review&&<Section title={`올릴 파일 · ${selected.length}개`} aside={<button type="button" className="rk-link" onClick={()=>setPickFiles(v=>!v)}>{pickFiles?'접기':'파일 고르기'}</button>}>
      {!pickFiles&&<div className="rk-chips">{selected.slice(0,4).map(path=><span key={path} className="rk-chip" title={path}><code>{base(path)}</code></span>)}{selected.length>4&&<span className="rk-chip">+{selected.length-4}</span>}</div>}
      {pickFiles&&<Box><div style={{maxHeight:240,overflow:'auto',padding:'8px 0'}}>{files.map(file=><Checkbox key={file.path} compact disabled={busy||Boolean(file.blocked)} checked={selected.includes(file.path)} onChange={on=>setSelected(items=>on?[...items,file.path]:items.filter(p=>p!==file.path))} style={{marginBottom:3}}
        label={<span><code>{file.path}</code></span>} description={file.blocked?`제외: ${file.blocked}`:gitStatusText(file.status)}/>)}</div></Box>}
      {blocked.length>0&&<Line icon="lock" tone="ok">{blocked.map(f=>base(f.path)).slice(0,3).join(', ')}{blocked.length>3?` 외 ${blocked.length-3}개`:''} 는 비밀값이 있을 수 있어 자동으로 뺐습니다</Line>}
    </Section>}
    {signedIn&&!picking&&review&&<Section title="커밋 메시지">
      <input className="rk-input" aria-label="커밋 메시지" value={commitMessage} disabled={busy} onChange={e=>{setCommitMessage(e.target.value);setMissing('');}}/>
      <Collapse summary={`커밋 작성자 · ${author||'입력 필요'}`} open={!author||!email}>
        <div style={{display:'flex',flexDirection:'column',gap:8,padding:'4px 0 10px'}}>
          <input className="rk-input" aria-label="커밋 작성자 이름" placeholder="이름" value={author} onChange={e=>setAuthor(e.target.value)}/>
          <input className="rk-input" aria-label="커밋 작성자 이메일" placeholder="이메일" value={email} onChange={e=>setEmail(e.target.value)}/>
        </div>
      </Collapse>
    </Section>}
    {signedIn&&!picking&&!review&&!pending&&git.head&&!git.dirty&&<Line icon="check" tone="ok">새로 바뀐 파일이 없습니다 · 커밋된 내용을 올릴 수 있어요</Line>}

    <div className="rk-foot">
      <Button testId="github-primary" onClick={main.run} disabled={busy}>{main.label}</Button>
      {missing?<p className="rk-err" role="alert">{missing}</p>:main.hint&&<p className="rk-hint">{main.hint}</p>}
      <p className="rk-hint" style={{marginTop:14}}><button type="button" className="rk-link" onClick={()=>!busy&&onWorkflow()}>GitHub Actions 워크플로 만들기</button></p>
    </div>
  </section>;
}
