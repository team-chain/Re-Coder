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
