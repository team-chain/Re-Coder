const http=require('node:http');const c=require('./service.json');http.createServer((q,s)=>{s.statusCode=q.url===c.path?200:404;s.end('ok')}).listen(c.port,'0.0.0.0');
