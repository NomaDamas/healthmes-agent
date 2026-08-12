# HealthMes Channel Workspace Client UX

> 대상: PR #111 Apple 앱과 웹 UI 재설계
>
> 상태: 구현 전 Target UX 계약
>
> 범위: iPhone, macOS, watchOS, 웹의 client presentation layer
>
> 변경 금지: HealthMes engine, API, DB, migration, scheduler, calendar provider,
> 인증 계약, `vendor/hermes-agent/`

## 1. Global Goal

HealthMes를 일반 채팅 앱, Todo 앱, 건강 점수판이 아니라 사용자가 목적별
채널과 커스터마이징 가능한 Wellness UI를 통해 건강 상태, 일정 영향,
HealthMes의 선제적 제안, 사용자 결정과 실제 결과를 연결하는 개인 Wellness
Workspace로 만든다.

이번 작업은 클라이언트 UI에만 한정한다. 기존 API와 `WellnessScene`,
proposal, decision, calendar approval 계약을 그대로 소비하며 서버에 없는
상태나 기능을 UI가 추측하거나 생성하지 않는다.

```text
기존 HealthMes 데이터와 판단
          ↓
Client Workspace Adapter
          ↓
카테고리 → 채널 → Canvas → Card/Post
                            ↓
                          Thread
                            ↓
             기존 승인·Calendar 계약 호출
```

모든 후속 GitHub issue에는 이 Global Goal과 변경 금지 경계를 그대로
포함한다. 개별 issue가 완성되어도 전체 Workspace 목적과 충돌하면 완료로
간주하지 않는다.

## 2. 핵심 정보 구조

채널은 반드시 대화방일 필요가 없다. 채널은 특정 목적과 데이터 범위를 담는
client-side canvas다.

```text
Workspace
├─ 기본 기능                         시스템 관리
│  ├─ # overview                    dashboard canvas
│  ├─ # calendar                    calendar canvas
│  ├─ # insights                    visualization canvas
│  ├─ # decisions                   decision feed
│  └─ # agent                       conversational canvas
│
├─ 사용자가 만든 카테고리            사용자 관리
│  ├─ 업무
│  │  ├─ # deep-work                custom dashboard
│  │  └─ # weekly-goals             mixed canvas
│  └─ 건강
│     ├─ # sleep-reset              visualization canvas
│     └─ # nutrition               capture plus insight canvas
│
└─ Settings / Advanced
```

### 2.1 기본 기능

기본 기능 그룹은 모든 사용자에게 생성한다.

| 채널 | 기본 역할 | 기본 canvas |
|---|---|---|
| `#overview` | 현재 상태, 오늘 일정 영향, 결정 한 가지 | Dashboard |
| `#calendar` | Apple·Google 일정과 Wellness overlay | Calendar |
| `#insights` | baseline, 에너지, 수면, 식사, 결과 시각화 | Visualization |
| `#decisions` | 선제적 제안, 승인 상태, 적용 결과 | Decision Feed |
| `#agent` | 음성·텍스트·사진을 통한 HealthMes 요청 | Conversation |

기본 채널은 데이터 계약과 deep link의 안정성을 위해 삭제하거나 이름을
변경하지 않는다. 사용자는 sidebar에서 숨기거나 즐겨찾기 순서를 바꿀 수 있다.

### 2.2 사용자 카테고리

기본 기능을 제외한 카테고리는 사용자가 자유롭게 관리한다.

- 카테고리 생성, 이름 변경, 삭제
- drag-and-drop 순서 변경
- 접기와 펼치기
- 카테고리별 아이콘 또는 색상
- 카테고리 안에서 채널 생성과 이동
- 다른 카테고리로 채널 drag-and-drop
- 즐겨찾는 채널을 sidebar 상단에 고정
- 카테고리 삭제 시 포함 채널을 함께 삭제하거나 다른 카테고리로 이동
- 앱 재실행 후 sidebar 상태와 선택한 채널 복원

카테고리 이름은 `업무`, `건강`, `운동`처럼 제품이 강제하지 않는다. 사용자의
생활과 목표에 맞게 자유롭게 정한다.

### 2.3 사용자 채널

사용자는 다음 template에서 시작하거나 빈 채널을 만든다.

- 빈 Dashboard
- Agent 대화
- Calendar
- Wellness 시각화
- 집중 업무
- 수면 회복
- 운동 계획
- 식사·영양
- 장기 목표

채널 설정은 클라이언트에 이미 존재하는 기능만 노출한다.

- 표시할 card와 순서
- compact, regular, expanded card 크기
- 연결해 보여줄 기존 calendar source
- 표시 기간
- 사용할 기존 WellnessScene primitive
- 음성, 텍스트, 카메라 composer 표시 여부

API에 없는 새로운 분석, 자동화, 권한을 UI 설정만으로 제공한다고 표시하지
않는다.

## 3. 왼쪽 Sidebar UX

### 3.1 macOS와 웹

Slack·Discord처럼 왼쪽 sidebar를 지속적으로 표시한다.

```text
┌──────────────────────┬──────────────────────────┬─────────────────────┐
│ HealthMes        ＋  │ # deep-work              │ Thread              │
│ Search               │                          │                     │
│                      │ Dashboard / Feed /       │ 선택한 post, card,  │
│ FAVORITES            │ Calendar / Insight       │ event의 상세 대화   │
│   # overview         │                          │                     │
│                      │                          │                     │
│ 기본 기능        ˅   │                          │                     │
│   # calendar         │                          │                     │
│   # insights         │                          │                     │
│   # decisions    2   │                          │                     │
│   # agent            │                          │                     │
│                      │                          │                     │
│ 업무             ˅ ⋯ │                          │                     │
│   # deep-work        │                          │                     │
│   # weekly-goals     │                          │                     │
│                      │                          │                     │
│ 건강             ˃ ⋯ │                          │                     │
│                      │                          │                     │
│ ＋ 카테고리          │                          │                     │
└──────────────────────┴──────────────────────────┴─────────────────────┘
```

Sidebar 원칙:

- 카테고리 header에는 접기, context menu, drag handle을 제공한다.
- 채널에는 unread indicator, pending decision count, notification mute를
  표시할 수 있다.
- sidebar가 좁아져도 채널명과 중요 badge를 우선 보존한다.
- Mac은 `NavigationSplitView` 기반 3-column 구조를 사용한다.
- 웹은 CSS grid 기반 3-column 구조를 사용한다.
- sidebar와 thread inspector는 각각 접을 수 있다.
- 선택한 채널의 canvas는 중앙 column에서 바뀌며 별도 페이지로 이동하는
  느낌을 최소화한다.

### 3.2 iPhone

iPhone에서는 왼쪽 sidebar를 화면에 항상 두지 않는다.

```text
┌───────────────────────────────────┐
│ ☰  # overview          Search  ⋯ │
├───────────────────────────────────┤
│                                   │
│       선택한 Channel Canvas       │
│                                   │
├───────────────────────────────────┤
│ 말하거나 입력하세요       📷  🎙 │
└───────────────────────────────────┘
```

- 왼쪽 edge swipe 또는 `☰`으로 sidebar drawer를 연다.
- drawer 안에서는 macOS와 같은 카테고리·채널 계층을 사용한다.
- channel 선택 후 drawer를 닫고 기존 scroll position을 복원한다.
- 카테고리·채널 관리는 long press context menu와 Edit mode에서 수행한다.
- thread는 bottom sheet로 시작하고 긴 대화에서는 full screen으로 확장한다.

## 4. Channel Canvas

채널은 다음 canvas mode 중 하나를 기본값으로 갖는다.

| Mode | 목적 | 기본 출력 |
|---|---|---|
| Dashboard | 핵심 상태를 한눈에 확인 | 커스터마이징 card grid |
| Calendar | 실제 시간 배치와 충돌 확인 | 일정, energy overlay, proposal preview |
| Visualization | Wellness 근거와 추이 분석 | graph, confidence, short conclusion |
| Decision Feed | HealthMes가 먼저 보낸 제안 처리 | proposal post와 status |
| Conversation | 사용자와 HealthMes의 지속 요청 | post, response, generated UI |
| Mixed | 위 표현을 목적에 맞게 조합 | card, post, graph, calendar |

Dashboard와 Mixed canvas의 card는 추가, 제거, 순서 변경, 크기 변경이 가능하다.
편집 상태와 읽기 상태는 명확히 구분하며 실수로 card가 움직이지 않게 한다.

## 5. Slack 방식 Thread

### 5.1 대화형 채널

Conversation과 Decision Feed에서 각각의 root post는 독립 thread를 가질 수
있다.

```text
# agent

사용자
이번 주 운동을 세 번 배치해줘.

HealthMes
회복과 현재 일정을 기준으로 화·목·토를 제안합니다.
[Calendar preview] [대안] [적용]

💬 4 replies · 마지막 답변 3분 전
```

root post를 선택하면:

```text
Thread: 이번 주 운동 배치

사용자       이번 주 운동을 세 번 배치해줘.
HealthMes    화·목·토를 제안합니다.
사용자       토요일 대신 일요일은?
HealthMes    일요일 회복 상태가 더 안정적입니다.
사용자       적용
HealthMes    Google Calendar 반영 완료
```

Thread UX:

- Mac·웹에서는 오른쪽 inspector에서 연다.
- iPhone에서는 sheet 또는 full-screen detail로 연다.
- root post 본문과 현재 decision status를 thread 상단에 고정한다.
- reply composer는 텍스트, 음성, 필요한 경우 사진을 지원한다.
- thread 안에서 생성된 새 proposal은 기존 proposal을 조용히 덮어쓰지 않는다.
- accepted, pushed, declined, expired, failed를 별도 상태로 표시한다.
- thread를 닫으면 원래 채널과 scroll 위치로 돌아간다.

### 5.2 비대화형 채널

Dashboard, Calendar, Visualization도 필요한 항목에 thread를 열 수 있다.

- Dashboard card: 카드가 보여주는 상태에 대한 질문과 판단 기록
- Calendar event: 해당 일정의 영향, 이동안, 적용 결과
- Graph point/range: 특정 시점의 변화에 대한 설명
- Decision card: 제안, 사용자 대안, 승인과 결과
- Nutrition capture: 사진, 분석, 이후 상태 변화

비대화형 항목의 thread 버튼은 항상 노출하지 않고 hover, context menu,
detail action 또는 accessibility action으로 제공한다.

### 5.3 UI-only 저장 경계

현재 API에 일반 thread 저장 계약이 없다면 이번 PR은 다음까지만 구현한다.

- 기존 proposal, decision, command 결과를 thread timeline으로 재구성
- 현재 앱 session의 client-only reply와 draft
- thread navigation, composer, state UI
- 서버가 receipt를 제공하는 기존 action 연결

앱 재설치와 기기 간 동기화를 지원하는 영구 thread 저장은 서버 계약 없이는
완료로 주장하지 않는다. UI가 저장 성공처럼 표시해서도 안 된다.

## 6. 알림에서 Thread로 연결

알림 action은 다음 세 가지로 유지한다.

```text
[아니요] [예] [다른 방법]
```

`다른 방법`은 시스템 텍스트 입력 또는 받아쓰기를 열고, 입력 결과로 기존
command pipeline을 호출해 새 proposal을 요청한다.

```text
알림의 proposal
→ 다른 방법: "오늘 7시는?"
→ 같은 decision thread로 deep link
→ 새 proposal preview
→ 사용자 승인
→ 기존 Calendar gateway
→ 적용 receipt를 thread에 표시
```

음성이나 텍스트 입력만으로 Calendar를 즉시 변경하지 않는다.

## 7. 플랫폼 책임

### iPhone

- sidebar drawer
- 모든 channel canvas
- sheet/full-screen thread
- HealthKit, 카메라, 음성·텍스트 composer
- 잠금화면 알림과 Live Activity

### macOS

- 고정 sidebar, 중앙 canvas, 오른쪽 thread inspector
- iPhone과 동일한 핵심 channel과 action
- 넓은 calendar·graph, 키보드 탐색, 메뉴바 pending decision

### 웹

- macOS와 같은 sidebar와 inspector 구조
- 주·월간 calendar와 장기 insight
- card layout 편집
- 기존 viewer token, reverse-proxy base path, read-only 경계 유지

### Watch

Watch에는 전체 sidebar나 channel 관리 UI를 복제하지 않는다.

- `#overview`의 축약 glance
- `#decisions`의 pending proposal
- 정확한 before/after와 이유 한 줄
- No, Yes, 다른 방법
- 자세한 thread는 iPhone으로 handoff

## 8. 실제 도입과 참고 오픈소스

현재 웹은 React가 없는 Jinja/HTML/CSS/JavaScript이고 Apple 앱은 SwiftUI다.
새 프런트엔드 runtime을 도입하지 않는다.

실제 도입 후보:

| 프로젝트 | 사용 범위 | 라이선스 |
|---|---|---|
| Apache ECharts | 웹 Wellness visualization | Apache-2.0 |
| SortableJS | 웹 카테고리·채널·card drag-and-drop | MIT |
| FullCalendar Core | 웹 calendar canvas | MIT |
| SwiftUI·Swift Charts | iPhone, Mac, Watch native UI | Apple SDK |

FullCalendar Premium plugin은 사용하지 않는다. 라이선스와 attribution은
고정 version, third-party notice, 배포 artifact 검사를 통해 보존한다.

설계만 참고:

- Slack: sidebar section, channel, right-side thread
- Discord: category와 channel hierarchy
- Zulip: 이름 있는 topic 중심의 장기 thread
- CopilotKit: agent result와 bounded generative UI
- assistant-ui: composer와 thread 상태
- shadcn/ui: 접근성 있는 sidebar와 card

React 기반 프로젝트나 대형 채팅 제품의 shell 코드는 복사하지 않는다.
Element Web처럼 AGPL인 프로젝트도 코드 의존성으로 사용하지 않는다.

## 9. 단계별 GitHub Issue 계획

모든 issue는 문서 1장의 Global Goal과 변경 금지 경계를 본문 첫 부분에
포함한다.

### Issue 1: Client Workspace 계약

- Workspace, category, channel, canvas, block, thread client model
- 기존 WellnessScene adapter
- local schema version과 migration
- unknown data fail-closed

완료 기준:

- 기존 API fixture만으로 모든 client model 생성
- server model과 engine 파일 변경 없음
- proposal와 decision identity 보존

### Issue 2: 공통 Sidebar와 사용자 카테고리

- 시스템 기본 그룹
- 사용자 카테고리 CRUD
- 접기, 정렬, drag-and-drop, 즐겨찾기
- iPhone drawer, Mac·웹 persistent sidebar
- selection과 scroll state 복원

완료 기준:

- 카테고리 이름과 개수 제한을 UI에서 명확히 처리
- 기본 그룹은 삭제 불가
- 사용자 카테고리와 채널은 자유롭게 이동 가능
- Dynamic Type, VoiceOver, keyboard navigation

### Issue 3: 채널 생성과 Canvas Template

- 빈 Dashboard, Agent, Calendar, Visualization, 목적별 template
- channel rename, icon, color, hide, delete
- API에 없는 설정 비활성화

완료 기준:

- template 선택 직후 실제 기존 데이터만 표시
- 빈 상태에서 가짜 chart나 일정 생성 금지

### Issue 4: Universal Card Canvas

- card catalog, 추가, 제거, 정렬, 크기
- edit mode와 view mode
- native drag-and-drop과 웹 SortableJS
- local layout persistence와 reset

완료 기준:

- 작은 iPhone과 넓은 Mac·웹에서 동일 card 의미 보존
- missing data와 confidence 표시

### Issue 5: 기본 시스템 채널

- `#overview`, `#calendar`, `#insights`, `#decisions`, `#agent`
- 현재 상태 → 일정 영향 → 결정 한 가지 흐름
- 실제 calendar, visualization, decision receipt

완료 기준:

- 각 기본 채널의 목적이 겹치지 않음
- `#overview`의 primary decision은 최대 한 개
- 일반 Todo list와 건강 점수판 UI 제거

### Issue 6: Slack 방식 Universal Thread

- conversational root post와 replies
- card, event, graph, decision의 thread entry
- Mac·웹 inspector, iPhone sheet/full screen
- reply composer와 상태 timeline

완료 기준:

- thread 종료 후 원래 위치 복원
- stale proposal action 차단
- server 저장이 없는 reply를 영구 저장처럼 표시하지 않음

### Issue 7: Agent Conversation과 생성형 UI

- 음성, 텍스트, 카메라 composer
- plain text, chart, calendar, proposal block 조합
- 무한 대화보다 명령과 결과 중심
- thread continuation

완료 기준:

- WellnessScene catalog 밖 UI 생성 금지
- 실행 가능한 결과는 preview와 승인 필수

### Issue 8: 알림 Order와 Thread Deep Link

- No, Yes, 다른 방법
- text input와 dictation
- 새 proposal preview
- exact decision thread routing

완료 기준:

- 음성만으로 Calendar mutation 금지
- privacy-sensitive lock-screen copy 축약
- expired notification action 차단

### Issue 9: macOS 3-Column Workspace

- sidebar, canvas, thread inspector
- keyboard navigation과 command surface
- menu bar pending decision

완료 기준:

- iPhone과 핵심 기능 parity
- window resize와 column collapse 대응

### Issue 10: 웹 Workspace

- sidebar, canvas, thread inspector
- ECharts, SortableJS, FullCalendar Core
- Advanced 기본 닫힘
- viewer token과 base path 보존

완료 기준:

- React runtime 추가 없음
- read-only와 실행 가능 action 경계 표시
- third-party license notice

### Issue 11: Watch Decision Remote

- overview glance
- pending decision
- No, Yes, 다른 방법
- iPhone thread handoff

완료 기준:

- Series 10 42mm 첫 화면에서 행동과 시간 확인
- 전체 sidebar와 긴 thread 미구현

### Issue 12: 기존 UI 제거와 호환 Migration

- 새 Workspace parity 확인 후 기존 home 제거
- 기존 route와 deep link를 channel로 redirect
- client preferences migration

완료 기준:

- 기능 손실과 dead navigation 없음
- engine, API, DB diff 0

### Issue 13: Cross-platform UI QA

- iPhone 13 mini, Watch Series 10 42mm, Mac, 웹
- empty, missing, offline, stale, expired 상태
- Dynamic Type, VoiceOver, keyboard, reduced motion
- screenshot artifact와 third-party notice

완료 기준:

- 사용자에게 각 플랫폼 결과를 보여주고 병합 전 승인
- PR #111 전체 UI regression gate 통과

## 10. PR #111 완료 조건

```text
왼쪽 sidebar
→ 사용자 카테고리와 채널 선택
→ Dashboard / Calendar / Insight / Agent canvas
→ card, post, event 또는 graph에서 thread 열기
→ 질문·대안·승인
→ 기존 Calendar 결과 표시
```

다음 조건이 모두 충족되어야 Target UX 완료로 본다.

- 사용자 카테고리를 자유롭게 생성, 정렬, 접고 삭제할 수 있다.
- 기본 기능 채널은 안정적으로 유지되며 숨기기만 가능하다.
- 채널은 대화형과 비대화형 canvas를 모두 지원한다.
- 대화형 root post는 Slack 방식 thread를 연다.
- 비대화형 card, calendar event, graph, decision도 thread를 연다.
- iPhone과 Mac의 핵심 기능이 동일하다.
- Watch는 Decision Remote 역할을 유지한다.
- 기존 HealthMes engine, API, DB, Calendar 계약을 변경하지 않는다.
- 사용자에게 시뮬레이터와 웹 결과를 보여주기 전 병합하지 않는다.
