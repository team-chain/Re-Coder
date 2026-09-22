import * as path from 'path';

/**
 * 이미 떠 있는 Core 를 재사용해도 되는가.
 *
 * 왜 이 판단이 필요한가
 *   개발 모드(ReCoder 저장소를 워크스페이스로 연 경우)에는 **예전 VSIX 가
 *   남긴 번들 Core** 를 재사용하면 안 된다. 그 프로세스에는 지금 고치고 있는
 *   API 가 없어서, 화면은 붙는데 기능이 조용히 옛날 동작을 한다.
 *
 * 예전 구현의 문제
 *   "개발 모드면 재사용 경로를 통째로 건너뛴다" 로 처리했다. 그런데 개발
 *   중에는 **워크스페이스의 Core 를 직접 띄워 놓는 게 정상 사용법**이다.
 *   그 경우에도 재사용 경로를 건너뛰므로, 확장은 멀쩡히 떠 있는 Core 를
 *   못 찾고 매번 새로 spawn 하려 든다. 싱글턴 락과 포트가 부딪히면서
 *   17894 ↔ 17895 로 포트가 튀었고, 연결이 한 번 끊기면 창을 리로드하기 전엔
 *   복구되지 않았다.
 *
 * 지금 방식
 *   Core 가 runtime.json 에 자기 entrypoint(절대경로)를 적는다. 그 값이
 *   워크스페이스의 main.py 와 같으면 "내 Core" 이므로 재사용한다. 다르면
 *   남의(번들) Core 이므로 원래대로 재사용하지 않는다.
 *
 * @param workspaceCorePath 개발 모드에서 워크스페이스의 core/main.py 절대경로.
 *                          개발 모드가 아니면 null.
 * @param runtimeEntrypoint runtime.json 의 entrypoint. 구버전 Core 는 null.
 */
export function shouldReuseRunningCore(
    workspaceCorePath: string | null | undefined,
    runtimeEntrypoint: string | null | undefined,
): boolean {
    // 개발 모드가 아니면(일반 사용자·VSIX) 떠 있는 Core 를 그대로 쓴다.
    if (!workspaceCorePath) { return true; }

    // 개발 모드인데 상대가 자기 정체를 안 밝힌다 = 이 필드가 없던 구버전 Core.
    // 정확히 "예전에 깔려 있던 오래된 Core" 라는 뜻이므로 재사용하지 않는다.
    if (!runtimeEntrypoint) { return false; }

    return samePath(workspaceCorePath, runtimeEntrypoint);
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
        const resolved = path.resolve(p).replace(/[\\/]+$/, '');
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
