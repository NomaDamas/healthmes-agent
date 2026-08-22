# HealthMes Decision Agent 컴포넌트 아키텍처

> **결정일:** 2026-08-16
>
> **상태:** 현재 단일-runtime 구현의 내부 컴포넌트 기준.
>
> 제품 전체 경계와 저장 구조는
> [`HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md)
> 를 먼저 읽는다.
>
> PR #138의 구현 범위, 코드 검토 순서와 검증 증거는
> [`PR-138-REVIEW-GUIDE.ko.md`](PR-138-REVIEW-GUIDE.ko.md)를 따른다.

## TLDR

“HealthMes Decision Agent”는 한 Python 클래스 이름이 아니라 다음 컴포넌트의
합성으로 제공되는 제품 기능이다.

```text
HealthMesDecisionService
          |
          v
HealthMesDecisionEngine
          |
          +-- HermesResponsesDecisionAgent
          |       |
          |       +-- Hermes /v1/responses
          |       +-- HealthMes MCP tool loop
          |
          +-- DecisionFinalizer
                  |
                  +-- source revalidation
                  +-- conditional persistence
```

HealthMes와 Hermes가 각각 LLM loop를 하나씩 가지지 않는다. Hermes가 유일한
autonomous LLM/tool loop를 실행하고 HealthMes가 그 loop의 제품 입출력과 데이터
경계를 소유한다.

외부 제품 ingress는 `POST /v1/wellness-decisions` 하나다. 내부
`POST /v1/responses`는 `HermesResponsesDecisionAgent`가 사용하는 구현 계약이며
두 번째 사용자 경로가 아니다.

## 1. 컴포넌트 책임

| 컴포넌트 | 추상화 수준 | 책임 |
|---|---|---|
| `HealthMesDecisionService` | 제품 ingress | REST/channel/proactive/scheduled 요청을 server-owned `DecisionRequest`로 변환 |
| `HealthMesDecisionEngine` | 수명주기 | admission, agent 실행, finalization, shutdown과 결과 publication |
| `HermesResponsesDecisionAgent` | runtime adapter | Hermes 한 turn 호출, transcript·envelope·tool/source 검증, session cleanup |
| `DecisionContextSearchSessionService` | tool session | tool budget, canonical trace, begin/finish/abort와 source 집합 |
| `ContextAccessLayer` | 데이터 접근 경계 | consent, retention, timezone, privacy, row/byte/call limit |
| Domain provider | 전문 계산/조회 | Activity, Nutrition, Calendar, Wearable 결과와 provenance |
| `DecisionFinalizer` | 결과 확정 | source 재검증, effective persistence intent, compact record 또는 미저장 |

외부 채널이 붙을 때는 `DecisionChannelAdapter`가 `source`, `session_id`, privacy,
budget과 hints를 그대로 `HealthMesDecisionService`에 한 번 전달한다. 현재 실제
Telegram/UI inbound는 없고 adapter contract만 있다. 채널 구현이 Hermes를 직접
호출하거나 별도 agent loop를 추가하면 단일 runtime 보장을 깨뜨린다.

## 2. 한 요청의 실제 순서

```text
1. 사용자 질문
2. HealthMesDecisionService가 owner/timezone/scope를 채움
3. HealthMesDecisionEngine이 요청 admission
4. HermesResponsesDecisionAgent가 search session 시작
5. Hermes /v1/responses 호출
6. Hermes가 HealthMes MCP 도구를 자율·반복 호출
7. search session이 canonical tool trace와 source_refs 보관
8. Hermes가 healthmes.decision-draft.v2 JSON 반환
9. adapter가 tool call/output, allowlist와 used refs 검증
10. DecisionFinalizer가 source와 현재 정책을 다시 확인
11. 필요하면 compact DecisionRecord 저장
12. 공통 DecisionResult 반환
```

Hermes 호출은 “한 번”이지만 그 한 요청 안에서는 여러 LLM/tool iteration이
일어난다. HealthMes가 매 iteration마다 Hermes를 다시 호출하는 구조가 아니다.

## 3. LLM이 결정하는 것과 코드가 결정하는 것

### LLM

```text
질문 의도
먼저 볼 domain
추가 조회가 필요한지
여러 domain 사이의 의미
사용자에게 설명할 결론
자료가 부족할 때 물어볼 질문
```

### HealthMes 코드

```text
허용된 6개 tool profile
현재 owner와 domain consent
retention cutoff
timezone과 query budget
정확한 domain 집계
source_refs 정합성
저장할지 여부와 compact payload
```

결정론적 계층이 있다는 것이 질문 종류를 고정한다는 뜻은 아니다. LLM은 자유롭게
도구를 고르고, 코드는 선택된 도구가 실제 데이터 규칙을 지키도록 한다.

## 4. Tool과 Skill

Decision runtime tool allowlist:

```text
mcp__healthmes__search_activity
mcp__healthmes__search_nutrition
mcp__healthmes__search_calendar
mcp__healthmes__search_wearable
mcp__healthmes__list_wellness_skills
mcp__healthmes__read_wellness_skill
```

Skill catalog는 도메인 전문가가 작성한 읽기 전용 지침을 제공한다. Skill은 도구를
직접 실행하지 않고, runtime이 어떤 도구를 언제 참고할지 설명한다.

VLM 사진 분석, 식사 확정, 설정 변경과 캘린더 mutation은 이 read-only decision
profile에 넣지 않는다. 이들은 각 bounded intake/command workflow가 소유하고,
필요한 판단만 같은 Decision Service에 요청한다.

### Skill을 읽은 뒤 데이터가 조회되는 순서

Skill과 데이터 검색은 같은 `healthmes` MCP 서버를 사용하지만 역할이 다르다.

| 종류 | 예시 | 반환하는 것 |
|---|---|---|
| Skill catalog tool | `list_wellness_skills`, `read_wellness_skill` | 검토된 판단 절차와 도구 사용 지침 |
| Domain search tool | `search_activity`, `search_nutrition`, `search_calendar`, `search_wearable` | 실제 저장 데이터에서 계산된 context와 `source_refs` |

LLM은 사용자 질문을 읽고 Skill이 필요하면 catalog에서 관련 Skill을 골라 읽는다.
Skill 문서는 예를 들어 "후보 카페인, 오늘의 확정 섭취량, 현재 시각과 수면을
확인하라"고 안내할 수 있다. Skill 자체가 다음 MCP 도구를 실행하지는 않는다.
**Skill 내용을 읽은 같은 LLM이** 필요한 `search_*` 도구를 다시 선택한다.

```text
사용자 질문
  -> LLM이 관련 Skill 필요 여부 판단
  -> 필요하면 list/read_wellness_skill
  -> LLM이 Skill 지침과 질문을 함께 해석
  -> search_nutrition 또는 다른 search_* 호출
  -> 결과의 freshness/coverage/limitations 확인
  -> 필요하면 다른 domain을 추가 조회
  -> 최종 DecisionDraft
```

간단한 조회는 Skill을 읽지 않고 바로 domain search를 호출할 수 있다. 반대로 Skill을
읽었다고 해서 Skill에 적힌 모든 domain을 기계적으로 조회해서도 안 된다. 실제
질문에 필요한 최소 자료를 LLM이 선택한다.

### 여러 domain을 조회하는 방식

여러 입력이 필요하면 LLM은 한 Hermes `/v1/responses` 요청 안에서 하나 이상의
MCP `function_call`을 만든다. 각 호출 결과는 `function_call_output`으로 다시
LLM에게 들어가고, LLM은 결과를 본 뒤 추가 호출 여부를 판단한다.

```text
LLM
  -> function_call: search_nutrition
  <- function_call_output: 오늘 카페인 ledger
  -> function_call: search_wearable
  <- function_call_output: 수면/readiness
  -> function_call: search_activity
  <- function_call_output: 연속 작업과 휴식
  -> final assistant output: healthmes.decision-draft.v2
```

따라서 `search_*` 호출들은 **중간 출력**이고 최종 assistant 출력은 하나의 strict
`DecisionDraft`다. 구현은 여러 call/output pair를 검증하지만 현재
`DecisionContextSearchSessionService`는 한 요청의 canonical trace와 policy
일관성을 위해 검색 작업을 직렬화한다. 여러 도구를 호출할 수 있다는 말이 곧
동시 병렬 조회를 의미하지는 않는다.

### Domain Provider와 서브에이전트 경계

Domain Provider는 AI가 아니라 정해진 입력 계약을 실행하는 조회·계산 adapter다.

```text
LLM이 capability 선택
  -> HealthMes MCP search tool
  -> DecisionContextSearchSessionService
  -> Context Access Layer
  -> Provider Registry가 capability owner 결정
  -> Activity/Nutrition/Calendar/Wearable Provider.query()
  -> HealthMes DB, mirror 또는 bounded Open Wearables reader
  -> ContextResult + source_refs
```

Provider Registry의 매핑은 결정론적이다. 예를 들어
`nutrition.caffeine-ledger`는 `NutritionContextProvider`가 처리한다. Provider는
질문의 의미를 해석하거나 최종 행동을 추천하지 않고, 내부에서 서브에이전트를
spawn하지도 않는다.

현재 MVP에는 검색 서브에이전트가 없다. 하나의 부모 Hermes LLM이 모든 검색 도구를
직접 선택한다. 향후 복잡한 장기·다중 domain 검색을 병렬 위임하는 기능은
[#193](https://github.com/NomaDamas/healthmes-agent/issues/193)에서 추적한다.
그 기능도 Provider 안에서 agent를 만들지 않고, 부모 판단 계층이 제한된 읽기 전용
retrieval worker를 선택적으로 생성하는 구조여야 한다. 최종 판단, source 검증과
저장은 계속 부모 HealthMes Decision Agent만 소유한다.

## 5. Source Refs와 Canonical Trace

Hermes transcript는 신뢰 가능한 DB trace가 아니다. 모델이 본 function output과
HealthMes가 실제 실행한 search session trace를 대조한다.

```text
Hermes function_call
  -> HealthMes MCP
  -> canonical ContextQuery
  -> canonical ContextResult
  -> SourceRef 집합
  -> Hermes function_call_output
```

최종 `used_source_ref_ids`는 canonical SourceRef 집합의 부분집합이어야 한다.
모델이 ref를 새로 만들거나 다른 요청의 ref를 재사용하면 성공으로 승격하지 않는다.

`source_refs`는 다음 용도로 사용한다.

- 답변이 어느 관측값과 파생값을 사용했는지 추적
- finalization 직전 source와 policy 재검증
- compact DecisionRecord와 결과 설명 연결
- 삭제·retention 변경 후 stale 답변 복구 방지

## 6. Strict 결과 계약

Hermes 마지막 assistant text는 JSON 하나여야 한다.

```json
{
  "schema": "healthmes.decision-draft.v2",
  "decision": {
    "status": "completed",
    "answer": "Take a restorative break before continuing.",
    "record_summary": null,
    "record_summary_code": "take_restorative_break",
    "proposed_action": true,
    "persistence_intent": "action",
    "used_source_ref_ids": [
      "sr_0123456789abcdef0123456789abcdef"
    ],
    "limitations": [],
    "clarification_question": null,
    "confidence": 0.8,
    "uncertainty": null,
    "follow_up_question": null
  }
}
```

다음은 계약 위반이다.

- JSON 앞뒤 자유 텍스트나 code fence
- 알 수 없는 field
- 허용되지 않은 tool
- 짝이 없는 function call/output
- 실제 결과에 없는 source ref
- status와 answer/clarification invariant 불일치
- persisted answer와 `record_summary_code`의 canonical 문장 불일치
- response size 또는 요청 deadline 초과

`healthmes.decision-draft.v2`는 저장 대상 결론의 단일 정본을
`record_summary_code`로 둔다. `action`, `risk`, `explicit_tracking`이면 Hermes는
runtime prompt에 명시된 allowlist에서 code를 고르고 `answer`를 그 code의
canonical 문장과 정확히 같게 반환해야 한다. 따라서 최초 응답과 재시작 후 복구가
서로 반대 의미가 될 수 없다. `record_summary`는 과거 runtime을 설명하기 위한
legacy transient field이며 v2에서는 `null`이어야 하고 장기 저장하지 않는다.

## 7. 조건부 Finalization

LLM이 `persistence_intent`를 주장했다고 그대로 저장하지 않는다.

```text
구체적 행동 제안                  -> action
행동 가능한 중요 위험 경고        -> risk
trusted caller의 명시적 추적 요청  -> explicit_tracking
단순 조회                         -> none
근거 없는 LLM 저장 주장            -> none
```

행동 제안이 없는 `completed` 결과에서는 trusted request와 모델 출력이 정확히
일치해야 한다.

```text
persistence_requested=false -> persistence_intent=none
persistence_requested=true  -> persistence_intent=explicit_tracking
read-only 판단 경로          -> mutation 금지
```

불일치는 Hermes 응답 adapter와 `DecisionFinalizer` 양쪽에서 실패로 처리한다.
따라서 모델이 사용자의 추적 요청을 조용히 무시하거나, 요청하지 않은 항목을
“tracked”라고 답하는 경로가 없다.

`none`이면 DecisionRecord를 만들지 않는다. 저장하는 경우에도 원문 질문, 자유 형식
전체 답변, 모델 작성 `record_summary`, transcript와 tool payload를 복제하지
않는다. Hermes가 선택할 수 있는 code와 HealthMes가 렌더링하는 문장은 다음의
고정 계약이다.

```text
action
  pause_and_reassess           -> Pause and reassess before continuing.
  take_restorative_break       -> Take a restorative break before continuing.
  delay_and_reassess           -> Delay this choice and reassess later.
  reduce_or_avoid              -> Reduce or avoid this choice for now.
  proceed_with_caution         -> Proceed cautiously and monitor how you feel.
  seek_professional_support    -> Seek qualified professional support before acting.

risk
  pause_and_reassess
  delay_and_reassess
  reduce_or_avoid
  seek_professional_support

explicit_tracking
  track_for_review             -> Keep this wellness item tracked for later review.
```

저장 대상 결정은 최초 `DecisionResult`와 복구 결과 모두 같은 canonical 문장을
사용한다. 자유 형식 상세 설명이 필요한 단순 조회는 `none`으로 반환해 저장하지
않는다. 장기 레코드는 code, source_refs와 최소 runtime metadata만 남긴다. 과거
payload v1-v5는 read compatibility를 유지하지만 새 쓰기는
`healthmes.decision-private.v6` 형식을 사용한다.

finalizer는 다음을 하나의 bounded write 절차로 처리한다.

```text
현재 정책 재조회
write-plane fence
source row/generation 재검증
retention basis와 expires_at 계산
compact payload 생성
flush와 commit
publication
```

commit 시작 전 deadline이면 실패로 확정하고 late write를 막는다. commit이 이미
시작된 뒤 outcome을 알 수 없으면 성공/실패를 추측하지 않고 `unknown`을 반환하며,
request ID recovery가 실제 저장 결과를 확인한다.

명시적 request-ID 복구와 persisted receipt replay는 현재 앱 설정 timezone이
아니라 검증된 저장 payload의 원래 request timezone을 사용한다. 설정 timezone이
바뀌어도 과거 source selector의 local-day 의미가 변하지 않는다. 저장 record가
손상돼 원래 timezone을 신뢰할 수 없으면 예외로 우회하지 않고 finalizer의 기존
감사 가능한 `decision_record_contract_invalid` 실패 경로로 보낸다.

## 8. 취소와 종료

```text
HTTP client disconnect
  -> 진행 중 Hermes reasoning 취소
  -> search session abort
  -> transport stream 종료

finalization이 이미 irreversible commit 단계
  -> commit outcome 추적 계속
  -> app shutdown이 DB teardown 전에 drain
```

요청 전체에는 absolute deadline이 적용된다. startup/profile 검증, search session,
Hermes call, response parse, finish/abort와 cleanup이 서로 독립된 무한 timeout을
갖지 않는다.

성공 Hermes session은 bounded retry로 삭제한다. 실패 응답에 session ID가 없는
현재 upstream 한계는 전용 state directory와 TTL purge로 제한한다.

## 9. 단일 Runtime 보장

현재 production composition은 다음 builder만 사용한다.

```text
build_configured_decision_engine(...)
  -> build_healthmes_responses_decision_engine(...)
  -> HermesResponsesDecisionAgent
```

폐기된 split-runtime의 public adapter, builder와 iteration 계약은 제품 코드에서
제거한다. 테스트가 legacy endpoint의 404를 확인하는 것은 재등장을 막기 위한
negative invariant다.

`HealthMesDecisionService`가 REST, channel, proactive와 scheduled 요청의 공통
진입점이다. bounded command가 별도 endpoint를 가지더라도 자유 형식 LLM 판단을
하지 않으므로 두 번째 reasoning 경로가 아니다.

일반 HealthMes MCP에는 임의 판단을 저장하는 writer가 없다. 자유 형식 결과는
`DecisionFinalizer`만 조건부 저장하며, 캘린더 confirmation 같은 제한된 internal
command만 자기 workflow 안에서 감사 레코드를 남길 수 있다.

### 프로세스 시작 경계

HealthMes core는 optional Hermes runtime보다 먼저 시작한다.

```text
HealthMes /health + /mcp ready
  -> Hermes decision runtime ready
  -> 첫 ask()가 profile/model/toolset을 lazy 검증
```

첫 검증 실패는 해당 요청만 `blocked`로 만들고 다음 요청에서 재시도한다. 이 순서로
Hermes가 HealthMes MCP를 필요로 하면서 HealthMes startup이 Hermes를 기다리는
순환 의존을 제거한다.

## 10. 확장 원칙

새 wellness domain을 추가할 때 순서는 다음과 같다.

```text
1. 저장/외부 provider 경계
2. bounded domain query와 provenance
3. Context Access Layer capability
4. HealthMes MCP search tool 또는 기존 search 확장
5. read-only domain Skill
6. cross-domain E2E와 source-ref 검증
```

새 domain마다 별도 agent, 별도 제품 MCP나 별도 질문 taxonomy를 만들지 않는다.
필요한 데이터 특성만 논리적으로 분리하고 같은 Decision Service와 source 계약에
연결한다.

검색 복잡도가 실제 병목으로 확인되면 #193의 bounded retrieval subagent를 검토한다.
도입 조건은 단일 ingress, 부모 단일 최종 판단, 기존 Context Access Layer와
Provider 계약, canonical trace와 `source_refs` 검증을 모두 유지하는 것이다.
