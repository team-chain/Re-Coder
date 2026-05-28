/**
 * BridgeClient 의 내부 로직(펜스 필터·라인 드레인·무결성 비교) 단위 테스트.
 *
 * VSCode API 의존 부분은 BridgeClient 의 *순수 함수만* 추출해서 테스트한다.
 * 실제 ws/editor 통합 테스트는 별도 e2e 스위트에서.
 *
 * 실행: node --test out/test/bridgeClient.unit.test.js
 *      (또는 npm run smoke 처럼 npm 스크립트 추가)
 */

import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

// BridgeClient 의 private 헬퍼를 테스트하기 위해 동등한 순수 함수를 여기 복제한다.
// 핵심: 이 함수들은 BridgeClient 와 *비트 단위로* 동일한 동작을 해야 한다.

interface MiniSession {
    pendingBuffer: string;
    emittedLineCount: number;
}

function isFenceLine(line: string): boolean {
    const trimmed = line.trim();
    return /^```[a-zA-Z0-9_+\-]*$/.test(trimmed);
}

function drainCleanLines(session: MiniSession, atEnd: boolean): string {
    const out: string[] = [];
    while (true) {
        const idx = session.pendingBuffer.indexOf('\n');
        if (idx === -1) {
            if (!atEnd) break;
            const lastLine = session.pendingBuffer;
            session.pendingBuffer = '';
            if (lastLine === '') break;
            if (isFenceLine(lastLine) && session.emittedLineCount > 0) break;
            out.push(lastLine);
            session.emittedLineCount++;
            break;
        }
        const line = session.pendingBuffer.slice(0, idx);
        session.pendingBuffer = session.pendingBuffer.slice(idx + 1);
        if (session.emittedLineCount === 0 && isFenceLine(line)) continue;
        out.push(line);
        session.emittedLineCount++;
    }
    if (out.length === 0) return '';
    return out.join('\n') + (atEnd ? '' : '\n');
}

function applyFenceFilter(full: string): string {
    const lines = full.split('\n');
    if (lines.length > 0 && isFenceLine(lines[0])) lines.shift();
    let lastNonEmpty = lines.length - 1;
    while (lastNonEmpty >= 0 && lines[lastNonEmpty] === '') lastNonEmpty--;
    if (lastNonEmpty >= 0 && isFenceLine(lines[lastNonEmpty])) {
        lines.splice(lastNonEmpty, 1);
    }
    return lines.join('\n');
}


// ── 펜스 라인 식별 ───────────────────────────────────────────────────────

describe('isFenceLine', () => {
    it('단독 ``` 라인은 펜스', () => {
        assert.equal(isFenceLine('```'), true);
        assert.equal(isFenceLine('  ```'), true);
        assert.equal(isFenceLine('```   '), true);
    });

    it('```lang 형식 펜스 인식', () => {
        assert.equal(isFenceLine('```html'), true);
        assert.equal(isFenceLine('```python'), true);
        assert.equal(isFenceLine('```ts'), true);
        assert.equal(isFenceLine('```c++'), true);
    });

    it('본문 안의 ``` 는 펜스 아님 — XSS 회귀 방지', () => {
        // 이전 버그: drawGhost 안의 const str = '```' 같은 코드가 펜스로 오인됨
        assert.equal(isFenceLine('const str = "```";'), false);
        assert.equal(isFenceLine('echo "```"'), false);
        assert.equal(isFenceLine('// ``` end of section'), false);
    });

    it('빈 줄/일반 코드는 펜스 아님', () => {
        assert.equal(isFenceLine(''), false);
        assert.equal(isFenceLine('function foo() {'), false);
        assert.equal(isFenceLine('<!DOCTYPE html>'), false);
        assert.equal(isFenceLine('  margin: 0;'), false);
    });
});


// ── drainCleanLines ──────────────────────────────────────────────────────

describe('drainCleanLines', () => {
    it('완전한 라인만 출력, 미완성 라인은 버퍼에 남김', () => {
        const s = { pendingBuffer: 'a\nb\nc partial', emittedLineCount: 0 };
        const out = drainCleanLines(s, false);
        assert.equal(out, 'a\nb\n');
        assert.equal(s.pendingBuffer, 'c partial');
        assert.equal(s.emittedLineCount, 2);
    });

    it('atEnd=true 면 마지막 미완성 라인까지 강제 출력', () => {
        const s = { pendingBuffer: 'partial', emittedLineCount: 5 };
        const out = drainCleanLines(s, true);
        assert.equal(out, 'partial');
        assert.equal(s.pendingBuffer, '');
    });

    it('첫 라인 단독 펜스는 제거 (응답 시작의 ```html)', () => {
        const s = { pendingBuffer: '```html\n<!DOCTYPE html>\n', emittedLineCount: 0 };
        const out = drainCleanLines(s, false);
        assert.equal(out, '<!DOCTYPE html>\n');
    });

    it('본문 안의 ``` 는 절대 제거하지 않음 — 회귀 방지', () => {
        // 이전 버그: 코드 안의 ``` 가 제거되어 </script></body></html> 가 사라짐
        const s = {
            pendingBuffer: '<script>\nconst delim = "```";\n</script>\n',
            emittedLineCount: 5,  // 이미 본문 진입 상태
        };
        const out = drainCleanLines(s, false);
        assert.equal(out, '<script>\nconst delim = "```";\n</script>\n');
    });

    it('빈 버퍼는 빈 문자열', () => {
        const s = { pendingBuffer: '', emittedLineCount: 0 };
        assert.equal(drainCleanLines(s, false), '');
        assert.equal(drainCleanLines(s, true), '');
    });

    it('여러 번 호출해도 라인 카운트 유지 — 펜스 false positive 방지', () => {
        const s = { pendingBuffer: '```html\n<html>\n', emittedLineCount: 0 };
        drainCleanLines(s, false); // 첫 펜스 제거
        s.pendingBuffer += 'mid\n```\nmore\n';
        const out2 = drainCleanLines(s, false);
        // 두 번째 ``` 는 본문 안이므로 살아남는다
        assert.equal(out2, 'mid\n```\nmore\n');
    });
});


// ── applyFenceFilter (무결성 검증용 전체 텍스트 필터) ──────────────────

describe('applyFenceFilter', () => {
    it('앞뒤 펜스 제거', () => {
        const input = '```html\n<html></html>\n```\n';
        assert.equal(applyFenceFilter(input), '<html></html>\n');
    });

    it('펜스 없는 평문은 그대로', () => {
        const input = '<!DOCTYPE html>\n<html></html>\n';
        assert.equal(applyFenceFilter(input), input);
    });

    it('본문 안의 ``` 는 보존', () => {
        const input =
            '```html\n<script>const d="```";</script>\n```\n';
        const expected = '<script>const d="```";</script>\n';
        assert.equal(applyFenceFilter(input), expected);
    });

    it('앞 펜스만 있는 경우 — 뒤는 건드리지 않음', () => {
        const input = '```\n<html>\n</html>\n';
        assert.equal(applyFenceFilter(input), '<html>\n</html>\n');
    });

    it('실제 손상 케이스 재현 — code.html 식 응답', () => {
        const input = [
            '```html',
            '<!DOCTYPE html>',
            '<html lang="ko">',
            '<head><style>.engine-buttons { display: grid; }</style></head>',
            '<body></body>',
            '</html>',
            '```',
            '',
        ].join('\n');
        const out = applyFenceFilter(input);
        assert.ok(out.startsWith('<!DOCTYPE html>'));
        assert.ok(out.includes('.engine-buttons { display: grid; }'));
        assert.ok(out.includes('</html>'));
        assert.ok(!out.includes('```'));
    });
});


// ── 시뮬레이션 — 스트리밍 청크가 작게 쪼개진 상황 ───────────────────

describe('chunk streaming simulation', () => {
    it('한 글자씩 들어와도 최종 결과는 완전한 HTML', () => {
        const full = [
            '```html',
            '<!DOCTYPE html>',
            '<html>',
            '<head><title>t</title></head>',
            '<body><h1>hi</h1></body>',
            '</html>',
            '```',
        ].join('\n');

        const s = { pendingBuffer: '', emittedLineCount: 0 };
        let assembled = '';
        // 1자씩 chunk 로 흘려넣기 — 진짜 Bedrock 스트림 시뮬레이션
        for (const ch of full) {
            s.pendingBuffer += ch;
            assembled += drainCleanLines(s, false);
        }
        // end 시 atEnd 로 한 번 더
        assembled += drainCleanLines(s, true);

        // 펜스는 사라지고 본문은 완전 보존
        assert.ok(assembled.startsWith('<!DOCTYPE html>'));
        assert.ok(assembled.includes('<title>t</title>'));
        // 마지막 행이 </html> — 트레일링 개행은 허용
        assert.ok(/<\/html>\s*$/.test(assembled), `tail: ${JSON.stringify(assembled.slice(-30))}`);
        assert.ok(!assembled.includes('```'));
    });

    it('Bedrock 의 messageStop 직후 end 가 와도 마지막 라인 보존', () => {
        const s = { pendingBuffer: '<final>without newline', emittedLineCount: 3 };
        const out = drainCleanLines(s, true);
        assert.equal(out, '<final>without newline');
    });

    it('펜스 없이 시작하는 응답도 정상 처리', () => {
        const s = { pendingBuffer: '<!DOCTYPE html>\n<html>\n', emittedLineCount: 0 };
        const out = drainCleanLines(s, false);
        assert.equal(out, '<!DOCTYPE html>\n<html>\n');
    });
});


// ── 큐 직렬화 ─────────────────────────────────────────────────────────

describe('Promise queue serialization', () => {
    function makeQueue() {
        let q: Promise<void> = Promise.resolve();
        return {
            enqueue(work: () => Promise<void>) {
                q = q.then(work).catch(() => {});
                return q;
            },
            wait() { return q; },
        };
    }

    it('큐에 push 된 작업은 도착 순서대로 실행', async () => {
        const q = makeQueue();
        const log: number[] = [];

        // 일부러 늦게 끝나는 작업
        q.enqueue(async () => {
            await new Promise((r) => setTimeout(r, 20));
            log.push(1);
        });
        q.enqueue(async () => {
            await new Promise((r) => setTimeout(r, 0));
            log.push(2);
        });
        q.enqueue(async () => {
            log.push(3);
        });

        await q.wait();
        assert.deepEqual(log, [1, 2, 3]);
    });

    it('한 작업이 throw 해도 다음 작업이 계속 실행 — 회귀 방지', async () => {
        const q = makeQueue();
        const log: number[] = [];

        q.enqueue(async () => {
            log.push(1);
            throw new Error('boom');
        });
        q.enqueue(async () => {
            log.push(2);
        });
        await q.wait();
        assert.deepEqual(log, [1, 2]);
    });
});
