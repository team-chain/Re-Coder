import * as path from 'path';
import { Worker } from 'worker_threads';
import type { ProjectResult, FileResult } from './analyzer';
import type { FlowGraph, FlowPreview } from './flow';

export interface AnalysisOptions {
    /** project(기본) · file · flow(dry run 그래프) · preview(적용 전 흐름 미리 보기) */
    mode?: 'project' | 'file' | 'flow' | 'preview';
    /** preview: 상대 경로 → 바뀔 내용(null 이면 지움) */
    overrides?: Array<[string, string | null]>;
}

// File reads and parsing must never block chat, approvals or the VS Code host.
export class AnalysisJob {
    private cancelCurrent?: () => void;
    constructor(private timeoutMs = 30000) {}

    cancel(): void { this.cancelCurrent?.(); }

    run(workspace: string, file = '', options: AnalysisOptions = {}): Promise<ProjectResult | FileResult | FlowGraph | FlowPreview> {
        this.cancel();
        return new Promise((resolve, reject) => {
            const worker = new Worker(path.join(__dirname, 'analysisWorker.js'), { workerData: { workspace, file, mode: options.mode, overrides: options.overrides } });
            let settled = false;
            const finish = (error?: Error, graph?: ProjectResult | FileResult | FlowGraph | FlowPreview) => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                if (this.cancelCurrent === cancel) this.cancelCurrent = undefined;
                void worker.terminate();
                if (error) reject(error); else resolve(graph!);
            };
            const cancel = () => finish(new Error('소스 분석을 취소했습니다.'));
            const timer = setTimeout(() => finish(new Error('소스 분석 시간이 초과되었습니다. 프로젝트의 접근 권한과 로컬 파일 동기화 상태를 확인한 뒤 다시 분석하세요.')), this.timeoutMs);
            this.cancelCurrent = cancel;
            worker.once('message', result => finish(result.error ? new Error(result.error) : undefined, result.graph));
            worker.once('error', error => finish(error));
            worker.once('exit', code => { if (!settled) finish(new Error(`소스 분석 작업이 결과 없이 종료되었습니다 (${code}).`)); });
        });
    }
}
