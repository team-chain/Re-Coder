from pathlib import Path


def test_verify_handler_is_exposed_by_sam_template():
    template = (Path(__file__).resolve().parents[1] / "template.yaml").read_text(
        encoding="utf-8"
    )

    assert "VerifyFunction:" in template
    assert "Handler: verify.handler" in template
    assert "DynamoDBReadPolicy:" in template
    assert "Path: /verify" in template
    assert "Method: POST" in template
    assert "VerifyEndpoint:" in template


def test_invoke_handler_can_write_quota_transactions():
    template = (Path(__file__).resolve().parents[1] / "template.yaml").read_text(
        encoding="utf-8"
    )

    invoke_block = template.split("  InvokeFunction:", 1)[1].split(
        "  AdminFunction:", 1
    )[0]
    assert "dynamodb:TransactWriteItems" in invoke_block
    assert "Resource: !GetAtt GatewayTable.Arn" in invoke_block
