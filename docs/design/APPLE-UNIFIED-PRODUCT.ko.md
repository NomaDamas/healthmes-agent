# HealthMes Apple 제품과 웹 Control Surface UX

> 상태: issue #108, PR #111의 현재 구현과 후속 Target UX를 함께 구분한 계약
>
> 이번 구현 범위: iPhone, macOS, watchOS, 웹 presentation layer
>
> 변경 금지 경계: HealthMes 엔진, MCP, 캘린더 worker·쓰기 계약, 인증 계약,
> `vendor/hermes-agent/`

> 후속 Target UX: 채널·카테고리·커스터마이징 canvas·Slack 방식 thread를
> 사용하는 client-only Workspace 전환은
> `docs/design/CHANNEL-WORKSPACE-CLIENT-UX.ko.md`를 우선 계약으로 따른다.

## TL;DR

HealthMes는 Todo 앱이나 건강 점수판이 아니다. 사용자의 몸 상태가 오늘 계획에
어떤 영향을 주는지 설명하고, 한 번에 한 가지 조정안을 제시하며, 사용자가
승인한 뒤 실제 결과까지 학습하는 wellness control surface다.

```text
┌─────────────────────────────────────────────────────────┐
│ 고정 HealthMes Shell                                     │
│ 날짜 · 데이터 시점 · 읽기/실행 가능 범위                 │
├─────────────────────────────────────────────────────────┤
│ Bounded Wellness Canvas                                  │
│ 현재 상태 → 계획 영향 → 필요한 한 가지 행동             │
├─────────────────────────────────────────────────────────┤
│ 고정 Voice + Text Command Dock                           │
│ 웹: 읽기 전용 안내 / iPhone·Mac: 실제 명령 실행          │
└─────────────────────────────────────────────────────────┘
                         │ 전체 보기
                         ▼
       일정·목표 / 결정 결과 / 웹 Dashboard / Advanced
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
하나의 Wellness Canvas / 고정 Voice+Text Dock / Settings+Advanced
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

iPhone과 Mac의 기본 화면은 분류를 고르게 하지 않는다. 앱을 열면 언제나
다음 질문에 바로 답한다.

> 내 몸 상태가 오늘 계획에 어떤 영향을 주며, 지금 바꿔야 할 한 가지가 있는가?

기본 출력은 현재 상태, 신뢰도, 오늘 일정 영향, 다음 보호 일정, pending
proposal이다. 상단에 고정된 `지금 / 조율 / 변화` 세그먼트는 사용자가
HealthMes 내부 분류를 먼저 학습하게 하므로 Apple 앱에서 제거한다.

`전체 보기`는 주 흐름이 아니라 필요할 때만 여는 보조 메뉴다.

| 상세 목적지 | 사용자의 질문 | 출력 |
|---|---|---|
| 현재 영향 | 몸 상태가 오늘 계획에 어떤 영향을 주는가? | 현재 상태, 다음 일정, pending proposal |
| 일정과 목표 | 무엇을 보호하고 어떤 제약을 확인해야 하는가? | 목표, task, mirrored calendar |
| 결정 결과 | 이전 결정이 실제로 적용되고 도움이 되었는가? | 승인, 적용, 기록, 결과 부족 상태 |
| 웹 대시보드 | 더 긴 기간과 근거를 검토하고 싶은가? | 주간·월간 변화, 판단 상세, Advanced |

내부 `WellnessLens`는 기존 command와 deep-link 호환을 위해 유지할 수 있지만
Apple 앱의 고정 탐색 UI로 노출하지 않는다. 웹은 읽기 전용 분석 면적이
충분하므로 같은 문서 안의 lens를 계속 사용할 수 있다.

제품의 일관된 문장 구조:

```text
현재 상태
→ 오늘·주간 목표에 미치는 영향
→ 제안하는 한 가지 개입
→ 보호할 제약과 trade-off
→ 유지 / 변경 승인 / 대안 확인
→ 캘린더 적용 결과
→ 이후 건강·집중 결과
```

### 3.1 두 가지 진입 방식

HealthMes의 제품 루프는 사용자가 질문하는 경우에만 시작되지 않는다. 같은
wellness 판단과 UI 계약을 두 가지 방식으로 실행한다.

#### 사용자 시작형

```text
사용자 음성·텍스트 질문
→ 건강·일정·목표·행동 데이터 조합
→ wellness insight
→ 텍스트·시각화·실제 캘린더
→ 필요한 경우 일정 변경안
→ 유지 / 대안 / 승인
→ Apple·Google Calendar 반영
```

예: `오늘 일정 조정해줘`, `이번 주 운동 세 번 배치해줘`,
`왜 이렇게 피곤해?`, `집중 업무는 언제 하는 게 좋아?`

#### HealthMes 시작형

```text
늦잠 · 일정 지연 · 회복 저하 · 스트레스 상승 · 목표 위험 감지
→ 현재 Apple·Google Calendar 재조회
→ 고정 일정과 이동 가능한 일정 구분
→ 건강·에너지·목표 제약 재계산
→ 가장 작은 안전한 개입 한 가지 생성
→ iPhone · Watch · Mac 알림
→ 유지 / 대안 / 승인
→ 캘린더 반영
→ 실제 결과 기록과 다음 판단 학습
```

선제형 개입은 일반 메시지를 많이 보내는 기능이 아니다. 사용자가 지금
결정해야 하고 실제 행동이나 일정에 영향을 줄 수 있을 때만 보낸다. 조언만
필요하면 짧은 설명을 보내고, 실행 가능한 변경이 있을 때만 Yes/No 또는
`유지 / 대안 / 적용` action을 제공한다.

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

생성형 UI의 기본 좌표계는 실제 캘린더다. Apple Calendar와 Google Calendar의
mirrored event를 같은 시간축에 놓고, HealthMes가 계산한 가용 에너지, 일정
요구량, 회복 구간, 목표 위험, pending proposal을 overlay한다. v1은 provider
identity를 보존하고 HealthMes의 semantic color로 구분한다. 원본 캘린더
색상은 현재 mirror에 없으므로 보존했다고 주장하지 않는다.

```text
Apple Health · wearable · usage · nutrition
                         │
                         ▼
              wellness state and forecast
                         │
Apple Calendar ──────────┼────────── Google Calendar
                         │
                         ▼
       calendar + energy + goal + proposal canvas
                         │
                         ▼
       explanation / alternative / explicit approval
                         │
                         ▼
              existing calendar action gateway
```

현재 PR은 bearer 인증이 필요한 `POST /v1/wellness/scenes` presentation API를
추가한다. 이 API는 엔진을 재구현하지 않고 기존 `DashboardView`와 exact active
proposal을 bounded scene으로 투영한다. 별도로 정적 웹 dashboard는
`DashboardView`를 다음 primitive로 렌더링한다.

| Primitive | 목적 | 사용할 수 있는 기존 데이터 |
|---|---|---|
| `wellness_state` | 현재 상태와 데이터 최신성 | headline, energy, alerts |
| `impact_flow` | 상태에서 계획 영향까지 연결 | headline, next blocks, pending proposal |
| `calendar_canvas` | Apple·Google 실제 일정과 wellness overlay | mirrored calendar, provider, event identity, proposal |
| `schedule_timeline` | 다음 일정과 7일 계획 | next blocks, plan events |
| `proposal_preview` | exact proposal의 승인 전 블록 표시 | proposal ID, task, proposed start/end |
| `capacity_bar` | 가용 에너지와 일정 부하 비교 | energy score, demand, confidence |
| `time_series` | 수면·에너지·스트레스 등의 시간 추이 | timestamped read model |
| `factor_contribution` | 판단에 사용된 요인의 방향과 크기 | decision inputs, engine components |
| `baseline_band` | 개인 baseline 대비 현재 상태 | baseline, current value, coverage |
| `comparison_bar` | 현재 주간 목표 진행 | weekly goals와 현재 task 완료율 |
| `event_aligned_trend` | 식사·카페인·회의 전후의 관찰 변화 | events plus timestamped outcomes |
| `decision_remote` | 우선 확인할 proposal | task, start/end, expiry, decision URL |
| `goal_progress` | 조정 시 보호할 목표 | weekly goals와 task count |
| `decision_history` | 최근 판단 근거 | recent decisions |
| `outcome_summary` | 주간 결과 | weekly report |
| `insight_list` | 관찰된 개인 패턴 | recent insights |
| `learning_loop` | 상태·행동·결과의 제품 해자 설명 | 저장 가능 범위에 대한 정적 UX |

현재 scene API의 module kind는 `time_series`, `calendar_canvas`,
`capacity_bar`, `comparison_bar`, `nutrition_evidence`,
`proposal_preview`로 제한한다. `proposal_preview`는 operation과 source event
identity가 없을 때 사용하는 비시각 module이며 `visualization`은 `null`이다.
실제 before/after와 operation identity를 가진 후속 계약에서만
`schedule_comparison`을 허용한다.

`calendar_canvas`는 일반적인 일정 목록이 아니다. 일정 블록마다 출처, 시간,
이동 가능 여부, 예상 에너지 요구량을 표시하고, 시간대별 가용 에너지를 배경
곡선이나 heat band로 겹친다. 요구량이 가용량을 넘는 구간만 경고하고 모든
일정에 건강 라벨을 붙이지 않는다.

### 4.1 Calendar fidelity와 조작 범위

Apple·Google event를 표시할 때 v1 read model이 실제로 가진 다음 정보를
보존한다.

- 일정 제목과 시작·종료 시각
- provider와 외부 event identity
- 반복 일정과 종일 일정
- 참석자 존재 여부, organizer ownership, 잠금 상태
- HealthMes가 만든 event인지 여부

캘린더별 원본 색상, 장소, 화상회의 링크, 업무 위치, 개별 RSVP, 알림, 메모,
provider deep link는 현재 mirror에 없으므로 완료된 기능으로 표시하지 않는다.
후속 read-model 확장 뒤 optional detail로 추가한다. HealthMes는 원본 앱을
복제하거나 대체하지 않고 wellness 판단과 재조율을 추가한다.

HealthMes가 제안할 수 있는 일정 행동:

- 새 집중·운동·식사·회복 블록 생성
- HealthMes가 소유한 블록 이동
- 긴 task를 여러 블록으로 분할
- 고강도 계획을 가벼운 대안으로 변경
- 미완료 분량을 다음 안전한 시간으로 이동
- 목표 deadline을 지키도록 주간 후보 시간을 재배치
- 회복 시간을 확보하면서 고정 일정을 유지

실행 권한 경계:

- pending 변경은 실제 event와 구분되는 점선 preview로 표시한다.
- 사용자가 승인하기 전에는 외부 캘린더를 변경하지 않는다.
- HealthMes가 소유하지 않은 외부 회의와 약속은 기본적으로 고정 제약이다.
- 기존 calendar action gateway가 명시적으로 허용하는 event만 수정한다.
- 외부 event를 수정할 수 없으면 주변 HealthMes 블록을 재배치하거나 사용자에게
  원본 캘린더에서 수정하도록 안내한다.
- `accepted`와 실제 provider 반영 완료인 `pushed`를 별도로 표시한다.

생성형 UI는 항상 카드 하나를 만드는 것이 아니라 질문에 가장 적합한
primitive 조합을 선택한다. 자세한 선택 규칙과 scene 계약은
[`WELLNESS-VISUALIZATION-SKILL.ko.md`](./WELLNESS-VISUALIZATION-SKILL.ko.md)를
정본으로 사용한다.

### 4.2 텍스트와 시각화의 역할

- 모든 scene은 먼저 한두 문장의 평문 결론을 제공한다.
- 수치의 비교가 핵심이면 bar, baseline band, comparison을 사용한다.
- 시간 변화가 핵심이면 line/area trend와 event annotation을 사용한다.
- 일정 변경이 핵심이면 실제 캘린더의 before/after를 사용한다.
- 패턴이 핵심이면 표본 수와 confidence를 함께 표시한 관찰 그래프를 사용한다.
- 데이터가 부족하거나 단순 답변이 더 명확하면 차트를 억지로 만들지 않는다.
- 차트만으로 의학적 진단, 원인 관계, 미래 결과를 단정하지 않는다.

### 4.3 플랫폼별 축약

- iPhone, Mac, Web의 생성 scene은 핵심 시각화를 최대 2개만 제공한다.
- 정적 Web dashboard는 생성 scene과 별개로 calendar, 목표, 주간 추이 같은 여러
  읽기 전용 section을 한 페이지에 제공할 수 있다.
- Watch는 전체 차트를 축소 복제하지 않는다. 결론, 정확한 일정 변경,
  핵심 근거 한 개, Yes/No만 표시하고 상세는 iPhone 또는 웹으로 넘긴다.
- 알림은 가장 작은 실행 단위만 제공하고, `캘린더에서 보기` 또는 `왜?`를 통해
  확장한다.

### Fail-closed 규칙

- proposal의 정확한 쓰기 token과 action gateway가 없는 웹에서는 Yes/No 버튼을
  만들지 않는다.
- 에너지나 알림이 없으면 건강 원인을 추측하지 않는다.
- 캘린더 mirror에 보인다는 사실을 외부 캘린더 쓰기 성공으로 표현하지 않는다.
- `accepted`와 실제 외부 캘린더 반영 완료인 `pushed`를 같은 상태로 표현하지
  않는다.
- outcome 데이터가 부족하면 효과를 단정하지 않고 `모름`으로 남긴다.
- 상관관계를 원인으로 표현하지 않는다. 관찰 기간, 표본 수, confidence를 함께
  표시한다.
- 서로 다른 단위의 값을 같은 축에 놓아 비교 가능한 것처럼 표현하지 않는다.
- calendar mirror에 없는 이벤트나 provider 상태를 생성하지 않는다.
- 시각화 스킬은 원시 데이터를 직접 조회하거나 엔진 계산을 재구현하지 않는다.
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
현재 저장된 에너지 점수는 중간 구간입니다.

상태              계획 영향                 다음 제어
에너지 54     →   Deep Work 조정 대기   →   Apple 앱에서 Yes/No
```

그 아래에는 Apple·Google Calendar를 합친 오늘 calendar canvas와 `결정 대기`를
둔다. calendar canvas는 가용 에너지 곡선과 일정 요구량의 충돌만 강조한다.
원시 수치, 긴 일정 목록, 주간 리포트는 첫 화면에 넣지 않는다.

### 5.3 조율

조율은 생성된 proposal이 있을 때만 구체적 행동을 보여준다.

- 어떤 task를 조정하는가
- 제안 시작·종료 시각
- proposal 만료 시각
- 단일 proposal이며 사용자 승인이 필요하다는 제약
- 주간 목표 진행
- HealthMes에 저장된 Apple·Google 7일 calendar mirror
- proposal ID와 제안 start/end를 보존한 승인 전 preview
- 판단 근거 detail URL

현재 proposal contract에는 operation과 source event identity가 없으므로 웹은
가짜 before 상태나 이동 화살표를 만들지 않는다. 실제 before/after calendar는
후속 proposal 계약이 해당 identity를 제공할 때만 표시한다.

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
제공하고, 인과관계를 만들지 않는다. 질문에 따라 trend, baseline comparison,
event-aligned chart, goal trajectory를 bounded primitive로 조합한다.

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

### 5.6 선제적 재조율 장면

선제적 판단은 기본 dashboard에 조용히 쌓아두는 것으로 끝나지 않는다. 즉시
결정 가치가 있는 경우 알림, Live Activity, Watch remote로 전달한다.

지원할 대표 상황:

- 계획보다 늦은 기상으로 오전 일정이 겹침
- 시작한 task가 예상보다 늦어 다음 일정과 충돌
- 수면·회복·스트레스 상태가 개인 baseline보다 유의하게 악화
- 회의나 외부 일정 변경으로 기존 계획이 불가능해짐
- Screen Time 또는 앱 전환 증가로 집중 블록 완료 가능성이 낮아짐
- 식사 누락이나 늦은 식사가 예정된 운동·수면과 충돌
- 미완료 task 누적으로 주간 핵심 목표가 위험해짐

예:

```text
오전 계획을 다시 맞출까요?

기상 시간이 80분 늦어졌습니다.
10:00 집중 업무 → 15:00
15:00 정리 업무 → 내일 11:00
12:00 고객 회의는 유지합니다.

[유지] [캘린더 보기] [적용]
```

한 번에 여러 변경이 필요해도 알림에서는 사용자가 이해할 수 있는 하나의
조정 묶음으로 보여주고, 실제 before/after calendar는 확장 장면에서 확인한다.

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
- 에너지 SVG는 제목과 설명을 분리하고, 24시간의 실제 점수와 missing 상태를
  visually-hidden 목록으로 함께 제공한다.

## 8. Apple 표면과의 일관성

### iPhone과 Mac

모든 core capability를 공유한다.

- 현재 wellness state
- plan impact
- proposal와 대안 비교
- 유지 / 변경 승인 / 대안 확인
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

Watch 앱을 직접 열면 오늘 에너지, 다음 일정 최대 3개, 충돌 표시, pending
조정 건수를 제공한다. 변경안을 선택하면 정확한 before/after 시간과 이유를
Digital Crown으로 확인한다. Watch에서 긴 목표 관리, 전체 주·월 calendar,
raw data, Advanced를 제공하지 않는다.

## 9. 구현 상태와 QA 기준

### 웹 구현

- 페이지형 dashboard tabs를 제거한다.
- 같은 page 안에 `지금 / 조율 / 변화` lens를 제공한다.
- `health → plan impact`를 첫 scene의 중심 흐름으로 만든다.
- 허용된 bounded primitive에 `data-ui-primitive` 계약을 부여한다.
- voice/text command dock을 모든 lens 아래에 고정한다.
- 웹이 read-only이며 Apple 앱에서 실행한다는 설명을 항상 표시한다.
- `Advanced`는 기본으로 닫는다.
- 기존 route, fragment, base path, viewer token 링크를 보존한다.

### 현재 PR에서 검증하는 범위

- iPhone과 Mac 기본 화면에는 고정 `지금 / 조율 / 변화` 세그먼트가 없다.
- 상세 일정·목표와 결정 결과는 `전체 보기`에서 열 수 있다.
- 앱을 열면 현재 건강 영향과 필요한 한 가지 행동이 먼저 보인다.
- 사용자 질문형 scene을 지원한다. `source=proactive` 요청은 exact active
  `proposal_id`와 그 proposal의 `decision_record_id`가 모두 일치할 때만
  허용하며, 실제 trigger runtime wiring은 후속 작업이다.
- Apple·Google event의 출처, identity, 제목, 시각과 현재 mirror가 가진
  ownership metadata를 보존한다.
- HealthMes가 소유하지 않은 고정 event를 자동 변경하지 않는다.
- 정확한 proposal ID가 없는 scene은 승인 action을 만들지 않는다.
- proposal contract에 operation과 원본 event identity가 없으면 생성·이동을
  추정하거나 가짜 before 상태를 만들지 않는다.
- seed dashboard가 목표, proposal, 일정, insight, decision detail을 모두
  렌더링한다.
- 빈 dashboard가 데이터를 생성하지 않고 honest empty state를 표시한다.
- reverse proxy base path가 lens와 Advanced 링크에 유지된다.
- authenticated viewer URL은 파생 viewer token만 사용하고 API token을
  노출하지 않는다.
- command dock에는 form이나 mutation 동작이 없다.
- `/dashboard/plan`, `/dashboard/decisions`, `/dashboard/history`가 모두 같은
  control surface를 반환한다.

### 후속 엔진·read-model 작업

다음 항목은 UI schema와 renderer가 받을 자리는 마련하지만, 이번 PR에서
엔진·MCP·calendar write 계약을 변경하지 않으므로 완료로 주장하지 않는다.

- 원본 calendar color, location, meeting URL, RSVP, alert, note, provider
  deep link
- operation과 source event identity를 가진 create/move/split/resize proposal
- 늦잠·일정 지연·회복 저하를 실제로 감지해 proposal을 만드는 proactive trigger
- 승인 이후 calendar apply receipt와 장기 wellness outcome의 자동 연결
- Screen Time과 Mac 사용량을 근거로 한 새로운 엔진 판단

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
