import code_agent as ca
import commerce_starter as starter
import pytest


def test_explicit_commerce_choice_is_offered_before_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "get_router", lambda: pytest.fail("Foundation plan needs no model"))
    plan = ca.generate_plan("실제 운영 가능한 쇼핑몰을 만들어줘", project_root=str(tmp_path))
    assert plan["decisions"][0]["id"] == starter.STARTER_ID
    assert {x["key"] for x in plan["decisions"][0]["options"]} == {"reviewed", "custom"}


def test_approved_starter_keeps_auth_payment_and_locks_without_llm(tmp_path, monkeypatch):
    choice = starter.decision()
    choice["chosen_key"] = "reviewed"
    monkeypatch.setattr(
        ca,
        "get_router",
        lambda: pytest.fail("Reviewed security code must not be rewritten by model"),
    )
    result = ca.generate_code(
        "실제 운영 가능한 쇼핑몰을 만들어줘", decisions=[choice], project_root=str(tmp_path)
    )
    files = {x["file"]: x["content"] for x in result["ops"]}
    assert result["foundation"] == "commerce-v1"
    assert not [i for i in result["consistency_issues"] if i["severity"] == "error"]
    assert {
        "backend/package-lock.json",
        "frontend/package-lock.json",
        "backend/src/routes/orders.js",
        "backend/src/routes/webhooks.js",
        "frontend/src/pages/Register.jsx",
    } <= files.keys()
    assert "process.env.SEED_DEMO === 'true'" in files["backend/init-db.js"]
    assert not (tmp_path / "package.json").exists()


def test_starter_refuses_existing_project(tmp_path):
    (tmp_path / "index.js").write_text("keep me")
    choice = starter.decision()
    choice["chosen_key"] = "reviewed"
    with pytest.raises(ValueError, match="빈 프로젝트"):
        ca.generate_code("쇼핑몰", decisions=[choice], project_root=str(tmp_path))
    assert (tmp_path / "index.js").read_text() == "keep me"


def test_시작_방식에_따라_이어서_물을_결정을_알려_준다(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "get_router", lambda: pytest.fail("Foundation plan needs no model"))
    plan = ca.generate_plan("실제 운영 가능한 쇼핑몰을 만들어줘", project_root=str(tmp_path))
    f = plan["followups"][starter.STARTER_ID]
    assert f["custom"] == "ai"
    pay = f["reviewed"][0]
    assert pay["id"] == starter.PAYMENT_ID and {o["key"] for o in pay["options"]} == {"mock", "keys"}


def test_AI_자유_생성을_고르면_AI_에게_기술_결정을_받는다(tmp_path, monkeypatch):
    seen = {}

    class R:
        def call(self, req, *a, **k):
            seen["prompt"] = req.prompt
            raise RuntimeError("stop")

    monkeypatch.setattr(ca, "get_router", lambda: R())
    with pytest.raises(Exception):
        ca.generate_plan("실제 운영 가능한 쇼핑몰을 만들어줘", project_root=str(tmp_path), after_starter="custom")
    # 고정 기반 카드로 바로 돌아가지 않고 모델에 기술 결정을 묻는다
    assert "AI 자유 생성을 골랐습니다" in seen["prompt"] and "[결제]" in seen["prompt"]


def test_고른_결제_시작_방식이_첫_로컬_배포의_데모_여부를_정한다(tmp_path, monkeypatch):
    import deploy_settings as ds
    from tests.test_deploy_settings import shop
    ws = shop(tmp_path / "ws")
    choice = starter.decision(); choice["chosen_key"] = "reviewed"
    pay = starter.payment_decision(); pay["chosen_key"] = "mock"
    assert starter.payment_choice([choice, pay]) == "mock"
    ds.remember_payment_choice(str(ws), "mock")
    state = ds.evaluate("ws", str(ws), set())
    assert state["demo"]["enabled"] is True and ds.load("ws")["demo"] is True
    # 배포 화면에서 직접 끄면 그 뒤로는 그 선택을 따른다
    ds.set_demo("ws", False)
    assert ds.evaluate("ws", str(ws), set())["demo"]["enabled"] is False
    # 다시 코드를 만들며 "키 입력" 을 고르면 그 선택이 최신
    ds.remember_payment_choice(str(ws), "keys")
    assert ds.evaluate("ws", str(ws), set())["demo"]["enabled"] is False
    # 대상 폴더(하위 폴더)에서 만든 경우도 상위 배포 폴더가 찾는다
    sub = ws / "app"; sub.mkdir()
    ds.remember_payment_choice(str(sub), "mock")
    assert ds.payment_choice_for(str(ws))["payment"] == "mock"
