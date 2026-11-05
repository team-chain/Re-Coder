// Standalone worker: keeps watching/cleaning when the webview or VS Code closes.
// Credentials arrive through stdin, never command-line arguments or report files.
import {executeLocalTest, LocalRequest} from './localPentagi';
let input='';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {input+=chunk; if(input.length>32768) process.exit(2);});
process.stdin.on('end', () => {
    let request: LocalRequest;
    try {
        request=JSON.parse(input);
        if (!request.apiKey || typeof request.root!=='string' || typeof request.workspace!=='string' || !['openai','anthropic'].includes(request.provider)) throw new Error();
    }
    catch {process.exitCode=2;return;}
    input='';
    void executeLocalTest(request).catch(()=>{process.exitCode=1;});
});
