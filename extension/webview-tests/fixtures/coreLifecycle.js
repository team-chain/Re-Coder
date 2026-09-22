// Isolated Core stand-in: only loopback HTTP and files under the test directory.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

const [runtimeFile, requestedPort = '0'] = process.argv.slice(2);
const records = JSON.parse(fs.readFileSync(path.join(path.dirname(runtimeFile), 'records.json'), 'utf8'));
const startedAt = new Date().toISOString();
const token = 'restart-test-token';
const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/api/health') {
    res.end(JSON.stringify({ status: 'ok', port: server.address().port }));
  } else if (req.url === '/api/ecs/deployments') {
    res.end(JSON.stringify(records));
  } else if (req.url === '/api/shutdown' && req.method === 'POST') {
    if (req.headers['x-session-token'] !== token) {
      res.writeHead(403).end('{}');
      return;
    }
    res.end(JSON.stringify({ status: 'shutting_down' }));
    // Keep the old instance alive briefly to expose premature reconnection.
    setTimeout(() => {
      fs.unlinkSync(runtimeFile);
      server.close(() => process.exit(0));
    }, 100);
  } else {
    res.writeHead(404).end('{}');
  }
});
server.listen(Number(requestedPort), '127.0.0.1', () => {
  fs.writeFileSync(runtimeFile, JSON.stringify({
    pid: process.pid,
    port: server.address().port,
    session_token: token,
    started_at: startedAt,
    entrypoint: __filename,
  }));
  fs.appendFileSync(path.join(path.dirname(runtimeFile), 'starts.log'), `${process.pid}\n`);
});
