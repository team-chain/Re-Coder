require('node:http').createServer((q,s)=>{s.statusCode=q.url==='/health'?200:404;s.end('ok')}).listen(3000,'0.0.0.0');
