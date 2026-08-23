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

### 단일 LLM의 충분조건과 한계

MVP에는 하나의 부모 Hermes LLM이면 충분하다. 한 turn 안에서 질문을 해석하고,
Skill을 읽고, 여러 domain search를 순차 호출하고, 결과를 본 뒤 추가 조회하고,
최종 cross-domain 답변을 만들 수 있기 때문이다.

그러나 “한 LLM이면 엄밀한 조회도 보장된다”는 결론은 틀리다.

```text
LLM
  -> 무엇을 조회할지 계획

HealthMes MCP + Access Layer + Provider
  -> 정확히 어떤 행과 기간을 조회할지 실행
  -> retention, timezone, dedup, freshness와 단위를 적용

DecisionFinalizer
  -> 실제 반환된 source_refs만 사용했는지 재검증
```

| 품질 축 | 성격 | 보장 방법 |
|---|---|---|
| Retrieval-plan completeness | 확률적 | 질문 평가셋, tool-call telemetry, 반복 조회 prompt |
| Query execution integrity | 결정론적 | typed query, Provider, retention/timezone 경계 |
| Provenance integrity | 결정론적 | canonical trace, `source_refs`, finalizer 재검증 |
| 최종 설명 품질 | 확률적 | LLM 평가와 도메인 Skill 검토 |

따라서 단일 LLM은 **오케스트레이터로 충분**하지만 DB query engine이나 정책
검증기를 대체하지 않는다. 임의 질문에서 관련 domain을 빠뜨리는 비율이 실제
평가에서 문제로 확인되면 #193의 bounded retrieval subagent를 추가한다. 그
경우에도 Provider 내부가 아니라 부모 판단 계층이 읽기 전용 worker를 생성하고,
최종 판단과 저장은 계속 부모 하나가 소유해야 한다.

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
    "answer": "Your recovery is green with moderate day strain. Start with a 10-minute easy walk, drink water, and begin bedtime preparation 30 minutes earlier.",
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
- 저장 의도와 호환되지 않는 `record_summary_code`
- v2에서 `record_summary`가 `null`이 아닌 응답
- response size 또는 요청 deadline 초과

`healthmes.decision-draft.v2`는 저장 대상 결론의 단일 정본을
`record_summary_code`로 둔다. `action`, `risk`, `explicit_tracking`이면 Hermes는
runtime prompt에 명시된 allowlist에서 code를 고른다. `answer`는 해당 code와
의미가 일치하는 상세한 **현재 turn 응답**이며, canonical 문장으로 축약하지 않는다.
`record_summary`는 과거 runtime을 설명하기 위한 legacy transient field이며
v2에서는 `null`이어야 하고 장기 저장하지 않는다.

```text
live DecisionResult
  -> LLM의 상세 answer + actions + source_refs + related_record_ids

DecisionRecord v7
  -> answer 원문 미저장
  -> allowlisted record_summary_code + bounded actions + source_refs

receipt/record replay
  -> code로 렌더링한 canonical 짧은 answer
  -> decision_response_compacted limitation
  -> actions + source_refs + related_record_ids 복구
```

따라서 최초 응답과 replay의 문장은 서로 같을 필요가 없다. 대신 저장된 code가
최초 답변의 결론과 모순되지 않아야 하며, 상세 답변을 저장하지 않는 privacy
경계 때문에 replay는 의도적으로 compact하다.

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

최초 `DecisionResult`는 상세 답변을 반환하지만 장기 레코드는 그 답변을 복제하지
않는다. replay는 저장된 code의 canonical 문장을 반환하고
`decision_response_compacted`를 표시한다. 장기 레코드는 code, bounded actions,
source_refs와 최소 runtime metadata만 남긴다. 과거 payload v1-v6는 read
compatibility를 유지하지만 새 쓰기는 `healthmes.decision-private.v7` 형식을
사용한다.

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

## 10. WHOOP Recovery 기능의 공통 경로

Sake가 만든 WHOOP Recovery + Cycle `day_strain` 기능은 별도 WHOOP 판단 경로로
남기지 않고 #138의 공통 wearable 경로로 이관했다.

### 이전과 현재

```text
이전
WHOOP Skill
  -> WHOOP 전용 context 도구
  -> WHOOP 전용 판단 저장 요청

현재
WHOOP Skill
  -> HealthMes MCP search_wearable
       capability="wearable.whoop-recovery-package"
  -> 결정론적 WearableContextProvider
  -> Open Wearables Recovery + Cycle day_strain
  -> HealthMes WHOOP package snapshot v2
  -> 해당 WHOOP package의 canonical SourceRef 정확히 하나
  -> Hermes LLM의 다른 domain과 종합
  -> DecisionFinalizer 검증과 조건부 저장
```

현재 구조에는 WHOOP 전용 제품 MCP, WHOOP 전용 HealthMes 저장소, WHOOP 검색
subagent가 없다. `healthmes-whoop-recovery` Skill도 데이터를 계산하거나 저장하지
않는다. Skill은 LLM에게 공통 `search_wearable` capability를 언제, 어떤 제한으로
호출할지 알려주는 읽기 전용 지침이다.

### LLM과 Provider의 책임

| 책임 | Hermes LLM | `WearableContextProvider` |
|---|---|---|
| WHOOP Skill을 읽을지 결정 | 담당 | 담당하지 않음 |
| recovery package capability 선택 | 담당 | 담당하지 않음 |
| 정확한 upstream 행 선택 | 담당하지 않음 | local day, timestamp, revision 규칙으로 선택 |
| Recovery와 day strain의 Cycle 일치 검사 | 담당하지 않음 | 불일치·누락 시 fail closed |
| WHOOP 등급과 action package 계산 | 재계산 금지 | Sake 규칙으로 결정론적 계산 |
| 다른 wellness domain과 최종 종합 | 담당 | 담당하지 않음 |
| 저장 여부 | 주장만 가능 | 계산하지 않음; `DecisionFinalizer`가 확정 |

LLM에는 다음 **공개 package**만 반환한다.

```text
status, date, timezone, confidence
recovery label과 freshness
day_strain label과 freshness
cycle_linkage status
level
허용된 walk 선택지와 bounded actions
limitations
해당 WHOOP package의 canonical HealthMes SourceRef 정확히 하나
```

공개 top-level key 집합은 Sake 계산 결과의 canonical schema와 정확히 같아야
한다. snapshot envelope의 `retention_window`는 semantic public package에서
제외한다. 따라서 `raw_scores`나 다른 임의 별칭을 추가해 raw 값을 우회 공개할 수
없다.
WHOOP package는 recovery, day strain, Cycle linkage, level, actions와 limitations가
함께 검증되는 원자적 계약이다. 따라서 `fields` projection을 허용하지 않으며,
일부 필드 요청은 Provider 실행 전에 `query_fields_unsupported`로 거부한다.

두 종류의 limitation도 섞지 않는다.

```text
Sake 계산 limitation
  -> canonical public package의 limitations
  -> signal 누락, Cycle 불일치, stale row, source truncation

HealthMes runtime limitation
  -> ContextResult.limitations envelope
  -> upstream timeout, retained fallback, retention trim, snapshot write 실패
```

runtime limitation 때문에 저장된 Sake package를 다시 쓰거나 그 semantic identity를
바꾸지 않는다. live 조회와 retained snapshot이 같은 계산 결과를 가리키면 package는
동일하고, 해당 조회 턴의 실행 상태만 `ContextResult`에서 달라진다.

upstream provenance의 `source_provider=open-wearables`,
`upstream_provider=whoop`, raw record ID, metric definition, Cycle ID, revision
timestamp, raw score와 derivation algorithm은 LLM에 노출하지 않는다. 이 값은
HealthMes snapshot v2의 private provenance에 보존한다. 다만 LLM에 반환되는
canonical `SourceRef`에는 HealthMes mirror provider, snapshot event UUID, 관찰
범위와 `collected_at` 같은 snapshot 추적 정보가 포함된다.

`decision_record.evidence_refs`는 `main`의 schema와 과거 레코드 read compatibility를
위해 nullable 상태로 남긴다. 새 WHOOP 판단은 이를 별도 저장 경로로 사용하지
않으며, snapshot private provenance와 DecisionRecord v7 source attestation이
활성 정본이다.

WHOOP snapshot payload schema는 v2이고 semantic identity schema는 v3이다.
Identity는 다음 네 값을 결합한다.

```text
query_digest
+ semantic_public_digest
+ private_provenance_digest
+ retention_policy_revision
```

`semantic_public_digest`에서는 동적인 `retention_window`를 제외한다.
`collected_at`은 freshness, audit와 provenance timestamp 검증에는 사용하지만
semantic identity에는 포함하지 않는다. 따라서 같은 query, 공개 의미, 원본 행과
retention policy로 나중에 다시 수집하면 기존 snapshot을 재사용하고, 같은 화면
결과라도 원본 행이나 revision이 바뀌면 다른 snapshot으로 추적한다.

성공한 각 WHOOP package tool result는 해당 snapshot event를 가리키는 canonical
WHOOP `SourceRef`를 정확히 하나 반환한다. 복합 판단에서는 Activity, Nutrition,
Calendar 등 다른 domain의 SourceRef가 함께 사용될 수 있다.

### 이전 package를 정확히 다시 사용하는 follow-up

```text
REST 또는 Channel request
  -> related_record_ids에 이전 WHOOP snapshot UUID
  -> 해당 턴 전용 rr_<16hex> alias 생성
  -> Hermes에는 UUID 대신 alias만 노출
  -> search_wearable(package_record_id=alias)
  -> HealthMes가 provider 호출 직전에 원래 UUID로 복원
  -> retained snapshot을 exact 조회
  -> 없거나 scope가 다르면 UNAVAILABLE
  -> 최신 snapshot으로 자동 대체하지 않음
```

선택된 걷기를 저장할 때는 request hint, 최종 WHOOP `SourceRef.record_id`, 실제
실행된 effective query의 `package_record_id`를 모두 비교한다. date-only query와
다른 package를 조회한 trace는 exact follow-up으로 인정하지 않는다.

`rr_` 값은 이전 응답에 영구 저장되는 ID가 아니라 요청마다 만드는 턴 전용
별칭이다. 공개 REST의 `hints.related_record_ids`와 Channel 계약은 같은
`DecisionContextHints`로 수렴한다. 성공 응답은 canonical WHOOP SourceRef가 정확히
하나일 때 `related_record_ids.whoop_recovery_package`를 함께 반환한다. UI는 이
map을 그대로 다음 요청의 `hints.related_record_ids`로 되돌려 보내며, SourceRef를
직접 해석하거나 실제 UUID를 질문 문자열에 넣거나 `rr_` alias를 만들지 않는다.

```json
{
  "question": "이전 제안대로 20분 걸을게",
  "hints": {
    "related_record_ids": {
      "whoop_recovery_package": "previous-whoop-snapshot-uuid"
    }
  }
}
```

Channel E2E는 최초 추천 응답의 `related_record_ids`를 받은 뒤 더 최신 snapshot을
추가하고, 20분 선택 turn에서 이전 snapshot을 exact 재조회하는 흐름을 검증한다.
두 turn은 각각 immutable DecisionRecord v7을 만들며, 새 service instance의
receipt replay는 LLM을 다시 호출하지 않고 compact 응답과 같은 related-record ID를
복구한다. REST 테스트는 응답 map 노출과 입력 map 전달을 각각 검증한다.

### 보존된 Sake 의미

이관은 다음 의미를 바꾸지 않는다.

- Recovery는 `0..33=red`, `34..66=yellow`, `67..100=green`이며 `33.5`,
  `66.5` 같은 경계 사이 값은 invalid다.
- Cycle day strain은 `[0,10)=light`, `[10,14)=moderate`,
  `[14,18)=high`, `[18,21]=all_out`이다.
- Recovery의 최신 행은 `recorded_at`, day strain의 최신 revision은
  `cycle_updated_at`으로 고른다.
- `recorded_at`은 원본 관찰 시각이고 `cycle_updated_at`은 Cycle revision
  시각이므로, 두 값은 각각 파싱·미래시각 검증하되 서로의 선후관계를 강제하지
  않는다. 이는 기존 Sake/Open Wearables가 허용한 과거·import 데이터의 의미를
  보존한다.
- 요청한 local day의 행이 과거 행보다 우선한다. 같은 최신 시각의 행이 둘이면
  내용이 같아도 `ambiguous_latest_row`로 처리한다.
- Recovery와 day strain은 같은 `cycle_id`여야 한다. Workout `strain`은 Cycle
  누적 `day_strain`을 대체하지 못한다.
- 두 upstream 조회의 truncation은 독립적으로 검사한다.
- upstream 조회 범위는 요청일 이틀 전부터 요청일 종료까지 유지해, stale 행과
  요청일 행의 우선순위를 같은 기준으로 판별한다.
- `basic`은 10분 걷기가 기본이고 10/20/30분을 선택할 수 있다.
- `enhanced`는 20분 걷기가 기본이고 10/20/30분을 선택할 수 있다.
- `priority`는 가벼운 10분 걷기 또는 휴식만 허용한다.
- 물 마시기와 평소보다 30분 이른 취침 준비는 항상 package에 포함한다.
- `selected`는 사용자가 선택했다는 뜻이며 완료했다는 뜻이 아니다.

`DecisionFinalizer`는 최종 action이 provider가 반환한 package와 일치하는지
검증한다. 허용되는 변경은 제공된 걷기 선택지 하나를 `selected`로 바꾸는 경우뿐이다.
package에 없던 시간, 임의 action 또는 완료 상태는 저장하지 않는다. 단순 조회는
저장하지 않고, 검증된 행동·위험·명시적 추적만 기존 compact DecisionRecord 경계로
보낸다.

### Canonical provider identity

Sake 리뷰에서 지적된 대소문자·공백 차이의 중복 저장 가능성은
DB마다 Unicode 소문자화가 다르지 않은 **portable ASCII provider ID** 계약으로
해결한다.

```text
입력 허용
  앞뒤 ASCII space(U+0020)는 제거
  본문은 ASCII 문자만 사용
  길이 1..64
  첫 글자는 A-Z, a-z 또는 0-9
  나머지는 A-Z, a-z, 0-9, ".", "_", "-"

저장 형식
  앞뒤 ASCII space 없음
  A-Z는 a-z로 변환
  첫 글자는 a-z 또는 0-9
  나머지는 a-z, 0-9, ".", "_", "-"
```

지원되는 write adapter는 DB 조회와 길이 검사 전에 이 공통 helper로 정규화한다.
1..64자 제한은 공백 제거 후 canonical 본문에 적용하므로 raw caller form의 총길이는
64를 넘을 수 있다. `casefold()`는 사용하지 않으며, Unicode 문자, NUL,
tab/newline, canonical 본문 65자 이상 값은 거부한다. migration은 유효한 기존
ASCII 대문자·공백 값만 정규화한다. 잘못된 기존 값이나 canonical collision이
하나라도 있으면 행, Alembic revision, 임시 index와 constraint를 변경하지 않고
전체 upgrade를 실패시킨다. DB check constraint는 비정규 직접 쓰기를 자동
변환하지 않고 거부한다.

raw-first ingest의 `raw_ingest_event.source`와 연결된
`wellness_event.source_provider`도 같은 migration transaction에서 같은 규칙으로
변경한다. 두 DB의 UUID 문자열 표기 차이를 제거해 연결을 검사하고, canonical
provider가 서로 다르면 원자적으로 실패한다.

idempotency key는 canonical provider와 `source_record_id`의 조합이다. 따라서
`WHOOP`, ` whoop `, `whoop`은 하나의 원본 identity가 되며, migration과 재시도도
같은 규칙을 사용한다.

구현상 DB allowlist 검사는 깊게 중첩된 `replace()` 호출이 아니라 허용 문자별
출현 수의 얕은 합을 문자열 길이와 비교한다. 이는 SQLite parser-stack 한도를
넘지 않으면서 기존의 portable ASCII 계약을 그대로 지킨다. migration은
allowlist를 통과한 값에만 명시적인 A–Z → a–z 치환을 적용하므로 PostgreSQL의
Unicode/locale 동작이 provider identity를 바꾸지 않는다.

## 11. 확장 원칙

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
