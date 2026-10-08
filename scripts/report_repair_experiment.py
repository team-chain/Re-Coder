"""Generate a research report from measured runs (never fill missing values)."""
import argparse,json,statistics,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from grounded_repair.benchmark import report


def pct(v):return '미확인' if v is None else f'{100*v:.1f}%'
def usd(v):return '미확인' if v is None else f'${v:.5f}'

def main():
 p=argparse.ArgumentParser();p.add_argument('runs',type=Path);p.add_argument('--budget',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
 runs=json.loads(args.runs.read_text());budget=json.loads(args.budget.read_text());summary=report(runs)
 lines=['# 문서 근거 배포 수정 실험 결과','', '실행일: 2026-10-03 (Asia/Seoul). 조건별 36개, 최종 144회 평가. 예비 실험·수정 후 재평가 비용도 모두 포함한다.','',
 f"총 모델 사용료 추정: **{usd(budget['measured_usd'])} / 승인 상한 ${budget['limit_usd']:.2f}**. Converse 실제 토큰 × 세전 공개 단가이며 청구서 금액은 아니다. 최악 비용 예약 포함 사용액도 {usd(budget['worst_case_used_usd'])}다.",'',
 '## 핵심 결과','', '| 조건 | AI 과제 해결 | 해결률 | 생성 호출 | 모델 비용 | 해결 1건당 비용 | 중앙 시간 | 인용 지지율 |', '|---|---:|---:|---:|---:|---:|---:|---:|']
 labels={'A':'대형 단독','B':'대형 + 문서','C':'소형 + 문서','D':'소형 → 대형 + 문서'}
 for c in 'ABCD':
  r=summary[c];lines.append(f"| {c} {labels[c]} | {r['solved']}/{r['tasks']} | {pct(r['solve_rate'])} | {r['total_generation_calls']} | {usd(r['total_estimated_cost_usd'])} | {usd(r['estimated_cost_per_solved_usd'])} | {r['median_elapsed_ms']/1000:.2f}초 | {('해당 없음' if c=='A' else pct(r['citation_accuracy'])+' ('+str(r['citations_reviewed'])+'건)')} |")
 lines+=['', '각 조건에서 기존 규칙이 처리한 4개 과제는 AI 해결률 분모에서 제외했다. 과제 실패에 사용한 비용도 해결당 비용에 포함했다. 캐시 사용은 0회다. A는 문서를 조회하지 않아 인용 평가는 적용하지 않는다.', '']
 a,b,c,d=[summary[k]for k in 'ABCD']
 reduction=1-d['estimated_cost_per_solved_usd']/b['estimated_cost_per_solved_usd']
 lines += [f"**문서를 붙이면 반드시 좋아진다는 가설은 이번 실험에서 확인되지 않았다.** A가 {pct(a['solve_rate'])}로 가장 높고 해결당 비용도 가장 낮았다. B→D에서는 해결률 {pct(b['solve_rate'])}→{pct(d['solve_rate'])}, 해결당 비용 {pct(reduction)} 감소를 관찰했으나, 이 수치만으로 A보다 우수하다고 말할 수 없다.",'',
 '문서가 제공하지 않은 로직·빌드 도구·파일 권한 근거를 억지로 인용하지 못하게 막은 사례가 많다. 문서 근거 없이 맞힐 수 있는 A와 인용이 필수인 B/C/D는 제품 전략 비교이며, 검색 하나의 순수한 인과 효과를 분리한 실험은 아니다.','',
 '## 검증 종류별 결과','', '| 검증 | A | B | C | D |','|---|---:|---:|---:|---:|']
 for kind in sorted({r['verification_kind']for r in runs}):
  cells=[]
  for condition in 'ABCD':
   rows=[r for r in runs if r['strategy']==condition and r.get('route')!='rules' and r['verification_kind']==kind]
   cells.append(f"{sum(r['benchmark_solved'] for r in rows)}/{len(rows)}")
  lines.append('| '+kind+' | '+' | '.join(cells)+' |')
 lines+=['','Docker-runtime는 실제 빌드 후 격리된 컨테이너에서 외부 oracle을 실행한다. AWS IAM simulation은 실제 SimulateCustomPolicy 호출이다. ECS/ECR contract는 로컬 계약 검사이며 실제 Fargate 배포나 ECR 서비스에서의 pull 성공을 뜻하지 않는다.','',
 '## 오류 종류별 결과','', '| 종류 | A | B | C | D |','|---|---:|---:|---:|---:|']
 for category in sorted({r['category']for r in runs}):
  cells=[]
  for condition in 'ABCD':
   rows=[r for r in runs if r['strategy']==condition and r.get('route')!='rules' and r['category']==category]
   cells.append(f"{sum(r['benchmark_solved']for r in rows)}/{len(rows)}")
  lines.append('| '+category+' | '+' | '.join(cells)+' |')
 lines+=['','## 재현 조건과 인용 검토','',
 '- Sonnet 4.5 global ($3/$15 per million tokens), Haiku 4.5 global ($1/$5). 가격 근거는 `prices.json`에 보존했다.',
 '- 36개 합성 프로젝트, 6개 코드 로직 대조군 포함. 조건별 최대 2회, seed 42, 동시 실행 3개. 모델 온도 0, 출력 한도 6,000 tokens.',
 '- 31종 readiness를 먼저 실행했다. 4개 규칙 과제는 모든 조건에서 LLM을 부르지 않았다. 새 파일 추가는 현재 제품에서 지원하지 않으므로 그런 제안도 실패로 센다.',
 '- 원본에 수정을 적용하지 않았다. 프로젝트 밖의 oracle을 사용하며 건강한 앱/중단된 앱 두 방향을 검사해 무조건 성공하는 healthcheck를 거부했다.',
 '- 인용은 마지막 스키마 유효 편집안 96개를 검토했다. 서버가 거절한 편집도 포함한다. 공급된 문서 ID와 핵심 수정 원리의 지지를 검사하며 각 파일 편집에 하나 이상의 직접 근거가 필요하다.',
 '- 검토자는 코드를 작성한 AI다. 독립적인 사람 평가가 아니다. ID가 존재하는 것과 문서가 주장을 지지하는 것은 구분했다. 예: Node 오류 문서로 UTC 날짜 로직 수정을 뒷받침한 인용은 부적절로 판정했다.',
 '- `evidence/citation-review.json`에 판정·근거를, `evidence/runs.json`에 각 사용량·검증·diff를 남겼다. 실험용 파일/ARN은 가짜 자원이며 실제 계정 식별자는 넣지 않았다.',
 '', '## 한계와 다음 판단','',
 '같은 개발 과제에서 검색기를 수정한 뒤 재실행했다. 별도 holdout, 다중 seed, 반복 실행이나 유의성 검정이 없으므로 일반적인 절감률을 주장하지 않는다. 두 번의 144회 예비/중간 실험과 healthcheck·ARN 마스킹 교정 전 기록도 증거 패키지에 보존했다. 예비 실험에서 좋았던 숫자만 골라 최종 결과에 합치지 않았다.', '',
 '설정한 의미 검색은 다국어 E5 + 공식 원문 148단락 + 직접 작성 요약 10개다. 한국어 증상 4개 개발 점검은 Top-1 4/4지만 전체 문서 검색 품질의 보증은 아니다. 특정 토픽의 원문 범위 부족과 문맥이 잘린 단락은 미해결 원인이다.', '',
 'AWS snapshot은 2023년 자료이며 라이선스는 검토했지만 최신 웹 문서와 같다고 보지 않는다. 현재 IAM/SCP/endpoint/KMS/cross-account 정책 전체를 실배포로 검증하지 않았다. 실제 클라우드용 자동 승인 기능으로 확대하기 전 환경별 verifier가 필요하다.', '',
 '로컬 Docker/임베딩의 CPU·전력, 저장 비용, 실제 클라우드 배포 비용은 표의 모델 토큰 비용에 포함하지 않는다. 첫 모델 로딩 시간도 과제 중앙 시간에 포함하지 않는다. 모든 SDK transport retry는 0회로 기록됐다.', '',
 '발표에는 전체 A/B/C/D 표를 함께 제시하고, “B 대비 D에서 44% 수준의 비용 감소를 관찰했지만 A보다 높은 해결률이나 낮은 비용은 확인하지 못했다”라고 말하는 것이 맞다.']
 args.output.write_text('\n'.join(lines)+'\n')

if __name__=='__main__':main()
