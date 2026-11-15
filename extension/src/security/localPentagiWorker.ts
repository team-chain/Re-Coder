// Standalone worker: keeps watching/cleaning when the webview or VS Code closes.
// Credentials arrive through stdin, never command-line arguments or report files.
import {executeLocalTest, LocalRequest} from './localPentagi';
import {runPrewarm} from './redTeamPrewarm';
let input='';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {input+=chunk; if(input.length>32768) process.exit(2);});
process.stdin.on('end', () => {
    let request: LocalRequest;
    try {
        //: 공격 테스트 환경 미리 받기 — 키·대상 없이 고정 이미지만 받는다.
        const prewarm=JSON.parse(input) as {mode?:string;root?:unknown};
        if(prewarm.mode==='prewarm') {
            if(typeof prewarm.root!=='string'){process.exitCode=2;return;}
            void runPrewarm(prewarm.root).then(state=>{process.exitCode=state.phase==='done'?0:1;},()=>{process.exitCode=1;});
            return;
        }
    } catch {process.exitCode=2;return;}
    try {
        request=JSON.parse(input);
        if (!request.apiKey || typeof request.root!=='string' || typeof request.workspace!=='string' || !['openai','anthropic'].includes(request.provider)) throw new Error();
    }
    catch {process.exitCode=2;return;}
    input='';
    void executeLocalTest(request).catch(()=>{process.exitCode=1;});
});
