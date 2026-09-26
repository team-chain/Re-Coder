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
const os = require('node:os');

const {
  isBinaryAsset,
  isSensitiveFile,
  isDeployableAsset,
  shouldSkipPath,
  pickStaticDir,
  collectStaticFiles,
  describeExcludedFiles,
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

test('빌드하지 않은 CRA/Vite 진입 파일은 업로드 전에 멈추고 빌드 결과는 허용한다', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'recoder-static-entry-'));
  try {
    for (const html of ['<link href="%PUBLIC_URL%/favicon.ico">', '<script type="module" src="/src/main.tsx"></script>']) {
      fs.writeFileSync(path.join(root, 'index.html'), html);
      assert.throws(() => collectStaticFiles(root, fs, path.join), /npm run build/);
    }
    fs.writeFileSync(path.join(root, 'index.html'), '<script src="/assets/main.js"></script>');
    assert.equal(collectStaticFiles(root, fs, path.join).files.length, 1);
  } finally {
    assert.ok(root.startsWith(path.join(os.tmpdir(), 'recoder-static-entry-')));
    fs.rmSync(root, {recursive:true,force:true});
  }
});

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
  const { files } = collectStaticFiles('.', fakeFs({
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
  const { files } = collectStaticFiles('.', fakeFs({
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

test('모노레포의 서로 다른 앱은 같은 원격 저장소여도 S3 프로젝트를 구분한다', () => {
  const siteA = normalizeRepositoryIdentity('git@github.com:team-chain/Re-Coder.git#apps/site-a');
  const siteB = normalizeRepositoryIdentity('https://github.com/team-chain/Re-Coder.git#apps/site-b');
  assert.strictEqual(
    siteA,
    normalizeRepositoryIdentity('https://github.com/team-chain/Re-Coder.git#apps/site-a'),
    'SSH/HTTPS 표기 차이로 같은 모노레포 앱을 나눴다',
  );
  assert.notStrictEqual(
    s3ProjectIdentifier(siteA),
    s3ProjectIdentifier(siteB),
    '모노레포 앱끼리 같은 버킷을 공유해 재배포 때 서로 파일을 지운다',
  );
});

test('걸러야 할 폴더는 수집하지 않는다', () => {
  const { files } = collectStaticFiles('.', fakeFs({
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
  const { files } = collectStaticFiles('.', fakeFs(tree), join, 'dist');
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
// 민감 파일 차단 + 허용 목록 — **이 버킷은 공개 읽기다**
// ---------------------------------------------------------------------------
//
// 예전 필터는 "금지 목록에 없으면 전부 통과"였다. 워크스페이스 루트를
// 배포하면 `.env` 의 DB 비밀번호와 `id_rsa` 가 공개 버킷에 그대로 올라갔고,
// 화면에는 "배포 완료"와 URL 만 표시됐다. 노출을 알아챌 단서가 없었다.

test('.env·키·인증서는 이름만으로 차단한다', () => {
  for (const p of [
    '.env', '.env.production', '.env.local', '.envrc',
    'key.pem', 'server.key', 'cert.p12', 'release.keystore',
    'id_rsa', 'id_rsa.pub', 'id_ed25519',
    'credentials', '.npmrc', '.netrc', '.htpasswd',
    'secrets.yaml', 'secret.json', 'secrets.toml',
    'KEY.PEM', '.ENV',                       // 대소문자 우회
    'config/.env', 'deep/nested/id_rsa',     // 하위 폴더
    '.aws/config', '.ssh/known_hosts',       // 민감 폴더는 통째로
  ]) {
    assert.strictEqual(isSensitiveFile(p), true, `${p} 가 공개 버킷에 올라간다`);
  }
});

test('음성대조 — 이름이 비슷한 정상 자산을 오판하지 않는다', () => {
  for (const p of [
    'privacy-policy.html', 'keyboard.css', 'monkey.js',   // key 포함 이름
    'environment.js', 'env.svg',                          // env 포함 이름
    'assets/keynote.pdf', 'turkey.png',
  ]) {
    assert.strictEqual(isSensitiveFile(p), false, `${p} 를 비밀 파일로 오판해 사이트가 깨진다`);
  }
});

test('허용 목록 — 정적 자산만 배포 대상이다', () => {
  for (const p of ['index.html', 'app.js', 'style.css', 'logo.png', 'font.woff2', 'scene.glb', 'data.json']) {
    assert.strictEqual(isDeployableAsset(p), true, `${p} 가 배포에서 빠져 사이트가 깨진다`);
  }
  for (const p of ['app.py', 'main.tsx', 'Dockerfile', 'run.sh', 'tool.exe', 'LICENSE', 'db.sqlite3']) {
    assert.strictEqual(isDeployableAsset(p), false, `${p} 처럼 목록에 없는 파일이 통과한다`);
  }
});

test('수집 결과 — 민감/비자산 파일은 업로드에서 빠지고, 빠졌다고 보고된다', () => {
  const { files, excludedSensitive, excludedNonAsset } = collectStaticFiles('.', fakeFs({
    'index.html': '<h1>hi</h1>',
    'app.js': 'x',
    '.env': 'DB_PASSWORD=hunter2',
    'id_rsa': 'PRIVATE',
    'secrets.yaml': 'token: abc',
    'server.py': 'print(1)',
  }), join);

  assert.deepStrictEqual(files.map(f => f.path).sort(), ['app.js', 'index.html']);
  assert.deepStrictEqual([...excludedSensitive].sort(), ['.env', 'id_rsa', 'secrets.yaml'],
    '비밀 파일이 공개 버킷으로 나간다');
  assert.deepStrictEqual(excludedNonAsset, ['server.py']);
  //: 내용까지 확인 — 제외 목록에만 있고 files 에도 있으면 의미가 없다.
  assert.ok(!files.some(f => /hunter2|PRIVATE/.test(f.content)), '비밀 내용이 업로드 본문에 남아 있다');
});

test('음성대조 — 정상 산출물만 있으면 아무것도 제외되지 않는다', () => {
  const { files, excludedSensitive, excludedNonAsset } = collectStaticFiles('.', fakeFs({
    'index.html': 'x', 'assets/app.js': 'x', 'assets/style.css': 'x',
  }), join);
  assert.strictEqual(files.length, 3);
  assert.deepStrictEqual(excludedSensitive, []);
  assert.deepStrictEqual(excludedNonAsset, []);
});

test('파일 수 상한은 실제로 올라갈 파일 기준이다', () => {
  // 소스 파일까지 세면, 자산 5개짜리 정상 배포가 소스 폴더라는 이유로 막힌다.
  const tree = { 'index.html': 'x' };
  for (let i = 0; i < MAX_FILES + 10; i++) { tree[`src/mod${i}.py`] = 'x'; }
  const { files, excludedNonAsset } = collectStaticFiles('.', fakeFs(tree), join, '');
  assert.strictEqual(files.length, 1);
  assert.strictEqual(excludedNonAsset.length, MAX_FILES + 10);
});

test('제외 안내문은 무엇이 왜 빠졌는지 말한다', () => {
  const note = describeExcludedFiles({
    excludedSensitive: ['.env', 'id_rsa'],
    excludedNonAsset: ['a.py', 'b.py', 'c.py', 'd.py'],
  });
  assert.match(note, /비밀 정보로 보이는 파일 2개/);
  assert.match(note, /\.env/);
  assert.match(note, /id_rsa/);
  assert.match(note, /정적 자산이 아닌 파일 4개/);
  assert.match(note, /외 1개/, '개수를 잘라 보여주면서 몇 개가 더 있는지 말하지 않는다');
  assert.strictEqual(describeExcludedFiles({ excludedSensitive: [], excludedNonAsset: [] }), '');
});

test('배선 — 제외 내역이 결과 화면까지 전달된다', () => {
  // 걸러 놓고 화면에 안 보여주면, 사용자는 "왜 그 파일이 사이트에 없지"를
  // 알 수 없고 필터는 없는 것과 같다.
  const provider = read('../src/sidebar/SidebarProvider.ts');
  assert.match(provider, /excluded_sensitive/, '민감 제외 내역을 결과에 담지 않는다');
  assert.match(provider, /describeExcludedFiles/, '제외 안내문을 만들지 않는다');

  const center = read('../webview-src/components/DeploymentCenter.tsx');
  assert.match(center, /excluded_note/, '결과 화면이 제외 안내문을 보여주지 않는다');
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
  assert.match(source, /rev-parse', '--show-toplevel'/,
    '모노레포 앱을 구분할 저장소 루트를 찾지 않는다');
  assert.match(source, /path\.relative\(realRoot, realWorkspace\)/,
    '저장소 루트 기준 앱 경로를 S3 프로젝트 ID에 넣지 않는다');
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
