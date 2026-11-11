import * as vscode from 'vscode';
import { createHash } from 'crypto';
import { PentagiClient, PentagiRejectedRequest, pentagiOutcome, serviceUrl, testTarget } from './pentagiClient';
import {assessPentagi} from './pentagiAssessment';
import { LocalPentagiHost } from './localPentagiHost';

type Run = { id?: string; target: string; provider: string; server: string; createdAt: string; uncertain?: boolean; stopped?: boolean; terminal?: boolean };
const SECRET = 'recoder.pentagi.connection.v1';
export class RedTeamHost {
    private busy = false;
    constructor(private secrets?: vscode.SecretStorage, private state?: vscode.Memento, private localRoot?: string) {}
    private key(workspace: string) { return `security.redteam.${createHash('sha256').update(workspace).digest('hex')}`; }
    async handle(action: string, workspace: string): Promise<Record<string, unknown>> {
        if (!this.secrets || !this.state) throw new Error('보안 저장소를 사용할 수 없습니다.');
        if (!vscode.workspace.isTrusted || !workspace) throw new Error('신뢰할 수 있는 프로젝트를 먼저 여세요.');
        if (this.busy) throw new Error('이전 레드팀 요청이 끝난 뒤 다시 시도하세요.');
        this.busy = true;
        try {
            if (action.startsWith('local')) {
                if (!this.localRoot) throw new Error('로컬 검사 저장소를 사용할 수 없습니다.');
                return await new LocalPentagiHost(this.localRoot, this.secrets).handle(action, workspace);
            }
            let connection: {server: string; token: string} | null = null;
            try {
                connection = JSON.parse(await this.secrets.get(SECRET) || 'null');
                if (connection && (typeof connection.server !== 'string' || typeof connection.token !== 'string')) throw new Error();
            } catch {
                // JSON parse errors may contain fragments of the stored credential.
                if (action !== 'configure') throw new Error('PentAGI 연결 정보를 읽지 못했습니다. 연결 설정을 다시 입력하세요.');
                connection = null;
            }
            const key = this.key(workspace);
            const stored = this.state.get<Run[]>(key, []);
            if (!Array.isArray(stored)) throw new Error('레드팀 실행 기록을 읽지 못했습니다. 서버 실행 목록을 확인하세요.');
            let runs = stored;
            if (action === 'configure') {
                if (runs.some(r => !r.terminal)) throw new Error('진행 중이거나 확인되지 않은 실행을 먼저 확인·중단하세요.');
                const serverInput = await vscode.window.showInputBox({title: 'PentAGI 서버 연결', prompt: '직접 운영하는 PentAGI 기본 주소 (원격은 HTTPS)', value: connection?.server || 'https://localhost:8443', ignoreFocusOut: true});
                if (!serverInput) return {cancelled: true};
                const server = serviceUrl(serverInput.trim());
                const token = await vscode.window.showInputBox({title: 'PentAGI API 토큰', prompt: 'PentAGI Settings → API Tokens. VS Code 보안 저장소에만 보관합니다.', password: true, ignoreFocusOut: true});
                if (!token) return {cancelled: true};
                const providers = await new PentagiClient(server, token).providers();
                await this.secrets.store(SECRET, JSON.stringify({server, token}));
                return {configured: true, server, providers, runs};
            }
            if (!connection) return {configured: false, runs};
            const client = new PentagiClient(connection.server, connection.token);
            if (action === 'start') {
                if (runs.some(r => !r.terminal)) throw new Error('진행 중이거나 확인되지 않은 실행이 있습니다. 먼저 상태를 확인하세요.');
                const providers = await client.providers();
                if (!providers.length) throw new Error('PentAGI에서 AI 공급자를 먼저 설정하세요.');
                const provider = await vscode.window.showQuickPick(providers, {title: '레드팀 검사에 사용할 PentAGI 모델 공급자'});
                if (!provider) return {cancelled: true};
                const input = await vscode.window.showInputBox({title: '레드팀 테스트 대상', prompt: 'PentAGI 컨테이너에서 접근 가능한 테스트 앱 주소. localhost는 해당 컨테이너 자신입니다.', placeHolder: 'http://host.docker.internal:3000', ignoreFocusOut: true});
                if (!input) return {cancelled: true};
                const target = testTarget(input.trim());
                const approved = await vscode.window.showWarningMessage(`이 테스트 앱에 레드팀 검사를 실행할까요?`, {modal: true,
                    detail: `대상: ${target}\n서버: ${connection.server}\n모델: ${provider}\n\n소유하거나 검사 권한이 있는 테스트 앱만 선택하세요. PentAGI 실행 환경의 네트워크 격리는 운영자가 설정해야 하며, 프롬프트만으로 범위를 강제할 수 없습니다. 테스트 데이터와 모델 API 비용이 발생할 수 있습니다. ReCoder를 닫아도 서버 작업은 계속됩니다.`}, '범위를 확인했고 실행');
                if (approved !== '범위를 확인했고 실행') return {cancelled: true};
                // Persist BEFORE a non-idempotent request: never retry an uncertain creation.
                const run: Run = {target, provider, server: connection.server, createdAt: new Date().toISOString(), uncertain: true};
                runs = [run, ...runs].slice(0, 20); await this.state.update(key, runs);
                let flow;
                try { flow = await client.start(target, provider); }
                catch (error) {
                    if (error instanceof PentagiRejectedRequest) { run.uncertain = false; run.terminal = true; await this.state.update(key, runs); }
                    throw error;
                }
                run.id = flow.id; run.uncertain = false; await this.state.update(key, runs);
                return {configured: true, server: connection.server, runs, flow, tasks: []};
            }
            const run = runs[0];
            if (run && run.server !== connection.server) throw new Error('실행을 생성한 PentAGI 서버에 다시 연결하세요.');
            if (action === 'recover') {
                const id = await vscode.window.showInputBox({title: '실행 복구', prompt: 'PentAGI 실행 목록에서 해당 검사의 Flow ID를 확인해 입력하세요. 새 검사를 생성하지 않습니다.'});
                if (!id || !run) return {cancelled: true};
                const snapshot = await client.snapshot(id.trim());
                const confirmed = await vscode.window.showInformationMessage(`이 실행을 연결할까요? ${snapshot.flow.title}`, {modal: true, detail: `ID: ${snapshot.flow.id}\n생성: ${snapshot.flow.createdAt}\n원래 대상: ${run.target}`}, '이 실행 연결');
                if (!confirmed) return {cancelled: true};
                run.id = snapshot.flow.id; run.uncertain = false; await this.state.update(key, runs);
            }
            if (action === 'stop') {
                if (!run?.id) throw new Error('실행 ID를 먼저 복구하세요.');
                await client.stop(run.id); run.stopped = true;
                // A successful request is not evidence all running tools have stopped.
                await this.state.update(key, runs);
            }
            if (run?.id) {
                const snapshot = await client.snapshot(run.id);
                const outcome=pentagiOutcome(snapshot);
                run.terminal = outcome==='finished'||outcome==='failed'||(run.stopped === true && snapshot.flow.status === 'waiting');
                await this.state.update(key, runs);
                let evidence,reportError;
                if(run.terminal){try{evidence=await client.evidence(run.id);}catch{reportError='상세 보고서를 받지 못했습니다. 마지막 작업 결과를 표시합니다. 상태 확인으로 다시 가져올 수 있습니다.';}}
                return {configured: true, server: connection.server, runs, ...snapshot,evidence,reportError,assessment:run.terminal?assessPentagi(snapshot.tasks,evidence,run.target):undefined};
            }
            return {configured: true, server: connection.server, providers: await client.providers(), runs};
        } finally { this.busy = false; }
    }
}
