import React from 'react';
import { describeS3Progress, s3UploadRatio, S3Progress } from './DeploymentCenter';

export interface DeploymentActivityEvent {
  step: string; message?: string; plan_id?: string; done_count?: number; total?: number; key?: string;
  result?: { status?: string; message?: string };
}
const stages = {
  docker: [['queued','준비'],['build','이미지 빌드'],['scan','보안 확인'],['start','컨테이너 시작'],['health','헬스 확인'],['record','결과 저장']],
  s3: [['plan','준비'],['bucket','버킷 확인'],['website','호스팅 설정'],['upload','파일 업로드'],['prune','마무리']],
};

export function DeploymentActivity({ target, event }: { target: 'docker'|'s3'; event: DeploymentActivityEvent | null }) {
  if (!event) return null;
  const failed = event.step==='error' || ['failed','error'].includes(event.result?.status || '');
  const pending = event.result?.status==='pending';
  const complete = event.step==='done' && !failed && !pending && event.result?.status!=='cancelled';
  const items=stages[target], index=complete?items.length:items.findIndex(([key])=>key===event.step);
  const ratio=target==='s3'?s3UploadRatio(event as S3Progress):null;
  const text=failed?event.message||event.result?.message||'배포 실패':pending?'컨테이너 실행됨 · HTTP 헬스 확인 대기':complete?'배포 완료':target==='s3'?describeS3Progress(event as S3Progress):event.message||'배포를 준비합니다';
  return <section aria-label={`${target==='docker'?'Docker':'S3'} 배포 진행`} style={{padding:'14px 16px',margin:'12px 0',border:'1px solid var(--vscode-panel-border,#323b47)',borderRadius:8,background:'var(--vscode-editorWidget-background,#181e27)'}}>
    <div style={{display:'flex',justifyContent:'space-between',gap:12,marginBottom:8}}><strong>{target==='docker'?'Docker':'S3'} 배포</strong><span role="status" style={{color:failed?'#f38a91':complete?'#73d39b':'var(--vscode-foreground)'}}>{text}{ratio!==null?` · ${Math.round(ratio*100)}%`:''}</span></div>
    <progress aria-label={target==='s3'&&ratio!==null?'파일 업로드 진행률':'배포 단계 진행'} max={ratio!==null?1:items.length} value={failed||pending?undefined:ratio!==null?ratio:Math.max(0,index)} style={{width:'100%',height:6,accentColor:failed?'#f38a91':'#4faff0'}} />
    <ol style={{display:'flex',gap:'8px 16px',flexWrap:'wrap',listStyle:'none',padding:0,margin:'8px 0 0',fontSize:11,color:'var(--vscode-descriptionForeground,#99a9b9)'}}>{items.map(([key,label],i)=><li key={key} aria-current={key===event.step?'step':undefined} style={{color:key===event.step?'#8dceff':undefined}}>{i<index?'✓':`${i+1}.`} {label}</li>)}</ol>
    {event.key&&<div style={{fontSize:11,marginTop:6,overflowWrap:'anywhere',color:'var(--vscode-descriptionForeground)'}}>{event.key}</div>}
  </section>;
}
