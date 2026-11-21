const test=require('node:test');const assert=require('node:assert/strict');
const {awsErrorText,awsNotes}=require('../out/webview-test/components/awsErrors');
const {isPassNote,isIsoTime}=require('../out/webview-test/components/Replay');
test('AWS 권한 오류는 필요한 권한만 한국어로, 계정 번호·ARN 없이',()=>{
 const raw='User: arn:aws:iam::419784061854:user/recoder-deploy is not authorized to perform: ecs:DescribeServices on resource: arn:aws:ecs:us-east-1:419784061854:service/test/test because no identity-based policy allows the ecs:DescribeServices action';
 const t=awsErrorText(raw);assert.equal(t,'AWS 권한이 없어요 — recoder-deploy 에 ecs:DescribeServices 권한을 추가하세요');assert.doesNotMatch(t,/419784061854|arn:/);
 const notes=awsNotes([`서비스 상태를 읽지 못했습니다: ${raw}`,`서비스 상태를 읽지 못했습니다: ${raw}`,'상시 감시 상태를 확인하지 못했습니다: x'],g=>g.startsWith('상시 감시'));
 assert.deepEqual(notes,['서비스 상태를 읽지 못했어요 · AWS 권한이 없어요 — recoder-deploy 에 ecs:DescribeServices 권한을 추가하세요']);
 assert.equal(awsErrorText('something else'),'something else');
});
test('배포 기록: 통과 기록은 경고가 아니고, 복귀 시각은 ISO 원문이 아니다',()=>{
 assert.ok(isPassNote('배포 검사 45/45 통과'));assert.ok(!isPassNote('배포 검사 44/45 통과'));assert.ok(!isPassNote('배포 검사 45/45 통과 · 경고 1'));
 assert.ok(isIsoTime('2026-11-18T14:41:00.533246+00:00'));assert.ok(!isIsoTime('recoder-app:abc'));
});
test('ADR 본문: **굵게** 기호가 그대로 보이지 않고, 머리줄과 같은 제목은 한 번만',()=>{
 const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
 const {AdrBody}=require('../out/webview-test/components/Hubs');
 const html=renderToStaticMarkup(React.createElement(AdrBody,{text:'# ADR-1: 결제\n\n**키 없이** 실행\n- `stripe` 없음',skipTitle:'ADR-1: 결제'}));
 assert.doesNotMatch(html,/\*\*/);assert.match(html,/<strong>키 없이<\/strong>/);assert.match(html,/<code[^>]*>stripe<\/code>/);assert.doesNotMatch(html,/ADR-1: 결제/);
});
test('AWS 정리 목록의 권한 오류도 한국어 한 줄(계정 번호 없음)',()=>{
 const g='ECS: 확인하지 못했습니다 — User: arn:aws:iam::419784061854:user/recoder-deploy is not authorized to perform: ecs:ListClusters on resource: * because no identity-based policy allows the ecs:ListClusters action';
 assert.deepEqual(awsNotes([g]),['ECS · AWS 권한이 없어요 — recoder-deploy 에 ecs:ListClusters 권한을 추가하세요']);
});
