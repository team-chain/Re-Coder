"""Explicit, versioned commerce foundation. Never silently replace a user's design."""
from __future__ import annotations
import json
import re
from pathlib import Path

STARTER_ID = 'commerce-foundation'
VERSION = 'commerce-v1'
ROOT = Path(__file__).parent / 'starter_templates' / 'commerce'

def matches(instruction: str) -> bool:
    return bool(re.search(r'(쇼핑몰|shopping\s+(mall|store)|e-commerce)', instruction, re.I)
                and re.search(r'(운영|production|실사용)', instruction, re.I)
                and re.search(r'(만들|생성|build|create)', instruction, re.I))

def decision() -> dict:
    return {'id': STARTER_ID, 'question': '운영형 쇼핑몰의 시작 방식을 선택하세요',
            'options': [
                {'key':'reviewed','label':'검증된 쇼핑몰 기반', 'recommended':True,
                 'summary':'React·Express·PostgreSQL·Stripe 구성. 가입, 상품, 장바구니, 주문, 재고, 서명 웹훅, 관리자 화면을 포함합니다.',
                 'pros':['주문·재고·권한 처리의 검증된 코드 재사용','매번 빌드 확인'],
                 'cons':['이 기술 구성을 사용합니다','실제 결제사 키·HTTPS·상품·사업 정책은 운영자가 설정합니다']},
                {'key':'custom','label':'AI 자유 생성', 'recommended':False,
                 'summary':'요청에 따라 새 코드를 생성합니다. 생성 후 별도 업무·보안 검증이 필요합니다.',
                 'pros':['자유로운 구성'], 'cons':['빌드 통과만으로 업무 정확성을 보장하지 않습니다']},
            ], 'impact':'기반 사용 여부와 고정 기술 구성을 설계 기록에 남깁니다. 실제 결제사 연동은 계정 설정 후 검증해야 합니다.'}

def selected(decisions: list[dict]) -> bool:
    return any(d.get('id') == STARTER_ID and d.get('chosen_key') == 'reviewed' for d in decisions)

def operations() -> list[dict]:
    if not ROOT.is_dir(): raise RuntimeError('Packaged commerce foundation is missing')
    ops=[]
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file() or any(p in {'node_modules','dist','__pycache__','.recoder','.git'} for p in path.relative_to(ROOT).parts): continue
        name=path.relative_to(ROOT).as_posix()
        if path.name.startswith('.env') and path.name != '.env.example': continue
        ops.append({'action':'create','file':name,'language':path.suffix.lstrip('.'),
                    'content':path.read_text(encoding='utf-8'),
                    'rationale':f'{VERSION}: reviewed commerce foundation; revalidate after changes'})
    return ops
