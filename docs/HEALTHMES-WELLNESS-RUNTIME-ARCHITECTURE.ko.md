# HealthMes 통합 Wellness Runtime 아키텍처

> **결정일:** 2026-08-16
>
> **상태:** PR #138의 canonical target. 이 문서가 Decision runtime, MCP,
> 저장 경계와 iPhone Screen Time 수집 계약의 최상위 기준이다.
>
> **구현 추적:** PR #138과 그 하위 이슈. 문서에 `목표`라고 표시된 항목은
> 해당 이슈가 닫히고 테스트가 통과하기 전까지 완료로 간주하지 않는다.

## TLDR

아래 그림은 **PR #138이 완료됐을 때의 목표 구조**다. 현재 production
composition은 아직 `/v1/model/iterations`를 사용하므로 이 문서만 병합해서 완료로
간주하지 않는다.

HealthMes에는 자유 형식 wellness 질문을 해석하고 판단하는 **reasoning 경로가
하나만** 있어야 한다.

```text
App / Web / Channel adapter / Proactive trigger
  |
  | public REST or internal DecisionRequest ingress
  v
HealthMes Wellness Decision Service
  |
  | Hermes /v1/responses
  v
Hermes autonomous LLM + tool loop
  |
  | only one product MCP server: healthmes
  | decision-read tools only
  v
HealthMes MCP
  |
  +-- Activity tools
  +-- Nutrition / caffeine / VLM tools
  +-- Calendar tools
  +-- Wearable tools
  |     |
  |     +-- bounded REST calls to Open Wearables
  |
  +-- source_refs and compact decision persistence
```

기존처럼 Hermes가 직접 처리하는 경로와 HealthMes가 별도 LLM loop를 돌리는
경로를 동시에 운영하지 않는다. `POST /v1/model/iterations`는 현재 Hermes에
존재하지 않으며, 그 endpoint를 전제로 한 split-runtime 설계는 폐기한다.

식사 기록, 설정 변경, 캘린더 제안 확인처럼 사용자가 이미 의도를 명시한 작업은
별도의 bounded command workflow로 처리할 수 있다. 이 workflow는 자유 형식
wellness 판단을 수행하지 않으며 decision-read profile을 우회해 자료를 탐색하지
않는다.

## 0. 현재 상태와 PR #138 종료 상태

| 영역 | 2026-08-16 현재 코드 | PR #138 종료 상태 |
|---|---|---|
| 제품 API | `POST /v1/wellness-decisions` 존재 | 그대로 공식 API 하나만 유지 |
| LLM 실행 | HealthMes loop가 존재하지 않는 Hermes `/v1/model/iterations`를 요구 | Hermes `/v1/responses`가 전체 LLM·tool loop를 한 번 실행 |
| Hermes tool surface | `healthmes`, `open_wearables` MCP와 API server 기본 도구가 함께 노출될 수 있음 | API server에는 filtered `healthmes` MCP의 decision-read 도구만 허용 |
| 최종 결과 | HealthMes 내부 iteration 계약이 `DecisionDraft`를 직접 요구 | Hermes 최종 텍스트를 엄격한 JSON envelope로 검증해 `DecisionDraft`로 변환 |
| domain consent | 기존 HealthMes Context Access Layer에서 검사 | HealthMes MCP의 decision context 도구도 동일한 현재 owner policy를 사용 |
| 채널 입력 | Telegram이 Hermes `run_conversation()`을 직접 실행 | 모든 자유 형식 wellness 질문은 HealthMes ingress를 사용하고 Hermes 직접 채널 판단은 비활성 |
| 명시적 command | 캡처·확인과 자유 형식 판단의 경계가 혼재 | 식사 기록·설정 변경·확인 응답은 별도 bounded command workflow이며 wellness reasoning을 수행하지 않음 |
| iPhone lifecycle | report/transport/sync seam은 있으나 앱 lifecycle 자동 실행은 없음 | 권한 승인 직후, foreground catch-up, background 기회, outbox 재전송 연결 |

따라서 이 표의 오른쪽 열과 E2E 테스트가 모두 완료되기 전에는 README나 PR
설명에서 단일 runtime 또는 iPhone 자동 수집을 구현 완료라고 표현하지 않는다.

## 1. capability boundary

### HealthMes가 소유한다

```text
제품 API
REST·channel·proactive 요청을 같은 DecisionRequest로 바꾸는 ingress
데이터 수집·정규화·저장·보존
Activity / Nutrition / Calendar / Wearable 도구
Open Wearables 접근 adapter
VLM 분석 adapter
source_refs 계약
compact DecisionRecord 정책
입력 설정 API
명시적 capture / confirmation command 계약
```

HealthMes는 질문별 고정 조회 표나 별도의 두 번째 LLM loop를 소유하지 않는다.

### Hermes가 소유한다

```text
LLM 호출
자연어 질문 해석
필요한 MCP 도구의 자율 선택
도구 결과를 본 뒤 추가 도구 호출
여러 domain의 종합 판단
최종 자연어 답변
세션·채널 전달
```

Hermes는 HealthMes보다 상위 제품이 아니다. HealthMes가 선택한 첫 runtime이며
나중에 다른 agent runtime으로 교체할 수 있는 실행 부품이다.

### Skill이 소유한다

Skill은 코드 엔진이 아니라 Hermes가 읽는 제품 사용 설명이다.

```text
질문의 wellness 목적을 해석하는 방식
HealthMes MCP 도구를 찾는 방법
불확실성·한계·추가 질문을 표현하는 방식
기록 질문과 섭취 전 질문을 구분하는 대화 규칙
```

Skill은 DB를 직접 읽지 않고, Open Wearables MCP를 직접 호출하지 않으며,
보존기간·권한·정확한 합계 계산을 대신 구현하지 않는다.

API server decision turn에는 Hermes의 쓰기 가능한 `skill_manage`를 노출하지
않는다. HealthMes가 검토된 wellness Skill을 읽기 전용 catalog로 제공하거나,
선택한 공통 Skill 내용을 request instructions에 포함한다. 따라서 도메인
전문가의 Skill은 계속 판단 지침으로 쓰이지만 runtime이 임의로 Skill 파일을
수정하지 못한다.

## 2. 왜 경로는 하나여야 하는가

기존 두 경로는 같은 질문에 서로 다른 도구, prompt, 기록 규칙과 답변을 사용할 수
있다.

```text
폐기할 구조

사용자 질문
   |
   +-- A. Hermes /v1/responses
   |      Hermes가 LLM과 MCP loop 실행
   |
   +-- B. /v1/wellness-decisions
          HealthMes가 별도 LLM loop 실행
          -> 존재하지 않는 /v1/model/iterations 필요
```

이 구조에는 네 문제가 있다.

1. B는 현재 실행할 수 없다.
2. A와 B가 서로 다른 판단 결과를 낼 수 있다.
3. 도구와 Skill을 두 경로에 중복 등록해야 한다.
4. 어떤 경로가 제품의 정식 기록·관찰 대상인지 모호하다.

채택 구조는 다음과 같다.

```text
채택 구조

모든 wellness reasoning UI·API client·channel adapter·proactive trigger
       |
       v
HealthMes DecisionRequest ingress
       |
       +-- public: POST /v1/wellness-decisions
       +-- internal: the same decision service contract
       |
       | HealthMes request envelope와 product metadata
       v
Hermes /v1/responses
       |
       | 한 번의 autonomous agent turn
       | 질문 해석 -> MCP 선택 -> 반복 조회 -> 최종 답변
       v
HealthMes response adapter
       |
       +-- 사용된 source_refs 검증
       +-- 필요할 때만 compact DecisionRecord 저장
       +-- 공통 DecisionResult 반환
```

Hermes `/v1/responses`를 클라이언트나 Telegram이 직접 호출하는 것은
HealthMes 제품의 wellness 경로가 아니다. 현재 Hermes Telegram platform은
별도의 `run_conversation()`을 실행하므로, PR #138의 canonical 배포에서는
wellness 질문용 inbound를 비활성화하고 outbound delivery에만 사용한다. 미래
Telegram·앱·웹 adapter는 UI를 소유해도 판단은 위 HealthMes ingress를 호출한다.
이 규칙을 지키지 않으면 source 검증과 compact 기록 정책을 건너뛰는 두 번째
판단 경로가 다시 생긴다.

반대로 다음 command는 별도의 작은 경로를 유지할 수 있다.

```text
식사 섭취 확정 -> Nutrition ingest command
설정 저장      -> Input settings command
적용 <handle>  -> Calendar confirmation command
```

이 command들은 사용자가 명시한 쓰기를 검증하고 실행할 뿐 질문 의미를 해석하거나
여러 domain을 자율 검색하지 않는다. command 처리 중 새로운 wellness 판단이
필요해지면 내부 `DecisionRequest`를 호출하고 그 결과를 받은 뒤 별도 확인을
요청해야 한다.

### 자유 형식 Hermes 응답을 구조화하는 계약

Hermes `/v1/responses` 자체는 JSON Schema 기반의 HealthMes 전용 최종 객체를
보장하지 않는다. `output`에는 tool call, tool output과 마지막 자유 형식 assistant
text가 들어간다. 따라서 HealthMes adapter가 기존
`healthmes.decision.contracts.DecisionDraft`와 정확히 연결된 strict envelope를
추가한다.

```text
HealthMes instructions
  -> 마지막 assistant text는 healthmes.decision-draft.v1 JSON 하나만 반환
  -> Hermes는 그 전에 필요한 HealthMes MCP 도구를 자율 반복 호출
  -> adapter가 전체 function_call / function_call_output 쌍을 검증
  -> envelope 안의 draft를 기존 DecisionDraft Pydantic model로 strict parse
  -> used_source_ref_ids가 실제 HealthMes tool output의 부분집합인지 검증
```

```json
{
  "object": "healthmes.decision-draft.v1",
  "draft": {
    "status": "completed",
    "answer": "...",
    "proposed_action": false,
    "used_source_ref_ids": ["sr_0123456789abcdef0123456789abcdef"],
    "limitations": [],
    "clarification_question": null,
    "confidence": 0.8,
    "uncertainty": null,
    "follow_up_question": null
  }
}
```

`DecisionDraft`의 enum, 길이 제한, source ID 정규식과 status별 invariant가 그대로
적용된다. 예를 들어 `completed`는 answer가 필요하고, `proposed_action=true`는
실제 source ref가 필요하며, clarification은 행동을 제안할 수 없다. 코드 fence,
JSON 앞뒤 설명, 알 수 없는 필드, 잘못된 source ref 또는 허용되지 않은 도구 호출이
있으면 정상 답변으로 승격하지 않는다. 이 계약은 Skill 문서가 아니라 HealthMes
parser와 테스트가 강제한다.

## 3. HealthMes MCP 하나의 의미

MCP 서버가 하나라는 말은 데이터가 한 테이블에 섞인다는 뜻이 아니다. 에이전트가
찾아야 할 **제품 도구 카탈로그의 입구가 하나**라는 뜻이다.

```text
Hermes
  |
  | mcp__healthmes__*
  v
HealthMes MCP server
  |
  +-- search_activity(...)
  +-- get_activity_summary(...)
  +-- search_nutrition(...)
  +-- analyze_intake_capture(...)
  +-- get_caffeine_context(...)
  +-- search_calendar(...)
  +-- get_schedule(...)
  +-- search_wearable(...)
  +-- get_sleep/readiness/stress context(...)
  +-- record or retrieve a compact decision(...)
```

LLM은 고정 `question_kind` 표를 따르지 않는다. 도구 설명과 반환 결과를 보고 필요한
domain, 기간과 상세도를 자율적으로 선택한다. 각 도구는 범위, 결과 수와 payload
크기가 제한돼야 하며 cursor 또는 명시적 time range를 사용한다.

### Decision context 도구와 consent

기존 범용 MCP 도구를 그대로 호출하는 것만으로는 Decision Agent의 domain consent를
보장할 수 없다. 따라서 source-bearing 조회는 HealthMes MCP 안의 전용 bounded
context 도구가 담당한다. PR #138은 **한 사용자가 소유하는 self-hosted Personal
Data Node**가 MVP 경계다.

```text
Hermes가 domain context 도구 호출
  -> HealthMes가 서버 설정의 단일 owner identity 사용
  -> DB의 최신 per-domain consent와 execution scope 조회
  -> Context Access Layer가 retention, privacy, timezone, range, row limit 검사
  -> ContextResult + SourceRef + access audit 반환
```

Hermes의 정적 bearer token은 end-user identity가 아니라 HealthMes가 허용한
service-to-service credential이다. Hermes나 Skill은 owner ID, consent 결과 또는
보존기간을 인자로 정하지 못한다. 사용자가 설정을 끄면 다음 도구 호출부터 현재 DB
policy가 적용된다. 일반 HealthMes MCP 도구가 존재하더라도 검증된 decision
source로 채택되는 것은 이 표준 context envelope를 반환한 도구뿐이다.

hosted multi-user 제품에서는 이 단일-owner 계약을 재사용하면 안 된다. 그때는
HealthMes가 서명한 request-scoped principal envelope 또는 사용자별 MCP session이
별도 설계돼야 한다.

### Hermes API server tool surface

`/v1/responses`는 Hermes의 `platform_toolsets.api_server` 설정을 사용한다.
server 이름만 허용하면 그 server의 mutation tool까지 노출되므로 두 단계 필터를
모두 사용한다. 또한 `platform_toolsets.api_server: [healthmes]` 하나만으로
native tool이 절대 노출되지 않는다고 가정하지 않는다. credential과 설정 상태에
따른 toolset 복구를 막기 위해 bootstrap은 API server용 deny-by-default profile을
함께 생성한다.

```yaml
platform_toolsets:
  api_server:
    - healthmes

agent:
  # 실제 목록은 bootstrap이 Hermes native toolset catalog에서 생성한다.
  # healthmes MCP는 native toolset이 아니므로 이 목록에 넣지 않는다.
  disabled_toolsets:
    - web
    - search
    - x_search
    - terminal
    - file
    - browser
    - delegation
    - memory
    - skills

mcp_servers:
  healthmes:
    tools:
      include:
        - search_activity
        - search_nutrition
        - search_calendar
        - search_wearable
        - list_wellness_skills
        - read_wellness_skill
```

실제 include 목록은 코드의 decision-read profile이 정본이며 위 목록은 최소
형태다. `healthmes`는 유일한 제품 MCP server 이름이다. Hermes 내장
`skills_list`, `skill_view`, `skill_manage`, HealthMes mutation tools, terminal,
file write, browser, delegation과 direct `open_wearables` MCP는 wellness decision
turn에 노출하지 않는다. HealthMes response adapter도 설정만 신뢰하지 않고 실제
transcript의 tool name allowlist를 다시 검사한다. 사후 검증은 이미 실행된
mutation을 되돌릴 수 없으므로 **등록 전 include filter가 1차 경계**다.

배포 시작 검사는 다음 두 검증을 모두 통과해야 한다.

1. 렌더된 Hermes config에는 `healthmes` MCP 하나와 정확한
   `mcp_servers.healthmes.tools.include` 목록만 존재한다.
2. 인증된 `GET /v1/toolsets` 결과에서 API server용 native toolset이 하나라도
   `enabled=true`면 HealthMes decision runtime은 fail closed한다.

`GET /v1/toolsets`는 native toolset 검사용이며 MCP 도구 allowlist의 정본은
렌더된 config와 HealthMes의 decision-read profile이다. transcript 검사는
잘못된 배포를 탐지하는 2차 방어이지 실행 전 경계를 대신하지 않는다.

### Open Wearables

Open Wearables의 상세 DB는 별도 물리 저장소로 유지한다. 그러나 Hermes에 별도
`open_wearables` MCP 서버를 노출하지 않는다.

```text
Hermes
  |
  v
HealthMes MCP wearable tools
  |
  v
OWClient
  |
  v
Open Wearables REST API / DB
```

이 경계의 목적은 Open Wearables를 숨기는 것이 아니라 다음을 한 곳에서 보장하는
것이다.

- 동일 사용자와 시간대 선택
- bounded range와 결과 크기
- retention된 local mirror 우선 사용
- 안정적인 `source_refs`
- HealthMes 도구 naming과 response shape
- Activity·Nutrition·Calendar 결과와 동일한 방식의 조합

## 4. 저장 아키텍처

Personal Data Node가 하나의 논리적 정본이다. 물리 저장소는 데이터 성격에 따라
분리한다.

```text
Personal Data Node
|
+-- postgres service / postgres_data volume
|   |
|   +-- healthmes database
|   |   +-- WellnessEvent: Activity
|   |   +-- WellnessEvent: Nutrition / caffeine
|   |   +-- CalendarEventMirror
|   |   +-- normalized wearable snapshots
|   |   +-- settings / retention / indexes / cursors
|   |   +-- optional compact DecisionRecord
|   |
|   +-- open-wearables database
|       +-- detailed sleep
|       +-- workouts
|       +-- provider health scores
|       +-- high-frequency wearable timeseries
|
+-- healthmes_data volume / HEALTHMES_DATA_DIR
|   |
|   +-- photos
|   +-- audio
|   +-- large raw payloads
|   +-- compressed high-frequency chunks
|
+-- ./data/hermes bind mount
    +-- local runtime state
```

Activity, Nutrition과 Calendar마다 별도 물리 DB를 만들지 않는다. HealthMes DB
안에서 event type, table, index와 retention class로 논리 분리한다. Open
Wearables는 같은 Postgres service와 `postgres_data` volume을 사용하지만 별도
database와 schema owner를 가진다. `HEALTHMES_DATA_DIR`의 large object volume과
Hermes의 local runtime bind mount도 서로 다르다. 이 물리 경계들을 backup
manifest와 Personal Data Node 운영 계약이 논리적으로 한 묶음으로 관리한다.

현재 `healthmes backup` snapshot은 Personal Data Node 전체를 자동 복구하는
완전 백업이 아니다. HealthMes DB, `media/`, `raw_ingest/`, 선택적 Hermes home,
그리고 `HEALTHMES_OW_DATABASE_URL`이 설정된 경우의 Open Wearables DB dump만
포함하는 **부분 스냅샷**이다. `.env`, 외부 OAuth credential, 별도 credential
store와 설정되지 않은 Open Wearables DB는 포함되지 않는다. PR #138은 backup
manifest와 compose 설정이 이 한계를 명시하고 restore drill이 포함된 구성요소만
복구한다고 검증한다. 전체 Personal Data Node 재해복구는 별도 범위다.

### 중앙 검색

“중앙화”는 하나의 SQL 테이블을 LLM에 직접 열어주는 것이 아니다.

```text
LLM이 질문을 해석
  -> HealthMes MCP의 domain search tool 선택
  -> domain adapter가 자기 저장소를 bounded query
  -> 정규화된 result + source_refs 반환
  -> LLM이 필요하면 다른 domain을 추가 조회
```

따라서 “커피를 마셔도 될까?”에는 nutrition/caffeine, 현재 시각, 수면,
activity와 calendar를 필요에 따라 조합할 수 있고, “어떤 앱이 집중을
방해했나?”에는 activity의 identity-level 조회만 선택할 수 있다.

## 5. source_refs와 DecisionRecord

`source_refs`는 의료적 증명이 아니라 답변에 사용한 데이터의 추적 주소다.

```json
{
  "domain": "activity",
  "source_id": "wellness-event-uuid",
  "observed_at": "2026-08-16T09:00:00+09:00",
  "derived_by": "activity-hour-summary.v1"
}
```

HealthMes는 Hermes가 최종 답변에 표시한 reference가 실제 MCP 결과에 있었는지
검증한다. 모든 원본 payload를 복제하거나 답변 전문을 무기한 저장할 필요는 없다.

PR #138 목표 DecisionRecord 원칙:

| 질문/행동 | 기본 저장 |
|---|---|
| 단순 정보 조회 | 저장하지 않거나 짧은 운영 trace |
| 사용자가 식사·활동을 기록 | 해당 domain event 저장 |
| 사용자 행동을 바꾸는 제안 | compact record 저장 |
| 캘린더·설정 등 실제 mutation | 해당 command workflow의 audit가 소유; wellness runtime은 저장 사유로 인정하지 않음 |
| 행동 가능한 중요 위험 경고 | compact record 저장 |
| UI/API가 `persistence_requested=true`로 보낸 명시적 추적 요청 | compact record 저장 |

compact record에는 request ID, 시각, 모델/runtime, 짧은 결론, 사용한
`source_refs`, 제안된 행동과 outcome 연결 ID만 둔다. 사진 bytes, 전체 MCP
payload와 전체 prompt를 기본 저장하지 않는다.

2026-08-16 구현된 `healthmes.decision-private.v2`는 다음만 저장한다.

- request/turn ID, 요청 시각, timezone, execution/privacy scope
- 모델/runtime와 token 계측
- 최종 답변, confidence, limitation과 실제 사용한 `source_refs`
- 실제 사용한 source만 재검증하는 데 필요한 bounded typed query attestation
- 해당 query의 access 결과
- `none/action/risk/mutation/explicit_tracking` persistence intent

질문 원문, caller principal, query의 model-authored `purpose`/자유 텍스트 검색어,
전체 tool payload, 사진·음성 bytes, 전체 transcript, 사용하지 않은 source와 tool
trace는 저장하지 않는다. `none`인 단순 조회는 source를 사용했더라도
DecisionRecord를 만들지 않는다.

LLM이 반환한 persistence intent는 신뢰 입력이 아니다. HealthMes가 다음처럼 최종
effective intent를 계산한다.

| 조건 | effective intent |
|---|---|
| 완료된 구체적 행동 제안 | `action` |
| 완료된 행동 가능한 중요 위험 경고 | `risk` |
| 행동 제안은 없고 trusted request의 `persistence_requested=true` | `explicit_tracking` |
| LLM이 `mutation`/`explicit_tracking`을 주장했지만 위 조건이 없음 | `none` |
| 단순 조회·요약 | `none` |

따라서 read-only wellness runtime은 mutation audit를 만들지 않는다. 실제 mutation은
별도 command workflow가 자신의 audit를 소유한다. 과거
`healthmes.decision-private.v1` 레코드는 고정 historical fixture와 기존
fingerprint를 기준으로 읽기 호환을 유지한다.

검토된 Skill은 wheel의 `healthmes/_wellness_skills` package resource에 포함하고,
source/Docker 실행에서는 repository `skills/`를 fallback으로 사용한다. catalog는
mutation 중심 `healthmes-nutrition` 대신 read-only
`healthmes-nutrition-decision`을 노출한다.

Hermes `/v1/responses` 호출은 `store=false`이며 `previous_response_id`,
`conversation`과 장기 memory tool을 사용하지 않는다. 다만 현재 Hermes
`AIAgent`는 `store=false`와 별개로 request-scoped transcript를 로컬
`state.db`에 쓴다. 성공 응답은 `X-Hermes-Session-Id`를 반환하므로 Adapter가
turn 종료 후 session 삭제를 요청하고, 실패한 cleanup은 bounded retry 대상으로
남긴다.

현재 Hermes `/v1/responses`의 500 실패 응답에는 session ID가 없다. 따라서
HealthMes가 실패 turn을 즉시 정확히 지운다고 보장할 수 없다. PR #138은 Hermes
runtime state를 전용 경로에 격리하고 짧은 TTL purge를 적용하며, 성공 session은
즉시 삭제한다. 실패 session까지 즉시 삭제하려면 Hermes가 실패 응답에도
session ID를 반환하거나 caller 지정 session ID를 지원하는 별도 upstream 계약이
필요하다. 이 transient state는 DecisionRecord가 아니며 cloud나 무기한 정본으로
취급하지 않는다. Hermes upstream이 truly ephemeral session을 제공하기 전까지
“모든 실패 session이 즉시 삭제된다”거나 “disk에 한 번도 쓰지 않는다”고
주장하지 않는다.

## 6. iPhone Screen Time 수집

iPhone Screen Time은 `activity monitoring` 입력이다. 성공한 집계는 Android와
ActivityWatch처럼 HealthMes DB의 `activity.*` WellnessEvent 파티션으로 들어간다.

2026-08-16 현재는 서버 report ingest와 iOS collector/sync seam까지 존재하지만,
실제 앱 lifecycle에서 자동 실행하지 않는다. 아래 흐름은 #168의 구현 종료 상태다.

```text
iPhone
  |
  +-- 사용자가 Apple 권한 승인
  +-- 완료된 local hour별 사용시간 집계
  +-- app identity는 기기 안에서 가명화
  +-- 제외 앱은 업로드 전에 제거
  +-- 암호화된 local outbox
  |
  v
POST /v1/activity/ios/report
  |
  v
HealthMes activity WellnessEvent + hourly/daily aggregate
```

소스코드가 완성할 수 있는 범위:

- 지원 OS/SDK에서 capability 감지
- 권한 요청 adapter
- 완료된 시간 버킷 수집
- 앱 ID 가명화와 source-side exclusion
- 첫 권한 승인 직후 sync
- app foreground 진입 시 catch-up
- OS가 시간을 줄 때 best-effort background sync
- 네트워크 실패 outbox와 재전송
- denied, restricted, unavailable 상태의 명시적 보고
- 서버 snapshot fence와 retention 적용

소스코드만으로 완료할 수 없는 외부 조건:

- Apple이 통제하는 entitlement/capability 승인
- 실제 Team ID와 distribution signing
- Apple이 허용한 지역·계정·OS 조건
- 실제 iPhone에서의 최종 dogfood

따라서 “자동 수집”의 정확한 의미는 **사용자가 권한을 승인하고 지원 조건이
충족되면 즉시 첫 sync를 시도하고, 이후 background 기회와 foreground 진입 때
자동 catch-up한다**는 뜻이다. iOS는 임의의 24시간 상시 daemon을 보장하지 않는다.

디바이스 팀은 권한 안내 화면과 설정 UI를 소유한다. #168은 UI를 만들지 않지만,
그 UI가 permission adapter를 호출한 직후 첫 sync가 실행되고 앱 lifecycle event가
collector coordinator로 전달되는 코드 연결은 소유한다.

## 7. 입력 설정과 보존

모든 디바이스 UI는 같은 계약을 사용한다.

```text
GET  /v1/inputs
GET  /v1/inputs/{source_id}
PUT  /v1/inputs/{source_id}/settings
```

설정 범위:

- input/instance 활성화와 일시정지
- 앱 또는 활동 제외
- Decision Agent 조회 허용
- 데이터 클래스별 `1d/7d/14d/30d/90d/forever`
- 연결, 권한 요청과 sync action descriptor

GET descriptor의 `revision`은 PUT에서 compare-and-swap으로 검증한다. 데스크톱과
휴대전화가 같은 설정을 동시에 바꾸면 오래된 revision을 가진 요청은 `409`로
거부하고 최신 descriptor를 다시 읽게 한다. UI는 별도 설정 목록을 하드코딩하지
않고 API가 반환한 capability, action, limitation과 setting definition을 렌더링한다.

## 8. 구현 순서와 완료 조건

```text
1. canonical docs와 deprecated 계약 정리
2. HealthMes MCP에 bounded Activity/Nutrition/Calendar/Wearable search 추가
3. /wellness-decisions -> Hermes /v1/responses 단일 adapter
4. direct channel 판단과 direct open_wearables MCP 제거
5. read-only wellness Skill catalog와 decision tool profile 추가
6. source_refs 검증 + 조건부 compact DecisionRecord + 성공 Hermes session cleanup
   + 실패 session 전용 TTL purge
7. iPhone Screen Time lifecycle/background/outbox 연결
8. input settings revision CAS
9. cross-domain E2E + app build + regression + independent review
```

최종 완료 조건:

- 자연어 질문이 공식 API 하나로 들어간다.
- 자유 형식 channel 질문과 proactive reasoning도 같은 internal
  DecisionRequest ingress를 사용한다.
- capture와 명시적 confirmation은 bounded command로만 동작하며 reasoning
  ingress를 우회해 자율 조회하지 않는다.
- Hermes가 고정 질문 표 없이 HealthMes MCP 도구를 자율 선택한다.
- Hermes 설정에는 제품용 MCP가 `healthmes` 하나만 있다.
- Hermes API server에는 decision-read include profile 밖의 도구가 없다.
- runtime 시작 시 native toolset 또는 MCP profile drift를 발견하면 fail closed한다.
- wearable 상세 조회도 HealthMes MCP를 통해 bounded하게 가능하다.
- Activity, Nutrition, Calendar와 Wearable을 한 답변에서 조합할 수 있다.
- 답변의 `source_refs`가 실제 도구 결과와 일치한다.
- 저장 대상 질문만 compact DecisionRecord로 남는다.
- 성공 Hermes session은 즉시 정리되고 실패 session은 전용 state TTL로
  제한된다. 실패 session 즉시 삭제는 upstream 계약 없이는 완료라고 주장하지
  않는다.
- 지원되는 iPhone에서 권한 승인 후 자동 첫 sync/catch-up seam이 연결된다.
- 입력 설정은 stale revision 덮어쓰기를 거부한다.
- UI와 `vendor/hermes-agent/`를 수정하지 않는다.
- Python, Android와 Apple build/test가 모두 통과한다.

## 9. 폐기된 설계

다음 문서는 역사와 기존 코드 이해용이며 새 구현의 기준이 아니다.

- `HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md`의 HealthMes-owned LLM loop
- `contracts/HERMES-MODEL-ITERATION-HOOK.ko.md`의
  `POST /v1/model/iterations`
- Hermes에 `healthmes`와 `open_wearables` MCP를 동시에 제품 노출하는 구조
- `question_kind -> fixed domains`를 주 자연어 판단 경로로 사용하는 구조
