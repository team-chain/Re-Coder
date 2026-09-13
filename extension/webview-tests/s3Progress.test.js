/**
 * S3 배포 진행 표시 — 보드 이슈 「S3 배포 진행이 안 보임 — SSE 스트리밍 전환」.
 *
 * 코어 쪽(이벤트 생성·실패 전달)은 pytest(test_s3_deploy_stream.py)가 검사한다.
 * 여기서는 (1) 진행 이벤트 → 화면 문구 변환, (2) 배선을 검사한다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const {
  describeS3Progress,
  s3UploadRatio,
} = require('../out/webview-test/components/DeploymentCenter.js');

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

test('각 단계가 사람이 읽는 문구로 바뀐다', () => {
  assert.match(describeS3Progress({ step: 'plan' }), /파일을 정리/);
  assert.match(describeS3Progress({ step: 'website' }), /정적 호스팅/);
  assert.match(describeS3Progress({ step: 'prune' }), /이전 배포 파일/);
});

test('업로드는 개수가 보인다 — 진행 중임을 알 수 있는 유일한 단서', () => {
  assert.strictEqual(
    describeS3Progress({ step: 'upload', done_count: 47, total: 83 }),
    '파일 업로드 47/83',
  );
});

test('진행 이벤트가 아직 없어도 빈 화면을 보여주지 않는다', () => {
  assert.match(describeS3Progress(null), /준비/);
});

test('스트림 내 실패 메시지를 그대로 보여준다', () => {
  //: 실패는 HTTP 상태가 아니라 이벤트로 온다(스트림은 이미 200 이다).
  const text = describeS3Progress({ step: 'error', message: 'S3 버킷 생성 실패: AccessDenied' });
  assert.match(text, /AccessDenied/);
});

test('[음성 대조] 업로드 단계가 아니면 진행률을 만들지 않는다', () => {
  //: 가짜 비율을 그리면 화면이 사실과 다른 말을 하게 된다.
  assert.strictEqual(s3UploadRatio({ step: 'bucket' }), null);
  assert.strictEqual(s3UploadRatio({ step: 'upload', total: 0 }), null);
  assert.strictEqual(s3UploadRatio(null), null);
  assert.strictEqual(s3UploadRatio({ step: 'upload', done_count: 5, total: 10 }), 0.5);
});

test('진행률은 1을 넘지 않는다', () => {
  assert.strictEqual(s3UploadRatio({ step: 'upload', done_count: 12, total: 10 }), 1);
});

// ---------------------------------------------------------------------------
// 배선
// ---------------------------------------------------------------------------

test('ApiClient 가 스트리밍 엔드포인트를 부르고 세션 토큰을 싣는다', () => {
  const source = read('../src/core/ApiClient.ts');
  assert.match(source, /\/api\/deploy\/s3\/stream/);
  const method = source.slice(source.indexOf('async deployS3Stream'), source.indexOf("async deployS3Stream") + 4200);
  //: EventSource 는 커스텀 헤더를 못 보낸다 — 토큰 인증이 깨진다.
  assert.ok(!/new EventSource/.test(method), 'EventSource 로는 세션 토큰을 보낼 수 없다');
  assert.match(method, /X-Session-Token/);
  //: 완료 이벤트 없이 끝나면 성공으로 처리하면 안 된다.
  assert.match(method, /연결이 끊겼습니다/);
});

test('SidebarProvider 가 진행 이벤트를 웹뷰로 중계한다', () => {
  const source = read('../src/sidebar/SidebarProvider.ts');
  assert.match(source, /deployS3Stream/, '스트리밍 경로를 쓰지 않는다');
  assert.match(source, /workspace\.deploy\.s3\.progress/, '진행 이벤트를 웹뷰로 안 보낸다');
});

test('배포 화면이 진행 이벤트를 받아 그린다', () => {
  const source = read('../webview-src/components/DeploymentCenter.tsx');
  assert.match(source, /workspace\.deploy\.s3\.progress/, '진행 이벤트 핸들러가 없다');
  assert.match(source, /describeS3Progress\(s3Progress\)/, '진행 문구를 렌더하지 않는다');
  //: 끝나면 진행 표시를 반드시 지운다 — 남으면 다음 배포에 옛 숫자가 보인다.
  const resultHandler = source.slice(source.indexOf('workspace.deploy.s3.result'));
  assert.match(resultHandler.slice(0, 300), /setS3Progress\(null\)/);
});
