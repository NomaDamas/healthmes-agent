# PR #138 구현 및 코드 리뷰 가이드

> **작성일:** 2026-08-23
>
> **대상:** PR #138 `feat: add HealthMes Decision Agent and unified wellness inputs`
>
> **WHOOP baseline 병합 기준점:** `3b81e956`
>
> **이번 갱신 이전 리뷰 문서 기준점:** `c8640707`
>
> **Sake 기능 기준점:** recovery package `103b7269`, Open Wearables Cycle
> `day_strain` 수집 `1726fd8a`.
>
> **목적:** Sake와 후속 리뷰어가 PR의 대목표, 실제 실행 경로, 저장 경계,
> 핵심 불변조건과 검증 지점을 빠르게 확인하도록 한다.

## TLDR

PR #138은 HealthMes의 자유 형식 wellness 판단 경로를 하나로 통합했다.

```text
REST / Channel / Proactive / Scheduled
                  |
                  v
      HealthMesDecisionService
                  |
                  v
       Hermes /v1/responses
                  |
        하나의 LLM/tool loop
                  |
                  v
         HealthMes MCP 6 tools
                  |
      +-----------+-----------+-----------+
      |           |           |           |
  Activity    Nutrition    Calendar    Wearable
      |           |           |           |
      +-----------+-----------+-----------+
                  |
                  v
      source_refs 검증과 조건부 저장
```

LLM은 질문에 필요한 domain, 기간, capability와 추가 조회를 자율적으로 고른다.
HealthMes 코드는 조회 허용 범위, 보존기간, 시간대, 정확한 계산, provenance와
저장을 소유한다. Hermes는 이 판단을 실행하는 교체 가능한 runtime이며
`vendor/hermes-agent/`는 수정하지 않았다.

중요한 경계가 하나 있다. **한 부모 LLM은 MVP의 조회 계획과 종합에는 충분하지만,
엄밀한 데이터 조회를 혼자 보장하지는 않는다.** 정확한 행, 기간, 중복 제거,
freshness, retention과 `source_refs`는 HealthMes의 결정론적 조회 코드가
보장한다.

Sake의 WHOOP 기능도 같은 경계를 따른다.

```text
WHOOP Skill
  -> search_wearable("wearable.whoop-recovery-package")
  -> WearableContextProvider
  -> Open Wearables Recovery + Cycle day_strain
  -> snapshot v2와 해당 WHOOP package의 SourceRef 정확히 하나
  -> Hermes LLM 종합
  -> DecisionFinalizer 검증과 조건부 저장
```

## 1. 아키텍처가 어떻게 바뀌었나

### #138 이전

```text
경로 A
사용자 -> Hermes + Skill -> 여러 MCP/전용 도구 -> Skill이 판단 저장 요청

경로 B
호출자 -> 고정 question_kind -> 고정 domain resolver -> context만 반환
```

이 구조에서는 질문 해석, 자료 선택, 최종 저장의 주인이 분산됐다. Skill별 전용
도구와 direct Open Wearables 접근을 계속 추가하면 같은 wellness 질문이 서로 다른
검증·저장 규칙을 타게 된다.

### #138 이후

```text
사용자/채널
    |
    v
HealthMesDecisionService
    |
    v
Hermes의 단일 LLM/tool loop
    |
    v
HealthMes MCP의 4개 domain search + 2개 Skill catalog 도구
    |
    v
Context Access Layer -> 결정론적 Domain Provider
    |
    v
ContextResult + source_refs
    |
    v
LLM 종합 -> DecisionFinalizer 검증 -> 필요한 경우만 compact 저장
```

| 경계 | 이전 | #138 |
|---|---|---|
| 자유 형식 판단 입구 | Hermes 경로와 HealthMes resolver가 분리 | `HealthMesDecisionService` 하나 |
| 자료 선택 | Skill별 절차 또는 고정 `question_kind` | 하나의 LLM이 질문별로 자율 선택 |
| 실제 조회 | 혼합 MCP와 전용 도구 | HealthMes MCP의 typed domain search |
| Open Wearables | Hermes에 직접 노출 가능 | Wearable Provider 뒤 bounded reader |
| 판단 저장 | Skill이 범용 writer 호출을 기억해야 함 | `DecisionFinalizer`만 조건부 저장 |
| 출처 검증 | 도구별로 상이 | canonical trace와 `source_refs` 공통 검증 |

## 2. 사용자 요구와 구현 결과

| 요구 | PR #138 구현 |
|---|---|
| 고정 질문 표가 아닌 자연어 기반 판단 | Hermes LLM이 허용된 HealthMes MCP 도구를 자율·반복 선택 |
| Activity·Nutrition·Calendar·Wearable 결합 | 네 domain의 typed search tool과 Provider 구현 |
| HealthMes가 제품 두뇌를 소유 | 단일 `HealthMesDecisionService` ingress와 finalizer 유지 |
| Skill 기반 전문 지침 | 검토된 Skill catalog를 읽기 전용 MCP 도구로 제공 |
| 실제 사용 데이터 추적 | canonical search trace와 `source_refs` 검증 |
| 불필요한 질문 기록 방지 | 단순 조회는 미저장, 행동·위험·명시 추적만 compact 저장 |
| 입력을 한곳에서 설정 | `/v1/inputs` descriptor와 ETag/`If-Match` CAS |
| 데이터별 보존기간 | `1d/7d/14d/30d/90d/forever` data-class policy |
| iPhone Screen Time 수준 activity | eligible build용 aggregate collector/sync/outbox와 activity 저장 연결 |
| UI와 Hermes vendor 격리 | UI 미구현, `vendor/hermes-agent/` 변경 없음 |

## 3. 실제 조회 책임

```text
LLM
  질문의 의미를 해석하고 필요한 자료를 선택
        |
        v
Skill
  필요할 때 읽는 검토된 업무 설명서
        |
        v
MCP search tool
  LLM의 구조화된 조회 요청을 HealthMes로 전달
        |
        v
Context Access Layer
  요청 범위, retention, privacy, timezone와 budget 확인
        |
        v
Provider Registry
  capability를 담당 Domain Provider에 결정론적으로 연결
        |
        v
Domain Provider
  DB/mirror/upstream adapter를 조회하고 정확한 수치 계산
        |
        v
ContextResult + source_refs
        |
        v
LLM
  추가 조회 또는 최종 종합
```

### LLM이 담당하는 것

- 사용자 질문의 목적 해석
- 어느 Skill을 읽을지
- 어느 domain과 capability를 조회할지
- 결과를 본 뒤 다른 자료가 필요한지
- 여러 domain의 trade-off와 최종 설명

### HealthMes 코드가 담당하는 것

- capability와 Provider의 정확한 연결
- 보존기간과 시간대 경계
- domain별 정확한 집계와 단위
- 누락, stale, partial coverage 표현
- 실제 source와 최종 used ref의 일치
- 최종 결과의 조건부 persistence

### 현재 하지 않는 것

- Provider 내부 subagent spawn
- LLM의 직접 SQL/DB/filesystem 접근
- Hermes의 direct Open Wearables MCP 접근
- 각 domain별 별도 최종 판단 agent
- `question_kind -> 고정 domain 목록`을 주 경로로 사용

### 단일 LLM이 충분한 범위

| 항목 | 한 부모 LLM | HealthMes 코드 |
|---|---|---|
| 질문 의도와 필요한 domain 추정 | 담당 | 고정 표로 대신하지 않음 |
| 여러 도구를 어떤 순서로 호출할지 | 담당 | 허용 도구와 호출 예산만 제한 |
| 정확한 DB/API 행 선택 | 직접 담당하지 않음 | Provider가 결정론적으로 담당 |
| retention·timezone·중복·단위 | 신뢰하지 않음 | Access Layer와 Provider가 담당 |
| 사용 출처의 진위 | 최종 ref를 선언 | canonical trace와 finalizer가 검증 |
| 여러 영역의 의미와 최종 설명 | 담당 | strict 결과 계약을 검증 |

따라서 엄밀성은 다음 합성 결과다.

```text
LLM의 조회 계획
  + 결정론적 domain query
  + access/retention/freshness 검사
  + source_refs 재검증
  = 검증 가능한 wellness 판단
```

현재 남은 품질 위험은 **retrieval-plan completeness**다. 즉 임의의 자연어 질문에서
LLM이 관련 domain을 모두 떠올리는지는 확률적이며, 평가 fixture와 tool-call
telemetry로 측정해야 한다. 반면 선택된 도구가 정확한 범위와 출처를 반환했는지는
코드가 검증한다. 측정 결과 실제 누락이 확인되기 전에는 subagent를 필수 구조로
추가하지 않는다.

## 4. Skill과 데이터 도구

Decision profile에 보이는 도구는 정확히 여섯 개다.

```text
데이터:
  search_activity
  search_nutrition
  search_calendar
  search_wearable

지침:
  list_wellness_skills
  read_wellness_skill
```

Skill은 데이터를 담은 별도 저장소가 아니며 도구를 직접 호출하는 실행 코드도 아니다.
LLM은 질문에 전문 절차가 필요하면 Skill 목록에서 관련 Skill을 골라 읽고, 그 내용을
참고해 같은 LLM turn에서 필요한 `search_*` 도구를 선택한다.

```text
"이 커피를 마셔도 될까?"
  -> LLM이 caffeine/nutrition Skill을 읽음
  -> Skill이 필요한 확인 항목을 안내
  -> LLM이 nutrition ledger/candidate 조회
  -> 필요하면 wearable sleep, activity, calendar 추가 조회
  -> LLM이 최종 DecisionDraft 생성
```

간단한 질문은 Skill을 읽지 않고 바로 검색할 수 있다. Skill을 읽었다는 이유만으로
모든 관련 domain을 조회하지도 않는다.

## 5. 여러 입력을 조회할 때

Hermes `/v1/responses` 호출은 한 번이지만 그 내부 transcript에는 여러
`function_call -> function_call_output` 쌍이 들어갈 수 있다.

```text
function_call: search_nutrition
function_call_output: nutrition context

function_call: search_wearable
function_call_output: wearable context

function_call: search_activity
function_call_output: activity context

final message: healthmes.decision-draft.v2
```

도구 호출은 중간 과정이고 최종 출력은 strict `DecisionDraft` 하나다. 현재 search
session은 canonical trace와 policy 일관성을 위해 호출을 직렬화한다. 향후 병렬
retrieval subagent는 #193에서 별도로 검토한다.

## 6. 저장 경계

```text
HealthMes Personal Data Node
|
+-- HealthMes DB
|   +-- Activity WellnessEvent
|   +-- Nutrition/caffeine events와 confirmation
|   +-- CalendarEventMirror
|   +-- normalized wearable snapshots/provenance
|   +-- input settings, retention, cursors, source refs
|   +-- 필요한 경우만 compact DecisionRecord
|
+-- Open Wearables DB
|   +-- 상세 수면, workout, health score, timeseries 원본
|
+-- HEALTHMES_DATA_DIR
|   +-- 사진, 음성, raw ingest와 큰 object
|
+-- Hermes runtime state
    +-- request-scoped transcript와 runtime metadata
```

한 MCP라는 말은 한 DB나 한 테이블이라는 뜻이 아니다. LLM이 보는 제품 조회 입구가
하나라는 뜻이다. Wearable Provider는 보통 HealthMes의 정규화 mirror를 사용하고,
상세 질문에서는 bounded Open Wearables reader를 호출한 뒤 필요한 결과와
provenance만 HealthMes 계약으로 반환한다.

## 7. iPhone Screen Time과 입력 설정

iPhone Screen Time은 별도 wellness domain이 아니라 Activity domain의 collector다.

```text
Apple authorization
  -> 완료된 local-hour aggregate
  -> app identity 가명화와 제외 앱 제거
  -> bounded offline outbox
  -> HealthMes activity ingest
  -> Activity Provider 검색
```

eligible opt-in build의 authorization-triggered first sync, foreground catch-up,
best-effort background refresh와 retry/outbox는 구현되어 있다. 실제 배포에는 Apple
App & Website Usage entitlement 승인, signing/provisioning, 권한 UI와 real-device
dogfood가 필요하다. 이 외부 조건을 완료했다고 주장하지 않는다.

입력 설정은 UI-neutral API로 제공한다.

```text
GET  /v1/inputs
GET  /v1/inputs/{source_id}
PUT  /v1/inputs/{source_id}/settings
```

데스크톱과 모바일의 오래된 설정 덮어쓰기는 ETag/`If-Match` CAS로 방지한다.

## 8. Sake WHOOP 기능의 공통 migration

### Before / After

```text
Before
WHOOP Skill
  -> WHOOP 전용 context
  -> Skill이 별도 persistence 흐름을 지시

After
WHOOP Skill
  -> HealthMes search_wearable
       capability="wearable.whoop-recovery-package"
  -> WearableContextProvider의 결정론적 계산
  -> Open Wearables Recovery + Cycle day_strain
  -> HealthMes snapshot v2와 해당 package의 SourceRef 정확히 하나
  -> Hermes LLM의 다른 domain과 종합
  -> DecisionFinalizer 검증/조건부 persistence
```

WHOOP 전용 제품 MCP, 전용 HealthMes 저장소, 전용 검색 subagent는 추가하지 않았다.
Skill은 조회 지침만 제공하고 저장 도구를 직접 호출하지 않는다. LLM은 capability와
추가 domain을 선택하지만 정확한 WHOOP 행, revision, Cycle, label과 action
package는 Provider 코드가 계산한다.

### 공개 package와 private provenance

| LLM에 공개 | snapshot v2 내부에만 보존 |
|---|---|
| status, local date, timezone, confidence | source/upstream provider |
| Recovery/day strain label과 freshness | raw record ID와 resource type |
| Cycle linkage 상태와 recovery level | metric definition |
| walk 선택지, bounded actions, limitations | Cycle ID와 revision timestamp |
| 해당 WHOOP package의 HealthMes `SourceRef` 정확히 하나 | raw score와 derivation algorithm |

LLM과 DecisionRecord는 Open Wearables 원본 행을 직접 참조하지 않는다. HealthMes
snapshot event 하나가 공개 source boundary다. private provenance는 원본
추적·replay·retention·변조 탐지에 사용하고 일반 prompt나 action metadata에는
노출하지 않는다. 단, canonical SourceRef에는 upstream provider가 아니라 HealthMes
mirror provider와 snapshot event UUID, 관찰 범위, `collected_at`이 포함된다.
복합 판단 전체에는 다른 domain의 SourceRef가 함께 있을 수 있다.

`main`에서 들어온 nullable `decision_record.evidence_refs` 컬럼과 migration은 기존
schema·과거 레코드 호환을 위해 보존한다. 그러나 새 WHOOP 경로는 이 컬럼을 쓰는
별도 writer를 갖지 않는다. 활성 경로의 원본 근거는 snapshot v2의
`private_provenance`, 공개 package의 canonical `SourceRef`, DecisionRecord v7의
검증된 source attestation에만 기록된다.

public package의 top-level key는 Sake 계산 함수가 반환하는 canonical schema와
정확히 같아야 한다. `retention_window`는 snapshot envelope metadata로만 별도
보존한다. `raw_scores`, `score_breakdown`, `provider_payload`처럼 이름만 바꾼
추가 필드도 public package와 LLM payload에 들어갈 수 없다.
WHOOP package는 각 필드가 함께 검증되는 원자적 결과이므로 `fields` projection을
지원하지 않는다. 일부 키만 요청하면 Context Access Layer가 Provider 호출 전에
`query_fields_unsupported`로 거부한다.

`public package.limitations`에는 Sake 계산이 설명하는 signal 한계만 들어간다.
예를 들면 `cycle_id_mismatch`, `not_current_local_day`,
`whoop_recovery_source_truncated`다. Open Wearables 호출 timeout, local mirror
fallback, snapshot writer 실패, retention trim 같은 HealthMes 실행 상태는 package를
변형하지 않고 바깥 `ContextResult.limitations`에만 기록한다. 따라서 같은 Sake
계산 결과는 live 조회와 retained snapshot 조회에서 같은 canonical package를
유지하면서, 해당 턴의 runtime 상태만 별도 envelope로 설명한다.

### Semantic identity와 exact follow-up

```text
WHOOP snapshot payload schema = v2
WHOOP semantic identity schema = v3

identity =
  query_digest
  + semantic_public_digest
  + private_provenance_digest
  + retention_policy_revision
```

동적인 `retention_window`와 `collected_at`은 semantic identity에 포함하지 않는다.
`collected_at`은 freshness, audit와 provenance timestamp 검증에 사용한다.

```text
이전 WHOOP snapshot UUID
  -> REST/Channel DecisionContextHints의 related_record_ids
  -> 턴 전용 rr_<16hex> alias
  -> Hermes search_wearable(package_record_id=alias)
  -> provider 직전에 원래 UUID 복원
  -> exact retained snapshot 또는 UNAVAILABLE
  -> 최신 snapshot으로 대체 금지
```

사용자가 걷기 선택지를 고른 follow-up은 요청 hint의 snapshot UUID, 최종
`SourceRef.record_id`, 실제 실행된 `effective_query.parameters.package_record_id`
세 값이 모두 같아야 저장된다. 날짜만 다시 조회했거나 다른 package ID를 조회한
turn은 같은 공개 결과를 반환했더라도 선택 행동을 저장할 수 없다.

`tests/decision/test_whoop_migration_e2e.py`는 최초 snapshot으로 상세 추천을 받은
뒤 더 최신 snapshot을 추가하고, 첫 응답의 `related_record_ids`를 Channel
follow-up에 전달해 정확히 이전 snapshot의 20분 걷기를 선택한다. 두 turn은 각각
immutable compact v7 record를 만들고, 새 service instance의 receipt replay는
LLM 재호출 없이 canonical compact 답변, actions, source refs와 같은
related-record ID를 복구한다. 공개 REST도 `related_record_ids`를 응답에 노출하고
입력의 `hints.related_record_ids`를 내부 `DecisionContextHints`까지 그대로
전달한다. 따라서 UI는 이전 응답 map을 다음 요청에 이렇게 되돌려 보내면 된다.

```json
{
  "question": "20분으로 할게",
  "hints": {
    "related_record_ids": {
      "whoop_recovery_package": "previous-whoop-snapshot-uuid"
    }
  }
}
```

클라이언트는 `rr_...` alias를 만들거나 저장하지 않는다. 그 값은 HealthMes가
요청마다 생성해 Hermes에만 노출한다. REST 전달 회귀는
`tests/api/test_wellness_decisions.py`, exact package 선택과 저장 E2E는
`tests/decision/test_whoop_migration_e2e.py`가 각각 검증한다.

### Sake 의미 보존 검증 기준

| 검증 항목 | 반드시 유지할 의미 |
|---|---|
| Recovery 경계 | `0..33 red`, `34..66 yellow`, `67..100 green`; `33.5`, `66.5` invalid |
| Day strain 경계 | `[0,10) light`, `[10,14) moderate`, `[14,18) high`, `[18,21] all_out` |
| 최신 Recovery | 요청 local day 우선, 그 안에서 `recorded_at` 최신 |
| 최신 day strain | 요청 local day 우선, 그 안에서 `cycle_updated_at` 최신 |
| 시각 의미 | `recorded_at`은 관찰 시각, `cycle_updated_at`은 Cycle revision 시각이며 둘의 선후관계는 강제하지 않음 |
| 동률 | exact duplicate도 `ambiguous_latest_row` |
| Cycle | Recovery와 day strain의 `cycle_id`가 같아야 usable |
| Metric | Workout `strain`은 Cycle `day_strain`을 대체하지 못함 |
| Pagination | 두 upstream 조회의 truncation을 독립 처리 |
| 조회 창 | 요청일 이틀 전부터 요청일 종료까지 |
| `basic` | 기본 10분, 선택 10/20/30분 |
| `enhanced` | 기본 20분, 선택 10/20/30분 |
| `priority` | 가벼운 10분 걷기 또는 휴식만 |
| 공통 행동 | 물 마시기와 30분 이른 취침 준비 항상 포함 |
| 선택 상태 | `selected`는 선택이며 완료가 아님 |

실제 dogfood migration 검증은 같은 사용자·timezone·요청일의 Sake fixture 또는
실데이터를 이전 계산과 새 capability에 입력해 다음을 비교한다.

1. 선택된 Recovery/day strain 원본 record ID와 Cycle ID
2. 선택된 revision과 freshness
3. Recovery/day strain label과 recovery level
4. 기본 걷기, 허용 선택지, 물과 취침 준비 action
5. insufficient-data와 limitation 사유

공개 결과가 같더라도 private provenance의 원본 record ID, metric definition,
Cycle/revision과 raw score가 누락되면 migration 통과가 아니다.

이번 PR은 위 기준을 synthetic fixture, 경계값, 실패 조건, exact follow-up E2E로
자동 검증했다. 실제 Sake 개인 WHOOP export나 production 계정에는 접근하지
않았으므로 **실데이터 dogfood를 수행했다고 주장하지 않는다.** Sake 재리뷰에서는
코드상 의미 보존과 함께, 사용 가능한 동일 실데이터 fixture가 있다면 이전 경로와
새 capability의 결과를 위 다섯 항목으로 비교해야 한다.

### Finalizer와 idempotency

`DecisionFinalizer`는 action 전체가 package와 같거나, 제공된 걷기 하나만
`selected`로 바뀐 경우만 허용한다. package에 없는 20/30분, 임의 action과 완료
상태는 거부한다. 단순 질문은 저장하지 않고 저장 조건을 만족한 결과만 compact
DecisionRecord로 남긴다.

Sake 리뷰의 idempotency 지적은 `source_provider`를 portable ASCII ID로 강제해
해결한다.

```text
허용 입력: 앞뒤 ASCII space + 1..64자 ASCII provider ID
첫 글자:   A-Z, a-z, 0-9
나머지:    A-Z, a-z, 0-9, ".", "_", "-"
저장 형식: 앞뒤 space 없음 + ASCII lowercase
```

길이 1..64는 앞뒤 ASCII space를 제거한 **본문**에 적용한다. 따라서 공백을
포함한 raw 입력의 총길이가 64보다 길어도 canonical 본문이 64자면 허용한다.
지원되는 write adapter는 이 공통 helper를 idempotency 조회와 길이 검사보다 먼저
실행한다. Unicode case-folding은 DB마다 결과가 달라질 수 있으므로 사용하지
않는다. Unicode, NUL, tab/newline, invalid 기호와 canonical 본문 65자 이상 값은
거부한다. migration은 유효한 legacy ASCII 값만 정규화한다. invalid row 또는
canonical collision이 있으면 원본 행, Alembic revision, index와 constraint를
그대로 둔 채 전체 upgrade가 원자적으로 실패한다. DB check constraint는 비정규
직접 쓰기를 자동 변환하지 않고 거부한다.

`raw_ingest_event.source`와 이를 가리키는
`wellness_event.source_provider`도 같은 Alembic transaction에서 함께
canonicalize한다. SQLite compact UUID와 PostgreSQL dashed UUID를 같은 ID로
비교하며, 연결된 두 provider가 정규화 후 다르면 전체 migration을 실패시킨다.

canonical provider + `source_record_id`가 동일 원본 identity다. 대소문자나 앞뒤
ASCII space만 다른 provider 값이 중복 원본을 만들면 안 된다.

DB 제약과 migration의 문자 검사는 허용 문자를 제거하는 깊은
`replace(replace(...))` 체인을 사용하지 않는다. 그 표현은 SQLite에서 schema 생성
중 parser stack overflow를 일으킬 수 있다. 현재 구현은 각 허용 문자의 출현 수를
얕은 합으로 계산하고 전체 문자열 길이와 비교한다. migration의 정규화는 DB
`lower()`가 아니라 A–Z를 a–z로 바꾸는 명시적 ASCII 치환을 유지한다. 따라서
PostgreSQL의 locale/Unicode 소문자화 차이는 canonical 결과에 들어오지 않는다.
Unicode, NUL, invalid 문자, collision과 raw/wellness provider 불일치의 원자적
실패 의미는 그대로 유지한다.

## 9. Sake 권장 리뷰 순서

1. **단일 제품 진입점**
   - `healthmes/api/wellness_decisions.py`
   - `healthmes/decision/service.py`
   - `healthmes/decision/engine.py`
2. **Hermes 단일 LLM/tool loop**
   - `healthmes/decision/responses.py`
   - `healthmes/decision/hermes_profile.py`
   - `config/hermes-decision-config.yaml.tmpl`
3. **실제 검색 경로**
   - `healthmes/mcp_server/domain_search.py`
   - `healthmes/decision/search.py`
   - `healthmes/decision/access.py`
   - `healthmes/decision/providers.py`
   - `healthmes/decision/domain_providers.py`
   - `healthmes/wearables/search.py`
   - `healthmes/wearables/provenance.py`
   - `healthmes/wearables/whoop_recovery.py`
4. **Skill catalog**
   - `healthmes/mcp_server/wellness_skills.py`
   - `skills/healthmes-wellness-decision/SKILL.md`
   - `skills/healthmes-caffeine/SKILL.md`
   - `skills/healthmes-nutrition-decision/SKILL.md`
   - `skills/healthmes-whoop-recovery/SKILL.md`
5. **결과 검증과 저장**
   - `healthmes/decision/finalizer.py`
   - `healthmes/store/decision_records.py`
   - `healthmes/store/decision_receipts.py`
6. **입력과 저장 경계**
   - `healthmes/api/inputs.py`
   - `healthmes/inputs/`
   - `healthmes/activity/`
   - `healthmes/nutrition/`
   - `healthmes/wearables/`
7. **대표 테스트**
   - `tests/decision/test_e2e.py`
   - `tests/decision/test_responses.py`
   - `tests/decision/test_search_sessions.py`
   - `tests/decision/test_providers.py`
   - `tests/wearables/test_whoop_recovery.py`
   - `tests/wearables/test_provenance.py`
   - `tests/api/test_wellness_decisions.py`
   - `tests/glue/test_single_wellness_runtime_repository.py`

## 10. 리뷰 핵심 불변조건

- 외부 자유 형식 reasoning ingress는 하나여야 한다.
- Hermes가 LLM/tool loop를 실행하지만 HealthMes가 제품 정책과 데이터를 소유해야
  한다.
- decision profile에는 위 여섯 read-only HealthMes MCP tool만 보여야 한다.
- Skill은 지침이고 Provider는 조회 코드다.
- Provider가 최종 wellness 결론을 만들거나 agent를 spawn하면 안 된다.
- WHOOP Skill은 공통 `search_wearable` capability만 사용하고 persistence를 직접
  시작하면 안 된다.
- WHOOP 원본 provenance는 snapshot v2 안에 비공개로 보존하고 LLM에는 public
  package와 해당 WHOOP package의 canonical HealthMes `SourceRef` 하나만 보여야
  한다. 복합 판단의 다른 domain SourceRef는 별개다.
- Sake 계산 limitation은 public package에 보존하되 HealthMes runtime limitation은
  `ContextResult.limitations`에만 있어야 한다.
- WHOOP 행·revision·Cycle·label·action 계산을 LLM 또는 Skill이 재구현하면 안 된다.
- `source_provider`는 1..64자의 canonical portable ASCII identity여야 한다.
- 과거 package follow-up의 `rr_` alias는 턴 전용이어야 하고 exact snapshot이
  없으면 최신 package로 대체하면 안 된다.
- direct Open Wearables MCP, native Hermes tools와 mutation tools가 decision
  profile에 노출되면 안 된다.
- tool transcript와 canonical search trace가 정확히 일치해야 한다.
- 최종 `used_source_ref_ids`는 실제 반환 ref의 부분집합이어야 한다.
- live 상세 answer는 반환되지만 DecisionRecord에 저장되면 안 된다.
- replay는 canonical compact answer와 `decision_response_compacted`를 반환해야
  하며 actions, source refs와 `related_record_ids`는 보존해야 한다.
- 단순 조회는 DecisionRecord를 만들지 않아야 한다.
- UI와 `vendor/hermes-agent/`는 이 PR에서 변경하면 안 된다.

## 11. 검증 결과

### WHOOP 공통 migration 최종 검증

- 분할 전체 Python 회귀:
  - backup 제외 `3955 passed, 99 skipped`
  - backup `419 passed, 1 skipped`
  - 합계 `4374 passed, 100 skipped`
- Decision/WHOOP/API/Skill 집중 suite: `368 passed`
- 상세 live 응답, exact 이전 package 선택, v7 저장과 compact replay E2E:
  `1 passed`
- Ruff, compileall, `git diff --check`: 통과
- Alembic:
  - 단일 head `b7c8d9e0f1a2`
  - PostgreSQL·SQLite offline SQL render 통과
  - 빈 SQLite 실제 `upgrade head`와 `current` 통과
  - provider 제약의 SQLite parser-stack 회귀, 정상화, invalid/collision 원자적
    실패와 metadata parity 집중 검증 통과
- 실제 wheel build 통과. wheel archive의
  `healthmes/_wellness_skills/healthmes-whoop-recovery/SKILL.md`는 정확히
  한 번 포함되며 authoring Skill과 byte identity가 같다.
- 이번 WHOOP migration working tree에서 `vendor/hermes-agent/`,
  `vendor/open-wearables/`, 디바이스 UI를 직접 수정한 파일: 없음
- PR 기준선 `ba2b5ea7` 이후 `vendor/open-wearables/`에는 최신 `main`의
  WHOOP Cycle `day_strain` 구현 8개 파일이 merge commit `3b81e956`을 통해
  승계됐다. 최종 vendor tree는 `origin/main@103b7269`과 동일하며 migration이
  vendor 코드를 재구현하거나 변형하지 않았다.
- 실제 개인 WHOOP dogfood: 미수행. Sake 실데이터 또는 동등한 승인 fixture가
  있어야 별도 검증 가능

### 기존 PR #138 기준선 검증

- macOS lifecycle: `166 passed, 1 skipped`
- 격리 Linux/procps-ng: targeted `2 passed`, full lifecycle `167 passed`
- GitHub Ubuntu/PostgreSQL: `4221 passed, 2 skipped`
- GitHub macOS/SQLite: `4123 passed, 100 skipped`
- iOS/watchOS: `141 tests, 2 skipped, 0 failures`
- macOS native: `26 tests, 0 failures`
- Android Gradle build/tests: 성공
- Compose, Ruff, `bash -n`, `git diff --check`, Alembic render: 통과
- 전체 기능 diff 독립 GPT-5.6 Sol xhigh 리뷰:
  `High 0 / Medium 0 / Low 0 / PASS`
- 이번 아키텍처·main 충돌 문서 갱신:
  canonical docs/glue targeted `23 passed`, `git diff --check` 통과

## 12. 후속 작업

- 검색 전용 bounded subagent: #193
- GPS/location input: #158
- Apple entitlement, signing과 실제 iPhone dogfood
- 디바이스 UI
- hosted mobile-only Personal Data Node
- 실시간 multi-master 기기 동기화

검색 subagent는 PR #138의 완료 조건이 아니다. 현재 단일 LLM 반복 조회가 기준선이며,
#193은 복잡한 장기·다중 domain 검색에서 측정 가능한 이득이 있을 때만 선택적으로
도입한다.
