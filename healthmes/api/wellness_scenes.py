"""Bounded generative wellness scenes over existing HealthMes read models.

This is a presentation API, not a second decision engine. It classifies a
small set of user questions, projects persisted health/calendar/goal/outcome
data into a trusted component catalog, and exposes only already-existing
schedule proposals as mutations.
"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from healthmes.api.common import utc_now
from healthmes.api.dashboard import (
    DashboardProposal,
    DashboardView,
    build_dashboard,
    calendar_sync_status,
    pending_proposal_by_id,
)
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/wellness", tags=["wellness-scenes"])

WellnessIntent = Literal[
    "explain_fatigue",
    "find_focus_window",
    "review_week_capacity",
    "review_nutrition_impact",
    "view_calendar",
    "reschedule_for_capacity",
    "proactive_intervention",
    "wellness_overview",
]
WellnessModuleKind = Literal[
    "time_series",
    "calendar_canvas",
    "capacity_bar",
    "comparison_bar",
    "nutrition_evidence",
    "proposal_preview",
]
WellnessVisualizationKind = Literal[
    "time_series",
    "calendar_canvas",
    "capacity_bar",
    "comparison_bar",
]


class WellnessSceneRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    source: Literal["user", "proactive"] = "user"
    proposal_id: UUID | None = None
    decision_record_id: UUID | None = None

    @field_validator("query")
    @classmethod
    def query_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must contain non-whitespace text")
        return value

    @model_validator(mode="after")
    def proactive_requires_exact_identity(self) -> Self:
        if self.source == "proactive" and (
            self.proposal_id is None or self.decision_record_id is None
        ):
            raise ValueError("proactive scenes require proposal_id and decision_record_id")
        return self


class WellnessConfidenceOut(BaseModel):
    level: Literal["high", "medium", "low", "insufficient_data"]
    coverage: str
    limitations: list[str] = Field(default_factory=list)


class WellnessPointOut(BaseModel):
    label: str
    value: float | None
    secondary_value: float | None = None
    annotation: str | None = None


class WellnessSeriesOut(BaseModel):
    id: str
    label: str
    points: list[WellnessPointOut]


class WellnessItemOut(BaseModel):
    id: str
    label: str
    value: str
    detail: str | None = None


class WellnessCalendarEventOut(BaseModel):
    id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    provider: str
    calendar_id: str = "default"
    calendar_name: str = "Calendar"
    calendar_color: str = "#6B7280"
    is_healthmes_managed: bool
    energy_demand: str | None = None
    is_all_day: bool = False
    is_recurring: bool = False
    is_locked: bool = False
    has_attendees: bool = False
    organizer_self: bool = False
    provider_status: str | None = None
    status: Literal["current", "proposed"] = "current"

    @model_validator(mode="after")
    def validate_time_range(self) -> Self:
        if self.starts_at >= self.ends_at:
            raise ValueError("calendar event starts_at must be before ends_at")
        return self


class WellnessVisualizationOut(BaseModel):
    kind: WellnessVisualizationKind
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    series: list[WellnessSeriesOut] = Field(default_factory=list)
    events: list[WellnessCalendarEventOut] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_numeric_range(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("visualization minimum must be less than maximum")
        return self


class WellnessModuleOut(BaseModel):
    id: str
    kind: WellnessModuleKind
    title: str
    summary: str
    items: list[WellnessItemOut] = Field(default_factory=list)
    visualization: WellnessVisualizationOut | None = None
    accessibility_summary: str

    @model_validator(mode="after")
    def visualization_matches_module(self) -> Self:
        if self.visualization is not None and self.visualization.kind != self.kind:
            raise ValueError("module and visualization kinds must match")
        return self


class WellnessActionOut(BaseModel):
    id: str
    kind: Literal[
        "accept_proposal",
        "decline_proposal",
        "modify_proposal",
        "open_web_detail",
        "refresh",
    ]
    label: str
    proposal_id: str | None = None
    url: str | None = None


class WellnessSceneOut(BaseModel):
    schema_version: Literal["1"] = "1"
    id: str
    intent: WellnessIntent
    timezone: str
    lens: Literal["now", "coordinate", "change"]
    title: str
    summary: str
    severity: Literal["neutral", "supportive", "caution", "action"]
    freshness: Literal["current", "stale", "insufficient_data", "offline"]
    confidence: WellnessConfidenceOut
    modules: list[WellnessModuleOut]
    actions: list[WellnessActionOut] = Field(default_factory=list)
    generated_at: datetime

    @model_validator(mode="after")
    def ids_are_unique(self) -> Self:
        module_ids = [module.id for module in self.modules]
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("module ids must be unique")
        action_ids = [action.id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action ids must be unique")
        return self


def _intent(query: str, source: str) -> WellnessIntent:
    normalized = query.casefold()
    if source == "proactive":
        return "proactive_intervention"
    if any(
        token in normalized
        for token in (
            "옮",
            "조정",
            "조율",
            "재배치",
            "미뤄",
            "move",
            "reschedule",
            "adjust",
        )
    ):
        return "reschedule_for_capacity"
    if any(token in normalized for token in ("식사", "먹", "meal", "food")):
        return "review_nutrition_impact"
    if any(token in normalized for token in ("피곤", "회복", "tired", "fatigue")):
        return "explain_fatigue"
    if any(token in normalized for token in ("언제", "집중", "focus", "deep work")):
        return "find_focus_window"
    if any(token in normalized for token in ("이번 주", "주간", "week")):
        return "review_week_capacity"
    if any(token in normalized for token in ("일정", "캘린더", "calendar", "schedule")):
        return "view_calendar"
    return "wellness_overview"


def _selected_proposal(
    view: DashboardView,
    proposal_id: UUID | None,
) -> DashboardProposal | None:
    if proposal_id is None:
        return None
    proposal_key = str(proposal_id)
    return next(
        (proposal for proposal in view.pending_proposals if proposal.id == proposal_key),
        None,
    )


def _confidence(
    view: DashboardView,
    intent: WellnessIntent,
    proposal: DashboardProposal | None,
) -> WellnessConfidenceOut:
    known = sum(point.score is not None for point in view.energy.curve_24h)
    limitations: list[str] = []

    if intent == "review_nutrition_impact":
        if view.nutrition.confirmed_count == 0:
            return WellnessConfidenceOut(
                level="insufficient_data",
                coverage="확정된 오늘 식사 기록 0건",
                limitations=["식사 전후 변화를 비교할 확정 기록이 없습니다."],
            )
        return WellnessConfidenceOut(
            level="insufficient_data",
            coverage=f"확정 식사 {view.nutrition.confirmed_count}건",
            limitations=[
                "식사 전후에 정렬된 호환 가능한 건강 결과 시계열이 없어 "
                "영향 분석을 만들지 않습니다."
            ],
        )

    if intent == "review_week_capacity":
        evidence = len(view.goals) + len(view.plan_events)
        if evidence == 0 or view.energy.score is None:
            missing = []
            if evidence == 0:
                missing.append("활성 목표 또는 캘린더 일정")
            if view.energy.score is None:
                missing.append("현재 가용 에너지")
            return WellnessConfidenceOut(
                level="insufficient_data",
                coverage=(
                    f"활성 목표 {len(view.goals)}개 · "
                    f"캘린더 {len(view.plan_events)}/{view.plan_events_total}건 · "
                    f"가용 에너지 {'있음' if view.energy.score is not None else '없음'}"
                ),
                limitations=[f"{' 및 '.join(missing)} 근거가 부족합니다."],
            )
        if view.plan_events_truncated:
            limitations.append(
                f"캘린더 {view.plan_events_total}건 중 앞선 {len(view.plan_events)}건만 표시합니다."
            )
        return WellnessConfidenceOut(
            level="low",
            coverage=(
                f"활성 목표 {len(view.goals)}개 · "
                f"캘린더 {len(view.plan_events)}/{view.plan_events_total}건 · "
                f"가용 에너지 {view.energy.score}/100"
            ),
            limitations=[
                *limitations,
                "목표와 일정 건수만으로 주간 실행 가능성이나 건강 효과를 예측하지 않습니다.",
            ],
        )

    if intent in {"reschedule_for_capacity", "proactive_intervention"}:
        if proposal is None:
            return WellnessConfidenceOut(
                level="insufficient_data",
                coverage=(
                    f"정확히 연결된 일정 제안 0건 · "
                    f"캘린더 {len(view.plan_events)}/{view.plan_events_total}건"
                ),
                limitations=[
                    "proposal_id와 정확히 일치하는 활성 제안이 없어 승인 동작을 만들지 않습니다."
                ],
            )
        if view.energy.score is None:
            return WellnessConfidenceOut(
                level="insufficient_data",
                coverage=(
                    "정확히 연결된 제안 1건 · "
                    f"캘린더 {len(view.plan_events)}/{view.plan_events_total}건 · "
                    "현재 에너지 없음"
                ),
                limitations=[
                    "현재 에너지 근거가 없어 건강 기반 일정 승인을 제공하지 않습니다."
                ],
            )
        if proposal.decision_record_id is None:
            limitations.append(
                "연결된 DecisionRecord가 없어 제안의 판단 근거를 검증할 수 없으므로 "
                "승인 동작을 제공하지 않습니다."
            )
        elif not proposal.decision_has_trusted_provenance:
            limitations.append(
                "활성 제안과 직접 연결된 schedule change 판단 기록의 UUID와 "
                "서버 시각을 검증하지 못해 승인 동작을 제공하지 않습니다."
            )
        else:
            limitations.append(
                "활성 제안의 판단 기록 연결과 현재 에너지 근거를 확인했지만 "
                "scene 신뢰도를 중간보다 높게 표시하지 않습니다."
            )
        if view.plan_events_truncated:
            limitations.append(
                f"캘린더 {view.plan_events_total}건 중 앞선 "
                f"{len(view.plan_events)}건만 표시하므로 승인 동작을 제공하지 않습니다."
            )
        return WellnessConfidenceOut(
            level=(
                "medium"
                if proposal.decision_has_trusted_provenance
                and view.energy.confidence in {"high", "medium"}
                else "low"
            ),
            coverage=(
                "정확히 연결된 제안 1건 · "
                f"캘린더 {len(view.plan_events)}/{view.plan_events_total}건 · "
                f"에너지 근거 {view.energy.confidence}"
            ),
            limitations=limitations,
        )

    if intent == "view_calendar":
        if not view.plan_events:
            return WellnessConfidenceOut(
                level="insufficient_data",
                coverage="연결된 캘린더 일정 0건",
                limitations=["표시할 mirrored calendar event가 없습니다."],
            )
        if view.plan_events_truncated:
            limitations.append(
                f"캘린더 {view.plan_events_total}건 중 앞선 {len(view.plan_events)}건만 표시합니다."
            )
        calendar_current = _calendar_is_current(view, view.generated_at)
        if not calendar_current:
            limitations.append(
                "연결된 provider의 최근 성공 sync를 확인할 수 없거나 30분보다 오래됐습니다."
            )
        return WellnessConfidenceOut(
            level="high" if calendar_current else "low",
            coverage=(f"캘린더 {len(view.plan_events)}/{view.plan_events_total}건"),
            limitations=limitations,
        )

    if view.energy.score is None:
        return WellnessConfidenceOut(
            level="insufficient_data",
            coverage=f"오늘 에너지 {known}/24시간",
            limitations=["현재 에너지 점수가 없습니다."],
        )
    if intent == "find_focus_window" and not view.plan_events:
        return WellnessConfidenceOut(
            level="insufficient_data",
            coverage=f"오늘 에너지 {known}/24시간 · 캘린더 0건",
            limitations=["집중 가능 시간과 비교할 캘린더 일정이 없습니다."],
        )
    if known < 6:
        limitations.append("시간별 데이터가 일부 비어 있습니다.")
    limitations.append(
        "저장된 에너지 관찰과 일정만 표시하며 피로 원인이나 미래 성과를 계산하지 않습니다."
    )
    return WellnessConfidenceOut(
        level="low",
        coverage=f"오늘 에너지 {known}/24시간 · 캘린더 {len(view.plan_events)}건",
        limitations=limitations,
    )


def _calendar_is_current(view: DashboardView, now: datetime) -> bool:
    providers = set(view.calendar_sources)
    if not providers:
        return False
    for provider in providers:
        if calendar_sync_status(view.calendar_sync_observed_at.get(provider), now) != "current":
            return False
    return True


def _energy_module(view: DashboardView) -> WellnessModuleOut:
    points = [
        WellnessPointOut(label=f"{point.hour:02d}", value=point.score)
        for point in view.energy.curve_24h
    ]
    score = view.energy.score
    summary = (
        f"현재 가용 에너지는 {score}/100입니다."
        if score is not None
        else "현재 가용 에너지를 계산할 데이터가 부족합니다."
    )
    has_known_points = any(point.value is not None for point in points)
    return WellnessModuleOut(
        id="energy-observations",
        kind="time_series",
        title="오늘 저장된 에너지 관찰",
        summary=(f"{summary} 이 시계열은 저장된 관찰값이며 미래 집중 가능성을 예측하지 않습니다."),
        visualization=(
            WellnessVisualizationOut(
                kind="time_series",
                unit="score",
                minimum=0,
                maximum=100,
                series=[
                    WellnessSeriesOut(
                        id="energy",
                        label="가용 에너지",
                        points=points,
                    )
                ],
            )
            if has_known_points
            else None
        ),
        accessibility_summary=(
            f"{summary} 시간별 값은 그래프에 표시됩니다."
            if has_known_points
            else summary
        ),
    )


def _calendar_module(
    view: DashboardView,
    *,
    proposal: DashboardProposal | None = None,
) -> WellnessModuleOut:
    events = [
        WellnessCalendarEventOut(
            id=f"{event.source}:{event.external_id}",
            title=event.title,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            provider=event.source,
            calendar_id=event.calendar_id,
            calendar_name=event.calendar_name,
            calendar_color=event.calendar_color,
            is_healthmes_managed=event.is_agent_created,
            is_all_day=event.is_all_day,
            is_recurring=event.is_recurring,
            is_locked=event.is_locked,
            has_attendees=event.has_attendees,
            organizer_self=event.organizer_self,
            provider_status=event.status,
            energy_demand=event.energy_demand,
        )
        for event in view.plan_events
    ]
    if proposal is not None:
        events.append(
            WellnessCalendarEventOut(
                id=f"proposal:{proposal.id}",
                title=proposal.task_title,
                starts_at=proposal.starts_at,
                ends_at=proposal.ends_at,
                provider="healthmes",
                calendar_id="healthmes",
                calendar_name="HealthMes",
                calendar_color="#D97706",
                is_healthmes_managed=True,
                organizer_self=True,
                provider_status="proposed",
                status="proposed",
            )
        )
    proposal_count = 1 if proposal is not None else 0
    return WellnessModuleOut(
        id="calendar-canvas",
        kind="calendar_canvas",
        title="연결된 캘린더 일정",
        summary=(
            f"앞으로 7일의 실제 일정 {len(view.plan_events)}/{view.plan_events_total}건과 "
            f"이 질문과 관련된 조정 {proposal_count}건입니다."
        ),
        visualization=(
            WellnessVisualizationOut(
                kind="calendar_canvas",
                events=events,
            )
            if events
            else None
        ),
        accessibility_summary=(
            f"캘린더 일정 {len(view.plan_events)}/{view.plan_events_total}건, "
            f"관련 제안 {proposal_count}건"
        ),
    )


def _capacity_module(view: DashboardView) -> WellnessModuleOut:
    available = view.energy.score
    high_load = sum(
        1 for block in view.next_blocks if getattr(block, "energy_demand", None) == "high"
    )
    summary = (
        f"현재 가용 에너지는 {available}/100이고, 다음 일정 중 고강도 블록은 {high_load}건입니다."
        if available is not None
        else (
            f"가용 에너지 데이터가 없습니다. 다음 일정 중 고강도 블록 {high_load}건만 확인했습니다."
        )
    )
    return WellnessModuleOut(
        id="capacity",
        kind="capacity_bar",
        title="현재 가용 에너지",
        summary=summary,
        visualization=(
            WellnessVisualizationOut(
                kind="capacity_bar",
                unit="score",
                minimum=0,
                maximum=100,
                series=[
                    WellnessSeriesOut(
                        id="capacity",
                        label="가용 에너지",
                        points=[
                            WellnessPointOut(
                                label="가용 에너지",
                                value=available,
                                annotation=f"{available}/100",
                            )
                        ],
                    )
                ],
            )
            if available is not None
            else None
        ),
        accessibility_summary=summary,
    )


def _goal_module(view: DashboardView) -> WellnessModuleOut:
    points = [
        WellnessPointOut(
            label=goal.title,
            value=(100 * goal.done_tasks / goal.total_tasks) if goal.total_tasks else 0,
            annotation=f"{goal.done_tasks}/{goal.total_tasks}",
        )
        for goal in view.goals
    ]
    return WellnessModuleOut(
        id="goal-progress",
        kind="comparison_bar",
        title="이번 주 목표 진행",
        summary="현재 저장된 task 완료율입니다. 미래 달성 가능성을 예측하지 않습니다.",
        visualization=(
            WellnessVisualizationOut(
                kind="comparison_bar",
                unit="percent",
                minimum=0,
                maximum=100,
                series=[WellnessSeriesOut(id="goals", label="목표 진행", points=points)],
            )
            if points
            else None
        ),
        accessibility_summary=f"활성 목표 {len(points)}개",
    )


def _nutrition_module(view: DashboardView) -> WellnessModuleOut:
    items = [
        WellnessItemOut(
            id=f"meal-{index}",
            label="확인된 식사",
            value=name,
            detail="오늘 저장된 식사 항목",
        )
        for index, name in enumerate(view.nutrition.latest_items)
    ]
    return WellnessModuleOut(
        id="nutrition-evidence",
        kind="nutrition_evidence",
        title="오늘의 식사 근거",
        summary=(
            f"식사 기록 {view.nutrition.interaction_count}건 중 "
            f"{view.nutrition.confirmed_count}건이 확정됐습니다. "
            "식사 전후의 호환 가능한 시계열이 없어 영향 그래프는 만들지 않습니다."
        ),
        items=items,
        visualization=None,
        accessibility_summary=(
            f"식사 기록 {view.nutrition.interaction_count}건, "
            f"확정 {view.nutrition.confirmed_count}건"
        ),
    )


def _proposal_preview_module(
    proposal: DashboardProposal | None,
) -> WellnessModuleOut:
    if proposal is None:
        return WellnessModuleOut(
            id="proposal-preview",
            kind="proposal_preview",
            title="일정 블록 제안",
            summary="proposal_id와 정확히 연결된 활성 일정 제안이 없습니다.",
            visualization=None,
            accessibility_summary="승인 가능한 일정 제안 없음",
        )
    summary = (
        "제안된 블록을 승인 전 미리보기로 표시합니다. 현재 proposal contract에는 "
        "변경 유형과 원본 event identity가 없어 생성인지 이동인지 단정하지 않습니다."
    )
    items = [
        WellnessItemOut(
            id="proposal-id",
            label="proposal_id",
            value=proposal.id,
        ),
        WellnessItemOut(
            id="proposal-task",
            label="일정",
            value=proposal.task_title,
        ),
        WellnessItemOut(
            id="proposal-window",
            label="제안 시간",
            value=(f"{proposal.starts_at.isoformat()}/{proposal.ends_at.isoformat()}"),
        ),
    ]
    if proposal.decision_summary:
        items.append(
            WellnessItemOut(
                id="proposal-reason",
                label="판단 근거",
                value=proposal.decision_summary,
            )
        )
    return WellnessModuleOut(
        id="proposal-preview",
        kind="proposal_preview",
        title="승인 전 일정 블록",
        summary=summary,
        items=items,
        visualization=None,
        accessibility_summary=f"{proposal.task_title}. {summary}",
    )


def _actions(
    proposal: DashboardProposal | None,
    *,
    actionable: bool,
) -> list[WellnessActionOut]:
    if proposal is None or not actionable:
        return []
    # The dashboard projection intentionally carries no mutation token.
    # Native clients correlate this scene with their already-fetched proposal.
    proposal_key = proposal.id
    actions = [
        WellnessActionOut(
            id=f"decline:{proposal_key}",
            kind="decline_proposal",
            label="유지",
            proposal_id=proposal.id,
        ),
        WellnessActionOut(
            id=f"accept:{proposal_key}",
            kind="accept_proposal",
            label="적용",
            proposal_id=proposal.id,
        ),
    ]
    if proposal.decision_url:
        actions.append(
            WellnessActionOut(
                id=f"detail:{proposal_key}",
                kind="open_web_detail",
                label="왜?",
                proposal_id=proposal.id,
                url=proposal.decision_url,
            )
        )
    return actions


def _scene_id(scene: WellnessSceneOut) -> str:
    evidence = scene.model_dump(
        mode="json",
        exclude={"id", "generated_at"},
    )
    canonical = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"scene:{sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def _scene_summary(
    view: DashboardView,
    intent: WellnessIntent,
    proposal: DashboardProposal | None,
) -> str:
    if proposal is not None:
        local_start = proposal.starts_at.astimezone(ZoneInfo(view.timezone))
        return (
            f"{proposal.task_title} 블록을 "
            f"{local_start:%m/%d %H:%M}에 배치할까요? "
            "승인 전에는 캘린더가 바뀌지 않습니다."
        )
    if intent in {"reschedule_for_capacity", "proactive_intervention"}:
        return "proposal_id와 정확히 일치하는 활성 일정 제안을 확인할 수 없습니다."
    if intent == "review_nutrition_impact":
        return (
            f"오늘 확정된 식사 기록 {view.nutrition.confirmed_count}건을 표시합니다. "
            "현재 근거만으로 식사가 컨디션 변화의 원인이라고 단정하지 않습니다."
        )
    if intent == "explain_fatigue":
        if view.energy.score is None:
            return "피로 질문과 연결할 현재 에너지 관찰이 없습니다."
        return (
            f"현재 저장된 에너지 관찰은 {view.energy.score}/100입니다. "
            "이 화면은 피로 원인을 새로 계산하지 않습니다."
        )
    if intent == "find_focus_window":
        return (
            f"저장된 에너지 관찰과 캘린더 "
            f"{len(view.plan_events)}/{view.plan_events_total}건을 함께 표시합니다. "
            "최적 집중 시간을 새로 예측하지 않습니다."
        )
    if intent == "review_week_capacity":
        return (
            f"활성 목표 {len(view.goals)}개와 캘린더 "
            f"{len(view.plan_events)}/{view.plan_events_total}건을 표시합니다."
        )
    if intent == "view_calendar":
        return (
            f"연결된 캘린더 일정 {len(view.plan_events)}/{view.plan_events_total}건을 표시합니다."
        )
    energy = f"{view.energy.score}/100" if view.energy.score is not None else "데이터 없음"
    return (
        f"현재 에너지 {energy}, 캘린더 "
        f"{len(view.plan_events)}/{view.plan_events_total}건을 표시합니다."
    )


def _freshness(
    view: DashboardView,
    intent: WellnessIntent,
    confidence: WellnessConfidenceOut,
) -> Literal["current", "stale", "insufficient_data", "offline"]:
    if confidence.level == "insufficient_data":
        return "insufficient_data"
    if intent in {
        "find_focus_window",
        "review_week_capacity",
        "view_calendar",
        "reschedule_for_capacity",
        "proactive_intervention",
        "wellness_overview",
    } and not _calendar_is_current(view, view.generated_at):
        return "stale"
    if (
        intent
        in {
            "explain_fatigue",
            "find_focus_window",
            "review_week_capacity",
            "reschedule_for_capacity",
            "proactive_intervention",
            "wellness_overview",
        }
        and view.energy.score is not None
        and view.energy.confidence == "low"
    ):
        return "stale"
    return "current"


def compose_scene(
    view: DashboardView,
    *,
    query: str,
    source: Literal["user", "proactive"],
    proposal_id: UUID | None,
    exact_proposal: DashboardProposal | None = None,
    now: datetime,
) -> WellnessSceneOut:
    intent = _intent(query, source)
    proposal = (
        exact_proposal or _selected_proposal(view, proposal_id)
        if intent in {"reschedule_for_capacity", "proactive_intervention"}
        else None
    )
    modules: list[WellnessModuleOut]
    if intent == "explain_fatigue":
        modules = [_energy_module(view), _capacity_module(view)]
    elif intent == "find_focus_window":
        modules = [_energy_module(view), _calendar_module(view)]
    elif intent == "view_calendar":
        modules = [_calendar_module(view)]
    elif intent == "review_week_capacity":
        modules = [_goal_module(view), _calendar_module(view)]
    elif intent == "review_nutrition_impact":
        modules = [_nutrition_module(view), _energy_module(view)]
    elif intent in {"reschedule_for_capacity", "proactive_intervention"}:
        modules = [
            _proposal_preview_module(proposal),
            _calendar_module(view, proposal=proposal),
            _capacity_module(view),
        ]
    else:
        modules = [_capacity_module(view), _calendar_module(view)]

    has_action = proposal is not None
    confidence = _confidence(view, intent, proposal)
    summary = _scene_summary(view, intent, proposal)
    freshness = _freshness(view, intent, confidence)

    scene = WellnessSceneOut(
        id="pending",
        intent=intent,
        timezone=view.timezone,
        lens=(
            "change"
            if intent in {"review_week_capacity", "review_nutrition_impact"}
            else "coordinate"
            if intent in {"find_focus_window", "reschedule_for_capacity", "proactive_intervention"}
            else "now"
        ),
        title={
            "explain_fatigue": "오늘 피로가 계획에 미치는 영향",
            "find_focus_window": "집중 업무를 보호할 시간",
            "review_week_capacity": "이번 주 목표와 가용량",
            "review_nutrition_impact": "식사와 컨디션 변화",
            "view_calendar": "연결된 캘린더 일정",
            "reschedule_for_capacity": "현재 몸 상태에 맞춘 일정 조율",
            "proactive_intervention": "HealthMes가 먼저 찾은 조정",
        }.get(intent, "현재 Wellness 상태"),
        summary=summary,
        severity="action" if has_action else "supportive",
        freshness=freshness,
        confidence=confidence,
        modules=modules,
        actions=_actions(
            proposal,
            actionable=(
                freshness == "current"
                and confidence.level in {"high", "medium"}
                and not view.plan_events_truncated
                and view.schedule_approval_available
            ),
        ),
        generated_at=now,
    )
    return scene.model_copy(update={"id": _scene_id(scene)})


@router.post("/scenes", response_model=WellnessSceneOut)
def create_wellness_scene(
    body: WellnessSceneRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> WellnessSceneOut:
    now = utc_now()
    view = build_dashboard(session, request.app.state.settings, now)
    exact_proposal = (
        pending_proposal_by_id(
            session,
            request.app.state.settings,
            now,
            body.proposal_id,
        )
        if body.proposal_id is not None
        else None
    )
    if body.source == "proactive" and (
        exact_proposal is None
        or exact_proposal.decision_record_id is None
        or exact_proposal.decision_record_id != str(body.decision_record_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "proactive scene identity must match one active proposal and its decision record"
            ),
        )
    response.headers["Cache-Control"] = "private, no-store"
    return compose_scene(
        view,
        query=body.query,
        source=body.source,
        proposal_id=body.proposal_id,
        exact_proposal=exact_proposal,
        now=now,
    )
