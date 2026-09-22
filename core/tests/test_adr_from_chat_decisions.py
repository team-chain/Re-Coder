"""
회귀: 채팅에서 정한 설계 결정이 ADR 네 절을 모두 채워 기록되는가

배경 — 이 테스트가 왜 있는가
    데모에서 대화로 정한 설계 결정(파일을 S3에 저장, multer 사용 등)이 ADR로
    하나도 남지 않았다. 실제로 생긴 ADR 은 배포 센터의 「이 앱을 어디에
    배포할까요?」 두 건뿐이었다.

    원인은 두 겹이었다.
      (1) Workspace 창에서 결정 카드 UI 자체가 숨겨져 plan→generate 경로를
          아예 타지 않았다. (별도 카드에서 수정)
      (2) 경로를 복구해도, 웹뷰가 서버로 보내는 결정 객체에서 `impact` 가
          빠져 있었다. 코어는 `d.get("impact")` 로 읽으므로 항상 빈 문자열이
          되어 모든 ADR 의 「## 영향」이 `(영향 미기재)` 로 남았다.

    (2)는 코어 쪽 코드가 멀쩡한데도 산출물이 비는 종류의 버그다. 코어 단위
    테스트는 impact 를 직접 넣어 호출하므로 전부 통과했고, 아무도 못 봤다.

이 테스트가 지키는 것
    **확장이 실제로 보내는 모양 그대로** 코어에 넣었을 때 ADR 네 절
    (결정·근거·검토한 대안·영향)이 전부 채워지는지 확인한다. 계약이 어느
    쪽에서 깨지든 여기서 걸린다.

DoD 근거: 칸반 「채팅에서 정한 설계 결정이 ADR로 기록되지 않음」(P0)
"""
import adr


#: extension/webview-src/components/CodeAgent.tsx 의 buildDecisionChoices 가
#: 만들어 /api/code/generate 로 보내는 것과 **같은 모양**. 필드를 여기서
#: 임의로 늘리지 말 것 — 늘리는 순간 이 테스트는 확장의 계약이 아니라
#: 상상 속 계약을 검사하게 된다.
CLIENT_PAYLOAD = [
    {
        "id": "storage",
        "question": "업로드 파일을 어디에 저장할까요?",
        "chosen_key": "s3",
        "impact": "저장 위치가 배포 대상과 비용 구조를 함께 결정합니다.",
        "options": [
            {
                "key": "s3",
                "label": "S3 오브젝트 스토리지",
                "summary": "AWS S3 버킷에 업로드",
                "pros": ["서버 디스크와 무관하게 확장", "정적 서빙 연계 쉬움"],
                "cons": ["요청·전송 비용 발생"],
                "recommended": True,
            },
            {
                "key": "local",
                "label": "로컬 디스크",
                "summary": "서버 파일시스템에 저장",
                "pros": ["구현이 단순"],
                "cons": ["인스턴스 교체 시 유실", "수평 확장 불가"],
                "recommended": False,
            },
        ],
    }
]

INSTRUCTION = "문서 업로드 기능을 만들어줘"


def _adr_body() -> str:
    normalized = adr.normalize_decisions(CLIENT_PAYLOAD)
    assert normalized, "확장이 보내는 모양을 코어가 하나도 인식하지 못했다"
    return adr.build_adr_markdown(1, normalized[0], INSTRUCTION)


def _section(body: str, heading: str) -> str:
    """`## 제목` 아래 다음 `## ` 전까지의 본문."""
    assert heading in body, f"{heading} 절 자체가 없다"
    after = body.split(heading, 1)[1]
    return after.split("\n## ", 1)[0].strip()


def test_영향_절이_채워진다():
    """이 테스트가 이 카드의 핵심이다 — 예전엔 여기가 `(영향 미기재)` 였다."""
    impact = _section(_adr_body(), "## 영향")
    assert "(영향 미기재)" not in impact, (
        "ADR 의 영향 절이 비었다 — 확장이 impact 를 안 보내고 있을 가능성이 크다"
    )
    assert "저장 위치가 배포 대상과 비용 구조를 함께 결정합니다." in impact


def test_음성대조_impact를_빼면_영향_절이_실제로_빈다():
    """
    위 테스트가 의미 있으려면, impact 가 없을 때는 **반드시** 실패 표식이
    나와야 한다. 이 대조가 깨지면 위 테스트는 항상 통과하는 껍데기다.
    """
    without = [{k: v for k, v in CLIENT_PAYLOAD[0].items() if k != "impact"}]
    normalized = adr.normalize_decisions(without)
    body = adr.build_adr_markdown(1, normalized[0], INSTRUCTION)
    impact = _section(body, "## 영향")
    #: cons 가 있으면 "감수: …" 만 남는다. 어느 쪽이든 원래 impact 문장은 없다.
    assert "저장 위치가 배포 대상과 비용 구조를 함께 결정합니다." not in impact


def test_결정_절에_사용자가_고른_선택지가_적힌다():
    decision = _section(_adr_body(), "## 결정")
    assert "S3 오브젝트 스토리지" in decision
    assert "로컬 디스크" not in decision, "고르지 않은 선택지가 결정으로 적혔다"


def test_근거_절이_채워진다():
    decision = _section(_adr_body(), "## 결정")
    assert "근거(장점):" in decision
    assert "서버 디스크와 무관하게 확장" in decision


def test_검토한_대안_절에_고르지_않은_선택지가_남는다():
    alts = _section(_adr_body(), "## 검토한 대안")
    assert "(기록된 대안 없음)" not in alts
    assert "로컬 디스크" in alts
    assert "수평 확장 불가" in alts, "제외 이유(cons)가 기록되지 않았다"


def test_요청문이_머리말에_남는다():
    body = _adr_body()
    assert f"- 요청: {INSTRUCTION}" in body


def test_네_절이_모두_비어있지_않다():
    """DoD: 결정·근거·검토한 대안·영향 네 섹션이 모두 채워진다."""
    body = _adr_body()
    빈표식 = {
        "## 결정": None,
        "## 검토한 대안": "(기록된 대안 없음)",
        "## 영향": "(영향 미기재)",
    }
    for heading, placeholder in 빈표식.items():
        text = _section(body, heading)
        assert text, f"{heading} 절이 비었다"
        if placeholder:
            assert placeholder not in text, f"{heading} 절이 미기재 표식으로 남았다"
    assert "근거(장점):" in _section(body, "## 결정")
