import * as fs from 'fs';
import * as path from 'path';
import { StringDecoder } from 'string_decoder';

type Stream = 'stdout' | 'stderr';
const MAX_LINE_CHARS = 16 * 1024;
const OMITTED_LINE = '[log line omitted: exceeds size limit]';

interface LogOptions {
    maxBytes?: number;
    backups?: number;
    secrets?: string[];
    onError?: (error: unknown) => void;
}

/** Core를 실행한 확장이 출력과 종료 원인을 함께 기록한다. */
export class CoreProcessLog {
    private readonly maxBytes: number;
    private readonly backups: number;
    private readonly decoders = { stdout: new StringDecoder('utf8'), stderr: new StringDecoder('utf8') };
    private readonly pending = { stdout: '', stderr: '' };
    private readonly dropping = { stdout: false, stderr: false };
    private readonly secrets = new Set<string>();
    private finished = false;
    private warned = false;
    private pid: number | undefined;

    constructor(private readonly file: string, private readonly options: LogOptions = {}) {
        this.maxBytes = options.maxBytes ?? 2 * 1024 * 1024;
        this.backups = options.backups ?? 3;
        if (!Number.isSafeInteger(this.maxBytes) || this.maxBytes < 256
            || !Number.isSafeInteger(this.backups) || this.backups < 1) {
            throw new RangeError('Invalid Core log size or backup count');
        }
        this.addSecrets(options.secrets ?? []);
    }

    setPid(pid: number | undefined): void { this.pid = pid; }

    addSecrets(values: string[]): void {
        for (const value of values) {
            if (typeof value === 'string' && value.length >= 8) { this.secrets.add(value); }
        }
    }

    write(stream: Stream, chunk: Buffer): void {
        if (!this.finished) { this.consume(stream, this.decoders[stream].write(chunk)); }
    }

    event(message: string): void {
        this.append('lifecycle', message.replace(/[\r\n]+/g, ' '));
    }

    /** exitより後に最後のpipeデータが届くため、closeイベントで呼ぶ。 */
    finish(): void {
        if (this.finished) { return; }
        for (const stream of ['stdout', 'stderr'] as const) {
            this.consume(stream, this.decoders[stream].end());
            if (this.pending[stream]) { this.append(stream, this.pending[stream]); }
            this.pending[stream] = '';
        }
        this.finished = true;
    }

    private consume(stream: Stream, text: string): void {
        let offset = 0;
        let newline: number;
        while ((newline = text.indexOf('\n', offset)) !== -1) {
            if (!this.dropping[stream]) {
                const line = this.pending[stream] + text.slice(offset, newline);
                this.append(stream, line.length > MAX_LINE_CHARS ? OMITTED_LINE : line.replace(/\r$/, ''));
            }
            this.pending[stream] = '';
            this.dropping[stream] = false;
            offset = newline + 1;
        }
        if (!this.dropping[stream]) {
            this.pending[stream] += text.slice(offset);
            if (this.pending[stream].length > MAX_LINE_CHARS) {
                // 긴 줄을 중간에서 자르면 비밀 값의 일부가 남을 수 있어 줄 전체를 생략한다.
                this.append(stream, OMITTED_LINE);
                this.pending[stream] = '';
                this.dropping[stream] = true;
            }
        }
    }

    private redact(text: string): string {
        let safe = text;
        for (const value of [...this.secrets].sort((a, b) => b.length - a.length)) {
            safe = safe.split(value).join('[REDACTED]');
        }
        safe = safe.replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, '[REDACTED]');
        return safe.replace(
            /(\b(?:authorization|x-session-token|(?:aws[_-])?(?:access[_-]key(?:[_-]id)?|secret[_-](?:access[_-])?key|session[_-]token)|api[_-]?key|password|token)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Bearer\s+|Basic\s+)?[^\s,;}]+)/gi,
            '$1[REDACTED]',
        );
    }

    private append(source: string, message: string): void {
        if (!message) { return; }
        try {
            const prefix = `${new Date().toISOString()} [host=${process.pid} pid=${this.pid ?? '?'}] [${source}] `;
            let entry = `${prefix}${this.redact(message)}\n`;
            if (Buffer.byteLength(entry, 'utf8') > Math.min(this.maxBytes, 64 * 1024)) {
                entry = `${prefix}${OMITTED_LINE}\n`;
            }
            fs.mkdirSync(path.dirname(this.file), { recursive: true, mode: 0o700 });
            if (fs.existsSync(this.file)) {
                const stat = fs.lstatSync(this.file);
                if (!stat.isFile()) { throw new Error('Core log path is not a regular file'); }
                if (stat.size + Buffer.byteLength(entry, 'utf8') > this.maxBytes) {
                    for (let i = this.backups; i >= 1; i--) {
                        const from = i === 1 ? this.file : `${this.file}.${i - 1}`;
                        const to = `${this.file}.${i}`;
                        if (fs.existsSync(to)) { fs.unlinkSync(to); }
                        if (fs.existsSync(from)) { fs.renameSync(from, to); }
                    }
                }
            }
            // 다른 창이 로그를 순환시켜도 현재 파일에 이어 쓰도록 매번 연다.
            const fd = fs.openSync(this.file, fs.constants.O_WRONLY | fs.constants.O_CREAT
                | fs.constants.O_APPEND | (fs.constants.O_NOFOLLOW ?? 0), 0o600);
            try {
                if (process.platform !== 'win32') { fs.fchmodSync(fd, 0o600); }
                fs.writeSync(fd, entry, undefined, 'utf8');
            } finally {
                fs.closeSync(fd);
            }
        } catch (error) {
            // 로그 저장 실패가 Core를 중단하지 않도록 경고만 한 번 남긴다.
            if (!this.warned) {
                this.warned = true;
                try { this.options.onError?.(error); } catch { /* logging is best effort */ }
            }
        }
    }
}
