# HealthMes Decision Agent 컴포넌트 아키텍처

> **결정일:** 2026-08-16
>
> **상태:** 현재 단일-runtime 구현의 내부 컴포넌트 기준.
>
> 제품 전체 경계와 저장 구조는
> [`HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md)
> 를 먼저 읽는다.

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

## 2. 한 요청의 실제 순서

```text
1. 사용자 질문
2. HealthMesDecisionService가 owner/timezone/scope를 채움
3. HealthMesDecisionEngine이 요청 admission
4. HermesResponsesDecisionAgent가 search session 시작
5. Hermes /v1/responses 호출
6. Hermes가 HealthMes MCP 도구를 자율·반복 호출
7. search session이 canonical tool trace와 source_refs 보관
8. Hermes가 healthmes.decision-draft.v1 JSON 반환
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
  "object": "healthmes.decision-draft.v1",
  "draft": {
    "status": "completed",
    "answer": "짧은 사용자 답변",
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
- response size 또는 요청 deadline 초과

## 7. 조건부 Finalization

LLM이 `persistence_intent`를 주장했다고 그대로 저장하지 않는다.

```text
구체적 행동 제안                  -> action
행동 가능한 중요 위험 경고        -> risk
trusted caller의 명시적 추적 요청  -> explicit_tracking
단순 조회                         -> none
근거 없는 LLM 저장 주장            -> none
```

`none`이면 DecisionRecord를 만들지 않는다. 저장하는 경우에도 원문 질문, 전체 답변,
transcript와 tool payload를 복제하지 않는다.

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

폐기된 split-runtime의 public adapter, builder와
`/v1/model/iterations` 계약은 제품 코드에서 제거한다. 테스트가 해당 endpoint의
404를 확인하는 것은 재등장을 막기 위한 negative invariant다.

`HealthMesDecisionService`가 REST, channel, proactive와 scheduled 요청의 공통
진입점이다. bounded command가 별도 endpoint를 가지더라도 자유 형식 LLM 판단을
하지 않으므로 두 번째 reasoning 경로가 아니다.

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
