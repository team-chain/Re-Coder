import type {DockerRun} from './localPentagi';

export const LOCAL_TOOLBOX = 'vxcontrol/kali-linux@sha256:7f803e5be80ec33dcf4228d9702ea1264658b43301e9d6bf3bce6cf8b1077399';
// Give upstream PentAGI a fixed image tag and verify it against the digest-pinned image.
export const LOCAL_WORKER = 'recoder-redteam-toolbox:sha256-7f803e5be80ec33dcf4228d9702ea1264658b43301e9d6bf3bce6cf8b1077399';
export const SCOPE_NETWORK = 'recoder-isolated';
export interface RedTeamHttpEvent {id:number;method:string;path:string;status:number;durationMs:number;blocked:boolean}

/** Fixed-upstream HTTP relay. Workers have no default route or host gateway;
 * the relay cannot forward IP packets and only accepts the approved path.
 */
export const SCOPE_RELAY = String.raw`
import http.server,http.client,json,sys,time,urllib.parse,threading
cfg=json.loads(sys.argv[1])
lock=threading.Lock()
count=0
last=0.0
def allowed_path(raw):
    if len(raw)>4096 or not raw.startswith('/') or raw.startswith('//'): return False
    u=urllib.parse.urlsplit(raw)
    if u.scheme or u.netloc or u.fragment: return False
    p=u.path
    for _ in range(4):
        decoded=urllib.parse.unquote(p)
        if decoded==p: break
        p=decoded
    if '%' in p or '\\' in p or any(ord(c)<32 for c in p): return False
    if any(s in ('.','..') for s in p.split('/')) or p.startswith('//'): return False
    prefix=cfg['prefix']
    return not prefix or p==prefix or p.startswith(prefix+'/')
class Relay(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.0'
    def log_message(self,*args): pass
    def setup(self):
        super().setup()
        self.connection.settimeout(12)
    def handle_request(self):
        global count,last
        start=time.monotonic()
        with lock:
            count+=1
            eid=count
            permitted=eid<=300 and start-last>=0.20
            last=start
        status=502
        blocked=False
        response=b''
        headers=[]
        try:
            if not permitted: status=429;blocked=True
            elif self.command not in ('GET','HEAD','OPTIONS','POST'): status=405;blocked=True
            elif not allowed_path(self.path): status=403;blocked=True
            elif self.headers.get('Transfer-Encoding') or self.headers.get('Upgrade'): status=400;blocked=True
            else:
                lengths=self.headers.get_all('Content-Length',[])
                size=int(lengths[0]) if len(lengths)==1 else 0
                if len(lengths)>1 or size<0 or size>65536: raise ValueError()
                body=self.rfile.read(size) if size else None
                outgoing={k:v for k,v in self.headers.items() if k.lower() not in
                    ('host','connection','proxy-authorization','proxy-connection','transfer-encoding','upgrade','content-length','expect','forwarded','x-forwarded-host','x-forwarded-for','x-forwarded-proto')}
                outgoing['Host']=cfg['authority']
                outgoing['Connection']='close'
                cls=http.client.HTTPSConnection if cfg['scheme']=='https' else http.client.HTTPConnection
                connection=cls(cfg['host'],cfg['port'],timeout=10)
                try:
                    connection.request(self.command,self.path,body=body,headers=outgoing)
                    upstream=connection.getresponse()
                    response=upstream.read(262145)
                    if len(response)>262144: status=502;response=b'response limit';blocked=True
                    else:
                        status=upstream.status
                        headers=[(k,v) for k,v in upstream.getheaders() if k.lower() not in
                            ('connection','transfer-encoding','content-length','upgrade','keep-alive')]
                finally: connection.close()
        except (ValueError,OverflowError): status=400;blocked=True
        except Exception: status=502
        # Commit telemetry before returning the response so a completed request cannot
        # race final report collection and teardown. Never record request secrets.
        try:
            rawpath=urllib.parse.urlsplit(self.path).path
            path=rawpath if len(rawpath)<=256 and not any(ord(c)<32 for c in rawpath) else '[redacted]'
            event={'id':eid,'method':self.command,'path':path,'status':status,
                   'durationMs':int((time.monotonic()-start)*1000),'blocked':blocked}
            if eid<=301:
                with lock:
                    with open(cfg.get('journal','/tmp/recoder-http.jsonl'),'a',encoding='utf-8') as out: out.write(json.dumps(event)+'\n')
        except OSError:
            status=503;headers=[];response=b'evidence journal unavailable'
        try:
            self.send_response(status)
            for k,v in headers: self.send_header(k,v)
            self.send_header('Content-Length',str(len(response)))
            self.send_header('X-ReCoder-Evidence-ID',str(eid))
            self.end_headers()
            if self.command!='HEAD': self.wfile.write(response)
        except OSError: pass
    do_GET=do_HEAD=do_OPTIONS=do_POST=do_PUT=do_PATCH=do_DELETE=do_CONNECT=handle_request
class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=8
Server((cfg.get('bind','0.0.0.0'),cfg.get('listenPort',cfg['port'])),Relay).serve_forever()
`;

export function scopeConfig(target:string) {
    const u=new URL(target);
    if(!['http:','https:'].includes(u.protocol)||u.hostname!=='host.docker.internal'||!u.port||u.username||u.password||u.search||u.hash) throw new Error('검사 프록시 대상이 올바르지 않습니다.');
    const prefix=decodeURIComponent(u.pathname.replace(/\/+$/,''));
    if(prefix.includes('%')||prefix.includes('\\')||prefix.split('/').some(p=>p==='.'||p==='..')||/[\x00-\x1f]/.test(prefix))throw new Error('검사 경로 형식을 확인하세요.');
    return {host:u.hostname,authority:'localhost:'+u.port,port:Number(u.port),scheme:u.protocol.slice(0,-1),prefix};
}

export async function prepareLocalScope(engine:string,target:string,run:DockerRun,progress?:(step:string)=>Promise<void>):Promise<string> {
    const config=scopeConfig(target);
    const inner=(args:string[],timeout=60000)=>run(['exec',engine,'docker',...args],{timeout});
    let sourceId:string;
    await progress?.('검사 전용 엔진의 도구 확인');
    try {sourceId=await inner(['image','inspect',LOCAL_TOOLBOX,'--format','{{.Id}}']);}
    catch {await progress?.('검사 도구 설치 · 고정된 공식 이미지 다운로드');await inner(['pull',LOCAL_TOOLBOX],30*60*1000);sourceId=await inner(['image','inspect',LOCAL_TOOLBOX,'--format','{{.Id}}']);}
    if(!/^sha256:[a-f0-9]{64}$/.test(sourceId))throw new Error('Docker 검사 이미지의 내용 ID를 확인하지 못했습니다.');
    const loadedId=await inner(['image','inspect',LOCAL_WORKER,'--format','{{.Id}}']).catch(()=>'');
    if(!loadedId||loadedId!==sourceId) {
        await inner(['image','tag',LOCAL_TOOLBOX,LOCAL_WORKER]);
        if(await inner(['image','inspect',LOCAL_WORKER,'--format','{{.Id}}'])!==sourceId)throw new Error('Docker 검사 이미지의 내용 검증에 실패했습니다.');
    }
    await progress?.('대상 전용 네트워크 생성·접근 제한 확인');
    const resolved=await run(['exec',engine,'getent','hosts','host.docker.internal']);
    const hostIp=resolved.trim().split(/\s+/)[0];
    if(!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostIp)||hostIp.split('.').some(n=>Number(n)>255))throw new Error('Docker Desktop의 로컬 대상 주소를 확인하지 못했습니다.');
    await inner(['network','create','--internal','--subnet','172.30.0.0/24','-o','com.docker.network.bridge.gateway_mode_ipv4=isolated',SCOPE_NETWORK]);
    await inner(['run','--detach','--name','recoder-scope-proxy','--network','bridge',
        '--add-host',`host.docker.internal:${hostIp}`,'--cap-drop=ALL','--security-opt=no-new-privileges',
        '--sysctl','net.ipv4.ip_forward=0','--sysctl','net.ipv6.conf.all.forwarding=0',
        '--pids-limit','64','--memory','128m','--cpus','0.5',
        '--log-driver=none',LOCAL_WORKER,'python3','-u','-c',SCOPE_RELAY,JSON.stringify(config)]);
    await inner(['network','connect','--ip','172.30.0.10',SCOPE_NETWORK,'recoder-scope-proxy']);
    const probe=String.raw`import socket,time
for host,port in [('172.30.0.1',2375),('172.17.0.1',2375)]:
 try:
  s=socket.create_connection((host,port),1);s.close()
 except OSError: continue
 raise SystemExit('engine reachable')
for attempt in range(20):
 try:
  s=socket.create_connection(('172.30.0.10',${config.port}),1);s.close();break
 except OSError: time.sleep(0.5)
else: raise SystemExit('scope relay unavailable')
`;
    await inner(['run','--rm','--network',SCOPE_NETWORK,'--cap-drop=ALL','--security-opt=no-new-privileges',LOCAL_WORKER,'python3','-c',probe],30000);
    return `http://172.30.0.10:${config.port}${new URL(target).pathname}`;
}
export async function scopeEvents(engine:string,run:DockerRun):Promise<RedTeamHttpEvent[]> {
    const text=await run(['exec',engine,'docker','exec','recoder-scope-proxy','tail','-n','80','/tmp/recoder-http.jsonl']);
    return text.split(/\r?\n/).filter(Boolean).map(line=>JSON.parse(line)).filter(e=>
        Number.isInteger(e.id)&&Number.isInteger(e.status)&&typeof e.path==='string'&&e.path.length<=256&&
        typeof e.method==='string'&&/^[A-Z]{1,12}$/.test(e.method)&&Number.isFinite(e.durationMs)&&typeof e.blocked==='boolean').slice(-80);
}
