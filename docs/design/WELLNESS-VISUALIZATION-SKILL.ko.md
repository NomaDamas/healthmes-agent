# HealthMes Wellness Visualization Skill 계약

> 상태: 현재 WellnessScene v1 구현과 후속 시각화 Target UX를 구분한 설계
>
> 목적: HealthMes가 이미 계산하거나 조회한 건강·일정·목표·행동·결과를
> 질문에 맞는 시각적 설명으로 조합한다.
>
> 변경 금지 경계: 인지에너지 엔진, 건강 계산, MCP 도구, 캘린더 쓰기 계약,
> 인증 계약, `vendor/hermes-agent/`

## TL;DR

`healthmes-wellness-visualizer`는 차트를 예쁘게 만드는 스킬이 아니다. 사용자의
질문에 답하기 위해 필요한 근거를 고르고, 가장 이해하기 쉬운 시각화와 짧은
설명을 조합해 wellness 의사결정을 돕는 bounded presentation skill이다.

이 스킬은 사용자 질문뿐 아니라 HealthMes trigger가 먼저 만든 선제적
intervention에도 동일하게 사용한다.

현재 REST scene endpoint의 `source=proactive`는 exact active `proposal_id`와
그 proposal의 `decision_record_id`가 모두 일치하는 실행형 제안만 지원한다.
proposal 없는 정보형 proactive delivery는 기존 trigger runtime의 책임이며
이번 endpoint 범위가 아니다.

```text
사용자 질문 또는 HealthMes trigger
    ↓
질문의 의사결정 목적 분류
    ↓
기존 MCP·read model에서 검증된 데이터 수집
    ↓
HealthMes agent가 wellness insight 구성
    ↓
visualization skill이 표현 방식 선택
    ↓
검증된 WellnessScene schema
    ↓
iPhone · Mac · Web renderer
    ↓
설명 · 대안 · 승인 · 캘린더 적용
```

스킬은 건강 점수나 상관관계를 새로 계산하지 않는다. 임의 SwiftUI, HTML,
JavaScript 또는 차트 코드를 생성하지 않는다. 허용된 schema와 component만
선택한다.

## 1. 사용자에게 전달할 결과

모든 결과는 가능한 범위에서 다음 순서를 따른다.

```text
한 문장 결론
→ 핵심 시각화
→ 이 판단을 만든 근거
→ confidence와 데이터 한계
→ 지금 할 수 있는 한 가지 행동
→ 필요한 경우 calendar before/after와 승인
```

예:

```text
오늘 16시 집중 업무는 내일 오전이 더 안전합니다.

[시간대별 가용 에너지 곡선 + 실제 캘린더]

16시 예상 가용량 43%
집중 업무 예상 요구량 80%
근거: 수면 부족, 오전 회의 부하
신뢰도: 보통

[유지] [다른 시간] [변경 적용]
```

## 2. 능력 경계

### HealthMes agent

- 사용자의 질문과 현재 맥락을 해석한다.
- 필요한 기존 MCP 도구와 read model을 선택한다.
- 여러 데이터의 시점, 단위, coverage를 확인한다.
- observation, evidence, option, action을 구성한다.
- 실제 일정 변경은 기존 propose-then-confirm 경로만 사용한다.

### Visualization skill

- 질문의 의사결정 유형을 분류한다.
- 허용된 visualization primitive를 선택한다.
- axis, unit, range, annotation, confidence 표기를 명시한다.
- 텍스트 설명과 시각화가 같은 결론을 말하는지 검증한다.
- 플랫폼별 정보 밀도를 정한다.

### Platform renderer

- schema를 native SwiftUI 또는 신뢰된 web component로 렌더링한다.
- 색상 외에도 label, pattern, icon으로 의미를 전달한다.
- Dynamic Type, VoiceOver, reduced motion, 좁은 화면을 지원한다.
- 알 수 없는 schema version은 렌더링하지 않는다.

### Visualization skill이 하지 않는 일

- 건강 원시 데이터를 직접 REST로 조회
- 인지에너지 엔진 계산 재구현
- 의료 진단 또는 치료 결정
- 상관관계를 인과관계로 변경
- 캘린더 직접 쓰기
- 존재하지 않는 데이터 보간
- 임의 UI 코드 실행

## 3. Calendar-first 원칙

일정과 관련된 질문에서는 Apple Calendar와 Google Calendar의 실제 mirrored
event가 시각화의 기준이 된다.

```text
시간대별 가용 에너지
          │
          ▼
┌────────────────────────────────────┐
│ Apple ● 09:00 핵심 문서            │
│ Google ● 13:00 팀 회의             │
│ Google ● 16:00 집중 블록  ⚠ 충돌   │
│ HealthMes ╌ 내일 09:00 이동 제안   │
└────────────────────────────────────┘
          │
          ▼
    before / after / trade-off
```

v1 표시 계약:

- event identity, title, start/end, provider를 보존한다.
- 현재 read model에 존재하는 반복, 종일 일정, 참석자 존재 여부, ownership,
  lock 상태를 보존한다.
- HealthMes가 만들지 않은 외부 일정을 수정 가능한 것처럼 표현하지 않는다.
- pending proposal은 실선 event가 아니라 점선 또는 별도 proposal style로
  표시한다.
- `accepted`와 외부 캘린더 반영 완료인 `pushed`를 구분한다.

calendar color, 위치, 회의 링크, RSVP 응답, 알림, 메모, 원본 provider deep
link, cross-provider dedup은 현재 mirror가 해당 값을 제공하지 않으므로 v1 완료
항목이 아니다. read model이 확장된 뒤 같은 scene 계약의 optional field로
추가한다.

## 4. 허용된 시각화 카탈로그

| Primitive | 답하는 질문 | 핵심 표현 |
|---|---|---|
| `capacity_bar` | 오늘 얼마나 감당할 수 있는가? | 가용량, 계획 부하, 초과량 |
| `energy_curve` | 언제 집중하거나 회복해야 하는가? | 시간대별 예측과 confidence band |
| `calendar_canvas` | 몸 상태가 실제 일정과 어디서 충돌하는가? | provider event와 wellness overlay |
| `schedule_comparison` | 무엇이 어떻게 바뀌는가? | 변경 전·후 캘린더 |
| `proposal_preview` | operation/source event identity가 없는 제안은 무엇인가? | exact proposal ID와 제안 블록, 시각화 없음 |
| `time_series` | 최근 상태가 좋아지고 있는가? | 일·주·월 추이 |
| `baseline_band` | 평소의 나와 얼마나 다른가? | 개인 baseline 범위와 현재 위치 |
| `comparison_bar` | 어떤 날·행동·시간대가 더 나았는가? | 같은 단위의 범주 비교 |
| `factor_contribution` | 이 판단에 무엇이 영향을 줬는가? | 양·음 방향의 요인 기여 |
| `event_aligned_trend` | 식사·카페인·회의 전후에 무엇이 관찰됐는가? | 사건 전후의 시간 정렬 추이 |
| `goal_trajectory` | 이 계획으로 주간·월간 목표를 지킬 수 있는가? | 현재 경로, 목표선, 위험 구간 |
| `decision_outcome` | 이전 조정이 실제로 도움이 됐는가? | 제안, 선택, 실행, 이후 결과 |

renderer는 같은 primitive를 플랫폼에 맞게 표현할 수 있지만 의미와 단위는
바꾸지 않는다.

현재 REST scene composer가 허용하는 module kind는 `time_series`,
`calendar_canvas`, `capacity_bar`, `comparison_bar`,
`nutrition_evidence`, `proposal_preview`다. 현재 visualization kind는 앞의
네 가지로 제한한다. 나머지 카탈로그는 versioned renderer와 authoritative
read model이 준비된 뒤 사용하는 Target UX이며 현재 API가 반환한다고 주장하지
않는다.

`distribution`, `correlation_view`, `sleep_timeline`은 향후 후보이며
WellnessScene v1 renderer가 허용하는 vocabulary에는 포함하지 않는다.

## 5. 요청·상황별 조합 규칙

### “오늘 왜 이렇게 피곤해?”

```text
짧은 설명
+ baseline_band
+ factor_contribution 또는 최근 7일 time_series 중 하나
```

원인이 아니라 현재 판단에 기여한 관찰 요인으로 표현한다.

### “언제 집중 업무를 하는 게 좋아?”

```text
energy_curve
+ Apple·Google calendar_canvas
```

빈 시간을 찾는 것만으로 끝내지 않고 목표, deadline, 고정 일정, 회복 구간을
같이 고려한다. 사용자가 후보를 선택한 뒤에는 기존 scene을
`schedule_comparison`으로 교체하며, operation/source identity가 없으면
`proposal_preview`로 교체한다.

### “이번 주 일정 괜찮아?”

```text
주간 calendar_canvas
+ capacity_bar 또는 goal_trajectory 중 하나
+ 위험 구간 annotation
```

일정 개수보다 목표를 보호할 수 있는지와 회복 부족 구간을 우선한다.

### “늦게 일어났는데 어떻게 하지?”

```text
한 문장 상황 설명
+ 오늘 calendar_canvas
+ 최소 변경 schedule_comparison 또는 proposal_preview
+ 유지 / 대안 / 적용
```

모든 일정을 다시 만드는 대신 고정 일정과 핵심 목표를 보호하는 최소 변경안을
우선한다.

### “이 식사가 오후 컨디션에 영향을 줬어?”

```text
nutrition summary
+ event_aligned_trend
+ baseline_band 또는 비슷한 식사의 comparison 중 하나
+ 표본 수와 confidence
```

단일 식사 후 변화는 인과로 단정하지 않는다.

### “카페인을 언제 마시는 게 나아?”

```text
time_series + caffeine event annotation
+ event_aligned_trend가 필요하면 time_series를 대체
+ 취침 시간 제약
```

개인 데이터가 부족하면 일반적인 안전 범위와 추가 관찰 방법만 제시한다.

### “최근 무엇이 실제로 효과 있었어?”

```text
decision_outcome
+ time_series
+ 반복 횟수와 outcome coverage
```

승인율을 효과로 취급하지 않는다. 캘린더 실행과 이후 집중·스트레스·회복 결과가
연결된 경우에만 효과 후보로 표현한다.

### HealthMes가 늦잠을 감지함

```text
기상 지연 observation
+ 오늘 Apple·Google calendar_canvas
+ 고정 일정 constraint
+ 최소 변경 schedule_comparison 또는 proposal_preview
+ notification-safe summary
+ 유지 / 캘린더 보기 / 적용
```

### HealthMes가 task 지연을 감지함

```text
현재 진행 상태
+ 다음 고정 일정까지 남은 시간
+ 남은 task 분량
+ 이동 또는 분할 proposal
+ 변경 후 목표 영향
```

### HealthMes가 회복 저하를 감지함

```text
baseline_band
+ factor_contribution 또는 오늘 calendar_canvas 중 하나
+ 조언 또는 최소 calendar proposal
```

실행 가능한 변경이 없거나 confidence가 낮으면 시각화와 짧은 조언만 제공하고
Yes/No를 만들지 않는다.

## 6. 일정 조작 문법

scene은 다음 action intent를 표현할 수 있다. 실제 실행 가능 여부는 기존
calendar gateway와 event ownership이 결정한다.

- `create_block`: 새 집중·운동·식사·회복 블록 생성
- `move_block`: HealthMes 소유 블록을 다른 시간으로 이동
- `split_block`: 긴 task를 여러 실행 블록으로 분할
- `resize_block`: 실제 필요 시간에 맞게 길이 조정
- `replace_intensity`: 고강도 활동을 가벼운 대안으로 변경
- `carry_over`: 미완료 분량을 다음 안전한 시간으로 이동
- `reserve_recovery`: 일정 사이에 회복 구간 확보
- `keep_fixed_event`: 외부 회의·약속을 고정 제약으로 보호

모든 mutation intent는 current state, proposed state, trade-off,
ownership, confirmation choice를 포함한다.

## 7. WellnessScene 제안 계약

구현 시 scene은 다음과 같은 선언형 구조를 사용한다. 구체적인 필드명은 기존
API와 renderer 계약을 검토한 뒤 versioned schema로 확정한다.

```json
{
  "schema_version": "1",
  "id": "scene:example",
  "intent": "reschedule_for_capacity",
  "lens": "coordinate",
  "title": "현재 몸 상태에 맞춘 일정 조율",
  "summary": "오늘 16시 집중 업무는 내일 오전이 더 안전합니다.",
  "severity": "action",
  "freshness": "current",
  "confidence": {
    "level": "medium",
    "coverage": "수면 7일, 일정 14일, 주관 에너지 3회",
    "limitations": ["오후 주관 에너지 표본이 적습니다."]
  },
  "modules": [
    {
      "id": "proposal-preview",
      "kind": "proposal_preview",
      "title": "승인 전 일정 블록",
      "summary": "operation과 source event identity가 없어 생성인지 이동인지 단정하지 않습니다.",
      "items": [
        {
          "id": "proposal-id",
          "label": "proposal_id",
          "value": "proposal-id",
          "detail": null
        }
      ],
      "visualization": null,
      "accessibility_summary": "exact proposal의 승인 전 블록"
    },
    {
      "id": "capacity",
      "kind": "capacity_bar",
      "title": "현재 가용 에너지",
      "summary": "엔진이 저장한 현재 값입니다.",
      "items": [],
      "visualization": null,
      "accessibility_summary": "현재 가용 에너지"
    }
  ],
  "actions": [
    {
      "id": "accept:proposal-id",
      "kind": "accept_proposal",
      "label": "적용",
      "proposal_id": "proposal-id",
      "url": null
    }
  ],
  "generated_at": "2026-08-09T12:00:00Z"
}
```

중요한 제약:

- 모든 수치는 unit과 time range를 가진다.
- 모든 예측은 confidence를 가진다.
- 모든 행동은 기존 action gateway 식별자를 가진다.
- scene 안에 credential, API token, 원시 민감 데이터 전체를 넣지 않는다.
- renderer가 지원하지 않는 component는 평문 결론과 evidence link로 fallback한다.

## 8. Insight 선택 절차

```text
1. 사용자가 실제로 결정하려는 질문을 찾는다.
2. 답에 필요한 최소 데이터만 선택한다.
3. 시점, 단위, timezone, provider coverage를 맞춘다.
4. observation과 recommendation을 분리한다.
5. 비교인지, 추이인지, 일정 충돌인지 표현 목적을 고른다.
6. 가장 적은 수의 visualization primitive를 선택한다.
7. 결론, 그래프, 근거가 서로 모순되지 않는지 검사한다.
8. confidence가 낮으면 단정 대신 부족한 데이터를 설명한다.
9. 실행 가능한 경우에만 proposal action을 붙인다.
10. 기존 decision/proposal identity를 보존한다. 판단과 사용자 선택 기록은
    planner, trigger owner, approval gateway, outcome recorder가 담당하며
    visualizer는 중복 기록하지 않는다.
```

모든 플랫폼에서 한 생성 scene의 기본 상한은 primary/supporting visualization
합계 2개다. 그 이상은 `자세히 보기`로 이동한다. 정적 Web dashboard는 생성
scene과 별개로 calendar, 목표, 주간 추이 같은 여러 읽기 전용 section을 제공할
수 있다.

## 9. 안전성과 정직성

- 의료적 정상 범위를 개인 baseline과 혼동하지 않는다.
- 색상만으로 좋음·나쁨을 전달하지 않는다.
- 축을 잘라 작은 변화를 과장하지 않는다.
- 서로 다른 기간의 값을 동일 비교로 표현하지 않는다.
- missing data를 0으로 채우지 않는다.
- 모델이 만든 설명과 실제 engine component가 다르면 scene을 거절한다.
- 낮은 confidence에서는 calendar 자동 변경을 제안하지 않고 확인 질문 또는
  보수적 대안을 우선한다.
- `correlation_view`는 최소 표본 기준을 충족하지 못하면 렌더링하지 않는다.
- 민감한 건강·식사·생리 데이터는 잠금화면과 Watch 첫 화면에서 일반화한다.

## 10. 플랫폼별 UX

### iPhone

- 오늘 calendar canvas와 지금 필요한 결정을 첫 화면에 둔다.
- 질문 결과는 기존 화면 위에 ephemeral scene으로 나타난다.
- 결론, 핵심 시각화 최대 2개, 한 가지 행동을 우선한다.
- 상세 그래프는 세로 스크롤과 `자세히 보기`로 연다.

### Mac

- iPhone과 같은 core capability를 제공한다.
- 넓은 화면에서는 calendar와 insight inspector를 나란히 배치한다.
- 음성·텍스트 command 결과를 keyboard-first로 조작한다.

### Web

- 장기 추이, 주간·월간 목표, 판단 결과를 검토한다.
- 생성 scene은 calendar canvas를 포함해 최대 2개 visualization을 제공한다.
- 정적 dashboard는 여러 읽기 전용 section을 한 페이지에서 검토할 수 있다.
- 원시 데이터, provider 상태, 진단은 Advanced에 둔다.
- 현재 계약대로 read-only이며 쓰기는 Apple 앱으로 보낸다.

### Watch

- 전체 그래프를 작은 화면에 복제하지 않는다.
- 결론, 일정 변경 전후, 핵심 근거 한 개, Yes/No를 표시한다.
- 더 긴 trend와 이유는 iPhone 또는 web deep link로 넘긴다.

## 11. 스킬 패키징 상태

`skills/healthmes-wellness-visualizer/SKILL.md`를 추가했다. 스킬은 다음을
포함한다.

- 사용 시점과 사용하지 말아야 할 시점
- 사용자 질문과 proactive trigger의 공통 scene 생성 규칙
- 질문 유형별 필요한 MCP/read model
- visualization primitive 선택 절차
- confidence와 insufficient-data 처리
- calendar proposal의 propose-then-confirm 규칙
- decision record와 evidence URL 기록
- 플랫폼별 component 예산
- MCP 도구명, fail-closed 문구, 승인 경계를 검증하는 실행형 문서 계약 테스트

현재는 fixture/golden scene snapshot이 아니라 schema validator, API 회귀
테스트, 문서 계약 테스트가 구현되어 있다. golden scene fixture는 renderer가
안정화된 뒤 추가할 후속 항목이다.

스킬 스크립트가 REST를 직접 호출해서는 안 된다. Hermes 기반 실행에서는 기존
MCP를 사용하고, native iPhone·Mac·Watch는 인증된 REST read/action 계약을
사용한다. 어느 표면에서도 visualizer가 approval token을 소유하거나 판단
기록을 중복 생성하지 않으며, UI는 versioned WellnessScene만 소비한다.

## 12. 구현 경계

### 이번 PR에서 구현

- `WellnessScene v1` schema, 서버 composer, fail-closed proposal correlation
- Apple·Google mirror의 공통 display model
- Web renderer와 native renderer가 소비하는 공통 scene 의미
- voice/text 질문에서 scene을 요청하는 native command surface
- 기존 native proposal REST resolution과의 연결
- visualization skill 계약, schema validator, API·문서 회귀 테스트

### 후속 작업

- planner가 operation과 source event identity를 반환하는 계약
- operation/source event identity 기반 `schedule_comparison` renderer
- 실제 proactive trigger가 같은 composer를 호출하는 runtime wiring
- apply receipt와 장기 decision outcome을 scene 입력으로 되돌리는 학습 loop
- 원본 provider color와 deep link를 위한 calendar mirror 확장
- 실제 Apple·Google Calendar와 HealthKit을 연결한 실기기 Live QA

## 13. 전체 제품 구현 순서

1. 현재 read model과 MCP 출력으로 만들 수 있는 primitive와 action을 확정한다.
2. `WellnessScene v1` JSON schema와 validator를 정의한다.
3. Apple·Google event fidelity와 ownership을 포함한 display model을 만든다.
4. Web renderer에서 calendar, capacity, trend, comparison을 먼저 구현한다.
5. iPhone과 Mac에 동일 schema renderer를 구현한다.
6. Watch와 알림용 semantic reduction 규칙을 구현한다.
7. visualization skill과 질문·trigger별 fixture를 추가한다.
8. voice/text command 결과를 scene composer에 연결한다.
9. proactive trigger 결과도 같은 scene composer에 연결한다.
10. proposal action은 기존 confirmation gateway에만 연결한다.
11. decision outcome을 scene 입력으로 되돌리는 학습 loop를 연결한다.
12. 실제 Apple·Google Calendar와 HealthKit 데이터로 live QA한다.

## 14. 최종 제품 완료 기준

- 사용자가 기능 메뉴를 배우지 않고 질문으로 원하는 insight를 얻는다.
- 사용자가 묻지 않아도 중요한 상태 변화에는 HealthMes가 먼저 한 가지 제안을
  보낸다.
- 모든 일정 관련 insight가 실제 Apple·Google Calendar 맥락을 보여준다.
- 일정 생성, 이동, 분할, 길이 조정, 강도 변경, 미완료 이월, 회복 확보를
  ownership과 승인 경계 안에서 표현한다.
- 동일 데이터를 질문에 따라 bar, trend, baseline, calendar comparison 등으로
  적절하게 표현한다.
- 모든 scene에 평문 결론, confidence, 데이터 한계가 존재한다.
- 데이터가 부족하면 시각화를 만들어내지 않고 정직한 fallback을 제공한다.
- iPhone, Mac, Web이 같은 schema 의미를 공유한다.
- Watch가 긴 문장을 자르지 않고 의미 단위로 축약한다.
- 일정 변경은 사용자의 명시적 승인 후 기존 calendar gateway로만 실행된다.
- 이전 결정과 실제 결과를 연결해 다음 시각적 insight에 재사용할 수 있다.
