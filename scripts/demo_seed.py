#!/usr/bin/env python3
"""Seed the healthmes DB with a self-contained Korean demo-day showcase.

Idempotent-ish: wipes ONLY the rows this script owns (by the demo markers
below) and re-inserts them anchored to *today*, so every surface has
something worth projecting even without a wearable:

- weekly goals + tasks (energy_demand mix)
- today's calendar blocks + ONE pending schedule proposal (live Apply demo)
- a realistic cognitive-energy curve for today (persisted hourly windows)
- a stress alert (trigger_event, already "pushed") in the §8.5 grammar
- a rich decision tree for the Mermaid viewer ("왜 이 판단?")
- insight rows for the weekly report

Usage:
    uv run python scripts/demo_seed.py          # seed against Settings DB
    uv run python scripts/demo_seed.py --wipe   # remove demo rows only
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from healthmes.config import get_settings  # noqa: E402
from healthmes.store import session_scope  # noqa: E402
from healthmes.store.enums import (  # noqa: E402
    CalendarSource,
    DecisionKind,
    EnergyDemand,
    ProposalStatus,
    TaskSource,
)
from healthmes.store.models import (  # noqa: E402
    CalendarEventMirror,
    CognitiveEnergyEstimate,
    DecisionRecord,
    Insight,
    ScheduleProposal,
    Task,
    TriggerEvent,
    WeeklyGoal,
)

DEMO_MARK = "[DEMO]"
DEMO_DEDUP_PREFIX = "demo-day:"


def _tz() -> ZoneInfo:
    tz_name = getattr(get_settings(), "timezone", None) or "Asia/Seoul"
    try:
        return ZoneInfo(str(tz_name))
    except Exception:
        return ZoneInfo("Asia/Seoul")


def naive_utc(dt: datetime) -> datetime:
    """Store-convention: naive UTC datetimes."""
    return dt.astimezone(UTC).replace(tzinfo=None)


# Today's plausible energy shape (local hours -> score); gaps stay honest-null.
ENERGY_BY_LOCAL_HOUR = {
    7: 74, 8: 79, 9: 84, 10: 88, 11: 82,
    12: 71, 13: 62, 14: 58, 15: 66, 16: 76,
    17: 72, 18: 65, 19: 60, 20: 55, 21: 50,
}


def wipe(session) -> int:
    n = 0
    n += session.query(ScheduleProposal).filter(
        ScheduleProposal.decision_record_id.in_(
            session.query(DecisionRecord.id).filter(DecisionRecord.summary.like(f"{DEMO_MARK}%"))
        )
    ).delete(synchronize_session=False)
    for model, col in (
        (Insight, Insight.statement),
        (DecisionRecord, DecisionRecord.summary),
        (CalendarEventMirror, CalendarEventMirror.summary),
    ):
        n += session.query(model).filter(col.like(f"{DEMO_MARK}%")).delete(
            synchronize_session=False
        )
    n += session.query(TriggerEvent).filter(
        TriggerEvent.dedup_key.like(f"{DEMO_DEDUP_PREFIX}%")
    ).delete(synchronize_session=False)
    n += session.query(Task).filter(Task.title.like(f"{DEMO_MARK}%")).delete(
        synchronize_session=False
    )
    n += session.query(WeeklyGoal).filter(WeeklyGoal.title.like(f"{DEMO_MARK}%")).delete(
        synchronize_session=False
    )
    week_ago_utc0 = naive_utc(
        datetime.combine(date.today() - timedelta(days=6), time.min, tzinfo=_tz())
    )
    n += session.query(CognitiveEnergyEstimate).filter(
        CognitiveEnergyEstimate.window_start >= week_ago_utc0,
        CognitiveEnergyEstimate.inputs_snapshot.isnot(None),
    ).delete(synchronize_session=False)
    return n


def seed(session) -> dict[str, str]:
    tz = _tz()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    now_local = datetime.now(tz)

    goal = WeeklyGoal(
        week_start=week_start, title=f"{DEMO_MARK} 데모데이 발표 완성", priority=1, status="active"
    )
    goal2 = WeeklyGoal(
        week_start=week_start, title=f"{DEMO_MARK} 운동 루틴 되찾기 (주 3회)",
        priority=2, status="active",
    )
    session.add_all([goal, goal2])
    session.flush()

    t_rehearse = Task(
        title=f"{DEMO_MARK} 발표 리허설 2회", goal_id=goal.id, est_minutes=90,
        deadline=naive_utc(datetime.combine(today, time(13, 0), tzinfo=tz)),
        energy_demand=EnergyDemand.HIGH, status="scheduled", source=TaskSource.USER,
    )
    t_deepwork = Task(
        title=f"{DEMO_MARK} 데모 시나리오 최종 점검", goal_id=goal.id, est_minutes=90,
        deadline=naive_utc(datetime.combine(today, time(18, 0), tzinfo=tz)),
        energy_demand=EnergyDemand.HIGH, status="todo", source=TaskSource.AGENT,
    )
    t_run = Task(
        title=f"{DEMO_MARK} 저녁 가벼운 러닝 30분", goal_id=goal2.id, est_minutes=30,
        deadline=None, energy_demand=EnergyDemand.LOW, status="todo",
        source=TaskSource.AGENT,
    )
    session.add_all([t_rehearse, t_deepwork, t_run])
    session.flush()

    def block(summary: str, h1: int, m1: int, h2: int, m2: int, agent: bool = False, task_id=None):
        return CalendarEventMirror(
            external_id=f"demo-{uuid.uuid4().hex[:8]}", calendar_source=CalendarSource.GOOGLE,
            summary=f"{DEMO_MARK} {summary}",
            start_at=naive_utc(datetime.combine(today, time(h1, m1), tzinfo=tz)),
            end_at=naive_utc(datetime.combine(today, time(h2, m2), tzinfo=tz)),
            is_agent_created=agent, agent_task_id=task_id,
        )

    session.add_all([
        block("팀 스탠드업", 10, 0, 10, 30),
        block("데모데이 발표", 14, 0, 15, 0),
        block("발표 리허설 (에이전트 배치)", 11, 0, 12, 30, agent=True, task_id=t_rehearse.id),
    ])

    tree = {
        "id": "root", "type": "rule", "label": "stress_spike_vs_baseline 트리거",
        "detail": "10분 주기 결정론 스캔에서 발화",
        "children": [
            {"id": "in1", "type": "input", "label": "야간 수면 점수 68 (14일 평균 79)",
             "detail": "open-wearables 내부 4-요소 수면 점수", "children": []},
            {"id": "in2", "type": "input", "label": "오후 스트레스 71 (baseline+38%)",
             "detail": "Garmin 스트레스, 13:20-14:40 구간", "children": []},
            {"id": "in3", "type": "input", "label": "오후 캘린더 부하 2.5h + 컨텍스트 스위치 3회",
             "detail": "calendar_event_mirror 조인", "children": []},
            {"id": "llm1", "type": "llm_step", "label": "에이전트 판단",
             "detail": "리허설 직후 고강도 블록은 회복 방해 — 저강도로 대체 권고",
             "children": [
                 {"id": "opt1", "type": "option", "label": "A) 16:00 집중 블록 유지",
                  "detail": "기각: 에너지 예보 58", "children": []},
                 {"id": "opt2", "type": "option",
                  "label": "B) 16:00-17:30 저강도 정리 + 집중 블록 내일 오전 이동",
                  "detail": "채택: 내일 09-11시 에너지 예보 84", "children": []},
             ]},
            {"id": "act1", "type": "action", "label": "propose_schedule_blocks 호출",
             "detail": "사용자 승인 대기 (propose-then-confirm)", "children": []},
        ],
    }
    decision = DecisionRecord(
        kind=DecisionKind.ALERT, tree=tree,
        summary=f"{DEMO_MARK} 오후 스트레스 급등 → 16시 집중 블록을 내일 오전으로 이동 제안",
        llm_model="claude-fable-5", tokens=1842,
    )
    session.add(decision)
    session.flush()

    session.add(ScheduleProposal(
        task_id=t_deepwork.id,
        proposed_start=naive_utc(datetime.combine(today, time(16, 0), tzinfo=tz)),
        proposed_end=naive_utc(datetime.combine(today, time(17, 30), tzinfo=tz)),
        status=ProposalStatus.PROPOSED, decision_record_id=decision.id,
    ))

    feedback_tree = {
        "id": "root", "type": "rule", "label": "저녁 리뷰 브리핑 (21:30)",
        "detail": "cron 브리핑 — 오늘 하루 피드백",
        "children": [
            {"id": "f1", "type": "input", "label": "오전 에너지 84-88로 최고 구간",
             "detail": "cognitive_energy_estimate 09-10시", "children": []},
            {"id": "f2", "type": "input", "label": "리허설(11:00)을 최고 에너지 직후 배치",
             "detail": "energy_demand=high 태스크 배치 룰 적중", "children": []},
            {"id": "f3", "type": "input", "label": "14시 발표 전후 스트레스 스파이크 1회",
             "detail": "trigger_event stress_spike_vs_baseline", "children": []},
            {"id": "fj", "type": "llm_step", "label": "하루 피드백",
             "detail": "고에너지 시간대 활용은 좋았고, 발표 직후 회복 시간을 오늘은 확보하지 못함",
             "children": [
                 {"id": "fa", "type": "action",
                  "label": "내일 제안: 오전 집중 블록 + 발표류 일정 뒤 30분 회복 버퍼",
                  "detail": "propose_schedule_blocks 예약", "children": []},
             ]},
        ],
    }
    session.add(DecisionRecord(
        kind=DecisionKind.INSIGHT, tree=feedback_tree,
        summary=f"{DEMO_MARK} 오늘 피드백 — 오전 집중 배치는 적중, 발표 후 회복 버퍼가 없었어요",
        llm_model="claude-fable-5", tokens=976,
    ))

    session.add(TriggerEvent(
        fired_at=naive_utc(now_local - timedelta(minutes=35)),
        rule_id="stress_spike_vs_baseline",
        payload={
            "summary": "오후 스트레스가 baseline 대비 38% 높아요 (13:20부터 지속)",
            "proposal": "16:00 집중 블록을 내일 오전으로 옮기고 오후는 가벼운 정리만 배치할게요",
            "evidence": {"stress": 71, "baseline": 51.5, "sleep_score_last_night": 68},
        },
        alert_sent=True, dedup_key=f"{DEMO_DEDUP_PREFIX}{today.isoformat()}",
    ))

    # Today: full hourly curve; past 6 days: same shape with a per-day drift
    # so the weekly sparkline tells a story (rough start, steady recovery).
    day_drift = {6: -14, 5: -10, 4: -12, 3: -6, 2: -3, 1: -7, 0: 0}
    for days_ago, drift in day_drift.items():
        day = today - timedelta(days=days_ago)
        for local_hour, base_score in ENERGY_BY_LOCAL_HOUR.items():
            score = max(15, min(100, base_score + drift))
            ws_local = datetime.combine(day, time(local_hour, 0), tzinfo=tz)
            session.add(CognitiveEnergyEstimate(
                window_start=naive_utc(ws_local),
                window_end=naive_utc(ws_local + timedelta(hours=1)),
                score=score,
                components={
                    "sleep_debt": round(-14 + (score - 58) * 0.1, 1),
                    "stress": round(-11 + (score - 58) * 0.08, 1),
                    "body_battery": round(6 + (score - 58) * 0.05, 1),
                    "meeting_load": -4.0 if local_hour in (10, 14) else -1.0,
                },
                inputs_snapshot={"demo": True, "source": "scripts/demo_seed.py"},
            ))

    period = f"{week_start.isoformat()}..{(week_start + timedelta(days=6)).isoformat()}"
    session.add_all([
        Insight(period=period, kind="focus_dip",
                statement=(
                    f"{DEMO_MARK} 14-16시 집중 저하 패턴: "
                    "수면 부족일수록 오후 스트레스 급등이 1.8배 잦았어요"
                ),
                evidence={"window": "14-16h", "n_days": 12, "ratio": 1.8}, confidence=0.68),
        Insight(period=period, kind="factor_correlation",
                statement=(
                    f"{DEMO_MARK} 아침 러닝을 한 날은 저녁 스트레스 평균이 12% 낮았어요 (n=8)"
                ),
                evidence={"factor": "morning_run", "delta_pct": -12, "n": 8}, confidence=0.62),
    ])

    return {"decision_id": str(decision.id)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wipe", action="store_true", help="remove demo rows only")
    args = parser.parse_args()
    with session_scope() as session:
        removed = wipe(session)
        if args.wipe:
            print(f"removed {removed} demo rows")
            return 0
        out = seed(session)
        print(f"reseeded (removed {removed} old demo rows)")
        print(f"decision viewer id: {out['decision_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
