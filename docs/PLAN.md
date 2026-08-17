<!--
Originally recovered from the 2026-07-09 cloud ultraplan session, then updated
by later owner decisions. This file preserves the broad product roadmap.
HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md is authoritative for the current
wellness runtime, MCP, storage, and iPhone Screen Time boundaries.
-->
# HealthMes Agent — 아키텍처 & 구현 플랜

## Context

HealthMes Agent는 헬스케어 데이터 기반의 **선제적(proactive) 개인 비서**를
목표로 하는 오픈소스 프로젝트다. 이 문서는 day-zero 계획에서 시작했지만 현재는
HealthMes 서비스, 저장소, 수집기, 도메인 엔진과 앱 계약이 구현된 brownfield
저장소의 장기 로드맵이다.

- `vendor/hermes-agent/` — 성숙한 에이전트 런타임: 스킬 시스템, 메모리, 크론 스케줄러, 멀티채널 게이트웨이(Telegram 내장), MCP 클라이언트 지원
- `vendor/open-wearables/` — 웨어러블 데이터 플랫폼: 11개 프로바이더(Garmin/Oura/Fitbit/Whoop/Polar/Suunto/Ultrahuman/Strava/Apple/Google/Samsung), 스트레스·수면·HRV 점수, FastAPI+Postgres, **자체 MCP 서버**(`mcp/`)

**목표 기능:** ① 주간 목표/할일을 던지면 건강상태·인지에너지를 고려해 에이전트가 일정을 자동 배치·수정하고 변경 필요시 **먼저 alert** ② 스트레스 상관관계 인사이트 + 음식 기록 ③ 스케줄+스트레스+헬스+앱사용 데이터를 종합한 인지에너지/집중도 추정과 솔루션 ④ 의사결정을 트리/플로우차트로 웹 열람 ⑤ 의료 라이트(사진/음성→자동 디스크립션→로컬 저장) ⑥ 로컬 first + 암호화 백업 시임(비즈니스 기회)

**사용자 결정사항:** 웨어러블 11개 전부 지원 / Google Calendar + Apple Calendar(iCloud CalDAV) 둘 다 / 의료 라이트 간단 버전 포함 / LLM은 클라우드 API(Claude), 최소 컨텍스트만 전송

## 1. 전체 아키텍처 — 3개 플레인, 벤더 코드 무수정

**현재 원칙 (2026-08-16): `vendor/`는 HealthMes 작업에서 read-only다.**
모든 HealthMes 글루는 레포 루트의 `healthmes/` 패키지와 공개 계약에 산다.

1. **확장점 사용** — REST, MCP, config, Skill과 bounded delivery 계약으로
   연결한다.
2. **Hermes 변경 분리** — 필요한 Hermes 변경은 Hermes 자체 저장소의 별도
   branch/worktree/PR에서 제안한다. HealthMes PR이 vendored tree를 patch하지 않는다.
3. **Open Wearables 변경 분리** — 필요한 upstream 변경도 별도 기여로 처리하고,
   HealthMes는 문서화된 adapter 계약에만 의존한다.

```text
사용자 접점 / 선제 trigger
        |
        v
HealthMes DecisionRequest ingress
        |
        v
Hermes /v1/responses
autonomous LLM + tool loop
        |
        | filtered healthmes MCP only
        v
HealthMes 서비스
        |
        +-- Activity / Nutrition / Calendar
        +-- Wearable adapter
                |
                | bounded REST
                v
        Open Wearables data plane
```

**핵심 연결 결정 (PR #138 canonical 구현):**
- **단일 runtime:** production composition은 HealthMes
  `POST /v1/wellness-decisions`에서 Hermes `/v1/responses`를 정확히 한 번
  호출한다. 폐기된 split-runtime adapter와 공개 builder는 제거했다.
- **제품 질문 경로는 하나:** Client → HealthMes
  `POST /v1/wellness-decisions` → Hermes `/v1/responses` → HealthMes MCP.
  Hermes가 autonomous LLM/tool loop를 소유하고 HealthMes는 제품 ingress,
  데이터 도구, source 검증과 조건부 compact 기록을 소유한다.
- **Hermes ↔ HealthMes:** Hermes가 보는 제품 데이터 도구는 단일 HealthMes
  MCP의 `search_activity`, `search_nutrition`, `search_calendar`,
  `search_wearable`, `list_wellness_skills`, `read_wellness_skill` 6개다.
  REST, channel, proactive와 scheduled 입력은 모두 같은
  `HealthMesDecisionService`로 들어간다.
- **Hermes ↔ Open Wearables:** 제품 경로에서 직접 MCP 연결하지 않는다. Hermes는
  HealthMes MCP의 bounded wearable 도구만 호출한다.
- **HealthMes ↔ Open Wearables:** `OWClient` REST read-only adapter를 사용한다.
  트리거·에너지 엔진과 MCP wearable 도구가 같은 사용자, 기간, provenance와
  local mirror 경계를 공유한다.
- **글루 위치:** 루트에 `healthmes/`(uv 패키지, Python 3.12), `config/`, `scripts/`, 루트 `docker-compose.yml`(postgres+redis+open-wearables+healthmes+hermes). 벤더에 닿는 유일한 산출물은 `HERMES_HOME`에 렌더되는 config 파일과 스킬 심링크 — 둘 다 벤더 트리 밖.

## 1.5 지표 카탈로그 → 의사결정 도구 레이어 (스킬/MCP 설계)

### 사용 가능한 지표 (코드로 검증)

- **시계열 ~100+ 타입** (`constants/series_types/sdk/metric_types.py`, Apple HK + Health Connect 통합 `SeriesType`): 심혈관(heart_rate, resting HR, **HRV sdnn/rmssd**, 1분 심박회복, 보행심박), 혈액/호흡(SpO2, 혈당, 혈압, 호흡수, 폐활량), 체성분(체중/체지방/골격근/체온/**수면 중 손목체온**), 체력(VO2max, 6분 보행), 활동(걸음/에너지/거리/운동시간), 보행 품질(보행 안정성·비대칭·속도), **환경/행동(햇빛 노출 시간, 소음 노출, 음주량, 수분, 흡입기 사용)**, 심방세동 부담, 인슐린
- **헬스 점수** (`health_score` 모델: category/value/qualifier/**components JSONB**): SLEEP(5개 프로바이더), READINESS(Oura/Polar), STRESS(**Garmin만**), BODY_BATTERY(Garmin만), RECOVERY(Whoop/Suunto/Polar), STRAIN(Whoop/Polar) — Garmin은 `data_247.py`에서 avg/max 스트레스+qualifier 인제스트 확인
- **수면 상세** (`sleep_details`): 단계별 분(deep/rem/light/awake), 효율 점수, 낮잠 여부, 원시 스테이지 JSONB
- **운동 상세** (`workout_details`): HR min/max/avg, **HR zones/power zones JSONB**, 에너지, 고도, 케이던스
- **생리주기** (`menstrual_cycle_details`): 주기 단계·가임기·임신 스냅샷 — 인지에너지 v2 요인 후보
- **⭐ 내부 계산 점수가 이미 존재**: ① **OW 수면 점수 0–100** (`algorithms/sleep.py` — 시간/단계/일관성(취침시각 롤링 중앙값 대비)/각성 4-요소, 가중치 0.40/0.20/0.20/0.20) ② **회복탄력성 점수** (`services/scores/resilience_service.py` — 수면 구간 필터링된 HRV 변동계수(CV)→0–100, **원시 심박에서 야간 HRV 재계산**(`calculate_rmssd_ow`, deep-sleep-only 옵션) 포함). 둘 다 Celery 태스크가 `HealthScore(provider=internal)`로 저장 — **인지에너지 엔진이 재발명 없이 그대로 소비**
- **재사용 프리미티브**: `resilience.py`의 `calculate_rmssd/sdnn/hrv_cv`, `resilience_service.py`의 수면구간 추출·일별 그룹핑·baseline 로직, `scoring_primitives.py` sigmoid, summaries의 HR존별 강도 분(minutes)·활동/좌식 분 계산
- **프로바이더 커버리지 실측**: 스트레스+body battery는 **Garmin 전용**(시계열 `garmin_stress_level`/`garmin_body_battery`로도 존재). HRV 변형은 프로바이더가 결정 — Apple/Ultrahuman=SDNN, 나머지=RMSSD (혼용 금지, baseline은 변형별 분리). Fitbit/Strava는 워크아웃 전용(시계열·수면 없음). Whoop은 수면 단계 분은 주지만 hypnogram 구간이 없어 각성 분석 제한

### 검증된 갭: 벤더 MCP는 5개 도구뿐

`mcp/app/tools/` = get_users, get_activity_summary, get_sleep_summary, get_workout_events, get_timeseries. **REST에는 있지만 MCP에 없는 것: `/health-scores`(스트레스·body battery·readiness·내부 수면/회복탄력성 점수 전부!), `/summaries/recovery`, `/summaries/body`, `/events/sleep`의 hypnogram, 생리주기, 워크아웃 HR/파워 존.** 심지어 MCP `get_sleep_summary`는 REST가 주는 단계/효율/HRV/호흡/SpO2 필드를 **버리고** date/start/end/duration/source만 남긴다. 즉 벤더 MCP만으로는 에이전트가 스트레스 점수를 못 본다. 벤더 MCP 포크는 금지(업스트림 sync 부담) — HealthMes MCP에 아래 Layer B로 얹는다.

### Decision Agent와 도구 설계 원칙

**2026-08-16 개정:** MCP 도구는 결정론적 사실과 전문 context를 제공하고,
Hermes의 단일 autonomous LLM loop가 자연어 질문을 해석해 필요한 HealthMes MCP
도구를 선택한다. Skill은 핵심 판단 절차의 source of truth가 아니라 runtime별
도구 사용법과 표현을 돕는 얇은 adapter다. 상세 구조는
[`HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md)
를 따른다.

- **Open Wearables data plane:** 별도 DB와 REST API를 유지한다. Hermes에 직접
  MCP로 노출하지 않고 HealthMes의 bounded wearable adapter 뒤에 둔다.
- **HealthMes MCP 도구 (결정론적 조회·계산):**
  | 도구 | 답하는 의사결정 질문 |
  |---|---|
  | `get_health_scores(range, categories)` | 벤더 MCP 갭 보충: STRESS/BODY_BATTERY/READINESS/RECOVERY + qualifier/components |
  | `get_daily_readiness_context(date)` | "오늘 무리해도 되나?" — 수면부채, HRV vs 14일 baseline z-score, 스트레스(무Garmin 기기는 HRV 프록시), 전일 운동부하, **confidence** |
  | `get_stress_timeline(date)` | "언제/왜 스트레스?" — 시간대별 스트레스·HRV를 캘린더 이벤트·앱사용 세션과 **조인해 구간 라벨링** |
  | `get_cognitive_energy_forecast(date)` | "오늘 deep work은 언제?" — 엔진 출력 + components |
  | `compare_impact(factor, metric, window)` | "활동/음식/사람 X가 나에게 좋나?" — 태그된 이벤트 전후 지표 델타 집계 (n, 평균, confidence) |
  | `get_personal_baselines(metrics)` | 14일/90일 baseline과 현재 편차 |
  | `list_tasks / upsert_task / get_schedule / propose_schedule_blocks` | 일정 도메인 CRUD (propose-then-confirm 게이트) |
  | `log_food / create_medical_record` | 확인된 capture command |
  - 모든 Layer B 도구는 **원시 시계열이 아닌 해석된 델타 + confidence/coverage 필드**를 반환 (토큰 절약·프라이버시·환각 방지·설명가능성 4중 이득). 데이터가 빈약하면 "insufficient_data"를 정직하게 반환.
  - `/mcp`에는 범용 판단 기록 mutation을 제공하지 않는다. 자유 형식 wellness
    판단은 finalizer만 조건부로 저장하고, 캘린더 confirmation 같은 bounded
    command는 해당 내부 workflow만 자체 audit를 쓴다.
- **Decision runtime — Hermes + 얇은 Skill:** Hermes가 질문의 목적, 필요한 영역,
  기간과 tool을 선택하고 첫 결과에 따라 추가 조회한다. HealthMes
  `/v1/wellness-decisions` adapter는 제품 요청·응답 계약, source reference 검증과
  선택적 compact 기록만 소유한다. Hermes의 마지막 자유 형식 text는
  `healthmes.decision-draft.v1` JSON envelope로 strict parse하고, 실제 HealthMes
  MCP transcript에 없는 source reference는 거부한다. 검토된
  `healthmes-wellness-decision`과 domain Skill은 HealthMes MCP의 read-only
  catalog로 제공되며 도구 사용법과 표현 방식을 연결한다. 필수 권한·retention,
  source 검증과 저장 분류는 Skill이 아니라 Python 계약이 소유한다.

**다입력 플랫폼 해자:** Open Wearables 외 건강·행동·환경·일정·주관 상태·의료
입력을 계속 추가할 수 있는 범용 인터페이스 자체를 독립적인 해자로 둔다. 모든
입력은 provenance/confidence/consent/retention을 포함한 공통 계약으로 들어오며,
지원 범위는 넓게 설계하고 실제 adapter는 검증 순서대로 확장한다. 상세 전략은
[`WELLNESS-DATA-PLATFORM.ko.md`](WELLNESS-DATA-PLATFORM.ko.md)를 따른다.

**오픈 앱 커스터마이징 해자:** HealthMes 엔진뿐 아니라 공식 앱의 기능과 UI 연결
계약도 오픈소스로 제공한다. iOS·Android·데스크톱·웹 앱은 교체 불가능한 단일
클라이언트가 아니라 참조 구현이며, 개인과 조직은 화면·알림·승인 workflow·입력
adapter·출력 채널을 포크하거나 교체할 수 있어야 한다. 모든 커스텀 앱은 같은
저장소, 권한, provenance, retention, MCP/Skill 계약을 사용해야 하며 안전 경계는
우회할 수 없다. 코드 공개 자체보다 호환되는 커스텀 앱과 기여가 늘어나는 생태계를
보조 해자로 본다.

**활동 텔레메트리와 교차 영역 판단 계약:** 휴대전화·컴퓨터 activity는
[`ACTIVITY-WELLNESS-MVP.ko.md`](ACTIVITY-WELLNESS-MVP.ko.md)의 self-hosted
MVP 경계를 따른다. 모호한 질문의 LLM tool selection, Context Access Layer와
Hermes runtime 경계는
[`HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md),
해자 정의는
[`MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md`](MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md),
현재 고정 resolver 호환 계약은
[`HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md`](contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md)
를 사용한다. Activity Ingest는 Open Wearables를 다시 수집하지 않으며 각 domain
context는 HealthMes MCP의 bounded 도구를 거쳐 Hermes의 단일 autonomous turn이
종합하고, HealthMes가 source reference와 저장 결과를 검증한다.

**소유권 메모 (2026-08-05):** 음식 분석·음식 사진 인식의 추가 개발은 sake가
담당한다. HealthMes는 기존 음식 기록 경로만 유지하고, sake의 결과를 공통 웰니스
입력 계약으로 받아 다른 맥락과 연결한다. 중복 모델 조사·구현은 하지 않는다.

**다입력 플랫폼 해자:** Open Wearables 외 건강·행동·환경·일정·주관 상태·의료
입력을 계속 추가할 수 있는 범용 인터페이스 자체를 독립적인 해자로 둔다. 모든
입력은 provenance/confidence/consent/retention을 포함한 공통 계약으로 들어오며,
지원 범위는 넓게 설계하고 실제 adapter는 검증 순서대로 확장한다. 상세 전략은
[`WELLNESS-DATA-PLATFORM.ko.md`](WELLNESS-DATA-PLATFORM.ko.md)를 따른다.

**소유권 메모 (2026-08-05):** 음식 분석·음식 사진 인식의 추가 개발은 sake가
담당한다. HealthMes는 기존 음식 기록 경로만 유지하고, sake의 결과를 공통 웰니스
입력 계약으로 받아 다른 맥락과 연결한다. 중복 모델 조사·구현은 하지 않는다.

### 지표 신뢰도 경계 (도구에 내장)

손목 HRV는 야간 측정만 신뢰 구간(주간 스팟 측정은 노이즈), Garmin 스트레스 자체가 HRV 파생 추정치, 소비자기기 칼로리는 부정확. → Layer B 도구가 측정 조건·커버리지를 confidence로 계량화하고, 스킬 프롬프트에 "confidence 낮으면 단정적 조언 금지" 명시. 햇빛 노출·소음·음주·수분·생리주기 단계는 흔히 무시되지만 인지에너지와 상관이 높은 지표 — v2 에너지 요인으로 예약.

## 2. 신규 도메인 모델 — `healthmes/store/`

같은 Postgres 인스턴스의 **별도 `healthmes` 데이터베이스**, 자체 SQLAlchemy 모델 + 자체 Alembic. (open-wearables 모델 확장은 업스트림 sync 시 마이그레이션 충돌로 기각. 모델/타이핑 컨벤션은 `vendor/open-wearables/backend/app/models/health_score.py` 스타일을 따름.)

| 테이블 | 핵심 컬럼 |
|---|---|
| `weekly_goal` | week_start, title, priority, status |
| `task` | title, goal_id, est_minutes, deadline, energy_demand(low/med/high), status, source(user/agent) |
| `calendar_event_mirror` | external_id, calendar_source(google/caldav), start/end, is_agent_created, agent_task_id, etag/sync_token |
| `schedule_proposal` | task_id, proposed_start/end, status(proposed/accepted/pushed/declined), decision_record_id |
| `food_log` | logged_at, description(LLM 생성), media_path, meal_type, source |
| `app_usage_sample` | device_id, collection_generation, bucket_start, app_package, foreground_seconds, launches, category |
| `cognitive_energy_estimate` | window_start/end, score(0–100), components JSONB(요인별 기여), inputs_snapshot JSONB |
| `decision_record` | kind(schedule_change/alert/insight/capture), tree JSONB, summary, llm_model, tokens |
| `insight` | period, kind, statement, evidence JSONB, confidence |
| `medical_record` | kind(medication/symptom), description(LLM), media_path, transcript, context JSONB |
| `trigger_event` | fired_at, rule_id, payload, alert_sent, dedup_key |

미디어(사진/음성)는 `HEALTHMES_DATA_DIR/media/` 파일시스템에, DB에는 경로만 저장 (백업/export 단순화).

## 3. 인지에너지 엔진 — 설명가능한 룰 기반 v1

`healthmes/engine/cognitive_energy.py` — 순수 함수, ML 없음. 모든 요인이 이름·가중치가 붙은 항으로 `components` JSONB에 기록됨 (이것이 그대로 의사결정 트리의 "고려한 입력" 노드가 됨).

```
score = 100
  − sleep_debt_penalty      (OW 내부 수면 점수 그대로 소비 — algorithms/sleep.py의
                             4-요소 점수, 재발명 금지)
  − stress_penalty          (시간가중 STRESS 점수 — Garmin만 네이티브 제공,
                             타 기기는 내부 resilience/HRV-CV 프록시로 대체)
  − hrv_deviation_penalty   (오늘 야간 HRV vs 개인 14일 baseline —
                             resilience_service.py의 수면구간 필터링·재계산 로직 재사용,
                             SDNN/RMSSD 변형별 baseline 분리)
  + body_battery_bonus      (BODY_BATTERY/READINESS/RECOVERY 제공 시)
  − meeting_load_penalty    (calendar_event_mirror: 예약 시간 + 컨텍스트 스위칭 횟수)
  − fragmentation_penalty   (app_usage_sample: 방해성 앱 실행 빈도 — 데이터 있을 때만)
```

- **누락 신호는 항 자체가 빠지고 가중치 재정규화** (일반 iOS 빌드는
  앱사용 데이터가 없고, 조건부 Screen Time aggregate도 coverage가 없으면
  결측으로 처리; Fitbit/Strava는 수면조차 없음 — 필수 설계).
- 개인 baseline = 14일 트레일링 중앙값, 매일 밤 재계산. HRV는 야간(수면 구간) 측정만 사용 — 주간 스팟 측정은 노이즈.
- **실행 위치: HealthMes 서비스 내 APScheduler** (매시간 persist + 온디맨드 엔드포인트). Celery beat(벤더 수정 필요)와 Hermes cron(결정론적 산수에 LLM 호출 낭비) 기각.

## 4. 선제적 Alert 루프

**현재 제품 delivery:** `DecisionAlertSender`가 완료된 결과를 HealthMes의 durable
alert stream에 적재하고 native companion이 `/v1/alerts`와 glance로 읽는다.
Telegram을 포함한 messaging channel은 future bounded adapter이며 별도 wellness
판단 경로가 되어서는 안 된다.

1. **이벤트 구동 ("에이전트가 먼저 알림"):**
   `healthmes/engine/triggers.py`가 결정론적 룰을 평가한다. 발화 시 같은 internal
   DecisionRequest service를 호출하고, Hermes `/v1/responses`가 filtered
   HealthMes MCP로 필요한 근거를 자율 조회한다. HealthMes가 source를 검증하고
   필요한 경우 compact DecisionRecord를 저장한 뒤, 결과를 durable alert/native
   delivery로 전달한다. future channel adapter도 이 결과만 relay한다. 중복 방지는
   `trigger_event.dedup_key`가 담당한다.
2. **시간 구동 브리핑:** 아침 플랜(07:00), 저녁 리뷰(21:30), 주간 계획(일요일)
   스케줄은 입력 시점만 결정한다. 실제 wellness reasoning은 cron 안에서 별도
   Hermes 대화를 시작하지 않고 같은 internal DecisionRequest service를 호출한다.

2026-08-16 현재 시간 구동 입력과 proactive trigger는
`DecisionAlertSender`를 통해 같은 internal DecisionRequest service에 연결된다.
legacy parallel Hermes reasoning 구현과 설정은 제거됐다. bootstrap
migration은 HealthMes 소유권을 증명할 수 있는 과거 cron만 제거하고 사용자 또는
소유권 불명 cron은 보존한다. 실제 Telegram/UI inbound와 outbound channel
integration은 아직 없으며, 디바이스/채널 팀은 UI-neutral channel adapter 계약을
감싸서 구현해야 한다.

## 5. 의사결정 트리 설명가능성

- **스키마:** `decision_record.tree` JSONB — 재귀 노드 `{id, type: input|rule|llm_step|option|action, label, detail, children[]}`. 결정론 레이어(트리거 룰, 에너지 엔진)가 `input`/`rule` 노드를 **선기입**하고 LLM은 자기 rationale과 선택만 append — 사후 조작이 아닌 정직한 트리.
- **렌더링 (MVP): 서버사이드 Mermaid.** HealthMes 서비스의 `GET /decisions/{id}`가 Jinja+Mermaid.js로 플로우차트 페이지 반환. compact record가 있는 alert surface는 링크를 첨부할 수 있다. Phase 2에서 React Flow 뷰어(`healthmes/web/`)로 업그레이드 여지.

## 6. 캘린더 동기화 — Google + iCloud CalDAV

`healthmes/calendars/` — 공통 `CalendarBackend` 프로토콜 + 2개 구현:
- `google.py` — Google Calendar API, OAuth installed-app flow, **syncToken 증분 동기화**, 5분 폴링
- `caldav_icloud.py` — `caldav` 라이브러리 + 앱 전용 비밀번호(`caldav.icloud.com`), ctag/etag 비교, 10분 폴링

**충돌 철학 = 소유권 분할 (동기화 늪 회피):** 외부 캘린더가 에이전트가 만들지 않은 모든 이벤트의 source of truth. 에이전트는 자기 블록만 쓰고(`healthmes=1` extended property / `X-HEALTHMES` iCal 속성 태깅) 자기 것만 이동/삭제 가능. 사용자가 외부에서 에이전트 이벤트를 수정하면 외부 승리 → mirror diff가 `schedule_changed` 트리거 발화 → 에이전트 재계획 + 선제 alert (제품이 원하는 동작 그 자체).

**좁은 확정 예외:** morning recovery nudge는 외부 이벤트 불변 원칙을 기본값으로 유지하되,
사용자가 Telegram live reply에서 `적용 <handle>`을 보낸 뒤에만 Google의 단일 eligible
event를 `SHORTEN`할 수 있다. 대상/변경량/ETag는 server evaluator가 고정하고, agent는
07:00 cron에서 `evaluate_morning_calendar_nudge()` 결과 display packet과
`적용 <handle>` / `그대로 <handle>` 문구를 그대로 보내고 종료한다. 이후 allowed-user
reply는 live Hermes session이 원문에서 response와 handle을 분리하고, 기존 evaluation의
정확한 Telegram 응답 문자열과 변경하지 않은 `reply_handle`을
`resolve_calendar_adjustment()`에 전달한다. 서버는 owner-bound signed proof를
검증하고 handle로 pending proposal을 찾는다. MOVE,
삭제, 제목/참석자/반복 수정, iCloud/CalDAV 외부 이벤트 변경은 여전히 금지다.
Hermes는 allowed-user의 exact reply와 tool arguments가 일치할 때만 5분짜리 HMAC
session proof를 주입하고, HealthMes는 proof + one-time handle을 모두 검증한다. 따라서
cron이나 모델이 evaluator 응답에서 본 handle만 재사용해 자가승인할 수 없다.
`APPLIED_RECOVERED`, `UNKNOWN`, `FAILED_NO_CHANGE`는 이 예외의 서버 내부 terminal
receipt 상태일 뿐 새 사용자 동작이나 추가 calendar mutation 권한을 뜻하지 않는다.

**신뢰 구축:** 초기엔 propose-then-confirm(Telegram에서 승인 후 캘린더 기록), 패턴이 수락되면 자동 기록으로 승격.

## 7. 앱사용 추적 — 현실 점검

- **Android (MVP 경로):** 최소 컴패니언 앱 `apps/android-usage/` (Kotlin, 페어링+토글 한 화면). `UsageStatsManager.queryEvents` + WorkManager 30분 주기 → 시간별 버킷을 `POST /v1/app-usage/batch`로 전송. ~1주 작업량.
- **iOS (조건부 aggregate 경로):** 최신 Apple App & Website Usage data-access
  capability가 허용되는 환경에서는 완료된 local-hour별 앱 사용시간과 category를
  수집해 `POST /v1/activity/ios/report`로 보낸다. 앱 ID는 기기 Keychain key로
  HMAC 가명화하고 pickup을 launch로 가장하지 않는다. capability, entitlement,
  사용자·지역 조건이 맞지 않으면 사용시간 `0` 대신 명시적 unavailable 상태를
  보고한다. 권한 승인 직후 첫 sync, foreground catch-up, best-effort
  background task와 bounded offline outbox는 같은 UI-neutral single-flight
  pipeline에 연결한다. entitlement 승인, 실제 권한 UI, distribution signing과
  실기기 dogfood는 외부/device-team 조건이다.
- **통합 설정:** 데스크톱 웹과 미래 iPhone UI는
  `GET /v1/inputs`, `GET /v1/inputs/{source_id}`,
  `PUT /v1/inputs/{source_id}/settings`를 사용한다. 이 API는 별도 설정 DB를
  만들지 않고 activity collector의 기기별 수집 제어, domain별 Decision Agent
  동의와 데이터 클래스별 보존 정책을 합성한다. Nutrition, Wearable, Calendar는
  실제 adapter가 강제하는 connect/disconnect/sync action만 노출하며 구현되지
  않은 범용 enable/pause를 만들지 않는다. 기존 HealthKit raw-first receiver도
  `wearable.healthkit-bridge` source로 같은 목록에 포함한다.
- **GPS/location 후속:** iOS와 Android의 opt-in, coarse-first 수집,
  source-side private zone, 짧은 raw 좌표 보존, 파생 이동 context와 Decision
  Agent provider는 Issue #158에서 구현한다.

## 8. 음식 + 의료 라이트 캡처

**현재 범위는 UI-neutral capture command다.** 사진·음성·텍스트를 구조화한
호출자는 `log_food(...)` 또는 `create_medical_record(...)` 같은 bounded command를
사용한다. `healthmes-capture`는 channel wrapper가 이를 연결하는 절차를 문서화하지만
PR #138은 실제 Telegram/UI inbound를 설치하지 않는다. routine capture는 domain
record만 만들고 DecisionRecord를 만들지 않는다.

**디바이스 제약:** 워치 카메라는 없으므로 future device wrapper에서 사진은 폰이
담당하고 워치는 alert 수신과 음성 transcript 전달만 맡는다. 이 UI wiring은
device-team 범위다. 의료 기록은 `doctor-visit-summary`의 로컬 브리핑으로 연결된다.

## 8.5 UX 전달 모델 — "화면이 아니라 알림 문법이 UX다"

두 벤더의 프론트엔드는 모두 소비자용 개인 건강 UI가 **아니다** (Hermes `web/` =
관리콘솔+채팅, open-wearables `frontend/` = 개발자 포털). PR #138은 새 UI를
만들지 않고 제품 ingress와 UI-neutral adapter 계약만 제공한다.

**3-표면 모델:**
1. **Future app/channel wrapper** — `DecisionChannelAdapter`로
   `HealthMesDecisionService`를 정확히 한 번 호출하고 결과를 표시한다. Telegram,
   iOS, Android, web inbound 구현은 device/channel 팀 범위다.
2. **의사결정 뷰어 웹페이지** (HealthMes 서비스가 서빙, 유일하게 새로 만드는 UI) — alert의 "자세히" 링크로 열리는 Mermaid 트리 + 주간 리포트 페이지. 모바일 브라우저 대응이면 충분.
3. **Hermes web ChatPage** (이미 존재) — runtime 관리와 개발자 진단용이다.
   제품 wellness 대화 UI로 노출하려면 별도 adapter가 HealthMes ingress를
   호출해야 한다.

**알림 문법 표준화 (planner/insight 스킬에 명시, 이것이 곧 제품 디자인):**
```
[관찰 1줄] 오늘 회복 점수 38, 어젯밤 깊은수면 22분.
[근거 1줄] 최근 2주 평균 대비 HRV -18%.
[제안]     14시 집중 블록을 내일 오전으로 옮기고 오후는 가벼운 일만 배치할게요.
[버튼]     ✅ 적용   ✏️ 수정   ❌ 오늘은 그대로     (Telegram inline keyboard)
[링크]     왜 이 판단? → http://…/decisions/abc123
```
모든 선제 메시지가 같은 형태 → 사용자는 3초 안에 읽고 원탭으로 결정. 인터랙티브 Q&A는 이 메시지에 답장하면 시작.

**단계적 확장:** Phase 1 canonical service + native alert stream → Phase 2
channel wrappers와 결정 뷰어/주간 리포트 → 이후 필요 시 실시간 push 또는 PWA를
검토한다.

## 9. 로컬 first + 암호화 백업 시임 (비즈니스 레이어)

MVP는 클라우드가 아닌 **시임(인터페이스)만** 정의:
- `healthmes/backup/provider.py` — `BackupProvider` 프로토콜: `export_snapshot()`, `restore(path)`, `list_snapshots()`
- 현재 스냅샷 포맷(버전드 envelope): manifest.json + HealthMes DB + `media/` +
  `raw_ingest/`를 기본으로 하고, 설정된 경우 Open Wearables dump와
  `HERMES_HOME`을 추가 → tar → **age 암호화**(passphrase 파생). 외부 provider
  credential, 모든 compose volume과 연결 상태까지 복구하는 전체 Personal Data
  Node 재해복구본은 아니다.
- MVP 구현: `LocalDirectoryProvider` + CLI `healthmes backup create/restore` + 주간 자동 백업
- 미래 유료 서비스 = 동일 프로토콜의 `RemoteVaultProvider`(S3 호환 + 클라이언트사이드 암호화, 서버는 평문 불가시). **이 인터페이스를 우회한 데이터 반출 금지.**
- LLM 프라이버시(지금부터 강제): Claude API 호출만 머신 밖으로, 스킬은 요약-후-전송, MCP 도구는 집계값 반환, 원시 시계열/미디어는 반출 안 함.

저장 정본, 모바일 queue, `HEALTHMES_DATA_DIR`, 데이터별
`1/7/14/30/90일/무기한` 보존, iCloud 역할, RemoteVault 용량 관리,
가격 정책 Future Work, 삭제·복구 및
worktree 격리의 상세 계약은
[`STORAGE-ARCHITECTURE.ko.md`](STORAGE-ARCHITECTURE.ko.md)를 따른다.

## 10. 단계별 로드맵

**Phase 0 — 기반 & 글루 (~1–2주)**
- 루트 `docker-compose.yml`: postgres(+healthmes db), redis, open-wearables backend+worker+beat, healthmes 서비스, optional `hermes-decision`
- `healthmes/` uv 패키지: FastAPI 스켈레톤, `store/` 모델+Alembic, fastmcp 마운트
- `config/hermes-decision-config.yaml.tmpl`: API server에는 filtered HealthMes MCP만
  등록하고 direct Open Wearables MCP와 mutation tool을 제외, Telegram은
  decision profile에 등록하지 않음
- `scripts/bootstrap.py`: 격리된 decision profile 렌더·attestation, API 키 생성,
  legacy HealthMes 소유 cron reasoning만 제거
- **종료 데모:** `POST /v1/wellness-decisions`에 "이번 주 수면 어땠어?" →
  Hermes가 HealthMes MCP의 wearable search를 선택 → source_refs가 검증된 답변
  → future channel wrapper도 같은 `DecisionChannelAdapter` 계약으로 동일 service를
  정확히 한 번 호출. 실제 Telegram/UI 연결은 별도 device/channel 작업이다.

**Phase 1 — MVP: 데이터 인입 + 일정 비서 + 선제 alert + 기본 인사이트 (~4–6주)**
- 도메인 모델(weekly_goal, task, calendar_event_mirror, schedule_proposal, food_log, trigger_event, insight) + REST
- **Decision-read MCP 도구 1차분:** bounded
  `search_activity/search_nutrition/search_calendar/search_wearable`과
  specialist context 도구. 일정 CRUD와 `log_food` 같은 mutation은 별도 capture
  또는 confirmation profile에만 노출하며 wellness decision turn에는 노출하지
  않는다.
- `healthmes/calendars/` Google + iCloud 동기화 (§6)
- `healthmes/engine/triggers.py` + internal DecisionRequest + outbound delivery
  (§4), 같은 ingress를 사용하는 scheduled briefing
- 스킬: `healthmes-wellness-decision` 공통 조회 지침,
  `healthmes-nutrition-decision` read-only 영양·카페인 조회 지침,
  `healthmes-planner`(목표 덤프→태스크 분해→배치 룰→확인된 캘린더 제안),
  `healthmes-capture`(bounded capture command)
- 인사이트 v1: 템플릿 SQL 상관 (시간대별/요일별/캘린더 키워드별 스트레스, 활동유형 vs 스트레스) — 자유 데이터마이닝 아님

**Phase 2 — 인지에너지 + 설명가능성 UI + Android 사용량 (~3–4주)**
- `cognitive_energy.py` + baseline + 매시간 persist (§3), **Layer B 2차분**: `get_cognitive_energy_forecast`, `get_stress_timeline`(캘린더·앱사용 조인), `compare_impact`
- source-validated conditional DecisionRecord E2E: 행동 제안·행동 가능한 위험
  경고·trusted explicit tracking만 compact record로 저장하고 단순 조회는 기본
  미저장. mutation audit는 별도 command workflow가 소유하며, 저장된 판단은
  Mermaid 뷰어에서 확인
- `apps/android-usage/` + `/v1/app-usage/batch`, fragmentation 항 활성화
- 집중도 인사이트 ("14–16시 집중 저하: 수면 부족 + Slack 시간당 9회 실행")

**Phase 3 — 의료 라이트 + 백업 시임 (~2–3주)**
- `medical_record` 모델 + capture 스킬 의료 분기(약/증상 사진, 음성 메모) + `doctor-visit-summary` 스킬
- `healthmes/backup/` 프로토콜 + LocalDirectoryProvider + age 암호화 + CLI, RemoteVault 계약 문서(비즈니스 시임)
- 하드닝: 트리거 dedup/rate-limit, 복원 훈련, 벤더 업스트림 sync 드라이런

## 11. 리스크 & 단순화

- **최대 리스크 — 알림 소음.** 잘못 울리는 비서는 일주일 안에 음소거된다. 완화: 결정론적 트리거가 모든 push를 게이트(LLM 자체 발화 금지), 룰별 쿨다운, 일일 alert 예산, 방해금지 시간.
- **iOS 상세 foreground timeline은 하드월** — 조건부 Screen Time aggregate
  export seam만 사용하고 private API로 우회하지 않는다 (§7).
- **캘린더 쓰기 신뢰:** propose-then-confirm으로 시작.
- **벤더 드리프트:** 커플링 표면은 Open Wearables REST v1, Hermes
  `/v1/responses`, MCP/config와 outbound delivery 계약으로 제한한다. compose
  부팅 + Phase-0 demo query를 CI smoke test로 둔다.
- **MVP에서 잘라낸 것:** 실제 messaging/device channel UI, ML 전부, 자유형 인사이트
  마이닝, React 의사결정 UI(Mermaid 먼저), 멀티유저, 클라우드 백업 서비스,
  Apple entitlement 승인·distribution signing·실기기 iPhone Screen Time
  dogfood, Hermes MoA 루프.
- **이미 확보한 단순화:** channel-neutral ingress, MCP=글루(커스텀 통합 API 제거), 소유권 분할 캘린더 동기화(충돌 해결 제거), 룰 기반 에너지 엔진(ML 파이프라인 제거).

## 검증 방법

- **Phase 0:** `docker compose --profile decision up` →
  `POST /v1/wellness-decisions`에 "이번 주 수면 어땠어?" → Hermes transcript에서
  HealthMes wearable search 호출과 source_refs 검증 확인 → channel adapter
  contract가 canonical service를 한 번만 호출하는 테스트 확인. 실제 Telegram/UI
  전달은 이 범위 밖이다. 스모크: `curl :8100/health`, `curl :8000/docs`.
- **Phase 1:** channel adapter fixture로 주간 목표 3개를 제출 → planner가 태스크
  분해 + 캘린더 블록 제안 → bounded confirmation → Google/iCloud 캘린더에 태깅된
  이벤트 생성 확인. 외부에서 이벤트 이동 → 10분 내 `schedule_changed` alert
  확인. capture command → domain row와 디스크립션 확인.
- **Phase 2:** `GET /cognitive-energy/forecast` 응답의 components 합산 검증(단위 테스트), alert 링크 → Mermaid 트리 페이지 렌더 확인, Android 기기에서 사용량 배치 인입 확인.
- **Phase 3:** `healthmes backup create` → 새 환경 `restore` → 데모 쿼리 재통과. age 복호화 없이 스냅샷 열람 불가 확인.
- 공통: `healthmes/`에 pytest(엔진·트리거·동기화 단위 테스트 — factory-boy/testcontainers 패턴은 open-wearables backend 테스트 컨벤션 참조).

## 구현 시 핵심 파일

- `vendor/hermes-agent/tools/mcp_tool.py` — 읽기 전용 `mcp_servers` config 계약
- `vendor/hermes-agent/cron/jobs.py:940 create_job` — 기존 cron 동작을 이해하기
  위한 읽기 전용 참조. 목표 브리핑 판단은 HealthMes internal ingress를 사용한다.
- `vendor/hermes-agent/skills/productivity/google-workspace/` — Google OAuth/Calendar 참조 구현 (에이전트 ad-hoc 조작용으로도 활용 가능)
- `vendor/open-wearables/mcp/app/main.py` +
  `mcp/app/services/api_client.py` — 기존 MCP와 HealthMes가 재사용할 REST client
  pattern의 읽기 전용 참조. 제품 decision runtime에는 direct MCP를 등록하지 않는다.
- `vendor/open-wearables/backend/app/constants/health_scores.py` — 에너지 엔진이 소비할 점수 카테고리 (STRESS는 Garmin만 → 내부 resilience 프록시)
- `vendor/open-wearables/backend/app/algorithms/sleep.py` + `services/scores/resilience_service.py` — **재발명 금지 대상**: 내부 수면 점수(4-요소)와 HRV-CV 회복탄력성, 야간 HRV 재계산·수면구간 필터링 로직
- `vendor/open-wearables/backend/app/api/routes/v1/summaries.py`, `health_scores.py`, `timeseries.py`, `events.py` — Layer B 도구가 프록시할 REST 표면
- `vendor/open-wearables/backend/app/schemas/enums/series_types.py` — 통합 SeriesType 어휘 (~100+ 타입)
- `vendor/open-wearables/backend/app/models/` — `healthmes/store/`가 따를 모델 컨벤션

## Phase 4–7 로드맵

Phase 0–3 완료 이후의 확장 단계. issue #7(컴패니언 앱 글랜스 표면)이 Phase 5–7의
사전 실기기 작업 범위를 정의했고(feat/phase5-7-glance-vault에서 서버/앱 플럼빙 구현),
issue #10(풀 네이티브 폰 앱)·#11(macOS/Windows 데스크톱 글랜스)이 Phase 5를 실앱
수준으로 확장했다(feat/native-apps-desktop). **원칙 유지: vendor 무수정, 로컬 first,
알림 문법(§8.5)이 디자인 시스템, 워치 알림 UX 최종 설계는 헬스케어 도메인 전문가 몫.**

**Phase 4 — 실사용 안정화 (전부 남음 — 실기기·실크리덴셜 필요)**
- 실크리덴셜 가동: 선택한 decision model provider + open-wearables 프로바이더 OAuth +
  캘린더 자격증명을 실제로 연결하고 Phase-0 데모 쿼리부터 알림 루프까지 라이브 통과
- 알림 소음 튜닝: 실사용 데이터로 트리거 임계값·쿨다운·일일 예산 보정 (§11 최대 리스크)
- 전문가 스킬 온보딩: `docs/EXPERT-ONBOARDING.ko.md` 프로토콜대로 도메인 전문가가
  스킬/지표를 실기기 QA와 함께 반입

**Phase 5 — 글랜스 표면 → 네이티브 컴패니언/데스크톱 앱 (issue #7 → #10·#11)**
- 이번에 구현(#7 — 글랜스 플럼빙): ① `GET /v1/briefing/glance` — 위젯/컴플리케이션용
  경량 브리핑 계약(에너지 점수+24h 커브+confidence, 다음 블록 ≤3, 알림 요약, 최신 결정
  링크; ETag/304, 5분 캐시, bearer 인증) ② Android 컴패니언(`apps/android-usage/` —
  :shared 계약 모듈, :companion 홈/잠금 위젯+§8.5 문법 알림 채널, :wear Wear OS 타일+
  컴플리케이션) ③ iOS/watchOS 컴패니언(`apps/ios-companion/` — WidgetKit 홈/잠금 위젯,
  watchOS 앱+컴플리케이션, WatchConnectivity 페어링) — 모두 base-url+bearer 페어링으로
  자기 healthmes 인스턴스에만 접속(로컬 first) ④ 전문가 설계 워크시트
  `docs/design/WATCH-NOTIFICATIONS.ko.md`
- 이번에 구현(#10 — 풀 네이티브 폰 앱): ⑤ 서버 확장 — `POST /v1/media`(멀티파트 업로드,
  타입 화이트리스트+용량 캡), `GET /v1/media/{path}`(bearer 또는 파생 뷰어 토큰),
  `POST /v1/medical-records`(Telegram capture 스킬과 동일 계약의 REST — 건강 스냅샷은
  서버가 부착, 인프라 사유로 캡처가 실패하지 않음), `GET /v1/alerts`(§8.5 문법 알림
  이력 — glance top-alert와 동일 결정 링크 휴리스틱을 테스트로 핀) ⑥ iOS 풀 앱 —
  브리핑 홈(24h 커브·다음 블록·제안 승인/수정/유지·알림 이력), 주간 리포트 네이티브 뷰,
  결정 뷰어(SFSafariViewController), 카메라/음성 캡처→media→food/medical,
  BGAppRefreshTask+UNUserNotificationCenter §8.5 알림(실제 accept/decline 액션), 집중
  블록 Live Activity, en+ko 로컬라이즈+VoiceOver — 시뮬레이터 빌드+유닛/UI 테스트+라이브
  E2E로 증명 ⑦ Android :companion 풀 앱 승격 — Compose 단일 액티비티(브리핑·리포트·
  캡처·제안·설정 5탭), §8.5 알림 실제 액션(WorkManager, 409→"이미 처리됨"), 진행형
  집중블록 알림(포그라운드 서비스 없이 OS 크로노미터+자기소멸)+Wear 브리징,
  values-ko+TalkBack — gradle 빌드+JVM 테스트로 증명
- 이번에 구현(#11 — 데스크톱 글랜스): ⑧ macOS(`apps/macos-companion/`) — 메뉴바 앱
  (상태 아이템 점수+팝오버 브리핑+§8.5 알림/실제 액션), WidgetKit 위젯, 앰비언트
  스크린세이버 .saver(프라이버시 토글 — 숨김=부재가 테스트된 데이터 규칙), iOS Shared
  소스를 그대로 컴파일(계약/클라이언트 단일화) — 네이티브 빌드+XCTest+라이브 E2E로 증명
  ⑨ Windows(`apps/windows-companion/`) — 트레이 앱(플라이아웃·§8.5 토스트), .scr
  스크린세이버(/s·/p·/c+프라이버시 토글), 위젯 Adaptive Card 빌더(보드 프로바이더는
  MSIX 서명 요구로 유예 — DEFERRED.md), DPAPI 페어링, en+ko .resx — macOS에서
  크로스컴파일+xunit으로 증명, 실빌드 증명은 windows-latest CI 잡 ⑩ 크로스플랫폼 픽스처
  핀 확장 — `tests/api/test_glance_fixtures.py`가 glance·alerts·weekly 픽스처를 세
  플랫폼 사본 전부 서버 모델로 검증 ⑪ 앱 CI 신설 — `windows-apps.yml`·`apple-apps.yml`·
  `android-apps.yml`(경로 필터, 전부 무서명)
- 남음: 실기기/실OS 검증(시뮬레이터·JVM·크로스컴파일 증명까지 완료 — BG 태스크 실행
  주기·알림 배너 전달·Live Activity 실표시·카메라·Wear/워치 하드웨어·Windows 실기기,
  그리고 신설 windows/apple/android CI 잡의 첫 PR 실행이 곧 컴파일 증명), 전문가 UX
  설계 반영(시각 요소는 여전히 명시적 플레이스홀더 — 워크시트 Q1–Q6 대기, watch 앱
  심화도 함께), 푸시 릴레이는 설계상 제외 유지(폴링 전용, APNs/FCM/WNS
  미구축), alert→schedule_proposal 연결 필드(알림 액션 버튼이 특정 제안을
  겨냥하게 — 현재는 보류 제안이 정확히 1건일 때만 동작하는 무추측 정책), 제안 거절
  노트(store 컬럼+마이그레이션 필요 — 계약은 서버 에이전트 기록 참조), Windows 위젯
  보드 프로바이더(MSIX+서명 파이프라인)

**Phase 6 — 장기 맥락**
- 이번에 구현: ① 인지에너지 v2 요인 5종 — 생리주기 단계·햇빛 노출·소음 노출·음주·
  수분 (§1.5에서 예약한 v2 요인; 신호 없으면 항이 빠지고 재정규화되는 v1 규칙 유지,
  가중치·임계값은 전문가 튜닝용 플레이스홀더로 명시) ② 주간 리포트
  `GET /reports/weekly`(+`.json`) — 에너지 추이 스파크라인, 인사이트, 일정 수용률,
  알림 다이제스트, 결정 목록; 일요일 주간 계획 브리핑이 링크 안내
- 남음: `compare_impact` 축적 활용 심화 (태그 이벤트가 쌓인 뒤의 장기 상관 리뷰 절차,
  주간 리포트와의 연결)

**Phase 7 — 원격 저장 기반 (§9 시임의 구현)**
- 이번에 구현: `RemoteVaultProvider` — 동일 `BackupProvider` 프로토콜로 S3 호환
  엔드포인트(AWS/R2/MinIO)에 age 암호문 스냅샷만 복제(평문·비-age 업로드 거부, 서버는
  암호문만 보관), 로컬 스냅샷 우선 + 업로드 무결성 검증, `healthmes backup push`/
  `--provider remote` CLI, 주간 잡 셀렉터 연동 (`docs/BACKUP.md` §3)
- 남음: 멀티테넌트 서비스화와 가격 정책 (호스팅 vault, 키·테넌트 관리, SLA를
  포함하며 실제 저장량 계측 이후 별도 Future Work에서 결정)

---

## 12. 핵심 유즈케이스 정렬 — "던져놓으면 알아서" 비서 (2026-07-15)

사용자 정의 핵심 유즈케이스:
> 할 일·주간 목표·프로젝트를 대충 던져놓으면, 에이전트가 **일정과 건강 데이터(수면·
> 스트레스·인지에너지)를 종합해 알아서 스케줄을 배치·수정**하고, 일정 변경이나 컨디션
> 변화가 있으면 **내가 묻기 전에 먼저 알림**을 준다. 비서처럼.

### 적합성 판단 — 부품은 다 있고, "지능"만 켜면 된다

이 루프에 필요한 **모든 부품이 구현·테스트 완료**돼 있다:

| 루프 단계 | 담당 | 상태 |
|---|---|---|
| ① 할 일 던지기 (저마찰 인입) | `weekly_goal`/`task` REST·MCP, future channel adapter | ✅ 계약 / 실제 channel 미구현 |
| ② 건강·인지에너지 맥락 조회 | Layer B MCP 14종, 인지에너지 엔진 v2 | ✅ (실데이터 검증됨) |
| ③ 에너지-인지 기반 일정 배치 판단 | `healthmes-planner` 스킬 + `propose_schedule_blocks` | ⚠️ **LLM 키 필요** |
| ④ 캘린더에 기록 (승인 게이트) | Google/iCloud 동기화, propose-then-confirm | ✅ (실 OAuth 미검증) |
| ⑤ 변경/컨디션 시 선제 알림 | 트리거 4종 → 웹훅/네이티브 배달 | ✅ (시임 실증) |
| ⑥ 재계획 (외부 일정 변경 감지) | 캘린더 diff → `schedule_changed` 트리거 | ✅ |

**결론: 아키텍처는 이 유즈케이스에 정확히 맞다.** 유일한 실질 공백은 ③의 판단 지능이
`healthmes-planner` 스킬 안에 있는데 **LLM 키가 있어야 실행**된다는 것 — 즉 "미완성"이
아니라 "아직 안 켠 것"이다. 크리덴셜(LLM·웨어러블·캘린더)만 넣으면 루프가 돈다.

### 보강할 것 (핵심 루프 완성도)

1. **planner 스킬 E2E 실검증** — 목표 덤프 → 태스크 분해 → 에너지 예보 기반 블록 제안 →
   승인 → 캘린더 기록의 전 구간을 실 LLM으로 한 번 관통 (지금은 목/합성만).
2. **인입 마찰 최소화** — "대충 던지기"가 실제로 쉬워야 한다. future app/channel의
   한 줄·음성이 `DecisionChannelAdapter`를 거쳐 planner로 이어지는 경로를 실사용
   다듬기 (Phase 4).
3. **재계획 신뢰 구축** — 외부 일정 변경 → 재계획 알림이 과하지 않게 (쿨다운·예산은
   이미 있음), propose-then-confirm에서 자동 기록으로의 승격 기준 실사용 튜닝.

### 뺄 것 / 미루기 (기능 과다 방지 — 사용자 우려 반영)

핵심 루프가 실사용으로 검증되기 전까지 **아래는 의도적으로 뒤로 미룬다**. 지금 벌리면
핵심이 흐려진다:

- **의료 라이트 캡처 (§8)** — 결이 다른 별개 관심사(계획 자체가 분리 가능하다고 명시).
  이미 구현돼 있으니 **유지하되 홍보/확장 안 함**; 핵심 비서 루프에 인지 부담 주지 않기.
- **데스크톱 표면 (이슈 #11: macOS/Windows 위젯·화면보호기)** — 있으면 좋지만 스케줄
  비서의 본질이 아님. 핵심 루프 검증 후로.
- **네이티브 앱 정식 출시 (이슈 #37)** — REST와 channel adapter fixture로 먼저
  데모하고 스토어 출시는 유즈케이스가 검증된 뒤.
- **웹 디자인 전면 개편 (이슈 #38)** — 현재 UI로 근거 열람은 충분. 핵심 루프가 먼저.
- **푸시 릴레이(APNs/FCM)** — 로컬-first 원칙상 보류; durable alert stream과 앱
  폴링으로 시작하고 실시간성이 병목으로 확인될 때만 추가한다.

**한 줄 원칙: 지금은 "스케줄 조언 루프 하나를 실데이터로 완벽히 돌리는 것"에 집중하고,
표면·플랫폼·부가 도메인은 그 뒤에 넓힌다.**

## 13. 온보딩 마찰 제거 — "설치·로그인만으로 연동" (2026-07-16 결정)

소유자 결정: 앱스토어 출시·위젯/화면보호기 UX(#7·#10·#11·#37·#38)는 뒤로 미루고,
**연동 온보딩을 "자동 또는 로그인만"으로 만드는 것**과 **의미 있는 데이터가 끊기지
않고 계속 쌓이는 것**을 선행한다. 네이티브 앱 코드는 이 단계에서 건드리지 않는다.

| 연동 | 목표 경험 | 방법 | 상태 |
|---|---|---|---|
| 애플워치 백필 | 파일 하나 업로드 | Health 앱 내보내기 ZIP → `healthmes import apple <file>` → OW `/import/apple/xml/direct` (`healthmes/apple_import.py`) | ✅ 구현 |
| 애플워치 연속 수집 | 폰이 알아서 주기 업로드 | `POST /v1/ingest/healthkit`(`healthmes/api/ingest.py`): 기성 HealthKit 자동 내보내기 앱의 POST를 받아 **raw 원본을 무조건 먼저 저장**(`raw_ingest/` + `raw_ingest_event` 색인, 스냅샷 백업 포함) 후 베스트에포트로 OW SDK sync 계약으로 변환·전달. 파싱 실패도 저장·수용. `POST /v1/ingest/raw`는 임의 소스용 | ✅ 구현 |
| 구글 캘린더 | 브라우저 로그인 한 번 | 프로젝트 명의 OAuth 클라이언트(설치형 앱, gcloud/rclone 패턴)를 동봉 — 코드는 이미 `HEALTHMES_GOOGLE_CLIENT_SECRET_FILE`+표준 경로 폴백 구조라 등록된 클라이언트 JSON만 실으면 됨. 민감 스코프 심사(수일~수주)는 병행 신청 | ⏳ 소유자 콘솔 등록 대기 |
| iCloud 캘린더 | 앱 암호 1회 (구조적 한계 — 애플이 CalDAV OAuth 미제공) | 기존 `connect icloud` 안내 흐름 유지 | ✅ |
| 알림 | 설정 0 | `native_alert_delivery` 기본값 **true** 전환 완료 — 컴패니언 폴링만으로 알림 수신, Telegram은 옵션 | ✅ |
| 클라우드 웨어러블 (가민·오우라 등) | 로그인만 | 프로바이더별 파트너 앱 + OAuth 릴레이 호스팅 필요 — §9 시임과 같은 "통과만 하는 호스팅" 원칙으로만 허용. 배포 단계로 보류 | ⏸ |

**데이터 연속성 원칙:** 실사용 데이터(수면·HRV·활동·캘린더·결정 기록)는 중단 없이
축적된다 — 백필(import)로 과거를 채우고, 연속 수집(브리지/SDK)으로 미래를 잇고,
주간 암호화 스냅샷으로 유실을 막는다. 데모 시드는 실데이터가 붙는 즉시 폐기 가능해야
한다(`_demo` wipe 키 유지).

## 14. 연속 증거 원칙과 판단 보정 로드맵 (2026-07-27 소유자 결정)

### 판단 원칙 (2026-07-27 소유자 재결정 — 오버엔지니어링 회귀)

1. **sake의 원설계 유지** — 신호는 각각 **개별로** 센다 (HRV와 회복탄력성은
   측정 방식·시간축이 다른 별개 신호). 그의 프록시-HRV 이중계산 방지 예외도
   그가 쓴 그대로. "근거 그룹으로 묶어 1표" 일반화는 **철회**(에이전트 제안이
   과보수적·과설계였음 — 행동 지표의 증거 승격도 함께 철회, 맥락 전용 복원).
2. **강한 단일 신호 = 가벼운 제안 가능** (소유자 요구) — 신호가 하나여도
   개인 baseline 대비 큰 편차·중간+ 신뢰도면 선택적·가역적 제안 1개를 붙일
   수 있다. 결정 자체(reconsider)는 여전히 sake의 교차 확인 규칙을 따른다.
3. **해소됨 (2026-07-28, 도메인 전문가 sake 피드백)**:
   - #54 각성 힌트: 무이견 (raw 확인 후 승인).
   - 강한 단일 신호 제안: **동의** — 단 알림에 ⑴ 단일 신호 기반임과 ⑵ 기준이
     개인 baseline임을 명시할 것 (스킬에 반영됨).
   - 비-가민 "두 번째 증거": **수일간 안정심박(RHR) 상승 추세** 채택.
     핵심 프레이밍 — RHR은 "스트레스 지표"가 아니라 **자율신경계 등의 영향을
     받은 심장 반응의 간접 관찰값**이다. 스킬·알림·문서 어디서도 스트레스
     지표라 부르지 말 것. 구현은 기존 순서 유지: 소유자 실데이터 축적 →
     분석 → 배선 (추측 선행 금지).

### 임계값 자동 보정 로드맵 (하드코딩 상수의 수명 종료 계획)

- 현재 상수(각성 마진 +12bpm 등)는 **콜드스타트 임시값** — 코드에 명시됨.
- 1단계(실데이터 2주 후): 소유자 raw를 직접 분석해 **개인 분포 기반**으로 교체
  (예: 조용할-때-심박 상위 10% 분위수, 주간 재계산, 결정 기록에 산출 근거 기록).
- 2단계: 사용자 반응(제안 수락/거절·알림 무시)으로 상·하한 내 소폭 재보정.
- LLM은 숫자를 정하지 않는다 — 관찰·제안까지만 (제1원칙 유지).
- 안정심박(RHR) 다일 추세 배선도 같은 분석 후 진행 (추측 선행 금지 — 소유자 지시).

### 변인 커버리지 결정

- ✅ 이번에 추가: **월 목표**(monthly_goal + 주 목표 연결, list_goals/upsert_goal),
  **전날 과부하 이월**(carryover_load, 어제 예약시간 4h→9h 램프, 어제 데이터
  없으면 결측 처리로 기존 흐름 바이트 동일).
- ✅ 구현: **ActivityWatch 기반 데스크톱 활동 수집** — macOS, Windows, Linux의
  localhost 데이터를 bounded import와 scheduler로 중앙 activity 저장소에 적재.
- ⏸ 보류: **macOS companion 자체의 네이티브 활동 수집기** — 네이티브 앱 작업
  재개 시 별도 구현.
- ⚠️ 조건부: 아이폰 상세 foreground timeline은 제공하지 않는다. Screen Time
  aggregate export용 server/sync/lifecycle/background seam과 14일 bounded
  outbox는 연결됐지만 일반 빌드에서는 비활성이다. Apple entitlement 승인,
  signed provisioning, 사용자 data-access 승인과 실기기 검증은 외부 후속이다.
- ❌ 불가(구조적): 피부전기활동(애플워치 센서 없음).
