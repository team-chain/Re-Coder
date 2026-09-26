"""자동 터미널 분석에서 새던 시크릿 형식을 가린다(보안 검토 P1)."""
from context_gate import mask_secrets

SAMPLES = {
    "sts": '{"Credentials": {"AccessKeyId": "ASIAABCDEFGHIJKLMNOP", "SecretAccessKey": "abc/def+ghi1234567890abcdefghijklmnopqrst", "SessionToken": "IQoJb3JpZ2luX2VjEJr"}}',
    "asia": "export KEY=ASIAABCDEFGHIJKLMNOP",
    "pem": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----",
    "openai": "OPENAI=sk-proj-" + "a1B2" * 12,
    "anthropic": "key sk-ant-api03-" + "x" * 40,
    "discord": "MTIzNDU2Nzg5MDEyMzQ1Njc4.GAbCdE." + "x" * 38,
}


def test_새던_형식이_모두_가려진다():
    for name, text in SAMPLES.items():
        masked = mask_secrets(text)
        for secret in ("ASIAABCDEFGHIJKLMNOP", "abc/def+ghi", "IQoJb3JpZ2lu", "b3BlbnNzaC1r", "a1B2a1B2", "x" * 38, "sk-ant-api03"):
            if secret in text:
                assert secret not in masked, (name, masked)


def test_평범한_코드는_그대로():
    text = "def task():\n    return 'skip-this-step'\n"
    assert mask_secrets(text) == text.strip() or "skip-this-step" in mask_secrets(text)
