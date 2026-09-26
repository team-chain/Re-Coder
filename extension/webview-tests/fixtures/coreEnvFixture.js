// Core 대역: 받은 환경변수 중 확인할 것만 env.json 에 적고, 실행 정보를 남긴다.
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const [runtimeFile] = process.argv.slice(2);
fs.writeFileSync(path.join(path.dirname(runtimeFile), 'env.json'), JSON.stringify({
  provider: process.env.RECODER_AI_PROVIDER || null,
  key: process.env.RECODER_ANTHROPIC_API_KEY ? 'set' : null,
  parent: process.env.RECODER_PARENT_PID || null,
  awsKey: process.env.AWS_ACCESS_KEY_ID ? 'set' : null,
  awsProfile: process.env.AWS_PROFILE || null,
}));
const server = http.createServer((req, res) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ status: 'ok' })); });
server.listen(0, '127.0.0.1', () => fs.writeFileSync(runtimeFile, JSON.stringify({ pid: process.pid, port: server.address().port, session_token: 't', entrypoint: __filename })));
setTimeout(() => process.exit(0), 5000);
