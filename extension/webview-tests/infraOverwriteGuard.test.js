/**
 * 인프라 파일 덮어쓰기 가드 — 실기기 검증 D4 에서 드러난 "보이기만 하는 승인".
 *
 * 사용자가 손으로 고친 Dockerfile 이 "Dockerfile 생성 → 저장 Level 1" 한 번에
 * 조용히 원래대로 되돌아갔다. 생성 결과가 예전과 같아 화면엔 변화가 없었고,
 * 사용자는 자기 수정이 사라진 줄도 몰랐다. 이제 코어는 같은 경로에 내용이 다른
 * 파일이 있으면 쓰지 않고 `exists` + diff 를 돌려주고, 웹뷰는 그걸 보여 준 뒤
 * "기존 파일 그대로" / "덮어쓰기(백업)" 를 고르게 한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');
const SHIP = read('../webview-src/components/ShipMode.tsx');
const HOST = read('../src/sidebar/SidebarProvider.ts');
const API = read('../src/core/ApiClient.ts');
const CORE = read('../../core/api/routes/deploy.py');

test('코어: 내용이 다른 파일이 있으면 overwrite 없이는 쓰지 않는다', () => {
  assert.match(CORE, /def _existing_file_conflict\(/, '기존 파일 비교 헬퍼가 없다');
  assert.match(CORE, /"status": "exists"/, 'exists 응답이 없다 — 덮어쓰기를 묻지 못한다');
  assert.match(CORE, /unified_diff\(/, 'diff 를 만들지 않는다 — 무엇이 바뀌는지 보여줄 수 없다');
  assert.match(
    CORE,
    /async def approve_dockerfile\([\s\S]*?overwrite: bool = False/,
    '승인 라우트가 overwrite 를 받지 않는다',
  );
  //: exists 로 안 썼으면 제안은 남아 있어야 다시 승인할 수 있다.
  assert.match(
    CORE,
    /result = _write_proposal_to_workspace\(proposal, workspace_path, proposal_id, overwrite=overwrite\)\s*\n\s*if result\.get\("status"\) == "saved":\s*\n\s*del _infra_proposals\[proposal_id\]/,
    'exists 응답 뒤에도 제안을 지운다 — 사용자가 덮어쓰기를 골라도 404 가 난다',
  );
});

test('코어: 덮어쓸 때는 기존 파일을 백업으로 남긴다', () => {
  assert.match(CORE, /_OVERWRITE_BACKUP_SUFFIX = "\.recoder-prev"/);
  assert.match(CORE, /backup\.write_text\(conflict\["existing_content"\]/, '백업을 쓰지 않는다');
  assert.match(CORE, /"backup_path": backup_path/, '어디에 백업했는지 응답에 없다');
});

test('호스트·ApiClient: overwrite 플래그가 웹뷰 → 코어까지 전달된다', () => {
  assert.match(HOST, /approveDockerfile\(proposalId, approved, overwrite === true\)/, '호스트가 overwrite 를 버린다');
  assert.match(API, /&overwrite=\$\{overwrite\}/, 'ApiClient 가 overwrite 쿼리를 안 보낸다');
  assert.match(API, /diff: resp\.data\?\.diff/, 'ApiClient 가 diff 를 버린다 — 웹뷰가 보여줄 수 없다');
});

test('웹뷰: exists 응답이면 diff 와 두 선택지를 보이고, 자동으로 다음 단계로 가지 않는다', () => {
  assert.match(SHIP, /result\.status === "exists"/, 'exists 응답을 구분하지 않는다');
  assert.match(SHIP, /setExistingConflict\(\{ path: result\.path, diff: result\.diff/, 'diff 를 상태에 담지 않는다');
  assert.match(SHIP, /기존 파일 그대로 쓰고 검사/, '"기존 파일 그대로" 선택지가 없다');
  assert.match(SHIP, /handleApproveInfraFile\(true\)/, '"덮어쓰기" 선택지가 overwrite=true 로 다시 승인하지 않는다');
  //: 기존 파일을 쓰기로 했으면 초안은 거절돼야 서버에 고아로 남지 않는다.
  const keep = SHIP.slice(SHIP.indexOf('const handleKeepExistingFile'), SHIP.indexOf('const handleRejectDockerfile'));
  assert.match(keep, /approved: false/, '기존 파일 유지 시 초안을 거절하지 않는다');
  assert.match(keep, /startSecurityScan\(\)/, '기존 파일로 검사를 이어가지 않는다 — 흐름이 끊긴다');
  const scan = SHIP.slice(SHIP.indexOf('const startSecurityScan'), SHIP.indexOf('// Message listener'));
  assert.match(scan, /setStep\("scanning"\)/, '검사를 시작할 때 진행 상태를 표시해야 한다');
  assert.match(scan, /postMessage\("runScan"/, '공통 검사 경로가 요청을 보내야 한다');
});

test('웹뷰: 덮어썼으면 백업 위치를 알려준다', () => {
  assert.match(SHIP, /기존 파일을 덮어썼습니다/, '덮어쓴 사실을 말하지 않는다');
  assert.match(SHIP, /result\.backup_path/, '백업 위치를 보여주지 않는다');
});
