# Hermes 단일 모델 Iteration 계약

> **2026-08-16 폐기 알림:** HealthMes는 이 hook을 Hermes에 추가하지 않는다.
> 공식 wellness 경로는 HealthMes
> `POST /v1/wellness-decisions`가 Hermes의 기존 `POST /v1/responses`
> autonomous tool loop를 호출하는 구조다. 최신 기준은
> [`../HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](../HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md)다.
> 아래 내용은 검토했던 split-runtime 대안의 역사 기록이다.

> **계약일:** 2026-08-11
>
> **상태:** superseded. 구현·upstream 요청 대상이 아니다.
>
> **소유권:** HealthMes가 판단 loop, 정책, 도구 실행, 예산과 기록을
> 소유한다. Hermes는 정확히 한 번의 provider/model 호출만 수행한다.

## TLDR

현재 vendored Hermes의 chat, responses와 run API는 모두 도구를 직접 실행하는
전체 autonomous agent loop다. 따라서 HealthMes Decision Agent의 runtime으로
조용히 재사용하지 않는다.

```text
HealthMesDecisionAgent
  질문, 정책, 허용 도구, history, deadline
                    |
                    v
HermesRuntimeAdapter
  capability가 정확한 single-iteration 계약을 광고하는가?
        | yes                         | no
        v                             v
POST /v1/model/iterations       명시적 unavailable
provider call 정확히 1회        full chat fallback 금지
        |
        v
tool_calls 또는 structured output
        |
        v
HealthMes가 검증, 도구 실행, 반복, source ref 검증과 저장
```

## 1. 현재 Hermes에서 확인된 경계

체크인된 Hermes 0.18.2에서 다음 endpoint는
`AIAgent.run_conversation()`을 호출한다.

- `/api/sessions/{session_id}/chat`
- `/v1/chat/completions`
- `/v1/responses`
- `/v1/runs`

이 호출은 한 번의 model completion이 아니라 Hermes가 tool을 실행하고 필요한
후속 model call까지 수행하는 전체 turn이다.

추가로 확인된 한계:

- 요청의 `tools`와 `tool_choice`는 현재 per-turn runtime allowlist가 아니다.
- 실제 toolset은 정적 `api_server` platform 설정에서 결정된다.
- `max_iterations=1`도 budget finalizer의 추가 model call 가능성을 없애지 않는다.
- `/v1/runs/{id}/stop`은 best-effort interrupt이며 caller-owned hard deadline
  계약이 아니다.
- MCP `sampling/createMessage`는 가까운 내부 primitive지만 외부 generic API,
  usage 반환과 strict returned-tool allowlist를 모두 제공하지 않는다.

따라서 위 endpoint를 `HermesRuntimeAdapter.next_step()`의 정상 경로로 사용하지
않는다.

## 2. 책임 경계

| 구성요소 | 소유 책임 |
|---|---|
| HealthMes Decision Agent | 자연어 질문, 반복 횟수, tool/context/source 예산, hard deadline, 최종 상태 |
| Context Access Layer | 권한, consent, retention, timezone, privacy, query cap |
| Domain Provider | Activity, Nutrition, Wearable, Calendar의 정확한 계산과 provenance |
| Hermes Runtime Adapter | HealthMes turn을 generic model-iteration 요청으로 변환하고 응답을 검증 |
| Hermes single-iteration hook | provider/model 호출 정확히 1회, usage와 unexecuted tool call 반환 |
| Skill | 진입점과 채널 표현 안내. 정책, 권한, 계산, 저장 의무를 소유하지 않음 |

## 3. Capability discovery

HealthMes는 authenticated `GET /v1/capabilities`만 신뢰한다. endpoint가
존재할 것이라고 추측하거나 chat endpoint로 fallback하지 않는다.
Loopback runtime은 무인증 local transport를 허용하지만, remote HTTPS runtime은
명시적인 API key 없이는 adapter를 구성할 수 없다.

필수 feature:

```json
{
  "runtime": {
    "mode": "split_runtime",
    "tool_execution": "caller",
    "split_runtime": true
  },
  "features": {
    "model_iteration": true
  }
}
```

필수 endpoint descriptor:

```json
{
  "endpoints": {
    "model_iteration": {
      "method": "POST",
      "path": "/v1/model/iterations",
      "contract": "hermes.model-iteration.v1",
      "max_model_calls": 1,
      "tool_execution": "caller",
      "session_mutation": false,
      "supports": {
        "system_policy": true,
        "tool_allowlist": true,
        "conversation_snapshot": true,
        "structured_output": true,
        "usage": true,
        "external_deadline": true
      }
    }
  }
}
```

하나라도 없거나 값이 다르면 adapter는 capability를 unavailable로 반환한다.
`max_model_calls`는 JSON boolean이나 float가 아닌 정확한 JSON integer `1`이어야 한다.
`refresh=true` probe가 실패하면 이전 success cache는 즉시 폐기한다.

## 4. 요청 계약

`HermesRuntimeAdapter`는 다음을 매 iteration에 전달한다.

- agent가 생성한 runtime 전용 `request_id`, `turn_id`, `step_number`
- canonical request fingerprint
- HealthMes가 고정한 `model`과 `provider`
- mandatory HealthMes system policy와 version
- 사용자 질문, timezone, privacy scope
- 이전 validated tool exchange의 deep snapshot
- 이번 turn에서 허용된 virtual tool definition과 exact allowlist
- 남은 step, tool call, source ref와 context byte 예산
- HealthMes가 소유하는 남은 deadline
- `DecisionDraft` structured-output schema

전달하지 않는 것:

- caller principal, session과 channel identity
- 외부 `DecisionRequest.request_id/turn_id`
- HealthMes database/session/provider 객체
- raw SQL 또는 registry callback
- Hermes Skill 내용
- vendored Hermes 내부 class

virtual tool 이름은 adapter 내부의 opaque name이다. canonical HealthMes
capability와 Hermes tool prefix의 mapping을 core로 누출하지 않는다.

## 5. 실행과 응답 계약

Hermes hook은 다음을 지킨다.

1. provider/model을 정확히 한 번 호출한다.
2. tool을 실행하지 않는다.
3. Hermes session, memory 또는 channel state를 변경하지 않는다.
4. allowlist 밖 tool call을 성공 응답으로 반환하지 않는다.
5. caller cancellation과 deadline을 존중한다.
6. `tool_calls` 또는 structured output 중 하나만 반환한다.
7. model, provider와 input/output token usage를 반환한다.

HealthMes adapter는 다음을 다시 검증한다.

- contract version
- request/turn/step correlation
- canonical request fingerprint echo
- configured model/provider identity
- finish reason과 실제 action의 일치
- tool call ID의 존재와 중복
- exact tool allowlist
- model이 canonical `capability`를 arguments로 위조하지 않았는지
- `DecisionDraft`와 `ContextToolCall` schema

검증이 끝난 tool call만 HealthMes Decision Agent로 돌아온다. 실제 도구 실행,
`ToolCallRecord`, source ref 검증과 다음 iteration 여부는 HealthMes가 소유한다.
Capability discovery와 model iteration은 각각 남은 `turn.deadline_ms` 안에서
취소되며, HTTP 응답은 decoded bytes 기준 2 MB를 넘기기 전에 streaming 중단한다.
HTTP는 `Accept-Encoding: identity`만 허용하고 다른 `Content-Encoding`은 body를
읽기 전에 거부하므로 압축 해제 bomb이 decoded limit보다 먼저 메모리를 쓰지 못한다.
응답 envelope, `DecisionDraft`, `ContextToolCall`은 strict JSON scalar 검증을 거쳐
boolean, integer와 number의 문자열/불리언 강제 변환을 허용하지 않는다.
Python transport도 capability와 model 응답을 exact built-in `dict`, `list`,
`str`, `int`, `float`, `bool`, `null`로만 반환해야 한다. 사용자 정의 `Mapping`,
컨테이너 subclass와 scalar 객체는 메서드를 호출하지 않고
`hermes_transport_contract_invalid`로 거부한다. adapter는 파싱 전에 최대 64
depth, 20,000 node, 단일 scalar 256 KB, 전체 2 MB encoded JSON으로
정규화하므로 transport 객체가 검증기에서 임의 코드를 실행하거나 무제한 트리를
전달할 수 없다. JSON 구조와 scalar encoding 비용은 순회 중 누적하고 각
노드·scalar 전후에 남은 turn deadline을 검사한다. 큰 정수는 decimal 문자열로
변환하기 전에 encoded upper bound를 계산하므로 최종 직렬화 단계에서 뒤늦게
제한을 발견하지 않는다.
Idempotency key는 IDs만이 아니라 canonical request fingerprint에 결합되며,
transport가 cancellation을 억제해 늦게 반환해도 deadline 이후 결과는 거부한다.

## 6. Skill 경계

Skill이 설치됐는지는 보안과 정확성 계약에 영향을 주지 않는다.

Skill이 설명할 수 있는 것:

- 어느 HealthMes entrypoint를 호출하는가
- 사용자에게 관찰, 불확실성, 제안과 한계를 어떻게 표시하는가
- 채널에서 clarification을 어떻게 표현하는가

Skill에 두지 않는 것:

- mandatory system policy
- 권한, retention, timezone과 privacy 검사
- tool allowlist와 예산
- source ref 검증
- DecisionRecord 저장 의무

adapter 테스트는 Hermes `skills_api`가 켜진 경우와 꺼진 경우에 동일한 runtime
request가 생성됨을 검증한다.

## 7. 실패 동작

| 상황 | 결과 |
|---|---|
| current Hermes가 single iteration을 광고하지 않음 | `hermes_single_iteration_not_advertised`, `BLOCKED` |
| capability endpoint 접근 실패 | sanitized unavailable code, `BLOCKED` |
| near-miss contract 또는 unsafe endpoint | unavailable, full-loop fallback 금지 |
| correlation/model/tool allowlist 위조 | `runtime_contract_violation`, `FAILED` |
| malformed output 또는 usage | `runtime_contract_violation`, `FAILED` |
| HealthMes hard deadline 초과 | 기존 Decision Agent timeout/quarantine 정책 적용 |

## 8. Upstream 구현 요구

추적 이슈: [#139 HERMES-UPSTREAM-01](https://github.com/NomaDamas/healthmes-agent/issues/139)

별도 Hermes repository, branch와 PR에서 MCP sampling의 one-call primitive를
재사용하거나 추출해 generic `run_model_iteration(request)` 계약을 만든다.
HealthMes 카페인, 활동, 영양 또는 저장 규칙은 Hermes core에 넣지 않는다.

완료 조건:

- 한 요청당 provider call이 정확히 1회다.
- server-side tool execution과 session mutation이 없다.
- strict allowlist, structured output, usage와 external deadline이 테스트된다.
- 이 문서의 HealthMes fake transport fixture와 상호 운용된다.
