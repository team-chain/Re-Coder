import * as vscode from 'vscode';
import {AccessIdentity,AccessPolicy,accessKey,accessTarget,validateAccessPolicy} from './redTeamAccess';

export async function configureAccess(secrets:vscode.SecretStorage,workspace:string,previousTarget?:string):Promise<boolean> {
    const target=await vscode.window.showInputBox({title:'권한 검사 · 대상',prompt:'테스트 앱 주소. 두 일반 사용자의 소유 리소스만 읽어서 대조합니다.',value:previousTarget?.replace('host.docker.internal','127.0.0.1')||'http://localhost:3000',ignoreFocusOut:true});
    if(!target)return false;
    const canonical=accessTarget(target.trim()),users:AccessIdentity[]=[];
    const kind=await vscode.window.showQuickPick([{label:'Bearer 토큰',authKind:'bearer' as const},{label:'세션 Cookie',authKind:'cookie' as const}],{title:'테스트 앱의 로그인 방식'});
    if(!kind)return false;
    for(const label of ['A','B'] as const) {
        const credential=await vscode.window.showInputBox({title:`사용자 ${label} · 테스트 인증값`,prompt:kind.authKind==='bearer'?'Bearer 접두어 없는 토큰. AI에 전달하지 않고 보안 저장소에 보관합니다.':'예: session=테스트값. AI에 전달하지 않고 보안 저장소에 보관합니다.',password:true,ignoreFocusOut:true});
        if(!credential)return false;
        const resource=await vscode.window.showInputBox({title:`사용자 ${label} · 본인만 볼 수 있는 경로`,prompt:'GET으로 조회 가능한 소유 리소스. 다른 사용자와 비로그인은 거부되어야 합니다.',placeHolder:label==='A'?'/api/orders/test-a':'/api/orders/test-b',ignoreFocusOut:true});
        if(!resource)return false;
        const marker=await vscode.window.showInputBox({title:`사용자 ${label} · 테스트 리소스의 고유 표식`,prompt:'응답 본문에서 소유 데이터를 식별할 6~128자 표식. 실제 개인정보 대신 테스트용 값을 사용하세요.',placeHolder:label==='A'?'recoder-test-owner-a':'recoder-test-owner-b',ignoreFocusOut:true});
        if(!marker)return false;
        users.push({label,kind:kind.authKind,credential:credential.trim(),path:resource.trim(),marker});
    }
    const policy=validateAccessPolicy({schema:1,target:canonical,users} as AccessPolicy);
    await secrets.store(accessKey(workspace),JSON.stringify(policy));
    return true;
}
