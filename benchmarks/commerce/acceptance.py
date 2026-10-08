"""Black-box shopping acceptance tests; run only on the disposable validation DB."""
import concurrent.futures,copy,hashlib,hmac,json,os,subprocess,time,uuid
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parent
BASE=os.environ.get('SHOP_URL','http://127.0.0.1:14310');DB=os.environ.get('SHOP_DB','shop')
if os.environ.get('SHOP_ALLOW_DISPOSABLE_TESTS') != '1' or not BASE.startswith('http://127.0.0.1:'):
 raise SystemExit('Only disposable loopback fixtures: set SHOP_ALLOW_DISPOSABLE_TESTS=1')
(ROOT/'evidence').mkdir(exist_ok=True)
MOCK=os.environ.get('MOCK_URL','http://127.0.0.1:19093')
results=[];run=uuid.uuid4().hex[:10]
def check(name,condition,detail=''):
 results.append({'name':name,'passed':bool(condition),'detail':detail})
def sql(query):
 return subprocess.check_output(['docker','exec',os.environ.get('SHOP_DB_CONTAINER','recoder-commerce-db'),'psql','-U','shop','-d',DB,'-Atc',query],text=True).strip()
def account(label):
 c=httpx.Client(base_url=BASE,timeout=15);email=f'{run}-{label}@example.invalid'
 r=c.post('/api/auth/register',json={'email':email,'name':'Validation','password':'Validation-password-8!','role':'admin'})
 assert r.status_code==201,(r.status_code,r.text)
 u=r.json();c.headers['Authorization']='Bearer '+u['token'];return c,u,email

def post_event(intent,kind='payment_intent.succeeded',event_id=None,signature=True):
 event={'id':event_id or 'evt_'+uuid.uuid4().hex,'type':kind,'object':'event','data':{'object':intent}}
 body=json.dumps(event,separators=(',',':')).encode();stamp=str(int(time.time()))
 sig=hmac.new(b'whsec_mock',stamp.encode()+b'.'+body,hashlib.sha256).hexdigest() if signature else 'invalid'
 return httpx.post(BASE+'/api/webhooks/stripe',content=body,headers={'Content-Type':'application/json','Stripe-Signature':f't={stamp},v1={sig}'},timeout=15)

def intent_for(c,order):
 id=c.get('/api/orders/'+str(order)).json()['payment_id'];return httpx.get(MOCK+'/v1/payment_intents/'+id).json()
def order(c,pid=1):
 c.post('/api/cart',json={'product_id':pid,'quantity':1})
 key='checkout-'+uuid.uuid4().hex
 r=c.post('/api/orders',json={'idempotency_key':key,'total_amount':1,'user_id':99999,'status':'paid'})
 assert r.status_code in (200,201),(r.status_code,r.text)
 return r.json(),key

def run_tests():
 public=httpx.Client(base_url=BASE,timeout=15)
 check('health',public.get('/health').status_code==200)
 for path in ['/api/cart','/api/orders','/api/admin/orders']:
  check('anonymous denied '+path,public.get(path).status_code==401)
 check('forged JWT denied',public.get('/api/cart',headers={'Authorization':'Bearer invalid'}).status_code==401)
 a,ua,email=account('a');b,ub,_=account('b');lock,_,lockemail=account('lock')
 check('registration works',bool(ua.get('id')))
 check('registration cannot elevate role',a.get('/api/admin/orders').status_code==403)
 check('duplicate registration',public.post('/api/auth/register',json={'name':'Duplicate','email':email,'password':'Validation-password-8!'}).status_code==409)
 check('weak password rejected',public.post('/api/auth/register',json={'name':'Weak','email':'weak-'+email,'password':'a'}).status_code==400)
 check('SQL injection does not log in',public.post('/api/auth/login',json={'email':"' OR 1=1 --",'password':'x'}).status_code==401)
 for _ in range(6): bad=public.post('/api/auth/login',json={'email':lockemail,'password':'wrong'})
 check('login rate limit',bad.status_code==429)
 for q in [0,-1,1.5,'1']:
  check('invalid cart quantity '+str(q),a.post('/api/cart',json={'product_id':1,'quantity':q}).status_code==400)
 before=public.get('/api/products/1').json();o,key=order(a);oid=o['order_id']
 check('server recomputes price',o['total_amount']==round(float(before['price'])*100))
 check('stock reserved',public.get('/api/products/1').json()['stock']==before['stock']-1)
 r=a.post('/api/orders',json={'idempotency_key':key});check('order replay same ID',r.status_code==200 and r.json().get('order_id')==oid)
 check('IDOR read denied',b.get(f'/api/orders/{oid}').status_code in (403,404))
 check('IDOR cancel denied',b.post(f'/api/orders/{oid}/cancel').status_code in (403,404))
 check('cannot directly mark paid',a.put(f'/api/orders/{oid}/status',json={'status':'paid'}).status_code in (404,405))
 intent=intent_for(a,oid)
 check('unsigned webhook denied',post_event(intent,signature=False).status_code==400)
 for field,value in [('id','pi_wrong'),('amount',1),('currency','eur')]:
  invalid=copy.deepcopy(intent);invalid[field]=value
  check('webhook rejects wrong '+field,post_event(invalid).status_code==400)
 invalid=copy.deepcopy(intent);invalid['metadata']['user_id']=str(ub['id'])
 check('webhook rejects wrong owner',post_event(invalid).status_code==400)
 eid='evt_'+uuid.uuid4().hex
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
  statuses=list(pool.map(lambda _:post_event(intent,event_id=eid).status_code,range(3)))
 check('concurrent duplicate webhook acknowledged',statuses==[200]*3,statuses)
 check('successful payment recorded',a.get(f'/api/orders/{oid}').json()['status']=='paid')
 check('duplicate payment does not deduct again',public.get('/api/products/1').json()['stock']==before['stock']-1)
 post_event(intent,'payment_intent.payment_failed');check('late failure cannot change paid',a.get(f'/api/orders/{oid}').json()['status']=='paid')
 post_event(intent,'payment_intent.canceled');check('late cancellation cannot change paid',a.get(f'/api/orders/{oid}').json()['status']=='paid')
 o2,_=order(a);id2=o2['order_id'];stock=public.get('/api/products/1').json()['stock']
 statuses=[a.post(f'/api/orders/{id2}/cancel').status_code for _ in range(2)]
 check('cancel is idempotent',statuses==[200,200] and public.get('/api/products/1').json()['stock']==stock+1,statuses)
 check('cancelled order cannot be paid',post_event(intent_for(a,id2)).status_code==400 and a.get(f'/api/orders/{id2}').json()['status']=='cancelled')
 # Admin setup is restricted to this disposable database/test account.
 sql(f"UPDATE users SET role='admin' WHERE id={int(ua['id'])}")
 login=a.post('/api/auth/login',json={'email':email,'password':'Validation-password-8!'});a.headers['Authorization']='Bearer '+login.json()['token']
 p=a.post('/api/admin/products',json={'name':'Validation race '+run,'description':'Only test data','price':10,'stock':1,'image_url':''})
 check('admin product create',p.status_code in (200,201))
 pid=p.json().get('id',p.json().get('product',{}).get('id'))
 if pid:
  x,_,_=account('race1');y,_,_=account('race2')
  for c in [x,y]:c.post('/api/cart',json={'product_id':pid,'quantity':1})
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   rs=list(pool.map(lambda c:c.post('/api/orders',json={'idempotency_key':'race-'+uuid.uuid4().hex}),[x,y]))
  check('last item concurrent orders cannot oversell',sorted(r.status_code for r in rs)==[201,400] and public.get('/api/products/'+str(pid)).json()['stock']==0,[r.status_code for r in rs])
 # A DB update failure must not permanently consume the webhook event ID.
 o3,_=order(a);id3=o3['order_id'];i3=intent_for(a,id3);eid3='evt_'+uuid.uuid4().hex
 sql(f"CREATE OR REPLACE FUNCTION validation_fail_payment() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id={int(id3)} AND NEW.status='paid' THEN RAISE EXCEPTION 'injected validation failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER validation_payment_failure BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION validation_fail_payment();")
 try: first=post_event(i3,event_id=eid3)
 finally:sql('DROP TRIGGER validation_payment_failure ON orders; DROP FUNCTION validation_fail_payment();')
 retry=post_event(i3,event_id=eid3)
 check('failed webhook can be retried',first.status_code==500 and retry.status_code==200 and a.get(f'/api/orders/{id3}').json()['status']=='paid',{'first':first.status_code,'retry':retry.status_code,'status':a.get(f'/api/orders/{id3}').json()['status']})
 # A revoked admin token must lose permission immediately, without waiting seven days.
 sql(f"UPDATE users SET role='customer' WHERE id={int(ua['id'])}")
 check('admin revocation invalidates existing token',a.get('/api/admin/orders').status_code==403)
 sql(f"UPDATE users SET role='admin' WHERE id={int(ua['id'])}")
 config=public.get('/api/config').json()
 check('public config excludes private payment secrets',set(config)=={'stripe_public_key','test_payment_mode'} and config['stripe_public_key'] is None)
 for value in ['1x',1.5,-1,2147483648]:
  check('admin rejects invalid stock '+str(value),a.post('/api/admin/products',json={'name':'invalid','price':10,'stock':value}).status_code==400)
 # No customer page visit: the background worker must return expired stock.
 exp,_=order(a);expid=int(exp['order_id']);stock=public.get('/api/products/1').json()['stock']
 sql(f"UPDATE orders SET expires_at=NOW()-INTERVAL '1 minute' WHERE id={expid}")
 deadline=time.monotonic()+75
 while time.monotonic()<deadline and sql(f'SELECT status FROM orders WHERE id={expid}')!='cancelled':time.sleep(1)
 check('background expiry releases stock without user visit',sql(f'SELECT status FROM orders WHERE id={expid}')=='cancelled' and public.get('/api/products/1').json()['stock']==stock+1)
 def expiry_worker():
  subprocess.run(['docker','exec',os.environ.get('SHOP_APP_CONTAINER','recoder-commerce-app'),'node','--input-type=module','-e',"import {expirePendingOrders} from './backend/src/order_expiry.js'; import pool from './backend/src/db.js'; await expirePendingOrders(); await pool.end();"],check=True,capture_output=True,timeout=30)
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:list(workers.map(lambda _:expiry_worker(),range(2)))
 check('expiry replay does not release stock twice',public.get('/api/products/1').json()['stock']==stock+1)
 unknown,_=order(a);unknownid=int(unknown['order_id']);stock=public.get('/api/products/1').json()['stock']
 sql(f"UPDATE orders SET payment_id='pi_unavailable', expires_at=NOW()-INTERVAL '1 minute' WHERE id={unknownid}")
 expiry_worker()
 check('uncertain payment cancellation retains reservation',sql(f'SELECT status FROM orders WHERE id={unknownid}')=='pending' and public.get('/api/products/1').json()['stock']==stock)
 check('expiry never changes paid order',sql(f'SELECT status FROM orders WHERE id={int(oid)}')=='paid')
try: run_tests()
except Exception as exc:check('suite completed',False,str(exc)[:250])
finally:
 report={'results':results,'passed':sum(r['passed'] for r in results),'failed':sum(not r['passed'] for r in results),'base':BASE,'mock_provider':True}
 file=ROOT/'evidence'/os.environ.get('REPORT','commerce-acceptance.json');file.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))

if report["failed"]: raise SystemExit(1)
