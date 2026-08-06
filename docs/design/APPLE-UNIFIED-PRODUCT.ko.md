# HealthMes Apple 제품과 웹 Control Surface UX

> 상태: issue #108, PR #111의 Apple 통합 제품 위에 적용하는 최종 UX 계약
>
> 이번 구현 범위: iPhone, macOS, watchOS, 웹 presentation layer
>
> 변경 금지 경계: HealthMes 엔진, MCP, 캘린더 worker·쓰기 계약, 인증 계약,
> `vendor/hermes-agent/`

## TL;DR

HealthMes는 Todo 앱이나 건강 점수판이 아니다. 사용자의 몸 상태가 오늘 계획에
어떤 영향을 주는지 설명하고, 한 번에 한 가지 조정안을 제시하며, 사용자가
승인한 뒤 실제 결과까지 학습하는 wellness control surface다.

```text
┌─────────────────────────────────────────────────────────┐
│ 고정 HealthMes Shell                                     │
│ 날짜 · 데이터 시점 · 읽기/실행 가능 범위                 │
├─────────────────────────────────────────────────────────┤
│ [지금]              [조율]              [변화]           │
│ 같은 canvas의 관점만 전환하고 페이지를 떠나지 않음       │
├─────────────────────────────────────────────────────────┤
│ Bounded Wellness Canvas                                  │
│ 상태 → 계획 영향 → 제안 → 승인 → 결과 → 학습            │
├─────────────────────────────────────────────────────────┤
│ 고정 Voice + Text Command Dock                           │
│ 웹: 읽기 전용 안내 / iPhone·Mac: 실제 명령 실행          │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
                Advanced에서 긴 상세 열기
```

웹은 기존 read model만 표시한다. 웹에서 새로운 일정 조정안을 만들거나,
Yes/No를 실행하거나, 캘린더를 변경하지 않는다. 음성·텍스트 명령과 쓰기
행동은 iPhone/Mac 앱에서 실행한다.

## 1. 능력 경계와 런타임 책임

### HealthMes 엔진

- 건강·인지에너지·일정·목표 데이터를 계산한다.
- proposal, decision, insight, report를 기존 계약대로 만든다.
- 이번 UI 작업에서 계산 규칙과 상태 전이는 변경하지 않는다.

### 웹 presentation layer

- 기존 `DashboardView`의 값을 wellness 장면으로 재배열한다.
- 건강과 계획을 별도 목록이 아니라 같은 인과 흐름으로 보여준다.
- 데이터가 없으면 숨기거나 부족하다고 명시한다.
- 임의의 건강 원인, 캘린더 성공 상태, 제안 효과를 생성하지 않는다.
- 모든 쓰기 행동은 Apple 앱으로 돌려보낸다.

### Apple 앱

- iPhone과 Mac은 동일한 core command surface를 제공한다.
- 음성과 텍스트를 같은 command pipeline으로 처리한다.
- proposal 승인·거절·수정 전에는 명시적인 확인 장면을 거친다.
- Watch는 pending proposal과 짧은 wellness glance만 제공한다.

### Web Advanced

- 긴 판단 트리, 주간 리포트, 수면 원본, 연결 상태를 연다.
- 연결·스토리지·진단과 같은 운영 정보는 기본 canvas에서 숨긴다.
- 기존 URL과 viewer token 링크 계약을 유지한다.

## 2. 폐기하는 정보 구조

다음 구조는 기능 이름을 탐색하는 일반 productivity 앱처럼 느껴지므로
top-level 제품 구조에서 폐기한다.

```text
Today / Plan / Decisions / Speak / Settings
```

문제점:

- 사용자는 자신의 목적보다 기능 분류를 먼저 이해해야 한다.
- 건강 상태와 일정 영향이 서로 다른 페이지로 갈라진다.
- Speak가 별도 페이지가 되면 명령 중 현재 맥락을 잃는다.
- Decisions는 사용자의 목적이 아니라 내부 데이터 타입이다.
- 페이지 이동이 반복되면 3초 안에 판단하는 리모컨 철학과 충돌한다.

기존 `/dashboard/plan`, `/dashboard/decisions`, `/dashboard/history` route는
삭제하지 않는다. 북마크와 Apple 앱 deep link 호환을 위해 유지하되, 모두 같은
control canvas를 렌더링하고 해당 lens를 처음 선택한다.

## 3. 최종 제품 언어

사용자에게 노출하는 세 관점은 다음과 같다.

| Lens | 사용자의 질문 | 기본 출력 |
|---|---|---|
| `지금` | 내 몸 상태가 오늘 계획에 어떤 영향을 주는가? | 현재 상태, 신뢰도, 다음 일정, pending proposal |
| `조율` | 건강과 목표를 함께 지키려면 무엇을 바꿀 수 있는가? | 정확한 조정안, 시간, 제약, 목표, 7일 일정 |
| `변화` | 이전 결정이 실제로 도움이 되었는가? | 주간 결과, 관찰된 패턴, 학습 루프 |

Lens는 route나 독립 페이지가 아니다. 같은 snapshot과 같은 canvas를 보는
관점이다. Voice/Text command dock, 현재 proposal, 상세 presentation 상태는
lens를 바꿔도 사라지지 않아야 한다.

제품의 일관된 문장 구조:

```text
현재 상태
→ 오늘·주간 목표에 미치는 영향
→ 제안하는 한 가지 개입
→ 보호할 제약과 trade-off
→ Yes / No / Modify
→ 캘린더 적용 결과
→ 이후 건강·집중 결과
```

## 4. Bounded Generative UI

HealthMes는 LLM이 임의 SwiftUI나 HTML을 생성하는 방식을 사용하지 않는다.
모델 또는 deterministic composer는 허용된 scene schema를 선택하고, 각
플랫폼은 신뢰된 native/web component catalog만 렌더링한다.

```text
Voice / Text
→ intent
→ health · calendar · goal context
→ deterministic wellness calculation
→ bounded scene schema
→ schema · safety validation
→ trusted platform renderer
→ explicit confirmation
→ existing action gateway
```

이번 웹 slice는 별도의 scene API를 추가하지 않는다. 기존 `DashboardView`를
템플릿에서 다음 primitive로 투영한다.

| Primitive | 목적 | 사용할 수 있는 기존 데이터 |
|---|---|---|
| `wellness_state` | 현재 상태와 신뢰도 | headline, energy, alerts |
| `impact_flow` | 상태에서 계획 영향까지 연결 | headline, next blocks, pending proposal |
| `schedule_timeline` | 다음 일정과 7일 계획 | next blocks, plan events |
| `decision_remote` | 우선 확인할 proposal | task, start/end, expiry, decision URL |
| `goal_progress` | 조정 시 보호할 목표 | weekly goals와 task count |
| `decision_history` | 최근 판단 근거 | recent decisions |
| `outcome_summary` | 주간 결과 | weekly report |
| `insight_list` | 관찰된 개인 패턴 | recent insights |
| `learning_loop` | 상태·행동·결과의 제품 해자 설명 | 저장 가능 범위에 대한 정적 UX |

### Fail-closed 규칙

- proposal의 정확한 쓰기 token과 action gateway가 없는 웹에서는 Yes/No 버튼을
  만들지 않는다.
- 에너지나 알림이 없으면 건강 원인을 추측하지 않는다.
- 캘린더 mirror에 보인다는 사실을 외부 캘린더 쓰기 성공으로 표현하지 않는다.
- `accepted`와 실제 외부 캘린더 반영 완료인 `pushed`를 같은 상태로 표현하지
  않는다.
- outcome 데이터가 부족하면 효과를 단정하지 않고 `모름`으로 남긴다.
- 향후 알 수 없는 primitive/schema version은 임의 렌더링하지 않고
  `insufficient_data` fallback으로 닫는다.

## 5. 웹 Control Surface

### 5.1 고정 Shell

상단에는 기능 메뉴 대신 제품 상태만 둔다.

- 로컬 날짜와 timezone
- dashboard 생성 시점
- `웹 읽기 전용` capability 표시
- `지금 / 조율 / 변화` lens selector

기존 사이트 전역의 `Dashboard / Decisions / History / Actual sleep /
Settings` 메뉴는 웹 dashboard에서 숨긴다. 같은 목적지는 `Advanced`에서
필요할 때만 연다.

### 5.2 지금

첫 장면은 건강 점수보다 해석 가능한 한 문장을 우선한다.

```text
현재 상태
에너지를 아껴 쓸 구간을 확인하세요.

상태              계획 영향                 다음 제어
에너지 54     →   Deep Work 조정 대기   →   Apple 앱에서 Yes/No
```

그 아래에는 `다음 일정`과 `결정 대기`만 둔다. 원시 수치, 긴 일정 목록,
주간 리포트는 첫 화면에 넣지 않는다.

### 5.3 조율

조율은 생성된 proposal이 있을 때만 구체적 행동을 보여준다.

- 어떤 task를 조정하는가
- 제안 시작·종료 시각
- proposal 만료 시각
- 단일 proposal이며 사용자 승인이 필요하다는 제약
- 주간 목표 진행
- HealthMes에 저장된 7일 calendar mirror
- 판단 근거 detail URL

웹은 읽기 전용이므로 승인·거절 버튼을 만들지 않는다. Apple 앱에서 같은
proposal을 처리해야 한다는 capability boundary를 action bar에 명시한다.

### 5.4 변화

변화는 단순 history 목록이 아니라 다음 질문에 답해야 한다.

- 이번 주 평균 에너지는 어땠는가
- 제안을 얼마나 수용했는가
- 판단 기록이 얼마나 쌓였는가
- 최근 관찰된 패턴은 무엇인가
- 해당 결과를 확정적으로 말할 수 있는가

수용률은 효과가 아니다. 주간 결과의 집계 범위와 데이터 한계를 접힌 설명으로
제공하고, 인과관계를 만들지 않는다.

### 5.5 Persistent Voice + Text Command Dock

Command dock은 lens와 무관하게 화면 아래에 유지한다.

```text
[voice]  오늘 오후 집중 시간을 회복 상태에 맞춰 조율해줘  [실행]
         웹은 읽기 전용 · 실행은 iPhone 또는 Mac 앱
```

웹 slice에서는 input과 버튼을 시각적으로만 제공하고 `readonly/disabled`로
막는다. form, mutation endpoint, 숨은 API 호출을 추가하지 않는다.

Apple 앱의 최종 동작:

- 음성과 텍스트가 하나의 command pipeline으로 들어간다.
- 무한 chat transcript를 만들지 않는다.
- 결과는 카드 하나로 고정하지 않고 timeline, comparison, constraint,
  clarification, confirmation 등 필요한 bounded scene으로 교체된다.
- 지원하지 않는 명령은 추측 실행하지 않고 clarification 또는 unsupported
  scene으로 끝난다.

## 6. Progressive Disclosure

기본 canvas에서 숨기는 항목:

- 전체 건강 원시 수치
- 긴 판단 트리
- confidence 계산 과정
- model provider와 token
- server URL
- storage retention
- diagnostics와 export/restore
- 연결 credential

`Advanced`의 기본 상태는 닫힘이다. 내부에서만 다음 정본 URL을 제공한다.

- `/connect`
- `/sleep`
- `/decisions`
- `/reports/weekly`

URL에는 기존 `app_path()` base path와 읽기 전용 `token_qs`를 보존한다.
외부 decision/report URL도 기존 viewer URL 생성 규칙을 그대로 사용한다.

## 7. Route와 접근성 계약

### Route 호환

| 기존 진입점 | 선택할 Lens | 유지할 fragment/ID |
|---|---|---|
| `/dashboard` | 지금 | `#today`, `id="today"` |
| `/dashboard/plan` | 조율 | `#plan`, `id="plan"` |
| `/dashboard/decisions` | 조율 | `#decisions`, `id="decisions"` |
| `/dashboard/history` | 변화 | `#history`, `id="history"` |

Lens 클릭은 JavaScript가 같은 document의 panel만 교체하고 History API로
호환 URL을 기록한다. JavaScript가 없으면 세 scene을 문서 순서대로 모두
표시하므로 내용에 접근할 수 있다.

### 접근성

- lens는 `tablist / tab / tabpanel` 의미를 사용한다.
- `aria-selected`, `aria-controls`, `aria-labelledby`를 동기화한다.
- 좌우 방향키로 lens를 전환한다.
- panel 상태를 색상만으로 구분하지 않는다.
- 320pt 폭에서 timeline을 한 열로 바꾸고 command dock은 입력 중심으로 줄인다.
- `prefers-reduced-motion`은 공통 shell 정책을 따른다.
- readonly input과 disabled controls에는 실행 불가능한 이유를 텍스트로
  반복해서 제공한다.

## 8. Apple 표면과의 일관성

### iPhone과 Mac

모든 core capability를 공유한다.

- 현재 wellness state
- plan impact
- proposal와 대안 비교
- Yes / No / Modify
- calendar application receipt
- outcome 확인
- persistent voice + text command dock
- Settings와 Advanced

iPhone 추가 기능은 HealthKit, 잠금화면 알림, Live Activity, 카메라, Watch
연결이다. Mac 추가 기능은 메뉴바 glance, 키보드 호출, 넓은 inspector,
컴퓨터 사용 맥락이다.

### Watch

Watch는 예외적으로 bounded subset만 제공한다.

- 첫 화면: 건강 이유 한 줄, 정확한 행동과 시간, Yes/No
- Why: 짧은 근거
- 제안 없음: 에너지·회복과 다음 일정 영향
- 결과: accepted, pushed, declined, expired, offline

Watch에서 긴 목표 관리, 전체 calendar, raw data, Advanced를 제공하지 않는다.

## 9. 구현 및 QA 완료 조건

### 웹 구현

- 페이지형 dashboard tabs를 제거한다.
- 같은 page 안에 `지금 / 조율 / 변화` lens를 제공한다.
- `health → plan impact`를 첫 scene의 중심 흐름으로 만든다.
- 허용된 bounded primitive에 `data-ui-primitive` 계약을 부여한다.
- voice/text command dock을 모든 lens 아래에 고정한다.
- 웹이 read-only이며 Apple 앱에서 실행한다는 설명을 항상 표시한다.
- `Advanced`는 기본으로 닫는다.
- 기존 route, fragment, base path, viewer token 링크를 보존한다.

### 회귀 검증

- seed dashboard가 목표, proposal, 일정, insight, decision detail을 모두
  렌더링한다.
- 빈 dashboard가 데이터를 생성하지 않고 honest empty state를 표시한다.
- reverse proxy base path가 lens와 Advanced 링크에 유지된다.
- authenticated viewer URL은 파생 viewer token만 사용하고 API token을
  노출하지 않는다.
- command dock에는 form이나 mutation 동작이 없다.
- `/dashboard/plan`, `/dashboard/decisions`, `/dashboard/history`가 모두 같은
  control surface를 반환한다.

## 10. 참고한 설계 패턴

이번 설계는 특정 라이브러리를 런타임 의존성으로 추가하지 않고 다음 패턴만
차용한다.

- Raycast: 하나의 root command surface와 맥락을 유지하는 action model
- Google A2UI: 선언형 UI schema, trusted component catalog, incremental scene
- Google Natively Adaptive Interfaces: 고정 shell 안에서 상황에 맞는 module
  구성
- JITAI 연구 프레임: decision point, tailoring variable, intervention option,
  proximal outcome, distal outcome
- assistant-ui, CopilotKit, Vercel AI SDK: structured tool result와 approval
  surface 패턴

채택하지 않는 것:

- chat transcript를 제품의 기본 정보 구조로 사용
- 모델이 임의 HTML/SwiftUI를 생성
- component catalog 밖의 action 실행
- 모델 출력만으로 건강·캘린더 쓰기 상태를 확정

## 11. 제품 성공 기준

사용자는 기본 화면에서 10초 안에 다음 세 질문에 답할 수 있어야 한다.

1. 지금 내 몸 상태는 어떠한가?
2. 그 상태가 오늘 계획에 어떤 영향을 주는가?
3. 내가 지금 결정해야 할 한 가지는 무엇인가?

그리고 일주일 뒤에는 다음 질문에 답할 수 있어야 한다.

> HealthMes의 제안을 따른 결과, 내 회복과 목표 달성이 실제로 나아졌는가?

이 질문에 답하는 장기 `상태 → 판단 → 행동 → 결과` 연결이 HealthMes의 핵심
제품 해자다.
