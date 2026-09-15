"""S3 배포 진행 스트리밍(SSE) — 보드 이슈 「S3 배포 진행이 안 보임」.

무엇이 사고였나
    `/api/deploy/s3` 는 요청 하나에 응답 하나다. 파일 80개를 올리는 1분 동안
    화면은 "배포 중…" 에서 멈춰 있고, 실패해도 어느 단계에서 죽었는지 응답에
    없다. 확장 타임아웃(5분)이 먼저 끝나면 **화면은 실패인데 코어는 계속
    올리는** 상태가 되어, 절반만 올라간 사이트가 남는다.

여기서 검사하는 것
    1. 진행 이벤트가 실제 순서대로 나온다 (업로드는 파일마다).
    2. 실패는 **이벤트로** 전달된다 — 스트림은 이미 200 으로 열린 뒤다.
    3. [음성 대조] 기존 비스트리밍 경로의 동작이 변하지 않았다.
    4. 진행 보고가 실패해도 배포는 계속된다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from api.routes import deploy_s3  # noqa: E402


class _FakePlanItem:
    def __init__(self, key: str):
        self.key = key
        self.data = b"<html></html>"
        self.content_type = "text/html"


class _FakePlan:
    def __init__(self, keys):
        self.items = [_FakePlanItem(k) for k in keys]
        self.keys = list(keys)
        self.index_copied_from = None


def _patch_aws(monkeypatch, keys=("index.html", "a.js", "b.css")):
    """AWS 호출을 전부 가짜로 — 이 테스트는 **진행 보고**만 검사한다."""
    monkeypatch.setattr(deploy_s3.s3_byo, "plan_upload", lambda files: _FakePlan(keys))
    monkeypatch.setattr(deploy_s3.s3_byo, "bucket_name", lambda p, a: "recoder-test")
    monkeypatch.setattr(deploy_s3.s3_byo, "website_url", lambda b, r: f"http://{b}.s3-website")
    monkeypatch.setattr(deploy_s3, "_ensure_bucket", lambda c, b, r: (True, r))
    monkeypatch.setattr(deploy_s3, "_configure_public_website", lambda c, b, r: None)
    monkeypatch.setattr(deploy_s3, "_prune_obsolete_objects", lambda c, b, k: 0)

    class _Client:
        def put_object(self, **kwargs):
            return {}

    class _Session:
        def client(self, name, **kwargs):
            if name == "sts":
                class _Sts:
                    def get_caller_identity(self):
                        return {"Account": "123456789012"}
                return _Sts()
            return _Client()

    return _Session()


def _request(keys):
    return deploy_s3.S3DeployRequest(
        project="demo",
        files=[{"path": k, "content": "x"} for k in keys],
    )


def test_진행_이벤트가_순서대로_나온다(monkeypatch) -> None:
    keys = ("index.html", "a.js", "b.css")
    session = _patch_aws(monkeypatch, keys)

    events: list[dict] = []
    result = deploy_s3._deploy_sync(_request(keys), session, "us-east-1", events.append)

    steps = [e["step"] for e in events]
    #: 단계가 실제 순서대로 와야 화면이 진행을 그릴 수 있다.
    assert steps.index("plan") < steps.index("bucket") < steps.index("website")
    assert steps.index("website") < steps.index("upload")
    assert steps[-1] == "prune"

    #: 업로드는 **파일 하나마다** 보고한다 — 여기가 제일 오래 걸리는 구간이고,
    #: 뭉뚱그리면 진행률이 안 움직여 예전과 똑같아진다.
    uploads = [e for e in events if e["step"] == "upload"]
    assert len(uploads) == len(keys)
    assert [u["done_count"] for u in uploads] == [1, 2, 3]
    assert all(u["total"] == len(keys) for u in uploads)
    assert result.status == "deployed"


def test_음성대조_콜백이_없으면_기존과_동일하게_동작한다(monkeypatch) -> None:
    """기존 `/api/deploy/s3` 경로는 건드리지 않았다."""
    keys = ("index.html",)
    session = _patch_aws(monkeypatch, keys)

    result = deploy_s3._deploy_sync(_request(keys), session, "us-east-1")
    assert result.status == "deployed"
    assert result.uploaded == list(keys)


def test_진행_보고가_터져도_배포는_계속된다(monkeypatch) -> None:
    """스트림이 끊겨도(사용자가 창을 닫음) 절반만 올라간 사이트를 남기면 안 된다."""
    keys = ("index.html", "a.js")
    session = _patch_aws(monkeypatch, keys)

    def _broken(event):
        raise RuntimeError("stream closed")

    result = deploy_s3._deploy_sync(_request(keys), session, "us-east-1", _broken)
    assert result.status == "deployed", "보고 실패가 배포를 중단시켰다"


def test_스트리밍_라우트가_등록돼_있다() -> None:
    paths = {route.path for route in deploy_s3.router.routes}
    assert "/api/deploy/s3/stream" in paths
    #: 기존 경로도 그대로 남아야 한다 — 되돌아갈 길을 없애지 않는다.
    assert "/api/deploy/s3" in paths


def test_SSE_프레임_형식(monkeypatch) -> None:
    """`data: <json>\\n\\n` 이 아니면 클라이언트가 프레임을 못 자른다."""
    frame = deploy_s3._sse({"step": "upload", "done_count": 1, "total": 3})
    text = frame.decode("utf-8")
    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    payload = json.loads(text[len("data: "):].strip())
    assert payload["done_count"] == 1


def test_한글_메시지가_유니코드_이스케이프되지_않는다() -> None:
    """ensure_ascii=True 면 화면에 \\uXXXX 가 그대로 뜬다."""
    text = deploy_s3._sse({"message": "버킷 확인 중"}).decode("utf-8")
    assert "버킷 확인 중" in text
