"""Offline Korean symptom retrieval smoke against the actual configured corpus.
This is a four-query development check, not an independent retrieval benchmark.
"""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from grounded_repair.config import load_index
QUERIES=[('컨테이너가 켜지자마자 죽어요',['ecs-stopped','ecs-stop-archive','docker-runtime']),('설치하려는 패키지 버전이 없다고 나와요',['npm-dependencies','npm-package']),('이미지를 가져올 권한이 없어요',['ecs-execution-role','ecs-role-archive','ecr-policies']),('브라우저에서 환경변수 값이 보이지 않아요',['vite-env','vite-environment'])]
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
start=time.monotonic();index=load_index();rows=[]
for query,expected in QUERIES:
 found=index.search(query,[],3)
 rows.append({'query':query,'results':[p.id for p in found],'hit_at_1':any(found[0].id.startswith(k)for k in expected),'hit_at_3':any(any(p.id.startswith(k)for k in expected)for p in found)})
result={'model':'intfloat/multilingual-e5-small','corpus_version':index.version,'passages':len(index.passages),'cases':rows,'elapsed_seconds':time.monotonic()-start,'limitation':'Four development queries; no held-out quality claim.'}
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(rows,ensure_ascii=False,indent=2))
if not all(r['hit_at_3']for r in rows):sys.exit(1)
