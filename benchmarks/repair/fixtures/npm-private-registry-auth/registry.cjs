require('node:http').createServer((q,s)=>{s.writeHead(401);s.end('authentication required')}).listen(4873,'127.0.0.1');
