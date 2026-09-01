"""
aws_calls.py — ReCoder 가 실제로 호출하는 AWS API 를 찾아내는 두 가지 방법 (FR-04-02).

권한표(`aws_policy.py`)는 **코드와 어긋나도 조용하다.** 사용자가 배포하다 막혀야
비로소 드러나고, 그때는 원인을 찾기 어렵다. 그래서 권한표를 손으로 관리하지 않고
코드에서 **증거를 뽑아** 대조한다. 이 파일이 그 증거를 만든다.

## 왜 두 가지인가

  1. `scan_source()` — 소스를 **문법 단위로** 읽어 boto3 호출을 찾는다 (정적)
  2. `Recorder`      — 실행 중 **실제로 나간** 호출을 기록한다 (동적, IAM 권한 0)

둘 중 하나만으로는 반쪽이다.

  - 정적 분석만 보면: 안 걸어본 경로까지 다 훑지만, 동적으로 만들어지는 호출은
    놓친다. 그리고 "정말 그 순서로 부르나"는 증명하지 못한다.
  - 실행 기록만 보면: 실제 호출이라 확실하지만 **내가 걸어본 길 하나만** 증명한다.
    사용자는 다른 길로 간다 — 정적 사이트, 재배포, 롤백.

그래서 정적으로 전 경로를 훑고, 동적으로 실제 인가를 확인한다.

## 이 파일이 없었을 때 무슨 일이 있었나

이전 판은 정규식으로 `ecs.update_service(` 같은 문자열을 찾았다. 세 가지가 샜다.

  - 변수 이름이 다르면 못 봤다 (`self._client.converse(...)`)
  - `client.get(...)` 같은 **AWS 와 무관한 호출**을 AWS 호출로 착각했다
  - AWS CLI 를 subprocess 로 부르는 경로(`["aws", "ecr", "create-repository"]`)를
    통째로 못 봤다 — 실제로 `ecr:CreateRepository` 가 이렇게 호출되고 있었다

무엇보다, **모르는 호출을 만나면 조용히 넘어갔다.** 새 호출일수록 그냥 통과했다.
이 파일은 반대로 한다 — 모르면 **큰 소리로 실패**한다.

## 정적 분석이 **아직 못 보는 파이썬 형태** (알고 있는 한계)

숨기지 않고 적어둔다. 조용한 구멍을 "없다"고 두면 그게 제일 위험하다.
`UNSUPPORTED_PATTERNS` 에 목록으로도 박아두고 테스트가 그 목록과 실제 동작이
일치하는지 확인한다 — 목록이 낡으면 테스트가 먼저 알려준다.

  - 튜플 언패킹: `a, b = boto3.client("ecs"), x`
  - 바다코끼리: `if (c := boto3.client("ecs")):`
  - 반복문·컴프리헨션 바인딩: `for c in [boto3.client("ecs")]:`
  - 자료구조에 담기: `CLIENTS = {"ecs": boto3.client("ecs")}`
  - 동적 속성: `getattr(client, "update_service")()`
  - `global` / `nonlocal` 재바인딩
  - 상속 — 부모 클래스가 `self._client` 를 잡고 자식이 쓰는 경우
  - subprocess 인자를 변수로 조립: `cmd = ["aws", ...]; run(cmd)`

**이 한계를 메우는 것이 `Recorder` 다.** 실제로 배포를 한 번 돌리면 위 형태로
나간 호출도 전부 기록된다. 정적 분석만 믿으면 안 되는 이유가 이것이다.

## 정적 분석으로도 절대 안 보이는 것

  - **docker push** 가 쓰는 ECR 레이어 업로드 액션. 파이썬이 아니라 docker
    데몬이 호출하고, 그것도 AWS API 가 아니라 Docker Registry 규약이다.
  - **`iam:PassRole`**. `register_task_definition` 에 실행 역할 ARN 을 넘기면
    AWS 가 내부적으로 요구한다. 코드 어디에도 `pass_role` 이라는 호출은 없다.

이 둘은 `aws_policy.py` 와 테스트에서 **근거를 적어 따로 고정**한다.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 스캔 범위 ────────────────────────────────────────────────────────
#
# 제외는 **반드시 이유와 함께** 남긴다. 조용히 빼면 "다 봤다"고 오해하게 된다.
# `scan_source()` 는 무엇을 건너뛰었는지 함께 돌려준다.

SKIPPED_DIRS: dict[str, str] = {
    "tests":       "테스트 코드 — 실제 배포 경로가 아니다",
    "__pycache__": "빌드 산출물",
    "eval":        "평가 스크립트 — 사용자 배포 경로가 아니다",
    "demo":        "데모 스크립트 — 사용자 배포 경로가 아니다",
    "relay":       "팀 게이트웨이 서버 — 팀 계정에서 돌아간다. "
                   "사용자 BYO 권한표의 대상이 아니다 (DynamoDB 등)",
}

SKIPPED_FILES: dict[str, str] = {
    "server.py":      "죽은 코드 — 라우터가 api/routes/ 로 이관됨 (이슈 카드 등록됨)",
    "test_models.py": "임시 확인용 스크립트",
    "aws_calls.py":   "이 파일 자신 — 예시 문자열이 오탐을 만든다",
}


# ── boto3 서비스 이름 → IAM 접두사 ───────────────────────────────────
#
# 대부분 같지만 다른 것들이 있다. boto3 메타데이터로는 알아낼 수 없다
# (예: cloudwatch 의 endpointPrefix 는 monitoring 이지만 IAM 접두사는 cloudwatch).

SERVICE_TO_IAM_PREFIX: dict[str, str] = {
    "bedrock-runtime":       "bedrock",
    "bedrock-agent-runtime": "bedrock",
    "bedrock-agent":         "bedrock",
    "cloudwatch":            "cloudwatch",
    "logs":                  "logs",
}


# ── boto3 메서드 → IAM 액션 (이름 규칙이 안 맞는 것만) ───────────────
#
# AWS 는 대체로 `update_service` → `UpdateService` 규칙을 지킨다.
# 안 지키는 것만 여기 적는다. 근거 없이 늘리지 말 것.

OPERATION_TO_ACTION: dict[tuple[str, str], str] = {
    ("s3", "list_objects"):    "s3:ListBucket",
    ("s3", "list_objects_v2"): "s3:ListBucket",
    ("s3", "head_bucket"):     "s3:ListBucket",
    ("s3", "head_object"):     "s3:GetObject",
    # DeleteObjects API는 여러 키를 한 번에 지우지만 필요한 IAM 권한은
    # `s3:DeleteObject`다. 메서드 이름을 기계적으로 CamelCase화하면 존재하지
    # 않는 s3:DeleteObjects 권한으로 잘못 판정한다.
    ("s3", "delete_objects"):  "s3:DeleteObject",
    # API 이름은 PutPublicAccessBlock 인데 **IAM 액션은 버킷이 붙는다.**
    # 규칙대로 PutPublicAccessBlock 을 주면 실제로는 인가되지 않는다.
    # https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutPublicAccessBlock.html
    ("s3", "put_public_access_block"): "s3:PutBucketPublicAccessBlock",
    # boto3 가 제공하는 편의 함수 — 내부적으로 아래 API 로 풀린다.
    ("s3", "upload_file"):     "s3:PutObject",
    ("s3", "upload_fileobj"):  "s3:PutObject",
    ("s3", "download_file"):   "s3:GetObject",

    # Converse 는 이름과 달리 `bedrock:Converse` 가 아니라 **InvokeModel** 로
    # 인가된다. AWS 공식 문서: "This operation requires permission for the
    # bedrock:InvokeModel action."
    # https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
    #
    # 이름만 보고 `bedrock:Converse` 를 주면 실제로는 막힌다. 반대로 쓰지도
    # 않는 `bedrock:Converse` 를 같이 주면 권한만 넓어진다. 둘 다 피한다.
    ("bedrock-runtime", "converse"):        "bedrock:InvokeModel",
    ("bedrock-runtime", "converse_stream"): "bedrock:InvokeModelWithResponseStream",
    ("bedrock-runtime", "invoke_model"):    "bedrock:InvokeModel",
}


# ── IAM 호출이 아닌 boto3 클라이언트 메서드 ──────────────────────────
#
# 클라이언트 객체에 있지만 네트워크로 나가지 않는 것들. 액션으로 세면 안 된다.

NOT_AN_API_CALL: frozenset[str] = frozenset({
    "close", "can_paginate", "get_paginator", "get_waiter",
    "generate_presigned_url", "generate_presigned_post",
    "exceptions", "meta",
})


# ── AWS CLI 하위 명령 → IAM 액션 (이름 규칙이 안 맞는 것만) ──────────

#: 정적 분석이 못 잡는 것으로 **확인된** 파이썬 형태. 설명은 모듈 docstring 참고.
#: 목록과 실제 동작이 어긋나면 테스트가 실패한다 — 좋아졌는데 목록이 낡은
#: 경우도 잡힌다. 이걸 "없는 문제" 로 두지 않기 위한 장치다.
UNSUPPORTED_PATTERNS: dict[str, str] = {
    "tuple_unpack":   'a, b = boto3.client("ecs"), 1',
    "walrus":         'if (c := boto3.client("ecs")): c.update_service()',
    "for_binding":    'for c in [boto3.client("ecs")]: c.update_service()',
    "dict_of_clients": 'C = {"e": boto3.client("ecs")}\nC["e"].update_service()',
    "getattr_call":   'c = boto3.client("ecs")\ngetattr(c, "update_service")()',
    "global_rebind":  '_C = None\ndef s():\n    global _C\n    _C = boto3.client("ecs")\ndef u():\n    _C.update_service()',
    "subprocess_var": 'cmd = ["aws", "ecr", "create-repository"]\nsubprocess.run(cmd)',
}


CLI_TO_ACTION: dict[tuple[str, str], str] = {
    # 이름이 전혀 다르다. CLI 가 편의를 위해 감싼 것.
    ("ecr", "get-login-password"): "ecr:GetAuthorizationToken",
}


@dataclass(frozen=True)
class AwsCall:
    """코드에서 발견한 AWS 호출 하나."""

    service: str        #: boto3 서비스 이름 ("ecs", "bedrock-runtime")
    operation: str      #: boto3 메서드 이름 ("update_service") 또는 CLI 하위 명령
    where: str          #: "ecs_deploy_agent.py:633"
    via: str = "boto3"  #: "boto3" | "cli"

    def __str__(self) -> str:  # pragma: no cover - 표시용
        return f"{self.service}.{self.operation} ({self.via}, {self.where})"


@dataclass
class ScanResult:
    """정적 분석 결과. 무엇을 건너뛰었는지도 함께 돌려준다."""

    calls: list[AwsCall] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def actions(self) -> set[str]:
        """발견한 호출이 요구하는 IAM 액션. 매핑 실패는 제외(호출자가 검사)."""
        out = set()
        for call in self.calls:
            action = iam_action(call)
            if action:
                out.add(action)
        return out

    def unmapped(self) -> list[AwsCall]:
        """IAM 액션으로 옮기지 **못한** 호출. 비어 있어야 한다.

        일부러 무시하기로 한 것(`close()` 등 네트워크로 안 나가는 메서드)은
        여기 포함되지 않는다. 그 둘을 섞으면, 코드가 `client.close()` 를 부르는
        순간 테스트가 엉뚱하게 실패한다 — "모르겠다"와 "알고 넘긴다"는 다르다.
        """
        return [
            c for c in self.calls
            if iam_action(c) is None and not is_deliberately_ignored(c)
        ]

    def ignored(self) -> list[AwsCall]:
        """액션이 아니라고 **알고서** 넘긴 호출. 검토용."""
        return [c for c in self.calls if is_deliberately_ignored(c)]


# ── 이름 변환 ────────────────────────────────────────────────────────

def _pascal(snake: str) -> str:
    """`update_service` → `UpdateService`, `list_objects_v2` → `ListObjectsV2`."""
    parts = [p for p in snake.split("_") if p]
    out = []
    for part in parts:
        # v2 같은 버전 조각은 통째로 대문자화하지 않고 첫 글자만 올린다.
        out.append(part[:1].upper() + part[1:])
    return "".join(out)


def is_deliberately_ignored(call: AwsCall) -> bool:
    """액션이 아니라고 **알고서** 넘기는 호출인가.

    `close()` / `get_paginator()` 같이 네트워크로 나가지 않는 클라이언트
    메서드다. "매핑 실패"와 구분해야 한다 — 전자는 정상이고 후자는 사람이
    봐야 하는 신호다.
    """
    return call.via == "boto3" and call.operation in NOT_AN_API_CALL


def iam_action(call: AwsCall) -> str | None:
    """호출 하나를 IAM 액션 문자열로. 옮길 수 없으면 None.

    None 이 나오면 **조용히 넘기지 말고 실패**시켜야 한다. 모르는 호출이
    통과하는 순간 권한표는 낡기 시작한다.
    """
    if call.via == "cli":
        override = CLI_TO_ACTION.get((call.service, call.operation))
        if override:
            return override
        prefix = SERVICE_TO_IAM_PREFIX.get(call.service, call.service)
        return f"{prefix}:{_pascal(call.operation.replace('-', '_'))}"

    if call.operation in NOT_AN_API_CALL:
        return None
    override = OPERATION_TO_ACTION.get((call.service, call.operation))
    if override:
        return override
    if not call.operation or call.operation.startswith("_"):
        return None
    prefix = SERVICE_TO_IAM_PREFIX.get(call.service, call.service)
    return f"{prefix}:{_pascal(call.operation)}"


# ── 정적 분석 ────────────────────────────────────────────────────────

def _client_service(node: ast.AST) -> str | None:
    """이 표현식이 boto3 클라이언트를 만드는가? 그렇다면 서비스 이름.

    `boto3.client("ecs", ...)` / `self._boto3.client("ecs")` /
    `boto3.session.Session(...).client("s3")` 를 모두 잡는다.
    서비스 이름이 문자열 리터럴이 아니면(동적 생성) None — 정적으로는 알 수 없다.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "client":
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _target_name(node: ast.AST) -> str | None:
    """대입 대상의 이름. `x` → "x", `self._client` → "self._client"."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _target_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _called_name(node: ast.AST) -> str | None:
    """`x.foo()` 에서 `x`, `self._c.foo()` 에서 `self._c`."""
    return _target_name(node)


def _aws_cli_command(node: ast.AST) -> tuple[str, str] | None:
    """`["aws", "ecr", "create-repository", ...]` → ("ecr", "create-repository")."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    items = node.elts
    if len(items) < 3:
        return None
    vals = []
    for item in items[:3]:
        if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
            return None
        vals.append(item.value)
    if vals[0] != "aws":
        return None
    return vals[1], vals[2]


#: 지역 이름을 따로 관리할 범위. **람다는 일부러 뺐다.**
#:
#: 람다를 별도 범위로 두면 감싼 함수의 지역 변수를 못 본다. 실제로 그래서
#: `bedrock_provider.py` 의
#:     `run_in_executor(None, lambda: client.converse(**kwargs))`
#: 를 통째로 놓치고 있었다 — 비동기 대화 경로 전부가 검사 밖이었다.
#: 람다를 감싼 함수의 일부로 취급하면 그 흐름 상태로 풀린다.
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _walk_scope(root: ast.AST):
    """`root` 안을 훑되 **중첩 함수 안으로는 들어가지 않는다.**

    지역 변수를 함수 단위로 추적하기 위해서다. 모듈 전체를 한 덩어리로 보면
    서로 다른 함수의 `client` 가 뒤섞여, AWS 와 무관한 호출을 AWS 호출로
    착각하게 된다.
    """
    for child in ast.iter_child_nodes(root):
        if isinstance(child, _SCOPE_LIKE):
            continue
        yield child
        yield from _walk_scope(child)


#: 범위로 취급할 노드 전체. 클래스 본문을 포함한다 —
#: `class D: _client = boto3.client("ecs")` 를 모듈 전역으로 흘려보내면,
#: 다른 함수의 **매개변수** `_client` 까지 ECS 클라이언트로 오인한다.
_SCOPE_LIKE = _SCOPE_NODES + (ast.ClassDef,)


def _scopes(tree: ast.Module):
    """모듈 본문 + 함수 본문 + 클래스 본문을 각각 하나의 범위로 돌려준다."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, _SCOPE_LIKE):
            yield node


def _assignments(scope: ast.AST):
    """이 범위 안의 대입을 (대상이름, 값노드) 로."""
    for node in _walk_scope(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                key = _target_name(target)
                if key:
                    yield key, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            key = _target_name(node.target)
            if key:
                yield key, node.value
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    key = _target_name(item.optional_vars)
                    if key:
                        yield key, item.context_expr


def _callee_name(node: ast.Call) -> str | None:
    """호출되는 함수의 이름. `self.f()` → "f", `f()` → "f"."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _enclosing_classes(tree: ast.Module) -> dict[int, str]:
    """함수 정의 → 그것을 감싸는 클래스 이름.

    `self.foo(...)` 를 풀 때 **같은 클래스의 `foo`** 로 좁히기 위해 쓴다.
    이게 없으면 동명 메서드가 서로 오염된다.
    """
    out: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(id(child), node.name)
    return out


def _is_self_call(node: ast.Call) -> bool:
    """`self.foo(...)` / `cls.foo(...)` 인가."""
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("self", "cls")
    )


def _bind_params(node: ast.Call, funcdefs: dict, view: dict, scanner,
                 current_class: str | None = None) -> bool:
    """`f(client, ...)` 로 클라이언트가 넘어가면 받는 쪽 매개변수를 기록한다.

    `bedrock_provider` 가 `self._call_with_tool_config(client, ...)` 처럼
    클라이언트를 **인자로 넘긴다.** 이걸 못 따라가면 그 안의 `client.converse(...)`
    를 통째로 놓친다 — 실제로 이전 판이 `bedrock:Converse` 를 놓쳤다.

    ## 같은 이름의 함수가 여러 개일 때

    한 모듈에 `A.use` 와 `B.use` 가 둘 다 있으면, 이름 하나에 정의가 여럿이다.
    예전 판은 **마지막 것만** 남겨서 `A.use` 안의 AWS 호출을 통째로 놓쳤다.
    지금은 **같은 이름의 정의 전부**에 인자를 묶는다.

    과대 추정이 되지만 방향이 맞다 — 실제보다 많이 잡히면 테스트가 시끄럽게
    실패할 뿐이고, 적게 잡히면 새 AWS 호출이 **조용히** 검사를 빠져나간다.
    """
    fname = _callee_name(node)
    if not fname or fname not in funcdefs:
        return False

    candidates = funcdefs[fname]
    # `self.foo(...)` 는 **같은 클래스의 foo** 다. 좁힐 수 있으면 좁힌다.
    # 못 좁히면 동명 정의 전부에 묶는다 — 과대 추정이 누락보다 낫다.
    if _is_self_call(node) and current_class:
        same = [c for c in candidates
                if scanner.class_of.get(id(c)) == current_class]
        if same:
            candidates = same

    changed = False
    for target in candidates:
        names = [a.arg for a in target.args.args]
        # 메서드를 `self.f(...)` 로 부르면 첫 매개변수(self)는 인자에 없다.
        if names and names[0] in ("self", "cls") and isinstance(node.func, ast.Attribute):
            names = names[1:]

        pairs: list[tuple[str, ast.expr]] = list(zip(names, node.args))
        pairs += [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
        for pname, value in pairs:
            svc = scanner._service_of(value, view, current_class)
            # 같은 이름의 정의가 여럿이면 **모두**에 묶는다 (과대 추정).
            key = (id(target), pname)
            if svc and scanner.params.get(key) != svc:
                scanner.params[key] = svc
                changed = True
    return changed


class _ModuleScanner:
    """한 모듈에서 boto3 클라이언트를 추적하고 호출을 수집한다.

    잡는 형태는 다섯 가지다.

      1. `ecs = boto3.client("ecs")`                  → 지역 변수 바인딩
      2. `boto3.client("sts").get_caller_identity()`  → 즉석 호출
      3. `def f(): return boto3.client("ecs")`        → 팩토리 함수
      4. `def g(): return self._client`               → **한 다리 건넌 팩토리**
         (`self._client` 가 어딘가에서 클라이언트로 대입돼 있으면 g 도 팩토리다)
      5. `subprocess(["aws", "ecr", "..."])`          → AWS CLI 경로

    ### 왜 고정점(fixpoint)으로 도나

    정의가 사용보다 **뒤에** 올 수 있고, 팩토리가 팩토리를 부를 수 있다.
    한 번만 훑으면 순서에 따라 결과가 달라진다. 실제로 이전 판은
    `bedrock_provider._get_client()` 를 놓쳤다 — 그 함수는 클라이언트를 직접
    만들지 않고 `self._client` 를 돌려주기 때문이다.

    ### 범위 규칙

    `self._client` 같은 **점 찍힌 이름은 모듈 전체**에서 공유하고(인스턴스
    속성이므로), `client` 같은 **맨 이름은 함수 안에서만** 유효하다. 후자를
    모듈 전체로 보면 다른 함수의 HTTP 클라이언트까지 AWS 로 오인한다.
    """

    MAX_ROUNDS = 8  # 고정점 반복 상한 (순환 참조 방어)

    def __init__(self, rel_path: str) -> None:
        self.rel = rel_path
        # (소유 클래스, 이름) → 서비스.
        # **클래스별로 나눠야 한다.** 한 모듈의 두 클래스가 둘 다
        # `self._client` 를 쓰면(흔하다) 모듈 전체 dict 는 마지막 것만 남기고,
        # 앞 클래스의 AWS 호출을 통째로 다른 서비스로 오인한다.
        self.dotted: dict[tuple[str | None, str], str] = {}
        self.factories: dict[str, str] = {}   # 함수 이름 → 서비스
        self.globals: dict[str, str] = {}     # 모듈 전역 맨 이름 → 서비스
        self.params: dict[tuple[int, str], str] = {}  # (정의 id, 매개변수) → 서비스
        self.locals: dict[int, dict[str, str]] = {}   # 범위별 지역 이름
        self.class_of: dict[int, str] = {}            # 함수 정의 → 감싸는 클래스
        self.calls: list[AwsCall] = []

    def _service_of(self, node: ast.AST, view: dict[str, str],
                    current_class: str | None = None) -> str | None:
        """이 표현식이 어떤 AWS 서비스의 클라이언트인가."""
        svc = _client_service(node)
        if svc:
            return svc
        name = _target_name(node)
        if name:
            if name in view:
                return view[name]
            # 같은 클래스의 바인딩을 먼저 본다. 없으면 모듈 수준(클래스 밖).
            for owner in (current_class, None):
                svc = self.dotted.get((owner, name))
                if svc:
                    return svc
        if isinstance(node, ast.Call):
            fn = _callee_name(node)
            if fn and fn in self.factories:
                return self.factories[fn]
        return None

    def _view(self, scope: ast.AST) -> dict[str, str]:
        """이 범위에서 보이는 이름들. 파이썬 스코프 규칙대로 전역이 깔린다."""
        return {**self.globals, **self.locals.setdefault(id(scope), {})}

    # ── 고정점: 이름·팩토리·매개변수를 서로 참조하며 수렴시킨다 ─────

    def resolve(self, tree: ast.Module) -> None:
        scopes = list(_scopes(tree))
        self.class_of = _enclosing_classes(tree)
        # 이름 하나에 정의가 **여럿일 수 있다** (`A.use` 와 `B.use`).
        # 예전 판은 dict 로 덮어써서 마지막 정의만 남았고, 앞쪽 클래스의
        # AWS 호출을 통째로 놓쳤다. 이름 → 정의 **목록**으로 바꾼다.
        funcdefs: dict[str, list[ast.AST]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcdefs.setdefault(node.name, []).append(node)

        assigns = {id(s): list(_assignments(s)) for s in scopes}
        returns: dict[str, list[ast.expr]] = {}
        for name, nodes in funcdefs.items():
            values: list[ast.expr] = []
            for node in nodes:
                values += [
                    sub.value
                    for sub in _walk_scope(node)
                    if isinstance(sub, ast.Return) and sub.value is not None
                ]
            returns[name] = values

        for _ in range(self.MAX_ROUNDS):
            changed = False
            for scope in scopes:
                local = self.locals.setdefault(id(scope), {})
                # 이 함수의 매개변수가 클라이언트로 밝혀졌으면 지역에 깔아준다.
                if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 매개변수 바인딩은 **정의 하나하나**에 걸려 있다.
                    # 이름으로 맞추면 동명이인 함수끼리 서로 오염된다.
                    for (target_id, pname), svc in self.params.items():
                        if target_id == id(scope) and local.get(pname) != svc:
                            local[pname] = svc
                            changed = True
                view = self._view(scope)
                scope_class = self.class_of.get(id(scope))

                for key, value in assigns[id(scope)]:
                    svc = self._service_of(value, view, scope_class)
                    if not svc:
                        continue
                    if "." in key:
                        # 소유 클래스와 함께 저장한다 (없으면 모듈 수준).
                        dkey = (scope_class, key)
                        if self.dotted.get(dkey) != svc:
                            self.dotted[dkey] = svc
                            changed = True
                        continue
                    table = self.globals if scope is tree else local
                    if table.get(key) != svc:
                        table[key] = svc
                        changed = True

                # 호출 인자로 클라이언트를 넘기는가 → 받는 쪽 매개변수를 기록
                for node in _walk_scope(scope):
                    if isinstance(node, ast.Call) and _bind_params(
                        node, funcdefs, view, self, scope_class
                    ):
                        changed = True

            for fname, values in returns.items():
                # 같은 이름의 정의가 여럿이면 그중 하나라도 클라이언트를
                # 돌려주면 팩토리로 본다 (과대 추정 — 놓치는 것보다 낫다).
                first = funcdefs[fname][0]
                view = self._view(first)
                for value in values:
                    svc = self._service_of(value, view, self.class_of.get(id(first)))
                    if svc and self.factories.get(fname) != svc:
                        self.factories[fname] = svc
                        changed = True

            if not changed:
                break

    def collect(self, tree: ast.Module) -> None:
        for scope in _scopes(tree):
            self._collect_calls(scope, self._view(scope),
                                self.class_of.get(id(scope)))

    def _collect_calls(self, scope: ast.AST, local: dict[str, str],
                       current_class: str | None = None) -> None:
        """호출을 수집한다. **문장 순서를 지킨다.**

        한 함수 안에서 같은 이름을 다른 서비스로 다시 대입할 수 있다::

            c = boto3.client("ecs");  c.update_service()
            c = boto3.client("s3");   c.put_object()

        이름 하나에 값 하나만 두고 범위 전체에 적용하면 **마지막 대입만**
        남아, 위 코드가 `s3.update_service` 로 기록된다. 필요한
        `ecs:UpdateService` 는 목록에서 사라지고, 있지도 않은 액션이 생긴다.

        그래서 대입과 호출을 **소스 순서대로 재생**하며 그 시점의 값을 쓴다.
        (반복문에서 뒤쪽 대입이 다음 회차의 앞쪽 호출에 영향을 주는 경우까지는
        보지 않는다. 그런 코드는 지금 없고, 생기면 대조 테스트가 시끄럽게
        실패하는 쪽으로 기운다.)
        """
        state = dict(local)
        events: list[tuple[tuple[int, int], int, object]] = []
        for key, value in _assignments(scope):
            pos = (getattr(value, "lineno", 0), getattr(value, "col_offset", 0))
            events.append((pos, 0, ("assign", key, value)))
        for node in _walk_scope(scope):
            if isinstance(node, ast.Call):
                events.append(((node.lineno, node.col_offset), 1, ("call", node)))
        events.sort(key=lambda e: (e[0], e[1]))

        for _pos, _kind, payload in events:
            if payload[0] == "assign":
                _, key, value = payload
                if "." in key:
                    continue
                svc = self._service_of(value, state, current_class)
                if svc:
                    state[key] = svc
                elif isinstance(value, ast.Constant):
                    # `client = None` 같은 **센티널 대입은 지우지 않는다.**
                    #
                    #     client = self._client
                    #     if client is None:
                    #         client = self._get_client()   # 못 풀 수도 있음
                    #     ...
                    #     lambda: client.converse(...)
                    #
                    # 이 흔한 형태에서 `None` 대입에 바인딩을 날리면 뒤쪽
                    # 호출을 통째로 놓친다. 실제로 `bedrock_provider` 의
                    # 비동기 대화 경로가 이렇게 검사 밖에 있었다.
                    pass
                else:
                    # **다른 것으로 바뀌었으면 낡은 값을 지운다.**
                    # 안 지우면 `c = httpx.Client()` 뒤의 `c.get(...)` 이
                    # 직전 서비스로 기록돼 있지도 않은 액션(`ecs:Get`)이 생긴다.
                    #
                    # 대가: `c = wrap(c)` 처럼 못 푸는 **호출**을 거치면 이후
                    # 호출을 놓친다. 없는 권한을 지어내는 쪽이 더 나쁘다고 보고
                    # 이렇게 뒀다 — 사람이 그걸 보고 정책에 넣어버릴 수 있다.
                    state.pop(key, None)
                continue
            node = payload[1]
            self._record_call(node, state, current_class)

    def _record_call(self, node: ast.Call, local: dict[str, str],
                     current_class: str | None) -> None:
        # (5) AWS CLI 를 subprocess 로 부르는 경로
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            cli = _aws_cli_command(arg)
            if cli:
                self.calls.append(AwsCall(
                    service=cli[0], operation=cli[1],
                    where=f"{self.rel}:{arg.lineno}", via="cli",
                ))

        if not isinstance(node.func, ast.Attribute):
            return
        # 클라이언트를 만드는 호출 자체(`.client("ecs")`)는 액션이 아니다.
        if _client_service(node):
            return
        svc = self._service_of(node.func.value, local, current_class)
        if svc:
            self.calls.append(AwsCall(
                service=svc, operation=node.func.attr,
                where=f"{self.rel}:{node.lineno}", via="boto3",
            ))


def scan_source(root: str | Path) -> ScanResult:
    """`root` 아래 파이썬 소스에서 AWS 호출을 전부 찾아낸다.

    문법 트리를 읽으므로 변수 이름에 좌우되지 않고, AWS 와 무관한
    `client.get(...)` 같은 호출을 AWS 호출로 착각하지 않는다.
    """
    root = Path(root)
    result = ScanResult()

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()

        skip_reason = None
        for part in path.relative_to(root).parts[:-1]:
            if part in SKIPPED_DIRS:
                skip_reason = SKIPPED_DIRS[part]
                break
        if skip_reason is None and path.name in SKIPPED_FILES:
            skip_reason = SKIPPED_FILES[path.name]
        if skip_reason:
            result.skipped.append(f"{rel} — {skip_reason}")
            continue

        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:  # pragma: no cover - 문법 오류는 별개 문제
            result.skipped.append(f"{rel} — 파싱 실패: {exc}")
            continue

        scanner = _ModuleScanner(rel)
        scanner.resolve(tree)
        scanner.collect(tree)
        result.calls.extend(scanner.calls)

    return result


# ── 실행 중 기록 ─────────────────────────────────────────────────────

class Recorder:
    """실행 중 나간 AWS 호출을 전부 기록한다. **IAM 권한이 필요 없다.**

    botocore 의 `before-call` 이벤트에 붙는다. 이 이벤트를 고른 이유:

      - 작업 하나당 **정확히 한 번** 발생한다. `before-send` 는 재시도마다
        불려서 같은 호출을 여러 번 센다.
      - 자격증명이 틀렸든 엔드포인트가 죽었든 상관없이 발생한다. 그래서
        **권한이 막힌 아카데미 계정에서도 그대로 동작한다.**

    쓰는 법::

        import aws_calls
        rec = aws_calls.Recorder()
        rec.install()
        ...  # 실제 배포를 돌린다
        rec.dump("aws-calls.json")

    한계 — 이건 파이썬 안에서 나간 호출만 본다. `docker push` 는 docker
    데몬이 하고 AWS API 도 아니라서 안 잡힌다. subprocess 로 부르는 AWS CLI
    도 다른 프로세스라 안 잡힌다. 둘 다 정적 분석과 고정 목록이 담당한다.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._installed = False

    # botocore 가 넘겨주는 인자는 버전에 따라 늘어난다. **kwargs 로 받는다.
    def _on_before_call(self, model=None, **_kwargs) -> None:
        if model is None:
            return
        try:
            service = model.service_model.service_name
            operation = model.name
        except AttributeError:  # pragma: no cover - 방어적
            return
        with self._lock:
            key = (service, operation)
            self._seen[key] = self._seen.get(key, 0) + 1

    def install(self) -> None:
        """앞으로 만들어지는 **모든** boto3 클라이언트에 붙는다.

        세션 하나에만 붙이면, 라이브러리 안쪽에서 따로 만든 클라이언트를
        놓친다. 그래서 클라이언트 생성 지점 자체를 감싼다.
        """
        if self._installed:
            return
        import boto3
        import botocore.session

        boto3.setup_default_session()
        boto3.DEFAULT_SESSION.events.register(
            "before-call.*.*", self._on_before_call, unique_id="recoder-aws-calls"
        )

        original = botocore.session.Session.create_client

        def patched(session_self, *args, **kwargs):
            client = original(session_self, *args, **kwargs)
            client.meta.events.register(
                "before-call.*.*", self._on_before_call,
                unique_id="recoder-aws-calls",
            )
            return client

        patched._recoder_original = original  # type: ignore[attr-defined]
        botocore.session.Session.create_client = patched  # type: ignore[assignment]
        self._installed = True

    def uninstall(self) -> None:
        """원래대로 되돌린다. 테스트가 서로 오염되지 않도록."""
        if not self._installed:
            return
        import boto3
        import botocore.session

        current = botocore.session.Session.create_client
        original = getattr(current, "_recoder_original", None)
        if original is not None:
            botocore.session.Session.create_client = original
        if boto3.DEFAULT_SESSION is not None:
            try:
                boto3.DEFAULT_SESSION.events.unregister(
                    "before-call.*.*", unique_id="recoder-aws-calls"
                )
            except Exception:  # pragma: no cover - 방어적
                pass
        self._installed = False

    def calls(self) -> list[AwsCall]:
        with self._lock:
            items = sorted(self._seen)
        return [
            AwsCall(service=svc, operation=_snake(op), where="런타임", via="boto3")
            for svc, op in items
        ]

    def actions(self) -> set[str]:
        """기록된 호출이 요구한 IAM 액션."""
        out = set()
        for call in self.calls():
            action = iam_action(call)
            if action:
                out.add(action)
        return out

    def dump(self, path: str | Path) -> None:
        """나중에 권한표와 대조할 수 있게 파일로 남긴다."""
        with self._lock:
            payload = {
                "calls": [
                    {"service": s, "operation": o, "count": n}
                    for (s, o), n in sorted(self._seen.items())
                ]
            }
        Path(path).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _snake(pascal: str) -> str:
    """`UpdateService` → `update_service`. 런타임 기록을 정적 쪽과 같은 모양으로."""
    out = []
    for i, ch in enumerate(pascal):
        if ch.isupper() and i > 0 and not pascal[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


#: 소스에 박혀 있는 IAM 역할 이름을 찾는 두 패턴.
#: ARN 리터럴(`arn:aws:iam::…:role/ecsTaskRole`)과, 역할 이름을 그대로 넘기는
#: 배포 전 점검(`_check_iam_role("ecsTaskExecutionRole", …)`).
_ROLE_IN_ARN = re.compile(r":role/([A-Za-z0-9_+=,.@-]+)")
_ROLE_IN_CHECK = re.compile(r"_check_iam_role\(\s*f?[\"']([A-Za-z0-9_+=,.@-]+)[\"']")

#: 역할 이름 스캔에서 뺄 파일. 정책 자신과 이 파일은 예시 문자열이 오탐이 된다.
_ROLE_SCAN_SKIP_FILES = {"aws_policy.py", "aws_calls.py"}


def iam_roles_in_source(root: str | Path) -> dict[str, list[str]]:
    """배포 코드가 **실제로 쓰는 IAM 역할 이름** → 발견 위치.

    액션만 대조하면 놓치는 것이 있다. `ecs:RegisterTaskDefinition` 이 정책에
    있어도, 거기 붙는 역할이 `iam:PassRole` 대상에 없으면 배포는 그대로
    실패한다. **액션은 맞는데 리소스가 안 맞는** 경우다.

    실제로 이 구멍으로 한 건 샜다 — `ecs_agent` 가 실행 역할과 태스크 역할을
    따로 넘기는데 정책은 실행 역할만 PassRole 대상으로 두고 있었다.
    """
    root = Path(root)
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIPPED_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name in SKIPPED_FILES or path.name in _ROLE_SCAN_SKIP_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, line in _roles_in_module(text, rel):
            found.setdefault(name, []).append(f"{rel}:{line}")
    return found


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """모듈·클래스·함수 docstring 노드의 id 집합."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _roles_in_module(text: str, rel: str) -> list[tuple[str, int]]:
    """이 파일이 **코드에서** 쓰는 IAM 역할 이름과 줄 번호.

    원문을 정규식으로 훑지 않고 AST 의 문자열 리터럴만 본다. 이유가 둘이다.

    1. docstring 과 주석은 코드가 아니다. 설명문에 예시 ARN 을 적었다는
       이유로 "코드가 이 역할을 쓴다"고 보고하면 오탐이고, 오탐이 쌓이면
       사람이 이 검사를 통째로 무시하게 된다.
    2. f-string 안의 리터럴 조각도 AST 로는 보인다. 이 검사가 전에 실제
       리포에서 아무것도 못 잡던 이유가 f-string 을 통째로 놓쳐서였다.
       (`f"...:role/{name}"` 처럼 이름이 변수면 여전히 안 잡히는데 그건
        맞는 동작이다 — 리터럴이 아니니 대조할 대상 자체가 없다.)

    파싱에 실패하면 원문 정규식으로 물러선다. 조용히 건너뛰면 검사는
    도는데 아무것도 안 보는 상태가 된다.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        logger.warning("역할 스캔: 파싱 실패로 원문 대조로 물러섭니다 — %s", rel)
        fallback: list[tuple[str, int]] = []
        for pattern in (_ROLE_IN_ARN, _ROLE_IN_CHECK):
            for match in pattern.finditer(text):
                name = match.group(1)
                if "{" not in name:
                    fallback.append((name, text.count("\n", 0, match.start()) + 1))
        return fallback

    docstrings = _docstring_node_ids(tree)
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        # (a) 문자열 리터럴 안의 ":role/이름"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            for match in _ROLE_IN_ARN.finditer(node.value):
                name = match.group(1)
                if "{" not in name:
                    results.append((name, node.lineno))
        # (b) _check_iam_role("이름") 호출
        elif isinstance(node, ast.Call):
            func = node.func
            fname = getattr(func, "id", None) or getattr(func, "attr", None)
            if fname != "_check_iam_role" or not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                results.append((first_arg.value, node.lineno))
    return results


def missing_from_policy(actions: set[str], granted: set[str]) -> list[str]:
    """요구하는데 권한표에 없는 액션. 비어 있어야 한다."""
    return sorted(a for a in actions if a not in granted)
