"""실행 파일에 넣는 AWS 서비스 정의가 코드가 부르는 서비스를 모두 포함한다.

시작 속도 때문에 botocore 데이터는 BUNDLED_AWS_SERVICES 만 넣는다. 새 서비스를 부르는 코드를
추가하고 목록을 안 고치면 설치본에서만 UnknownServiceError 로 죽는다 — 여기서 먼저 잡는다.
"""
import re
from pathlib import Path

from release_check import BUNDLED_AWS_SERVICES

CORE = Path(__file__).resolve().parents[1]
CALL = re.compile(r"""\.client\(\s*(?:service_name\s*=\s*)?['"]([a-z0-9-]+)['"]""")


def test_every_called_service_is_bundled():
    used = set()
    for path in CORE.rglob("*.py"):
        if "tests" in path.parts or "eval" in path.parts:
            continue
        used |= set(CALL.findall(path.read_text(encoding="utf-8", errors="ignore")))
    assert used, "scanner found nothing"
    assert used <= set(BUNDLED_AWS_SERVICES), sorted(used - set(BUNDLED_AWS_SERVICES))


def test_spec_filters_botocore_data_by_the_bundled_list():
    spec = (CORE / "recoder-core.spec").read_text(encoding="utf-8")
    assert "BUNDLED_AWS_SERVICES" in spec and "a.datas = [entry for entry in a.datas if _keep_data(entry[0])]" in spec
