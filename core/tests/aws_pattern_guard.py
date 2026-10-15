"""moto 는 AWS 의 문자열 규칙(pattern·길이·enum)을 검사하지 않는다 — 실제 AWS 에서만 거절되던 값(예: WAF 설명의 '—')을
시험에서 잡으려고, 나가는 모든 요청 값을 botocore 서비스 모델의 규칙과 대조한다(2.0.32)."""
from __future__ import annotations

import re
from typing import Any

#: Java 정규식 문법이라 파이썬으로 옮길 수 없는 pattern 은 건너뛴다(대조 실패를 거짓 양성으로 만들지 않게).
_COMPILED: dict[str, Any] = {}


def _regex(pattern: str):
    if pattern not in _COMPILED:
        try:
            _COMPILED[pattern] = re.compile(pattern.replace(r"\p{L}", "a-zA-Z").replace(r"\p{Z}", " ")
                                            .replace(r"\p{N}", "0-9"), re.ASCII)  # Java 의 \w 는 영문·숫자·_ 만
        except re.error:
            _COMPILED[pattern] = None
    return _COMPILED[pattern]


def violations(shape: Any, value: Any, where: str = "") -> list[str]:
    out: list[str] = []
    if shape is None:
        return out
    t = shape.type_name
    if t == "structure" and isinstance(value, dict):
        for k, v in value.items():
            member = shape.members.get(k)
            out += violations(member, v, f"{where}.{k}")
    elif t == "list" and isinstance(value, list):
        for i, v in enumerate(value):
            out += violations(shape.member, v, f"{where}[{i}]")
    elif t == "map" and isinstance(value, dict):
        for k, v in value.items():
            out += violations(shape.value, v, f"{where}[{k}]")
    elif t == "string" and isinstance(value, str):
        meta = shape.metadata
        if "pattern" in meta:
            rx = _regex(meta["pattern"])
            if rx is not None and not rx.search(value):
                out.append(f"{where}={value!r} 가 pattern {meta['pattern']} 에 맞지 않음")
        if "max" in meta and len(value) > meta["max"]:
            out.append(f"{where} 길이 {len(value)} > {meta['max']}")
        if "min" in meta and len(value) < meta["min"]:
            out.append(f"{where} 길이 {len(value)} < {meta['min']}")
        if shape.enum and value not in shape.enum:
            out.append(f"{where}={value!r} 가 허용 값 {shape.enum[:6]}… 이 아님")
    return out


class PatternGuard:
    """boto3 기본 세션·새 세션 모두에 붙는다. found 에 '서비스.작업: 문제' 를 모은다."""

    def __init__(self) -> None:
        self.found: list[str] = []
        self._orig = None

    def _hook(self, params, model, **_kw):
        for problem in violations(model.input_shape, params):
            self.found.append(f"{model.service_model.service_name}.{model.name}: {problem}")

    def install(self) -> "PatternGuard":
        import botocore.client
        orig = botocore.client.BaseClient._make_api_call
        guard = self

        def wrapped(client, operation_name, api_params):
            model = client.meta.service_model.operation_model(operation_name)
            guard._hook(api_params, model)
            return orig(client, operation_name, api_params)
        self._orig = orig
        botocore.client.BaseClient._make_api_call = wrapped
        return self

    def uninstall(self) -> None:
        import botocore.client
        if self._orig is not None:
            botocore.client.BaseClient._make_api_call = self._orig
