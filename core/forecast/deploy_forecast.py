"""
core/forecast/deploy_forecast.py — Deploy Forecast (배포 일기예보) (설계서 §41)

§41 Deploy Forecast:
  - 과거 배포 데이터 기반 위험도·성공률 예측
  - 시간대/요일별 배포 성공률 히트맵
  - 현재 시스템 컨텍스트(인시던트 여부, 최근 변경량) 반영
  - "이 시간에 배포하면 얼마나 위험한가?" 직관적 날씨 아이콘으로 표현
  - Haiku 자연어 권고문 생성

날씨 아이콘 매핑:
  ☀️ CLEAR    — 성공률 90%+ / 활성 인시던트 없음
  ⛅ CLOUDY   — 성공률 75~89%
  🌧️ RAINY    — 성공률 50~74% / 경미한 리스크
  ⛈️ STORM    — 성공률 50% 미만 / 활성 인시던트 있음
  🌫️ FOGGY    — 데이터 부족 (신뢰도 낮음)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

RECODER_HOME = Path.home() / ".recoder"
DB_PATH = RECODER_HOME / "sessions.db"

# 날씨 아이콘 기준
_WEATHER_ICON = {
    "CLEAR": "☀️",
    "CLOUDY": "⛅",
    "RAINY": "🌧️",
    "STORM": "⛈️",
    "FOGGY": "🌫️",
}

_WEATHER_COLOR = {
    "CLEAR": "#10b981",
    "CLOUDY": "#f59e0b",
    "RAINY": "#f97316",
    "STORM": "#ef4444",
    "FOGGY": "#6b7280",
}


# ── 데이터 모델 ────────────────────────────────────────────────────────────

@dataclass
class HourSlot:
    """시간대별 배포 성공률 슬롯 (히트맵용)."""
    hour: int           # 0~23
    day_of_week: int    # 0=월 ~ 6=일
    total: int = 0
    success: int = 0

    @property
    def success_rate(self) -> float:
        return (self.success / self.total) if self.total > 0 else -1.0

    @property
    def weather(self) -> str:
        if self.total < 3:
            return "FOGGY"
        rate = self.success_rate
        if rate >= 0.90:
            return "CLEAR"
        if rate >= 0.75:
            return "CLOUDY"
        if rate >= 0.50:
            return "RAINY"
        return "STORM"


@dataclass
class ForecastReport:
    """배포 일기예보 전체 리포트 (§41)."""
    service: str
    generated_at: str
    current_weather: str                # CLEAR / CLOUDY / RAINY / STORM / FOGGY
    weather_icon: str
    success_rate_overall: float         # 0.0~1.0
    success_rate_now: float             # 현재 시간대 기준 예측
    confidence: float                   # 0.0~1.0 (데이터 충분도)
    recommendation: str                 # Haiku 자연어 권고문
    risk_factors: List[str] = field(default_factory=list)
    best_deploy_window: str = ""        # "화요일 10~14시" 식의 최적 배포 시간
    worst_deploy_window: str = ""       # 최악 배포 시간
    heatmap: List[Dict[str, Any]] = field(default_factory=list)   # 24x7 히트맵
    active_incidents: int = 0
    total_deploys_analyzed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @property
    def color(self) -> str:
        return _WEATHER_COLOR.get(self.current_weather, "#6b7280")


# ── 예측 엔진 ─────────────────────────────────────────────────────────────

class DeployForecaster:
    """
    과거 배포 기록으로 미래 배포 위험도를 예측한다 (§41).

    사용법:
        forecaster = DeployForecaster()
        report = await forecaster.forecast(service="my-api")
    """

    def __init__(self) -> None:
        self._db_path = DB_PATH
        self._haiku: Optional[Any] = None
        self._try_init_haiku()

    def _try_init_haiku(self) -> None:
        try:
            import anthropic
            import os
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if key:
                self._haiku = anthropic.AsyncAnthropic(api_key=key)
        except ImportError:
            pass

    async def forecast(
        self,
        service: str = "",
        window_days: int = 30,
    ) -> ForecastReport:
        """
        최근 `window_days`일 배포 기록을 분석하여 ForecastReport를 반환한다.
        """
        now = datetime.now(tz=timezone.utc)
        since = now - timedelta(days=window_days)

        # 1. 배포 기록 로드
        deploy_records = self._load_deploy_records(service, since)

        # 2. 히트맵 구성 (24h × 7day)
        heatmap = self._build_heatmap(deploy_records)

        # 3. 전체 성공률
        total = len(deploy_records)
        success = sum(1 for r in deploy_records if r.get("success", True))
        overall_rate = (success / total) if total > 0 else -1.0

        # 4. 현재 시간대 성공률
        current_hour = now.hour
        current_dow = now.weekday()
        now_slot = next(
            (s for s in heatmap if s.hour == current_hour and s.day_of_week == current_dow),
            None,
        )
        now_rate = now_slot.success_rate if now_slot else overall_rate

        # 5. 활성 인시던트 반영
        active_inc = self._count_active_incidents()
        if active_inc > 0:
            # 인시던트 중 배포는 위험도 상향
            now_rate = max(0.0, (now_rate if now_rate >= 0 else 0.5) - 0.3 * active_inc)

        # 6. 날씨 결정
        confidence = min(total / 20.0, 1.0)  # 20건 이상이면 신뢰도 1.0
        if total < 3:
            weather = "FOGGY"
        elif active_inc > 0 and now_rate < 0.5:
            weather = "STORM"
        elif now_rate >= 0.90:
            weather = "CLEAR"
        elif now_rate >= 0.75:
            weather = "CLOUDY"
        elif now_rate >= 0.50:
            weather = "RAINY"
        else:
            weather = "STORM"

        # 7. 최적/최악 배포 시간
        best_window, worst_window = self._find_best_worst_windows(heatmap)

        # 8. 리스크 요인
        risk_factors = self._build_risk_factors(
            overall_rate=overall_rate,
            now_rate=now_rate,
            active_incidents=active_inc,
            total=total,
            confidence=confidence,
        )

        # 9. 권고문
        recommendation = await self._generate_recommendation(
            service=service,
            weather=weather,
            now_rate=now_rate,
            active_inc=active_inc,
            risk_factors=risk_factors,
            best_window=best_window,
        )

        return ForecastReport(
            service=service or "all",
            generated_at=now.isoformat(),
            current_weather=weather,
            weather_icon=_WEATHER_ICON[weather],
            success_rate_overall=round(overall_rate, 3) if overall_rate >= 0 else -1.0,
            success_rate_now=round(now_rate, 3) if now_rate >= 0 else -1.0,
            confidence=round(confidence, 2),
            recommendation=recommendation,
            risk_factors=risk_factors,
            best_deploy_window=best_window,
            worst_deploy_window=worst_window,
            heatmap=[
                {
                    "hour": s.hour,
                    "dow": s.day_of_week,
                    "total": s.total,
                    "success": s.success,
                    "rate": round(s.success_rate, 2) if s.success_rate >= 0 else -1,
                    "weather": s.weather,
                    "icon": _WEATHER_ICON[s.weather],
                }
                for s in heatmap
            ],
            active_incidents=active_inc,
            total_deploys_analyzed=total,
        )

    # ── 내부 메서드 ───────────────────────────────────────────────────────

    def _load_deploy_records(
        self, service: str, since: datetime
    ) -> List[Dict[str, Any]]:
        """SQLite에서 배포 기록을 로드한다."""
        records: List[Dict[str, Any]] = []
        if not self._db_path.exists():
            return records
        try:
            conn = sqlite3.connect(self._db_path)
            query = (
                "SELECT session_id, project_id, start_time, end_time "
                "FROM sessions WHERE start_time >= ?"
            )
            params: Tuple = (since.isoformat(),)
            if service:
                query += " AND project_id = ?"
                params = (since.isoformat(), service)
            rows = conn.execute(query + " ORDER BY start_time", params).fetchall()
            conn.close()

            for session_id, project_id, start_time, end_time in rows:
                try:
                    dt = datetime.fromisoformat(start_time)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                records.append({
                    "session_id": session_id,
                    "service": project_id or "unknown",
                    "start_time": start_time,
                    "dt": dt,
                    "success": end_time is not None,
                    "hour": dt.hour,
                    "dow": dt.weekday(),
                })
        except Exception as exc:
            log.debug("배포 기록 로드 실패: %s", exc)
        return records

    def _build_heatmap(
        self, records: List[Dict[str, Any]]
    ) -> List[HourSlot]:
        """24h × 7day 히트맵 슬롯을 구성한다."""
        slots: Dict[Tuple[int, int], HourSlot] = {}
        for hour in range(24):
            for dow in range(7):
                slots[(hour, dow)] = HourSlot(hour=hour, day_of_week=dow)

        for r in records:
            key = (r["hour"], r["dow"])
            if key in slots:
                slots[key].total += 1
                if r.get("success"):
                    slots[key].success += 1

        return list(slots.values())

    def _count_active_incidents(self) -> int:
        """미해결 인시던트 수를 반환한다."""
        count = 0
        for inc_file in RECODER_HOME.glob("**/incident*.jsonl"):
            try:
                with open(inc_file, encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line)
                        if not entry.get("resolved_at"):
                            count += 1
            except Exception:
                pass
        return count

    def _find_best_worst_windows(
        self, heatmap: List[HourSlot]
    ) -> Tuple[str, str]:
        """충분한 데이터가 있는 슬롯 중 최적/최악 배포 시간을 찾는다."""
        valid = [s for s in heatmap if s.total >= 3]
        if not valid:
            return "데이터 부족", "데이터 부족"

        best = max(valid, key=lambda s: s.success_rate)
        worst = min(valid, key=lambda s: s.success_rate)

        _DOW = ["월", "화", "수", "목", "금", "토", "일"]

        def _fmt(slot: HourSlot) -> str:
            return f"{_DOW[slot.day_of_week]}요일 {slot.hour:02d}시 (성공률 {slot.success_rate:.0%})"

        return _fmt(best), _fmt(worst)

    def _build_risk_factors(
        self,
        overall_rate: float,
        now_rate: float,
        active_incidents: int,
        total: int,
        confidence: float,
    ) -> List[str]:
        factors: List[str] = []
        if active_incidents > 0:
            factors.append(f"🚨 활성 인시던트 {active_incidents}건 진행 중")
        if total < 5:
            factors.append(f"🌫️ 데이터 부족 ({total}건) — 예측 신뢰도 낮음")
        if 0 <= overall_rate < 0.75:
            factors.append(f"📉 전체 배포 성공률 낮음 ({overall_rate:.0%})")
        if 0 <= now_rate < 0.75:
            factors.append(f"⏰ 현재 시간대 성공률 낮음 ({now_rate:.0%})")
        if not factors:
            factors.append("✅ 주요 리스크 요인 없음")
        return factors

    async def _generate_recommendation(
        self,
        service: str,
        weather: str,
        now_rate: float,
        active_inc: int,
        risk_factors: List[str],
        best_window: str,
    ) -> str:
        """Haiku로 자연어 권고문을 생성한다."""
        if not self._haiku:
            return self._fallback_recommendation(weather, now_rate, active_inc)

        icon = _WEATHER_ICON[weather]
        prompt = (
            f"DevOps 배포 위험도 예측 결과를 바탕으로 팀에게 한국어 권고문 2문장을 작성하세요.\n\n"
            f"서비스: {service or '전체'}\n"
            f"배포 날씨: {icon} {weather}\n"
            f"현재 시간대 예측 성공률: {now_rate:.0%}\n"
            f"활성 인시던트: {active_inc}건\n"
            f"리스크 요인: {', '.join(risk_factors)}\n"
            f"최적 배포 시간: {best_window}\n\n"
            f"권고문 (2문장, 날씨 이모지 1개 포함):"
        )
        try:
            resp = await self._haiku.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:
            log.warning("Haiku 권고문 생성 실패: %s", exc)
            return self._fallback_recommendation(weather, now_rate, active_inc)

    def _fallback_recommendation(
        self, weather: str, rate: float, incidents: int
    ) -> str:
        if incidents > 0:
            return (
                f"⛈️ 현재 {incidents}건의 인시던트가 진행 중입니다. "
                "인시던트 해결 후 배포를 권장합니다."
            )
        if weather == "CLEAR":
            return f"☀️ 현재 시간대 배포 조건이 양호합니다 (성공률 {rate:.0%}). 배포를 진행해도 좋습니다."
        if weather in ("RAINY", "STORM"):
            return f"🌧️ 현재 시간대 성공률이 낮습니다 ({rate:.0%}). 최적 배포 시간을 참고하세요."
        return f"⛅ 현재 배포 조건을 검토하고 진행하세요 (성공률 {rate:.0%})."
