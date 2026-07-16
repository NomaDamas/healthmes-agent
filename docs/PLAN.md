<!-- Recovered verbatim from the cloud ultraplan session (2026-07-09, session_01NFxUZjjJqQqnSsc9se7biW). This is the authoritative implementation plan. "사용자 결정사항" below were set during that planning session — revisit if they change. -->
# HealthMes Agent — 아키텍처 & 구현 플랜

## Context

HealthMes Agent는 헬스케어 데이터 기반의 **선제적(proactive) 개인 비서**를 목표로 하는 오픈소스 프로젝트다. 현재 레포는 README + 두 개의 vendored 업스트림만 있는 day-zero 스캐폴드이며 글루 코드는 0줄이다.

- `vendor/hermes-agent/` — 성숙한 에이전트 런타임: 스킬 시스템, 메모리, 크론 스케줄러, 멀티채널 게이트웨이(Telegram 내장), MCP 클라이언트 지원
- `vendor/open-wearables/` — 웨어러블 데이터 플랫폼: 11개 프로바이더(Garmin/Oura/Fitbit/Whoop/Polar/Suunto/Ultrahuman/Strava/Apple/Google/Samsung), 스트레스·수면·HRV 점수, FastAPI+Postgres, **자체 MCP 서버**(`mcp/`)

**목표 기능:** ① 주간 목표/할일을 던지면 건강상태·인지에너지를 고려해 에이전트가 일정을 자동 배치·수정하고 변경 필요시 **먼저 alert** ② 스트레스 상관관계 인사이트 + 음식 기록 ③ 스케줄+스트레스+헬스+앱사용 데이터를 종합한 인지에너지/집중도 추정과 솔루션 ④ 의사결정을 트리/플로우차트로 웹 열람 ⑤ 의료 라이트(사진/음성→자동 디스크립션→로컬 저장) ⑥ 로컬 first + 암호화 백업 시임(비즈니스 기회)

**사용자 결정사항:** 웨어러블 11개 전부 지원 / Google Calendar + Apple Calendar(iCloud CalDAV) 둘 다 / 의료 라이트 간단 버전 포함 / LLM은 클라우드 API(Claude), 최소 컨텍스트만 전송

## 1. 전체 아키텍처 — 3개 플레인, 벤더 코드 무수정

**원칙 (2026-07-16 개정): `vendor/`는 "기본 무수정 + 필요 시 규율 있는 수정".** 모든 글루는 레포 루트의 `healthmes/` 패키지에 산다. 벤더를 신성불가침으로 두는 게 아니라, 업스트림 업데이트를 계속 받기 위한 **우선순위 규칙**이다:

1. **확장점 먼저** — 플러그인·스킬·MCP·설정·글루로 해결 가능하면 벤더를 건드리지 않는다 (지금까지 전 기능이 이 방식으로 충분했다).
2. **불가능하면 벤더 수정 허용** — 단 ⑴ `vendor(hermes):`/`vendor(ow):` prefix의 **분리 커밋**으로, ⑵ 커밋 메시지에 왜 확장점으로 안 됐는지 기록, ⑶ 업스트림 pull 시 재적용 가능하도록 최소 diff.
3. **범용 가치가 있으면 업스트림 PR로 환원** — 우리 포크에만 쌓지 말 것 (오픈소스 선순환, 공모전 §기대효과의 실증이기도 함).

```
┌────────────────── 사용자 접점 ──────────────────┐
│  Telegram (폰+워치 알림/음성응답)  의사결정 뷰어(웹) │
└──────────┬──────────────────────────▲──────────┘
           │ chat/push                │ alert 내 링크
┌──────────▼──────────┐     ┌─────────┴──────────────┐
│ 에이전트 플레인       │ MCP │ HealthMes 플레인 (신규)  │
│ vendor/hermes-agent │◄───►│ healthmes/ 서비스       │
│ gateway+cron+skills │◄─── │ FastAPI + fastmcp      │
│ Claude API          │웹훅  │ 도메인 DB, 엔진, 캘린더  │
└──────────┬──────────┘     └─────────┬──────────────┘
           │ MCP (stdio)              │ REST (read-only)
┌──────────▼──────────────────────────▼──────────────┐
│ 데이터 플레인 — vendor/open-wearables               │
│ FastAPI + Postgres + Celery + 11개 프로바이더        │
│ mcp/ 서버 (activity/sleep/workouts/timeseries)     │
└────────────────────────────────────────────────────┘
```

**핵심 연결 결정 (모두 실제 코드로 검증됨):**
- **Hermes ↔ open-wearables: 기존 MCP 서버 그대로 사용.** Hermes `config.yaml`의 `mcp_servers`에 `vendor/open-wearables/mcp/`를 stdio 서버로 등록 (계약: `vendor/hermes-agent/tools/mcp_tool.py`). 코드 0줄, 도구가 요약값을 반환하므로 "클라우드 LLM에 최소 컨텍스트" 정책과도 일치. 에이전트의 직접 DB 접근은 스키마 커플링 때문에 기각.
- **Hermes ↔ HealthMes: pull은 MCP, push는 게이트웨이 웹훅.** HealthMes FastAPI가 `/mcp`에 fastmcp 마운트(Streamable HTTP — `mcp_tool.py`가 `url:` 트랜스포트 지원). 선제 알림은 HealthMes → Hermes 웹훅 플랫폼(HMAC 서명, `gateway/platforms/webhook.py`의 route가 prompt 템플릿+skills+`deliver: telegram` 지원).
- **HealthMes ↔ open-wearables: REST read-only.** 트리거/에너지 엔진은 LLM 없이 결정론적으로 데이터를 읽어야 하므로 `mcp/app/services/api_client.py`와 같은 API-key 클라이언트 패턴 재사용.
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

### 3-레이어 도구 설계 원칙

**원칙: MCP 도구 = 결정론적 사실(조회·계산·해석된 델타), 스킬 = 판단 절차(어떤 도구를 언제 쓰고 어떻게 해석할지). 지표별 도구가 아니라 "의사결정 질문" 단위 도구.**

- **Layer A — 벤더 MCP (그대로 등록):** 범용 조회 5종. 111개 시계열은 `get_timeseries(types=[...])`가 이미 커버 — 지표별 마이크로 도구 금지 (도구 선택 붕괴).
- **Layer B — HealthMes MCP "해석된 컨텍스트" 도구 (결정론적, LLM엔 결과만):**
  | 도구 | 답하는 의사결정 질문 |
  |---|---|
  | `get_health_scores(range, categories)` | 벤더 MCP 갭 보충: STRESS/BODY_BATTERY/READINESS/RECOVERY + qualifier/components |
  | `get_daily_readiness_context(date)` | "오늘 무리해도 되나?" — 수면부채, HRV vs 14일 baseline z-score, 스트레스(무Garmin 기기는 HRV 프록시), 전일 운동부하, **confidence** |
  | `get_stress_timeline(date)` | "언제/왜 스트레스?" — 시간대별 스트레스·HRV를 캘린더 이벤트·앱사용 세션과 **조인해 구간 라벨링** |
  | `get_cognitive_energy_forecast(date)` | "오늘 deep work은 언제?" — 엔진 출력 + components |
  | `compare_impact(factor, metric, window)` | "활동/음식/사람 X가 나에게 좋나?" — 태그된 이벤트 전후 지표 델타 집계 (n, 평균, confidence) |
  | `get_personal_baselines(metrics)` | 14일/90일 baseline과 현재 편차 |
  | `list_tasks / upsert_task / get_schedule / propose_schedule_blocks` | 일정 도메인 CRUD (propose-then-confirm 게이트) |
  | `log_food / create_medical_record / record_decision` | 캡처 + 설명가능성 |
  - 모든 Layer B 도구는 **원시 시계열이 아닌 해석된 델타 + confidence/coverage 필드**를 반환 (토큰 절약·프라이버시·환각 방지·설명가능성 4중 이득). 데이터가 빈약하면 "insufficient_data"를 정직하게 반환.
- **Layer C — 스킬 (얇은 판단 지침):** `healthmes-planner`(배치 룰: 에너지 높은 시간에 energy_demand=high 태스크, 회복 낮으면 운동 대신 휴식 제안 등), `healthmes-capture`, `healthmes-insight`(주간 리뷰 절차), Phase 3 `doctor-visit-summary`. **스킬 스크립트가 REST를 직접 호출하는 것 금지** — 데이터 접근이 MCP를 우회하면 decision tree 기록이 끊긴다.

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
| `app_usage_sample` | device_id, bucket_start, app_package, foreground_seconds, launches, category |
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

- **누락 신호는 항 자체가 빠지고 가중치 재정규화** (iOS 사용자는 앱사용 데이터 없음, Fitbit/Strava는 수면조차 없음 — 필수 설계).
- 개인 baseline = 14일 트레일링 중앙값, 매일 밤 재계산. HRV는 야간(수면 구간) 측정만 사용 — 주간 스팟 측정은 노이즈.
- **실행 위치: HealthMes 서비스 내 APScheduler** (매시간 persist + 온디맨드 엔드포인트). Celery beat(벤더 수정 필요)와 Hermes cron(결정론적 산수에 LLM 호출 낭비) 기각.

## 4. 선제적 Alert 루프

**MVP 채널: Telegram 단일.** 폰+워치(Apple Watch/Wear OS 알림 미러링, 음성 빠른답장) 모두 커버, 접근성 좋음, Hermes 게이트웨이가 alert→응답→인터랙티브 세션 수명주기를 공짜로 제공. 벤더 코드 무수정으로 두 메커니즘:

1. **이벤트 구동 ("에이전트가 먼저 알림"):** `healthmes/engine/triggers.py`(APScheduler 10분 주기)가 결정론적 룰 평가 — 스트레스 스파이크 vs baseline, 낮은 body battery + 무거운 오후 일정, 외부 캘린더 변경이 기존 계획과 충돌, 데드라인 위험. 발화 시 → HMAC 서명 POST → Hermes 웹훅 route(`prompt` 템플릿 + `skills: [healthmes-planner]` + `deliver: telegram`) → 에이전트가 양쪽 MCP로 근거 조회 → `record_decision` 호출 → Telegram push → 사용자 응답 시 일반 게이트웨이 세션으로 Q&A. 중복 방지: `trigger_event.dedup_key`.
2. **시간 구동 브리핑:** Hermes cron(`cron/jobs.py::create_job`, `scripts/bootstrap.py`가 등록) — 아침 플랜(07:00, "오늘 일정을 에너지 예보 기반으로 배치 제안"), 저녁 리뷰(21:30), 주간 계획(일요일). `script:` 컨텍스트 주입으로 상태 스냅샷 JSON을 프롬프트에 선주입해 도구 왕복 절약.

## 5. 의사결정 트리 설명가능성

- **스키마:** `decision_record.tree` JSONB — 재귀 노드 `{id, type: input|rule|llm_step|option|action, label, detail, children[]}`. 결정론 레이어(트리거 룰, 에너지 엔진)가 `input`/`rule` 노드를 **선기입**하고 LLM은 자기 rationale과 선택만 append — 사후 조작이 아닌 정직한 트리.
- **렌더링 (MVP): 서버사이드 Mermaid.** HealthMes 서비스의 `GET /decisions/{id}`가 Jinja+Mermaid.js로 플로우차트 페이지 반환. 모든 Telegram alert에 링크 첨부. Phase 2에서 React Flow 뷰어(`healthmes/web/`)로 업그레이드 여지.

## 6. 캘린더 동기화 — Google + iCloud CalDAV

`healthmes/calendars/` — 공통 `CalendarBackend` 프로토콜 + 2개 구현:
- `google.py` — Google Calendar API, OAuth installed-app flow, **syncToken 증분 동기화**, 5분 폴링
- `caldav_icloud.py` — `caldav` 라이브러리 + 앱 전용 비밀번호(`caldav.icloud.com`), ctag/etag 비교, 10분 폴링

**충돌 철학 = 소유권 분할 (동기화 늪 회피):** 외부 캘린더가 에이전트가 만들지 않은 모든 이벤트의 source of truth. 에이전트는 자기 블록만 쓰고(`healthmes=1` extended property / `X-HEALTHMES` iCal 속성 태깅) 자기 것만 이동/삭제 가능. 사용자가 외부에서 에이전트 이벤트를 수정하면 외부 승리 → mirror diff가 `schedule_changed` 트리거 발화 → 에이전트 재계획 + 선제 alert (제품이 원하는 동작 그 자체).

**신뢰 구축:** 초기엔 propose-then-confirm(Telegram에서 승인 후 캘린더 기록), 패턴이 수락되면 자동 기록으로 승격.

## 7. 앱사용 추적 — 현실 점검

- **Android (MVP 경로):** 최소 컴패니언 앱 `apps/android-usage/` (Kotlin, 페어링+토글 한 화면). `UsageStatsManager.queryEvents` + WorkManager 30분 주기 → 시간별 버킷을 `POST /v1/app-usage/batch`로 전송. ~1주 작업량.
- **iOS: DeviceActivity/Screen Time API는 데이터 오프디바이스 반출 불가** (샌드박스 확장 안에서만 렌더). **권장: MVP에서 스킵** — 엔진이 신호 없이 재정규화. 옵션으로 주간 Screen Time 스크린샷을 Telegram 봇에 보내면 비전 모델이 대략적 버킷으로 추출하는 습관을 문서화 (capture 스킬 덕에 거의 공짜). 네이티브 iOS 추적은 만들지 않음.

## 8. 음식 + 의료 라이트 캡처

**Telegram 봇이 곧 캡처 앱 — 새 캡처 UI 없음.** Hermes 게이트웨이가 인바운드 사진/음성을 이미 처리. `healthmes-capture` 스킬(SKILL.md)이 지시: 미디어/음성 분류 → 음식 vs 약/증상 vs 기타 → Claude 비전/전사로 구조화된 디스크립션 생성 → MCP 도구 `log_food(...)` 또는 `create_medical_record(...)` 호출 (디스크립션 + 미디어 경로 + 타임스탬프 + 현재 건강 컨텍스트 스냅샷). 확인 메시지로 원탭 정정.

**워치 제약:** 워치 카메라는 없으므로 사진은 폰 전담. 워치는 alert 수신 + 음성 빠른답장(음성 메모 로깅 경로)으로 참여 — "워치와 폰 모두" 요구를 인터랙션 루프 수준에서 충족. 의료 기록은 Phase 3의 `doctor-visit-summary` 스킬(진료 브리핑 로컬 생성)로 연결.

## 8.5 UX 전달 모델 — "화면이 아니라 알림 문법이 UX다"

두 벤더의 프론트엔드는 모두 소비자용 개인 건강 UI가 **아니다** (Hermes `web/` = 관리콘솔+채팅, open-wearables `frontend/` = 개발자 포털). 그러나 MVP에서 새 앱을 만들 필요가 없다 — 이 제품의 UX는 화면이 아니라 **대화와 알림의 일관된 문법**이기 때문.

**3-표면 모델:**
1. **Telegram = 일상 UX의 90%** — 선제 alert, 승인/거절, 음식·의료 캡처, 질문답변. 폰·워치·데스크톱 전부 자동 커버, 접근성(스크린리더·음성입력) 기본 제공.
2. **의사결정 뷰어 웹페이지** (HealthMes 서비스가 서빙, 유일하게 새로 만드는 UI) — alert의 "자세히" 링크로 열리는 Mermaid 트리 + 주간 리포트 페이지. 모바일 브라우저 대응이면 충분.
3. **Hermes web ChatPage** (이미 존재) — 데스크톱 파워유저의 긴 대화·설정·cron 관리용. 우리가 만들 것 없음.

**알림 문법 표준화 (planner/insight 스킬에 명시, 이것이 곧 제품 디자인):**
```
[관찰 1줄] 오늘 회복 점수 38, 어젯밤 깊은수면 22분.
[근거 1줄] 최근 2주 평균 대비 HRV -18%.
[제안]     14시 집중 블록을 내일 오전으로 옮기고 오후는 가벼운 일만 배치할게요.
[버튼]     ✅ 적용   ✏️ 수정   ❌ 오늘은 그대로     (Telegram inline keyboard)
[링크]     왜 이 판단? → http://…/decisions/abc123
```
모든 선제 메시지가 같은 형태 → 사용자는 3초 안에 읽고 원탭으로 결정. 인터랙티브 Q&A는 이 메시지에 답장하면 시작.

**단계적 확장:** Phase 1 Telegram only → Phase 2 결정 뷰어+주간 리포트 → 이후 필요 시 PWA 대시보드 검토. 네이티브 앱은 최후의 수단 (Android 사용량 수집기는 UI 없는 백그라운드 수집기로 예외).

## 9. 로컬 first + 암호화 백업 시임 (비즈니스 레이어)

MVP는 클라우드가 아닌 **시임(인터페이스)만** 정의:
- `healthmes/backup/provider.py` — `BackupProvider` 프로토콜: `export_snapshot()`, `restore(path)`, `list_snapshots()`
- 스냅샷 포맷(버전드 envelope): manifest.json + `healthmes` pg_dump + open-wearables pg_dump + `media/` + `HERMES_HOME` 메모리/상태 → tar → **age 암호화**(passphrase 파생)
- MVP 구현: `LocalDirectoryProvider` + CLI `healthmes backup create/restore` + 주간 자동 백업
- 미래 유료 서비스 = 동일 프로토콜의 `RemoteVaultProvider`(S3 호환 + 클라이언트사이드 암호화, 서버는 평문 불가시). **이 인터페이스를 우회한 데이터 반출 금지.**
- LLM 프라이버시(지금부터 강제): Claude API 호출만 머신 밖으로, 스킬은 요약-후-전송, MCP 도구는 집계값 반환, 원시 시계열/미디어는 반출 안 함.

## 10. 단계별 로드맵

**Phase 0 — 기반 & 글루 (~1–2주)**
- 루트 `docker-compose.yml`: postgres(+healthmes db), redis, open-wearables backend+worker, healthmes 서비스, hermes gateway
- `healthmes/` uv 패키지: FastAPI 스켈레톤, `store/` 모델+Alembic, fastmcp 마운트
- `config/hermes-config.yaml.tmpl`: mcp_servers(open-wearables stdio + healthmes http), telegram 플랫폼, 웹훅 route
- `scripts/bootstrap.py`: config 렌더 → `HERMES_HOME`, 스킬 심링크, API 키 생성, cron 등록
- **종료 데모: Telegram에서 "이번 주 수면 어땠어?" → open-wearables MCP 경유 답변**

**Phase 1 — MVP: 데이터 인입 + 일정 비서 + 선제 alert + 기본 인사이트 (~4–6주)**
- 도메인 모델(weekly_goal, task, calendar_event_mirror, schedule_proposal, food_log, trigger_event, insight) + REST
- **Layer B MCP 도구 1차분**: `get_health_scores`(벤더 MCP 갭 보충 — 스트레스·body battery·내부 점수), `get_daily_readiness_context`, `get_personal_baselines`, 일정 CRUD(`list_tasks`/`upsert_task`/`get_schedule`/`propose_schedule_blocks`), `log_food`, `record_decision`
- `healthmes/calendars/` Google + iCloud 동기화 (§6)
- `healthmes/engine/triggers.py` + 웹훅 push (§4), Hermes cron 브리핑
- 스킬: `healthmes-planner`(목표 덤프→태스크 분해→배치 룰→캘린더 블록 제안→decision 기록), `healthmes-capture`(음식 경로만)
- 인사이트 v1: 템플릿 SQL 상관 (시간대별/요일별/캘린더 키워드별 스트레스, 활동유형 vs 스트레스) — 자유 데이터마이닝 아님

**Phase 2 — 인지에너지 + 설명가능성 UI + Android 사용량 (~3–4주)**
- `cognitive_energy.py` + baseline + 매시간 persist (§3), **Layer B 2차분**: `get_cognitive_energy_forecast`, `get_stress_timeline`(캘린더·앱사용 조인), `compare_impact`
- `decision_record` E2E: `record_decision` MCP 도구, 결정론 선기입, Mermaid 뷰어, 모든 alert에 링크
- `apps/android-usage/` + `/v1/app-usage/batch`, fragmentation 항 활성화
- 집중도 인사이트 ("14–16시 집중 저하: 수면 부족 + Slack 시간당 9회 실행")

**Phase 3 — 의료 라이트 + 백업 시임 (~2–3주)**
- `medical_record` 모델 + capture 스킬 의료 분기(약/증상 사진, 음성 메모) + `doctor-visit-summary` 스킬
- `healthmes/backup/` 프로토콜 + LocalDirectoryProvider + age 암호화 + CLI, RemoteVault 계약 문서(비즈니스 시임)
- 하드닝: 트리거 dedup/rate-limit, 복원 훈련, 벤더 업스트림 sync 드라이런

## 11. 리스크 & 단순화

- **최대 리스크 — 알림 소음.** 잘못 울리는 비서는 일주일 안에 음소거된다. 완화: 결정론적 트리거가 모든 push를 게이트(LLM 자체 발화 금지), 룰별 쿨다운, 일일 alert 예산, 방해금지 시간.
- **iOS 사용량은 하드월** — 싸우지 않고 우회 설계 (§7).
- **캘린더 쓰기 신뢰:** propose-then-confirm으로 시작.
- **벤더 드리프트:** 커플링 표면은 open-wearables REST v1 + MCP 도구명, Hermes config/skill/cron/웹훅 계약뿐. compose 부팅 + Phase-0 데모 쿼리를 CI 스모크 테스트로.
- **MVP에서 잘라낸 것:** Telegram 외 모든 채널, 네이티브 워치/폰 앱(Android 사용량 수집기 제외), ML 전부, 자유형 인사이트 마이닝, React 의사결정 UI(Mermaid 먼저), 멀티유저, 클라우드 백업 서비스, iOS 사용량 캡처, Hermes MoA 루프.
- **이미 확보한 단순화:** Telegram=캡처앱(모바일 앱 하나 제거), MCP=글루(커스텀 통합 API 제거), 소유권 분할 캘린더 동기화(충돌 해결 제거), 룰 기반 에너지 엔진(ML 파이프라인 제거).

## 검증 방법

- **Phase 0:** `docker compose up` → Telegram 봇에 "이번 주 수면 어땠어?" → open-wearables MCP 도구 호출로 실데이터 답변 확인. 스모크: `curl :8100/health`, `curl :8000/docs`.
- **Phase 1:** 주간 목표 3개를 Telegram으로 덤프 → planner가 태스크 분해 + 캘린더 블록 제안 → 승인 → Google/iCloud 캘린더에 태깅된 이벤트 생성 확인. 외부에서 이벤트 이동 → 10분 내 `schedule_changed` alert 수신 확인. 음식 사진 전송 → `food_log` 행 + 디스크립션 확인.
- **Phase 2:** `GET /cognitive-energy/forecast` 응답의 components 합산 검증(단위 테스트), alert 링크 → Mermaid 트리 페이지 렌더 확인, Android 기기에서 사용량 배치 인입 확인.
- **Phase 3:** `healthmes backup create` → 새 환경 `restore` → 데모 쿼리 재통과. age 복호화 없이 스냅샷 열람 불가 확인.
- 공통: `healthmes/`에 pytest(엔진·트리거·동기화 단위 테스트 — factory-boy/testcontainers 패턴은 open-wearables backend 테스트 컨벤션 참조).

## 구현 시 핵심 파일

- `vendor/hermes-agent/tools/mcp_tool.py` — `mcp_servers` config 계약 (두 MCP 브리지 연결점)
- `vendor/hermes-agent/cron/jobs.py:940 create_job` — 브리핑 등록 시그니처 (schedule/skills/deliver/script)
- `vendor/hermes-agent/gateway/platforms/webhook.py` — 선제 push용 HMAC 웹훅 route
- `vendor/hermes-agent/skills/productivity/google-workspace/` — Google OAuth/Calendar 참조 구현 (에이전트 ad-hoc 조작용으로도 활용 가능)
- `vendor/open-wearables/mcp/app/main.py` + `mcp/app/services/api_client.py` — 그대로 등록할 MCP 서버 + HealthMes 엔진이 재사용할 REST 클라이언트 패턴
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
- 실크리덴셜 가동: Telegram 봇 + Claude API + open-wearables 프로바이더 OAuth +
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
  심화도 함께), 푸시 릴레이는 설계상 제외 유지(폴링 전용, 보장 전달은 Telegram —
  APNs/FCM/WNS 미구축), alert→schedule_proposal 연결 필드(알림 액션 버튼이 특정 제안을
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

**Phase 7 — 비즈니스 레이어 (§9 시임의 구현)**
- 이번에 구현: `RemoteVaultProvider` — 동일 `BackupProvider` 프로토콜로 S3 호환
  엔드포인트(AWS/R2/MinIO)에 age 암호문 스냅샷만 복제(평문·비-age 업로드 거부, 서버는
  암호문만 보관), 로컬 스냅샷 우선 + 업로드 무결성 검증, `healthmes backup push`/
  `--provider remote` CLI, 주간 잡 셀렉터 연동 (`docs/BACKUP.md` §3)
- 남음: 과금/멀티테넌트 서비스화 (호스팅 vault 상품화, 키·테넌트 관리, SLA — 시임
  뒤편의 서버 사이드 사업 영역)

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
| ① 할 일 던지기 (저마찰 인입) | `weekly_goal`/`task` REST·MCP, Telegram/앱 캡처 | ✅ (라이브 미검증) |
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
2. **인입 마찰 최소화** — "대충 던지기"가 실제로 쉬워야 한다. Telegram 한 줄/음성으로
   목표·할 일을 넣으면 planner가 자동으로 도는 경로를 실사용 다듬기 (Phase 4).
3. **재계획 신뢰 구축** — 외부 일정 변경 → 재계획 알림이 과하지 않게 (쿨다운·예산은
   이미 있음), propose-then-confirm에서 자동 기록으로의 승격 기준 실사용 튜닝.

### 뺄 것 / 미루기 (기능 과다 방지 — 사용자 우려 반영)

핵심 루프가 실사용으로 검증되기 전까지 **아래는 의도적으로 뒤로 미룬다**. 지금 벌리면
핵심이 흐려진다:

- **의료 라이트 캡처 (§8)** — 결이 다른 별개 관심사(계획 자체가 분리 가능하다고 명시).
  이미 구현돼 있으니 **유지하되 홍보/확장 안 함**; 핵심 비서 루프에 인지 부담 주지 않기.
- **데스크톱 표면 (이슈 #11: macOS/Windows 위젯·화면보호기)** — 있으면 좋지만 스케줄
  비서의 본질이 아님. 핵심 루프 검증 후로.
- **네이티브 앱 정식 출시 (이슈 #37)** — 웹(공개 URL) + Telegram으로 충분히 데모·초기
  사용 가능. 스토어 출시는 유즈케이스가 검증된 뒤.
- **웹 디자인 전면 개편 (이슈 #38)** — 현재 UI로 근거 열람은 충분. 핵심 루프가 먼저.
- **푸시 릴레이(APNs/FCM)** — 로컬-first 원칙상 보류; Telegram 즉시 push + 앱 폴링으로
  커버. 실시간성이 유즈케이스의 병목으로 확인될 때만.

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
