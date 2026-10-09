const test=require('node:test'),assert=require('node:assert/strict');
const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
const {Checkbox,Switch,CHECK_CSS}=require('../out/webview-test/components/Check');
const {ApprovalModal}=require('../out/webview-test/components/ApprovalModal');
const {WORKSPACE_INFRA_PATH}=require('../out/webview-test/components/ShipMode');
const fs=require('node:fs'),path=require('node:path');

test('공통 체크박스·스위치: 실제 checkbox 를 쓰고 설명·비활성·스위치 역할을 가진다',()=>{
 const c=renderToStaticMarkup(React.createElement(Checkbox,{checked:true,onChange(){},label:'파일',description:'새 파일',ariaLabel:'a',tone:'warn'}));
 assert.match(c,/class="rc-ck warn"/);assert.match(c,/type="checkbox"/);assert.match(c,/aria-label="a"/);assert.match(c,/checked=""/);assert.match(c,/rc-ck-d">새 파일/);
 const d=renderToStaticMarkup(React.createElement(Checkbox,{checked:false,disabled:true,onChange(){},label:'x'}));
 assert.match(d,/rc-ck disabled/);assert.match(d,/disabled=""/);
 const s=renderToStaticMarkup(React.createElement(Switch,{checked:true,onChange(){},label:'팀 모드'}));
 assert.match(s,/role="switch"[^>]*aria-checked="true"/);
 assert.match(CHECK_CSS,/label\.rc-ck\.rc-ck input/,'캔버스 입력창 스타일에 덮이지 않게 우선순위를 높인다');
 assert.match(CHECK_CSS,/--vscode-checkbox-background/,'밝은·어두운 테마 색을 따른다');
 const native=fs.readdirSync(path.join(__dirname,'../webview-src/components'),{recursive:true}).filter(f=>/\.tsx$/.test(f)&&!/Check\.tsx$|DiscordPanel/.test(f))
  .filter(f=>/type="checkbox"/.test(fs.readFileSync(path.join(__dirname,'../webview-src/components',f),'utf8')));
 assert.deepEqual(native,[],'기본 체크박스가 남아 있다');
});

test('승인 카드: 한글 라벨, 실제 실행 순서의 명령 목록(없으면 예전 한 줄)',()=>{
 const steps=[{command:'docker network create recoder-shop',note:'네트워크'},{command:'docker run -d --name shop -e JWT_SECRET=*** shop:latest',note:'앱'}];
 const html=renderToStaticMarkup(React.createElement(ApprovalModal,{level:2,title:'t',summary:'s',riskLevel:'medium',riskReasons:['이 PC 의 포트 3002 로 열립니다'],commandSteps:steps,commandPreview:'old',onApprove(){},onReject(){}}));
 for(const w of ['승인 단계 2 · 확인 필요','중간 위험','요약','위험 사유','실행할 명령 · 순서대로','>거절<','>승인<'])assert.ok(html.includes(w),w);
 assert.match(html,/data-testid="command-steps"/);assert.match(html,/JWT_SECRET=\*\*\*/);assert.doesNotMatch(html,/>old</);
 for(const w of ['SUMMARY','Summary','Risk Reasons','Command Preview','>Approve<','>Reject<','Approval Level'])assert.ok(!html.includes(w),w);
 const old=renderToStaticMarkup(React.createElement(ApprovalModal,{level:2,title:'t',summary:'s',riskLevel:'low',riskReasons:[],commandPreview:'docker build .',onApprove(){},onReject(){}}));
 assert.match(old,/실행할 명령/);assert.match(old,/docker build \./);
});

test('Dockerfile 미리보기: 초안이 없으면 배포에 쓰는 폴더의 파일을 읽어 보여 주고, 호스트는 폴더 밖을 거절한다',()=>{
 assert.equal(WORKSPACE_INFRA_PATH.dockerfile,'Dockerfile');assert.equal(WORKSPACE_INFRA_PATH.actions,null);
 const ship=fs.readFileSync(path.join(__dirname,'../webview-src/components/ShipMode.tsx'),'utf8');
 for(const s of ['infra.readWorkspaceFile','infra.workspaceFile','워크스페이스 파일 사용 중','저장됨 · 배포에 이 파일을 씀','에디터에서 열기','commandSteps={plan.command_steps}'])assert.ok(ship.includes(s),s);
 const host=fs.readFileSync(path.join(__dirname,'../src/sidebar/SidebarProvider.ts'),'utf8');
 assert.match(host,/case 'infra\.readWorkspaceFile':/);assert.match(host,/프로젝트 폴더 밖의 파일은 열 수 없습니다/);
});
