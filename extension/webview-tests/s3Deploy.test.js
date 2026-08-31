/**
 * FR-05-03 「S3 배포 BYO 전환」 — 확장 쪽 배선
 *
 * 배경
 *   코어의 `POST /api/deploy/s3` 는 완성돼 있었다. 버킷 생성, 퍼블릭 액세스
 *   설정, 정적 웹사이트 호스팅, 업로드, URL 조립까지 다 한다. 테스트도 있다.
 *   그런데 **확장이 그 라우트를 한 번도 부르지 않았다** — 사용자 파일을 읽어
 *   보내는 쪽이 없어서 그 경로 전체가 도달 불가능이었다.
 *
 *   그래서 S3 탭에는 「배포 워크플로우 생성」 버튼만 있었다. 정적 사이트를
 *   지금 당장 올리는 방법은 제품 안에 없었다.
 *
 * 여기서 검사하는 것
 *   파일 선택은 **틀려도 예외가 안 난다.** node_modules 를 올리거나, 이미지를
 *   utf-8 로 읽어 깨뜨리거나, 빌드 폴더 대신 소스 폴더를 올려도 업로드는
 *   "성공" 한다. 사용자는 링크를 열어 본 다음에야 안다.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const {
  isBinaryAsset,
  shouldSkipPath,
  pickStaticDir,
  collectStaticFiles,
  s3ProjectIdentifier,
  normalizeRepositoryIdentity,
  StaticAssetReadError,
  StaticAssetTooLargeError,
  StaticAssetSymlinkError,
  describeTooManyFiles,
  STATIC_DIR_CANDIDATES,
  MAX_FILES,
  MAX_BYTES_PER_FILE,
} = require('../out/deploy/staticSite.js');

// ---------------------------------------------------------------------------
// 바이너리를 텍스트로 읽지 않는다
// ---------------------------------------------------------------------------

test('이미지·폰트·wasm 은 바이너리로 판정한다', () => {
  // utf-8 로 읽으면 잘못된 바이트가 U+FFFD 로 치환된다. 업로드는 성공하고
  // 파일 크기도 그럴듯한데, 브라우저에서 열면 깨진 이미지가 나온다.
  for (const p of [
    'logo.png', 'a/b/hero.JPG', 'font.woff2', 'app.wasm',
    'video.mp4', 'icon.ico', 'doc.pdf',
  ]) {
    assert.strictEqual(isBinaryAsset(p), true, `${p} 를 텍스트로 읽는다`);
  }
});

test('음성대조 — 알려진 텍스트 자산은 UTF-8로 읽는다', () => {
  // 전부 base64 로 보내면 정상 동작하지만, 이 테스트가 없으면 위 목록을
  // 무한정 넓혀도 아무도 모른다.
  for (const p of ['index.html', 'app.js', 'style.css', 'data.json', 'a/b/main.mjs']) {
    assert.strictEqual(isBinaryAsset(p), false, `${p} 를 base64 로 보낸다`);
  }
});

test('알 수 없는 확장자와 확장자 없는 파일은 바이트 보존을 우선한다', () => {
  assert.strictEqual(isBinaryAsset('scene.glb'), true);
  assert.strictEqual(isBinaryAsset('cursor.cur'), true);
  assert.strictEqual(isBinaryAsset('LICENSE'), true);
  assert.strictEqual(isBinaryAsset('.htaccess'), true);
});

// ---------------------------------------------------------------------------
// 올리면 안 되는 것을 거른다
// ---------------------------------------------------------------------------

test('node_modules 와 .git 은 건너뛴다', () => {
  // 안 거르면 파일 수 상한을 즉시 넘겨 "파일이 너무 많습니다" 만 보게 된다.
  // 진짜 원인은 폴더 선택인데 메시지는 개수 얘기만 한다.
  assert.strictEqual(shouldSkipPath('node_modules/react/index.js'), true);
  assert.strictEqual(shouldSkipPath('.git/config'), true);
  assert.strictEqual(shouldSkipPath('a/node_modules/b.js'), true);
  assert.strictEqual(shouldSkipPath('.next/cache/webpack/client-production/index.pack'), true);
  assert.strictEqual(shouldSkipPath('app/.next/cache/images/cache.bin'), true);
  assert.strictEqual(shouldSkipPath('.DS_Store'), true);
});

test('음성대조 — 평범한 산출물은 거르지 않는다', () => {
  assert.strictEqual(shouldSkipPath('index.html'), false);
  assert.strictEqual(shouldSkipPath('assets/app.abc123.js'), false);
  assert.strictEqual(shouldSkipPath('static/media/logo.png'), false);
});

// ---------------------------------------------------------------------------
// 빌드 산출물 폴더를 고른다
// ---------------------------------------------------------------------------

test('빌드 산출물 폴더가 있으면 그걸 고른다', () => {
  // 소스 폴더를 올리면 브라우저가 .tsx 를 실행할 수 없어 흰 화면이 나온다.
  // 그 실패는 배포가 아니라 앱 문제처럼 보인다.
  assert.strictEqual(pickStaticDir(['src', 'dist', 'node_modules']), 'dist');
  assert.strictEqual(pickStaticDir(['src', 'build']), 'build');
  assert.strictEqual(pickStaticDir(['out']), 'out');
});

test('후보 우선순위가 정해져 있다', () => {
  // 둘 다 있으면 매번 다른 걸 고르면 안 된다.
  const both = pickStaticDir(['public', 'dist']);
  assert.strictEqual(both, STATIC_DIR_CANDIDATES.find(c => ['public', 'dist'].includes(c)));
  assert.strictEqual(both, 'dist');
});

test('음성대조 — 후보가 없으면 루트를 쓴다', () => {
  assert.strictEqual(pickStaticDir(['src', 'tests']), '');
});

// ---------------------------------------------------------------------------
// 실제 수집
// ---------------------------------------------------------------------------

function fakeFs(tree) {
  // tree: { 'index.html': 'text', 'img/logo.png': Buffer }
  const dirs = new Map();
  for (const key of Object.keys(tree)) {
    const parts = key.split('/');
    for (let i = 0; i < parts.length; i++) {
      const parent = parts.slice(0, i).join('/');
      const name = parts[i];
      const isDir = i < parts.length - 1;
      if (!dirs.has(parent)) { dirs.set(parent, new Map()); }
      dirs.get(parent).set(name, isDir);
    }
  }
  return {
    readdirSync(dir, _opts) {
      const key = dir === '.' ? '' : dir;
      const entries = dirs.get(key);
      if (!entries) { throw new Error(`ENOENT ${dir}`); }
      return [...entries].map(([name, isDir]) => ({
        name, isDirectory: () => isDir, isFile: () => !isDir, isSymbolicLink: () => false,
      }));
    },
    readFileSync(file) {
      const value = tree[file];
      if (value === undefined) { throw new Error(`ENOENT ${file}`); }
      return Buffer.isBuffer(value) ? value : Buffer.from(value, 'utf-8');
    },
    statSync(file) {
      const value = tree[file];
      if (value === undefined) { throw new Error(`ENOENT ${file}`); }
      return { size: Buffer.isBuffer(value) ? value.length : Buffer.byteLength(value, 'utf-8') };
    },
  };
}

const join = (...parts) => parts.filter(p => p && p !== '.').join('/');

test('텍스트는 utf-8, 바이너리는 base64 로 담는다', () => {
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0xff, 0xfe]);
  const files = collectStaticFiles('.', fakeFs({
    'index.html': '<h1>안녕</h1>',
    'img/logo.png': png,
  }), join);

  const byPath = Object.fromEntries(files.map(f => [f.path, f]));
  assert.strictEqual(byPath['index.html'].encoding, 'utf-8');
  assert.strictEqual(byPath['index.html'].content, '<h1>안녕</h1>');

  assert.strictEqual(byPath['img/logo.png'].encoding, 'base64');
  // **원본 바이트가 그대로 살아 있어야 한다.** utf-8 로 읽었다면 0xff 0xfe 가
  // U+FFFD 로 바뀌어 되돌릴 수 없다.
  assert.deepStrictEqual(
    Buffer.from(byPath['img/logo.png'].content, 'base64'),
    png,
    '바이너리가 손상됐다 — 업로드는 성공하고 브라우저에서만 깨진다'
  );
});

test('목록에 없는 바이너리 자산도 base64로 원본 바이트를 보존한다', () => {
  const glb = Buffer.from([0x67, 0x6c, 0x54, 0x46, 0x02, 0x00, 0xff, 0xfe]);
  const files = collectStaticFiles('.', fakeFs({
    'index.html': '<script src="app.js"></script>',
    'assets/scene.glb': glb,
  }), join);
  const scene = files.find(file => file.path === 'assets/scene.glb');
  assert.strictEqual(scene.encoding, 'base64');
  assert.deepStrictEqual(Buffer.from(scene.content, 'base64'), glb);
});

test('같은 저장소는 다른 클론 경로·SSH/HTTPS 표기에서도 같은 S3 프로젝트 식별자를 쓴다', () => {
  const https = normalizeRepositoryIdentity('https://github.com/team-chain/Re-Coder.git');
  const ssh = normalizeRepositoryIdentity('git@github.com:team-chain/Re-Coder.git');
  assert.strictEqual(https, ssh, '같은 원격 저장소를 서로 다른 프로젝트로 본다');

  const first = s3ProjectIdentifier(https, 'first-local-clone');
  const second = s3ProjectIdentifier(ssh, 'another-local-clone');
  const other = s3ProjectIdentifier('https://github.com/team-chain/other-site.git', 'other-site');
  assert.strictEqual(first, second, '재클론·폴더 이동 뒤 새 버킷을 만든다');
  assert.notStrictEqual(first, other, '서로 다른 저장소가 같은 버킷을 공유한다');
  assert.ok(first.startsWith('re-coder-'));
  assert.ok(!first.includes('local-clone'), '로컬 경로·이름이 공개 버킷 이름에 드러난다');
});

test('걸러야 할 폴더는 수집하지 않는다', () => {
  const files = collectStaticFiles('.', fakeFs({
    'index.html': 'x',
    'node_modules/react/index.js': 'y',
    '.git/config': 'z',
  }), join);
  assert.deepStrictEqual(files.map(f => f.path), ['index.html']);
});

test('상한을 넘으면 자르지 않고 던진다', () => {
  // 조용히 30개만 올리면 사이트가 반쯤 올라간 채로 "배포 성공" 이 되고,
  // 사용자는 뭐가 빠졌는지 모른다.
  const tree = {};
  for (let i = 0; i < MAX_FILES + 5; i++) { tree[`f${i}.html`] = 'x'; }
  assert.throws(
    () => collectStaticFiles('.', fakeFs(tree), join, 'dist'),
    /30개|최대/,
    '상한을 넘겼는데 조용히 잘랐다'
  );
});

test('상한 초과 메시지가 **진짜 개수**를 말한다', () => {
  // 읽으면서 상한에서 멈추면 "31개" 라고밖에 못 한다. 그런데 400개가
  // 나왔다면 폴더를 잘못 고른 것이고 32개라면 몇 개만 빼면 된다 —
  // 사용자가 할 행동이 완전히 다르다.
  const tree = {};
  const total = MAX_FILES + 70;
  for (let i = 0; i < total; i++) { tree[`f${i}.html`] = 'x'; }
  try {
    collectStaticFiles('.', fakeFs(tree), join, 'src');
    assert.fail('던지지 않았다');
  } catch (err) {
    assert.strictEqual(err.count, total, `개수를 ${err.count} 로 잘라서 보고했다`);
    assert.match(err.message, new RegExp(String(total)));
  }
});

test('음성대조 — 상한과 같으면 통과한다', () => {
  const tree = {};
  for (let i = 0; i < MAX_FILES; i++) { tree[`f${i}.html`] = 'x'; }
  const files = collectStaticFiles('.', fakeFs(tree), join, 'dist');
  assert.strictEqual(files.length, MAX_FILES);
});

test('상한 메시지가 어느 폴더를 봤는지 알려준다', () => {
  // 코어도 같은 상한을 걸지만 "30개까지입니다" 라고만 한다. 진짜 원인은
  // 대개 폴더를 잘못 고른 것이다.
  const message = describeTooManyFiles(412, 'src');
  assert.match(message, /'src'/);
  assert.match(message, /412/);
  assert.match(message, /dist/);
});

test('읽을 수 없는 폴더는 빠진 자산 없이 배포가 중단된다', () => {
  const broken = fakeFs({ 'index.html': 'x', 'secret/app.js': 'x' });
  const original = broken.readdirSync.bind(broken);
  broken.readdirSync = (dir, opts) => {
    if (dir === 'secret') { throw new Error('EACCES'); }
    return original(dir, opts);
  };
  assert.throws(
    () => collectStaticFiles('.', broken, join),
    (err) => err instanceof StaticAssetReadError
      && err.relativePath === 'secret'
      && /secret/.test(err.message),
    '폴더를 조용히 건너뛰면 그 안의 JS/CSS가 빠진 성공 배포가 된다'
  );
});

test('읽을 수 없는 파일은 경로를 알리고 배포가 중단된다', () => {
  const broken = fakeFs({ 'index.html': 'x', 'assets/app.js': 'x' });
  const original = broken.readFileSync.bind(broken);
  broken.readFileSync = (file) => {
    if (file === 'assets/app.js') { throw new Error('EACCES'); }
    return original(file);
  };
  assert.throws(
    () => collectStaticFiles('.', broken, join),
    (err) => err instanceof StaticAssetReadError
      && err.relativePath === 'assets/app.js'
      && /assets\/app\.js/.test(err.message),
    '읽기 실패한 파일을 누락한 채 성공으로 처리한다'
  );
});

test('심볼릭 링크 자산은 조용히 누락하지 않고 배포를 중단한다', () => {
  const linked = fakeFs({ 'index.html': 'x' });
  const original = linked.readdirSync.bind(linked);
  linked.readdirSync = (dir, opts) => {
    const entries = original(dir, opts);
    if (dir === '.') {
      entries.push({
        name: 'assets-link', isDirectory: () => false, isFile: () => false,
        isSymbolicLink: () => true,
      });
    }
    return entries;
  };
  assert.throws(
    () => collectStaticFiles('.', linked, join),
    (err) => err instanceof StaticAssetSymlinkError
      && err.relativePath === 'assets-link'
      && /심볼릭 링크/.test(err.message),
    '링크 자산을 건너뛰면 실제 사이트에 필요한 파일이 빠진 성공 배포가 된다',
  );
});

test('상한 초과 자산은 읽기 전에 로컬에서 차단한다', () => {
  const oversized = fakeFs({
    'index.html': 'x',
    'assets/demo.mp4': Buffer.alloc(MAX_BYTES_PER_FILE + 1),
  });
  const original = oversized.readFileSync.bind(oversized);
  let oversizedWasRead = false;
  oversized.readFileSync = (file) => {
    if (file === 'assets/demo.mp4') { oversizedWasRead = true; }
    return original(file);
  };
  assert.throws(
    () => collectStaticFiles('.', oversized, join),
    (err) => err instanceof StaticAssetTooLargeError
      && err.relativePath === 'assets/demo.mp4'
      && /3,000,000/.test(err.message),
  );
  assert.strictEqual(oversizedWasRead, false, '큰 파일을 먼저 읽어 확장 호스트 메모리를 소모한다');
});

// ---------------------------------------------------------------------------
// 배선 — **이게 없어서 코어의 라우트가 여태 도달 불가능이었다**
// ---------------------------------------------------------------------------

const read = (rel) => fs.readFileSync(path.join(__dirname, rel), 'utf8');

test('ApiClient 가 /api/deploy/s3 를 호출한다', () => {
  const source = read('../src/core/ApiClient.ts');
  assert.match(source, /\/api\/deploy\/s3/, '코어 라우트를 부르는 코드가 없다');
  assert.match(source, /deployS3/);
  assert.match(source, /S3_DEPLOY_TIMEOUT_MS\s*=\s*5\s*\*\s*60\s*\*\s*1000/,
    'S3 업로드가 기본 30초 제한을 그대로 쓴다');
});

test('SidebarProvider 가 파일을 읽어 코어로 넘긴다', () => {
  const source = read('../src/sidebar/SidebarProvider.ts');
  assert.match(source, /case 'workspace\.deploy\.s3':/, '웹뷰가 요청해도 받는 곳이 없다');
  assert.match(source, /collectStaticFiles/, '파일을 읽는 쪽이 없다');
  assert.match(source, /deployS3/);
  assert.match(source, /s3ProjectIdentifier/, '폴더명만 보내 서로 다른 프로젝트가 같은 버킷을 쓴다');
  assert.match(source, /remote', 'get-url', 'origin'/,
    '로컬 절대 경로 대신 Git 원격 주소로 S3 프로젝트를 식별해야 한다');
});

test('S3 수집 전 선택 루트의 실제 경로가 워크스페이스 안인지 확인한다', () => {
  const source = read('../src/sidebar/SidebarProvider.ts');
  assert.match(source, /fs\.realpathSync\(workspacePath\)/,
    '워크스페이스의 실제 경로를 확인하지 않아 상위 심볼릭 링크를 놓친다');
  assert.match(source, /fs\.realpathSync\(root\)/,
    '선택한 배포 폴더의 실제 경로를 확인하지 않는다');
  assert.match(source, /path\.relative\(realWorkspacePath, realRoot\)/,
    '문자열 접두사 대신 경로 조상 관계로 containment를 확인해야 한다');
  assert.match(source, /collectStaticFiles\(realRoot,/,
    '검증한 실제 경로가 아닌 원래 링크 경로를 다시 수집한다');
});

test('S3 탭에 실제 배포 버튼과 URL 표시가 있다', () => {
  const source = read('../webview-src/components/DeploymentCenter.tsx');
  assert.match(source, /workspace\.deploy\.s3"/, '배포를 요청하는 버튼이 없다');
  // URL 을 안 보여 주면 사용자는 배포하고도 어디로 가야 할지 모른다.
  assert.match(source, /s3Result\.url/, '공개 URL 을 화면에 안 보여 준다');
});
