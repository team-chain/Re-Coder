import * as path from 'path';
import * as fs from 'fs';

/**
 * 이미 떠 있는 Core 를 재사용해도 되는가.
 *
 * 설치 실행과 F5 개발 실행 모두, 선택한 실행 파일과 runtime의 entrypoint가
 * 같아야 재연결한다. 바이너리 없는 경량 설치본만 수동 실행 Core에 연결한다.
 *
 * @param expectedEntrypoint 선택한 바이너리 또는 개발 소스 main.py. 수동 연결만 하면 null.
 * @param runtimeEntrypoint runtime.json 의 entrypoint. 구버전 Core 는 null.
 */
export function shouldReuseRunningCore(
    expectedEntrypoint: string | null | undefined,
    runtimeEntrypoint: string | null | undefined,
): boolean {
    if (!expectedEntrypoint) { return true; }

    // 실행 경로를 알 수 없는 구버전 Core도 자동으로 재사용하지 않는다.
    if (!runtimeEntrypoint) { return false; }

    return samePath(expectedEntrypoint, runtimeEntrypoint);
}

/**
 * 파일 경로 동일성 비교.
 *
 * Windows 는 대소문자를 구분하지 않고 구분자도 섞여 들어온다
 * (`C:\proj\core\main.py` vs `C:/proj/core/main.py`). 문자열을 그대로
 * 비교하면 같은 파일을 다르다고 판정해 매번 재spawn 하게 된다.
 */
export function samePath(a: string, b: string): boolean {
    const norm = (p: string): string => {
        // Core는 symlink를 해석한 경로를 기록한다. PATH/설치 경로에도 동일 적용.
        const canonical = fs.existsSync(p) ? fs.realpathSync(p) : path.resolve(p);
        const resolved = canonical.replace(/[\\/]+$/, '');
        return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
    };
    try {
        return norm(a) === norm(b);
    } catch {
        return false;
    }
}

/**
 * 이 오류가 **코어에 못 닿아서** 난 것인가.
 *
 * 왜 구분해야 하나
 *   「다시 검사」에 재연결을 붙이면서, 모든 실패를 연결 끊김으로 취급했다.
 *   코어는 멀쩡한데 `/api/deploy/preflight` 가 500 을 내면 — 예를 들어
 *   워크스페이스에 읽을 수 없는 파일이 있으면 — 사용자에게는 "코어가
 *   실행 중인지 확인해 주세요" 가 뜨고 **진짜 원인은 안쪽 catch 가 삼킨다.**
 *
 *   게다가 dev 모드에서 entrypoint 가 안 맞으면 ensureRunning() 이
 *   cleanupStale() 로 이어져 **멀쩡한 코어를 SIGTERM/SIGKILL 한다.**
 *   애플리케이션 오류 하나가 코어를 죽이는 셈이다.
 */
export function isCoreConnectionFailure(message: string): boolean {
    const text = (message ?? '').toLowerCase();
    if (!text) { return false; }
    return [
        'fetch failed',
        'econnrefused',
        'econnreset',
        'ehostunreach',
        'enotfound',
        'socket hang up',
        'network error',
        'failed to fetch',
        'core not running',
        'core is not running',
        '코어가 실행',
        'aborted',
        'timeout',
    ].some(marker => text.includes(marker));
}

/** 확장에 들어 있는 Core 실행 파일 경로면 그 확장 버전(`recoder-team.recoder-1.1.21/bin/…` → [1,1,21]). */
export function bundledCoreVersion(entrypoint: string | null | undefined): number[] | null {
    const m = /recoder-team\.recoder-(\d+)\.(\d+)\.(\d+)[^\\/]*[\\/]bin[\\/]recoder-core/i.exec(entrypoint || '');
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

/**
 * 실행 중인 Core 가 **같은 확장의 이전 버전**이 띄운 것인가.
 *
 * 확장을 업데이트하면 창을 다시 불러오기 전까지 이전 버전 Core 가 남는다. 예전에는 새 확장이
 * "다른 실행 경로의 Core 가 실행 중" 으로 막히거나, 사용자는 모르고 이전 Core 의 동작(이미 고친
 * 오류)을 계속 봤다(실기기: 1.1.21 을 설치했는데 1.1.18 Core 가 코드 생성을 처리). 이전 버전이면
 * 새 확장이 넘겨받는다. 반대 방향(새 Core 를 옛 창이 끄는 것)은 하지 않는다.
 */
export function isOlderBundledCore(runtimeEntrypoint: string | null | undefined, expectedEntrypoint: string | null | undefined): boolean {
    const running = bundledCoreVersion(runtimeEntrypoint);
    const wanted = bundledCoreVersion(expectedEntrypoint);
    if (!running || !wanted) { return false; }
    for (let i = 0; i < 3; i++) {
        if (running[i] !== wanted[i]) { return running[i] < wanted[i]; }
    }
    return false;
}
