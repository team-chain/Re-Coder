/** 봇 브리지 자동 연결 여부. vscode 런타임에 의존하지 않는다(테스트용). */
interface InspectableConfig {
    inspect<T>(section: string): { globalValue?: T } | undefined;
}
/**
 * 봇 브리지를 켤지. 명시 설정(enabled)이 우선이다. 설정하지 않았으면 **사용자 설정**에
 * 봇 서버 연결 정보(token·studentId·registrationKey)를 넣어 둔 기존 봇 사용자만 예전처럼 켠다 —
 * 작업 영역(.vscode/settings.json) 값으로는 켜지지 않는다.
 */
export function bridgeEnabled(cfg: InspectableConfig): boolean {
    const explicit = cfg.inspect<boolean>('enabled');
    if (typeof explicit?.globalValue === 'boolean') { return explicit.globalValue; }
    return ['token', 'studentId', 'registrationKey'].some(key => {
        const value = cfg.inspect<string>(key)?.globalValue;
        return typeof value === 'string' && value.trim() !== '';
    });
}
