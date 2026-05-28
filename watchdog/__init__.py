"""
ReCoder EC2 Watchdog 패키지.

설계서 §3.2.4 / §4.1.3 — EC2 인스턴스에 배포되어 컨테이너 / 헬스체크 / 메트릭을
모니터링하고 incident.jsonl + Discord webhook 으로 알림 전송.

본 패키지는 ReCoder core 와 **독립적으로** 동작하도록 설계되었다.
core 모듈을 import 하지 않으며, Python 표준 라이브러리 + requests + psutil 만 사용한다.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
