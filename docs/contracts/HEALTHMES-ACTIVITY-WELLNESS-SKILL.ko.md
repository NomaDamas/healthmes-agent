# HealthMes Activity Wellness Skill 계약

> **계약일:** 2026-08-09
>
> **상태:** HealthMes 엔진과 agent runtime 사이의 UI·runtime 독립 계약.
>
> **소유권:** 계산, 저장, privacy, retention과 context selection은 HealthMes가
> 소유한다. Hermes를 포함한 agent runtime adaptation은 future work다.

## TLDR

```text
phone / computer collectors
             |
             v
HealthMes canonical activity storage
             |
             v
hourly / daily activity engine
             |
             v
HealthMes REST + MCP context contract
             |
             v
this skill contract
             |
             v
future device or agent adapter
```

이 계약은 앱 이름, window title 또는 원시 telemetry를 모델에 전달하는 skill이
아니다. HealthMes가 계산한 작은 context를 어떤 질문에 선택하고, 근거와 한계를
어떤 형태로 보존할지 정의한다.

## 1. Capability boundary

| 계층 | 책임 |
|---|---|
| Activity collector | OS가 허용하는 foreground/idle aggregate를 수집하고 exclude, pause와 permission 상태를 source에서 적용한다. |
| HealthMes activity engine | canonical 저장, 보존, 시간 경계, hourly/daily 집계, focus와 overwork context를 결정론적으로 계산한다. |
| HealthMes cross-domain resolver | 질문에 필요한 activity, wearable, calendar, nutrition과 time context만 선택한다. |
| 이 skill 계약 | 어떤 질문에 어떤 context를 읽고 어떻게 설명할지 정의한다. 숫자를 다시 계산하지 않는다. |
| Agent/runtime adapter | MCP 호출, 대화 채널과 UI 표현을 연결한다. HealthMes 정책을 대체하지 않는다. |

다음은 이 계약의 범위가 아니다.

- UI 버튼, 카메라, 알림 또는 설정 화면
- Android, iOS, macOS, Windows UI 구현
- Hermes bootstrap, gateway, memory 또는 channel 변경
- `vendor/hermes-agent/` 수정
- 카페인 용량, 수면 점수 또는 의료 판단 재계산

## 2. Normative tools

runtime adapter는 다음 HealthMes 도구 이름을 기준으로 연결한다.

| 도구 | 용도 |
|---|---|
| `get_activity_summary(date)` | 한 local day의 active, idle, late activity, category, baseline과 coverage |
| `get_focus_context(start, end)` | 명시적 시간 구간의 sustained/fragmented/mixed focus context |
| `get_overwork_context(date, lookback_days)` | 총 활동, 긴 연속 활동, 야간 활동과 개인 baseline 기반 과로 context |
| `resolve_wellness_context(question_kind, ...)` | 필요한 영역만 선택하는 bounded cross-domain context |

`recovery`와 `caffeine_for_focus`는 별도 raw 도구를 조합하지 않고
`resolve_wellness_context`의 `question_kind`로 요청한다.

이 문서의 도구 이름은 runtime-neutral canonical name이다. Hermes 등 특정
runtime의 registry prefix는 해당 adapter가 추가하며 이 계약에 고정하지 않는다.
REST를 직접 호출해 MCP/context 경계를 우회해서는 안 된다.

## 3. Question routing

| 사용자 의도 | 호출 | 선택 영역 |
|---|---|---|
| "오늘 컴퓨터를 얼마나 썼어?" | `get_activity_summary` | activity |
| "왜 집중이 자꾸 끊겼지?" | `get_focus_context` 또는 `resolve_wellness_context(focus)` | activity, 필요 시 wearable와 calendar |
| "오늘 너무 오래 일했나?" | `get_overwork_context` 또는 `resolve_wellness_context(overwork)` | activity, 필요 시 wearable와 calendar |
| "지금 쉬어야 할까?" | `resolve_wellness_context(recovery)` | activity와 wearable |
| "집중하려고 이 커피를 마셔도 될까?" | `resolve_wellness_context(caffeine_for_focus)` + 별도 caffeine policy | activity, wearable, calendar, nutrition과 time |

단일 영역 질문은 선택되지 않은 영역을 읽지 않는다. 복합 질문도 모든 raw
데이터를 모으지 않고 미리 허용된 작은 context만 선택한다.

## 4. Response contract

future skill 또는 UI는 다음 네 부분을 분리해서 표현한다.

```text
observation
  HealthMes context에 실제로 존재하는 관찰

evidence
  evidence ID, freshness, coverage와 사용한 전문 context

proposal
  전문 정책 결과가 있을 때만 제시하는 제한된 행동 대안

boundary
  missing data, limitation, 상관관계 한계와 비의료 경계
```

HealthMes context의 다음 필드는 삭제하거나 숨기지 않는다.

- `status`
- `decision_ready`
- `evidence_ids` 또는 최상위 `evidence`
- `freshness`
- `coverage` 또는 `source_coverage`
- `limitations`
- `boundaries`

`status=insufficient_data` 또는 `unavailable`은 사용시간 0, 정상 또는 안전으로
바꾸지 않는다.

`decision_ready=false`도 단순 경고가 아니다. runtime은 이를 행동 가능한
승인이나 안전 판단으로 렌더링하지 않고, 어떤 전문 입력이 더 필요한지
`limitations`와 함께 설명해야 한다.

## 5. Specialized-policy boundary

교차 영역 resolver는 각 전문 정책의 숫자를 다시 계산하지 않는다.

```text
activity policy
  focus, overwork, break와 late-use context

caffeine policy
  confirmed intake, candidate amount, timing과 safety boundary

wearable policy
  sleep, HRV, stress와 recovery context

HealthMes resolver
  위 결과의 evidence, freshness, coverage와 limitation을 함께 전달
```

예를 들어 `caffeine_for_focus` 결과만 보고 agent가 카페인 mg을 새로 계산하거나
"마셔도 안전하다"고 말해서는 안 된다. 후보와 안전 입력이 갖춰진 별도 caffeine
policy 결과를 보존해서 설명하고, activity context는 휴식이나 과로 대안을
추가하는 데만 사용한다.

현재 `caffeine_for_focus`의 `decision_ready=true` 조건은 다음과 같다.

1. 요청된 nutrition interaction이 `caffeine_sleep` scope다.
2. 후보 식품에서 숫자가 있는 `exact` 또는 `range` 카페인 값이 `mg`
   단위로 확인된다. `unknown`, 잘못된 단위와 숫자 없는 값은 근거가 아니다.
3. 후보와 요청 날짜가 resolver의 같은 local day에 속한다.
4. 당일 카페인 섭취 ledger와 caffeine boundary가 모두 완료 확인됐다.

wearable, activity, calendar 또는 time context가 충분해도 위 조건을
대체하지 않는다.

## 6. Privacy contract

context와 agent 입력에는 다음 데이터를 포함하지 않는다.

- raw app identity
- window title
- URL 또는 browser domain
- click, key, pointer coordinate
- clipboard, notification body와 screen pixel
- raw wearable timeseries
- photo 또는 voice bytes

허용되는 것은 집계된 시간, category, 횟수, opaque evidence ID, coverage,
freshness와 limitation이다. 제외된 앱은 raw storage, summary, context와 로그
어디에도 나타나면 안 된다.

## 7. Failure behavior

1. 데이터가 없으면 `insufficient_data`와 누락 이유를 그대로 말한다.
2. coverage가 낮으면 단정적 원인이나 행동 지시를 만들지 않는다.
3. 한 영역이 실패해도 다른 영역의 유효한 context를 삭제하지 않는다.
4. 상관관계를 원인으로 표현하지 않는다.
5. 전문 정책 결과가 없으면 숫자 proposal을 만들지 않는다.
6. 사용자의 질문과 관계없는 영역을 추가 조회하지 않는다.
7. 날짜가 다른 wearable 또는 nutrition context를 현재 질문의 근거로 쓰지 않는다.
8. 만료된 summary나 raw event를 maintenance 전이라도 읽거나 복원하지 않는다.
9. 중첩 readiness block에 유효한 `recorded_at`, `freshest_at` 또는
   `observed_at`이 있으면 freshness를 재귀적으로 보존하고 `unavailable`로
   바꾸지 않는다.
10. raw 보존 경계를 걸친 focus 구간은 provenance가 완전할 때만 `exact`로
    표현한다. 일부 최신 raw만으로 과거 summary 구간을 누락하지 않는다.

## 8. Adapter acceptance

미래 device/agent adapter는 다음 fixture를 통과해야 한다.

- canonical tool name을 정확히 연결한다.
- app identity 없이 context를 렌더링한다.
- evidence, freshness, coverage와 limitations를 보존한다.
- `insufficient_data`를 0으로 바꾸지 않는다.
- `decision_ready=false`를 승인 가능한 proposal로 바꾸지 않는다.
- `caffeine_for_focus`에서 caffeine policy 숫자를 재계산하지 않는다.
- REST, database 또는 vendored Hermes 내부를 직접 우회하지 않는다.

이 계약을 변경할 때는 Activity REST/MCP tests, cross-domain resolver tests와
이 문서의 contract test를 함께 갱신한다.
