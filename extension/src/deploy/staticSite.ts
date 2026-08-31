/**
 * 정적 사이트 파일 수집 — S3 BYO 배포(FR-05-03)의 확장 쪽 절반.
 *
 * 코어에는 `POST /api/deploy/s3` 가 완성돼 있다. 버킷 생성, 공개 설정,
 * 업로드, URL 조립까지 다 한다. 그런데 **확장이 그걸 한 번도 부르지 않았다** —
 * 사용자 파일을 읽어 보내는 쪽이 없어서, 코어의 그 경로 전체가 도달 불가능
 * 상태였다.
 *
 * 여기서 하는 일은 하나다. 어떤 파일을 올릴지 정하고, 코어가 받는 모양
 * (`{path, content, encoding}`)으로 바꾼다.
 *
 * 왜 이 판단을 순수 함수로 빼는가
 *   잘못 골라도 예외가 안 난다. node_modules 를 통째로 올리거나, 이미지를
 *   utf-8 로 읽어 깨진 채로 올리거나, 빌드 폴더 대신 소스 폴더를 올려도
 *   업로드는 "성공" 한다. 사용자는 열어 본 다음에야 안다.
 */

/** 정적 산출물이 놓이는 흔한 폴더. 앞쪽이 우선. */
export const STATIC_DIR_CANDIDATES = ['dist', 'build', 'out', 'public', '_site'];

/**
 * 올리면 안 되는 경로.
 *
 * `.git` 과 `node_modules` 를 안 거르면 파일 수 상한(30개)을 즉시 넘겨서
 * "파일이 너무 많습니다" 만 보게 된다. 진짜 원인은 폴더 선택인데 메시지는
 * 개수 얘기만 하므로, 사용자는 무엇을 고쳐야 할지 알 수 없다.
 */
const SKIP_DIRS = new Set([
    'node_modules', '.git', '.svn', '.hg', '.venv', 'venv', '__pycache__',
    '.next/cache', '.cache', '.idea', '.vscode', 'coverage', '.pytest_cache',
]);

const SKIP_FILES = new Set(['.DS_Store', 'Thumbs.db', '.gitkeep']);

/**
 * UTF-8 텍스트임을 확신할 수 있는 확장자.
 *
 * 바이너리 목록을 유지하면 `.glb`, `.cur`처럼 새로 등장한 자산 하나를 빠뜨릴
 * 때마다 UTF-8 변환으로 원본 바이트가 깨진다. 따라서 반대로 **알려진 텍스트만**
 * 텍스트로 보내고, 나머지는 base64로 보존한다. base64는 텍스트 자산에도 안전하지만
 * 알려진 텍스트는 응답 크기를 줄이기 위해 UTF-8을 유지한다.
 */
const TEXT_EXTENSIONS = new Set([
    'html', 'htm', 'css', 'js', 'mjs', 'cjs', 'json', 'map', 'svg', 'txt',
    'xml', 'webmanifest', 'webapp', 'md', 'csv', 'tsv',
]);

export function isBinaryAsset(filePath: string): boolean {
    const name = filePath.split('/').pop() ?? '';
    const dot = name.lastIndexOf('.');
    // 확장자가 없거나 모르는 확장자는 바이트 손상을 막기 위해 base64로 보낸다.
    if (dot <= 0) { return true; }
    return !TEXT_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

/**
 * 공개 버킷 이름에 쓸 워크스페이스 식별자.
 *
 * `frontend`처럼 폴더 이름만 보내면 서로 다른 저장소가 같은 버킷을 재사용한다.
 * 전체 로컬 경로는 URL에 드러나면 안 되므로, 사람이 읽는 폴더명 뒤에 경로의
 * 안정적인 지문만 붙인다. 같은 위치를 다시 배포하면 같은 버킷을 쓴다.
 */
export function s3ProjectIdentifier(workspacePath: string, folderName: string): string {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { createHash } = require('crypto') as typeof import('crypto');
    const normalized = (workspacePath || '').replace(/\\/g, '/').trim();
    const fingerprint = createHash('sha256').update(normalized || folderName || 'site').digest('hex').slice(0, 12);
    return `${(folderName || 'site').trim() || 'site'}-${fingerprint}`;
}

/** 경로의 어느 부분이든 건너뛸 대상이면 true. */
export function shouldSkipPath(relativePath: string): boolean {
    const parts = relativePath.split('/').filter(Boolean);
    if (!parts.length) { return true; }
    const name = parts[parts.length - 1];
    if (SKIP_FILES.has(name)) { return true; }
    return [...SKIP_DIRS].some((skipped) => {
        // `.next/cache`처럼 여러 경로 조각으로 이뤄진 제외 대상도 있다.
        // `parts.some(part => ...)`만 쓰면 `.next`와 `cache`를 각각 비교해
        // 이 항목은 절대 일치하지 않아 대형 캐시를 전부 읽게 된다.
        const skippedParts = skipped.split('/');
        return parts.some((_, start) => skippedParts.every(
            (part, offset) => parts[start + offset] === part,
        ));
    });
}

/**
 * 워크스페이스에서 올릴 폴더를 고른다.
 *
 * 빌드 산출물 폴더가 있으면 그걸 쓴다. 소스 폴더를 올리면 브라우저가
 * `.tsx` 를 실행할 수 없어 흰 화면이 나오는데, 그 실패는 배포가 아니라
 * 앱 문제처럼 보인다.
 *
 * @param existingDirs 워크스페이스 최상단에 실제로 있는 디렉터리 이름들
 */
export function pickStaticDir(existingDirs: string[]): string {
    const available = new Set(existingDirs);
    for (const candidate of STATIC_DIR_CANDIDATES) {
        if (available.has(candidate)) { return candidate; }
    }
    //: 없으면 워크스페이스 루트. 이미 정적 파일만 있는 프로젝트도 흔하다.
    return '';
}

export type StaticFile = {
    path: string;
    content: string;
    encoding: 'utf-8' | 'base64';
};

/**
 * 선택한 정적 자산을 읽지 못했을 때의 오류.
 *
 * 파일 하나를 빼고 계속 올리면 배포 API 는 성공을 돌려주지만, 실제 사이트는
 * JS/CSS/이미지 누락으로 깨질 수 있다. 어느 경로에서 멈췄는지 함께 보여줘야
 * 사용자가 권한·삭제·동기화 문제를 바로 고칠 수 있다.
 */
export class StaticAssetReadError extends Error {
    constructor(public readonly relativePath: string, public readonly operation: 'folder' | 'file') {
        const target = relativePath || '선택한 배포 폴더';
        const action = operation === 'folder' ? '폴더 목록을 읽을 수 없습니다' : '파일을 읽을 수 없습니다';
        super(`${target}: ${action}. 권한을 확인하거나 빌드 산출물을 다시 만든 뒤 재시도하세요.`);
        this.name = 'StaticAssetReadError';
    }
}

/** 코어가 거부하기 전에 확장에서 먼저 잡는 상한. core/s3_byo.py 와 같은 값. */
export const MAX_FILES = 30;
/** 파일을 읽기·base64 변환하기 전에 막을 바이트 상한. core/s3_byo.py 와 같다. */
export const MAX_BYTES_PER_FILE = 3_000_000;

/**
 * 파일 수 상한을 넘었을 때 **무엇을 고치면 되는지** 말해 준다.
 *
 * 코어도 같은 상한을 걸지만 "30개까지입니다" 라고만 한다. 여기서는 어느
 * 폴더를 봤는지까지 붙인다 — 대개 진짜 원인은 폴더를 잘못 고른 것이다.
 */
export function describeTooManyFiles(count: number, dirLabel: string): string {
    const where = dirLabel ? `'${dirLabel}'` : '워크스페이스 루트';
    return (
        `${where} 에서 ${count}개 파일을 찾았습니다. S3 정적 배포는 최대 ` +
        `${MAX_FILES}개까지만 올립니다. 빌드 산출물 폴더(dist·build·out 등)를 ` +
        `지정했는지 확인하세요.`
    );
}

/** 업로드 결과를 한 줄로. index.html 이 없으면 사용자가 링크를 받고도 403 을 본다. */
export function describeUploadResult(result: {
    url?: string;
    uploaded?: string[];
    index_copied_from?: string | null;
}): string {
    const count = result.uploaded?.length ?? 0;
    const parts = [`${count}개 파일을 올렸습니다.`];
    if (result.index_copied_from) {
        parts.push(`index.html 이 없어 ${result.index_copied_from} 를 진입 문서로 함께 올렸습니다.`);
    }
    return parts.join(' ');
}

// ---------------------------------------------------------------------------
// 파일 수집 — fs 를 주입받아 검사 가능하게 둔다
// ---------------------------------------------------------------------------

export type FileSystemLike = {
    readdirSync(dir: string, opts: { withFileTypes: true }): Array<{
        name: string;
        isDirectory(): boolean;
        isFile(): boolean;
    }>;
    statSync(file: string): { size: number };
    readFileSync(file: string): Buffer;
};

/** 큰 자산을 확장 호스트 메모리에 올리기 전에 중단할 때의 오류. */
export class StaticAssetTooLargeError extends Error {
    constructor(public readonly relativePath: string, public readonly size: number) {
        super(
            `${relativePath}: 파일이 너무 큽니다 (${size.toLocaleString()} 바이트, ` +
            `상한 ${MAX_BYTES_PER_FILE.toLocaleString()} 바이트). ` +
            '동영상·압축 파일은 별도 CDN에 올리거나 더 작은 자산으로 바꿔 주세요.',
        );
        this.name = 'StaticAssetTooLargeError';
    }
}

/**
 * `root` 아래 파일을 코어가 받는 모양으로 모은다.
 *
 * 상한을 넘으면 **자르지 않고 던진다.** 조용히 30개만 올리면 사이트가
 * 반쯤 올라간 채로 "배포 성공" 이 되고, 사용자는 뭐가 빠졌는지 모른다.
 */
export function collectStaticFiles(
    root: string,
    fsImpl: FileSystemLike,
    join: (...parts: string[]) => string,
    dirLabel = '',
): StaticFile[] {
    //: **두 번 훑는다.** 한 번에 읽으면서 상한에서 멈추면 "31개 찾았습니다"
    //: 라고밖에 말할 수 없는데, 사용자가 알아야 할 건 진짜 개수다. 400개가
    //: 나왔다면 폴더를 잘못 고른 것이고, 32개라면 몇 개만 빼면 된다 —
    //: 완전히 다른 행동이다. 게다가 미리 세면 실패할 배포를 위해 수백 개
    //: 파일을 메모리에 읽어들이지도 않는다.
    const paths: Array<{ rel: string; full: string }> = [];

    const walk = (dir: string, prefix: string): void => {
        let entries: ReturnType<FileSystemLike['readdirSync']>;
        try {
            entries = fsImpl.readdirSync(dir, { withFileTypes: true });
        } catch {
            // 폴더를 조용히 건너뛰면 그 안의 JS/CSS가 빠져도 성공으로 보인다.
            throw new StaticAssetReadError(prefix, 'folder');
        }
        for (const entry of entries) {
            const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
            if (shouldSkipPath(rel)) { continue; }
            const full = join(dir, entry.name);
            if (entry.isDirectory()) {
                walk(full, rel);
            } else if (entry.isFile()) {
                paths.push({ rel, full });
            }
        }
    };
    walk(root, '');

    if (paths.length > MAX_FILES) {
        //: 자르지 않고 던진다. 조용히 30개만 올리면 사이트가 반쯤 올라간
        //: 채로 "배포 성공" 이 되고, 사용자는 뭐가 빠졌는지 모른다.
        throw new TooManyFilesError(paths.length, dirLabel);
    }

    const files: StaticFile[] = [];
    for (const { rel, full } of paths) {
        let size: number;
        try {
            // 읽기·base64·JSON 직렬화는 원본보다 훨씬 큰 메모리를 쓴다. 코어가
            // 결국 거부할 파일이라면 확장 호스트가 먼저 멈춰야 한다.
            size = fsImpl.statSync(full).size;
        } catch {
            throw new StaticAssetReadError(rel, 'file');
        }
        if (!Number.isFinite(size) || size < 0 || size > MAX_BYTES_PER_FILE) {
            throw new StaticAssetTooLargeError(rel, size);
        }
        let buffer: Buffer;
        try {
            buffer = fsImpl.readFileSync(full);
        } catch {
            // 자산 하나라도 빠진 "성공"은 깨진 사이트를 만들 뿐이다.
            throw new StaticAssetReadError(rel, 'file');
        }
        const binary = isBinaryAsset(rel);
        files.push({
            path: rel,
            //: 바이너리를 utf-8 로 읽으면 잘못된 바이트가 U+FFFD 로 치환돼,
            //: 업로드는 성공하는데 브라우저에서 깨진다.
            content: binary ? buffer.toString('base64') : buffer.toString('utf-8'),
            encoding: binary ? 'base64' : 'utf-8',
        });
    }
    return files;
}

export class TooManyFilesError extends Error {
    constructor(public readonly count: number, public readonly dirLabel: string) {
        super(describeTooManyFiles(count, dirLabel));
        this.name = 'TooManyFilesError';
    }
}
