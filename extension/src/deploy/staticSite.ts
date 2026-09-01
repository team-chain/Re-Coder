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
 * 바이너리로 읽어야 하는 확장자.
 *
 * **이걸 틀리면 조용히 깨진다.** 이미지를 utf-8 로 읽으면 잘못된 바이트가
 * U+FFFD 로 치환돼서, 업로드는 성공하고 파일 크기도 그럴듯한데 브라우저에서
 * 열면 깨진 이미지가 나온다. 원인을 배포 쪽에서 찾기 매우 어렵다.
 */
const BINARY_EXTENSIONS = new Set([
    // 이미지
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'ico', 'bmp', 'tiff',
    // 폰트
    'woff', 'woff2', 'ttf', 'otf', 'eot',
    // 미디어
    'mp3', 'mp4', 'webm', 'ogg', 'wav', 'mov', 'avi',
    // 기타
    'wasm', 'pdf', 'zip', 'gz', 'br', 'jar', 'bin',
]);

export function isBinaryAsset(filePath: string): boolean {
    const name = filePath.split('/').pop() ?? '';
    const dot = name.lastIndexOf('.');
    if (dot <= 0) { return false; }
    return BINARY_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

/** 경로의 어느 부분이든 건너뛸 대상이면 true. */
export function shouldSkipPath(relativePath: string): boolean {
    const parts = relativePath.split('/').filter(Boolean);
    if (!parts.length) { return true; }
    const name = parts[parts.length - 1];
    if (SKIP_FILES.has(name)) { return true; }
    return parts.some(part => SKIP_DIRS.has(part));
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

/** 코어가 거부하기 전에 확장에서 먼저 잡는 상한. core/s3_byo.py 와 같은 값. */
export const MAX_FILES = 30;

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
    readFileSync(file: string): Buffer;
};

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
            return;   // 읽을 수 없는 폴더는 건너뛴다 — 배포 전체를 막지 않는다
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
        let buffer: Buffer;
        try {
            buffer = fsImpl.readFileSync(full);
        } catch {
            continue;
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
