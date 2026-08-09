# HealthMes 활동 텔레메트리 MVP 결정

> **결정일:** 2026-08-09
>
> **지위:** 소유자가 확정한 활동 텔레메트리 MVP 범위와 후속 구현 Issue의
> 기준 문서.
>
> **범위:** 휴대전화·노트북·데스크톱 사용 활동의 수집, 저장, 최소 가공,
> 프라이버시 제어, HealthMes 판단 인터페이스. UI와 Hermes adaptation은 제외한다.
>
> **Skill 계약:** `contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md`

## TLDR

HealthMes MVP는 사용자가 소유한 노트북, 데스크톱, Mac mini 또는 홈서버에서
실행한다. 휴대전화와 컴퓨터의 활동 수집기는 이 HealthMes 노드로 데이터를
주기적으로 전송한다.

```text
Android activity collector ───────────┐
                                      │
macOS/Windows/Linux ActivityWatch ────┼─> HealthMes Activity Ingest
                                      │
iOS capability-limited adapter ───────┘
                                             |
                                             v
                                  activity.* WellnessEvent
                                             |
                                             v
                                  hourly/daily activity context
                                             |
                                             v
                                  HealthMes decision interfaces
```

이 흐름은 **컴퓨터와 휴대전화의 사용 활동만** 다룬다. 캘린더, 식사,
Open Wearables 데이터는 Activity Ingest로 들어오지 않는다. 이들은 각자의
기존 입력 경로를 유지하고 HealthMes의 상위 context/decision 계층에서만
activity context와 결합한다.

휴대전화만 사용하는 사용자를 위해 전체 HealthMes 에이전트를 휴대전화에
상주시켜 구현하지 않는다. 휴대전화 단독 사용은 미래
**Hosted Personal Data Node**가 담당한다.

## 1. 제품과 런타임 경계

HealthMes가 최상위 제품이며 저장, 정규화, 파생 특징, 판단 정책과 사용자
데이터 계약을 소유한다.

```text
HealthMes
  |
  +-- input adapters
  +-- WellnessEvent storage
  +-- activity/nutrition/wearable/calendar engines
  +-- cross-domain context resolver
  +-- decision policies
  +-- REST/MCP/skill contracts
  |
  +-- optional agent/channel adapters
        |
        +-- Hermes adaptation (future)
```

Hermes는 HealthMes와 동등한 데이터·판단 계층이 아니다. 향후 HealthMes의
MCP와 skill 계약을 사용하는 교체 가능한 agent/channel runtime adapter다.
이번 MVP는 Hermes 코드나 `vendor/hermes-agent/`를 변경하지 않는다.

## 2. 실행 위치와 저장 결정

### MVP

```text
권장: 항상 켜진 Mac mini / 홈서버
가능: 데스크톱
가능: 노트북, 단 절전 중 수집 처리는 지연
제외: 휴대전화 안에서 전체 HealthMes/Hermes 상시 실행
```

- HealthMes 노드는 장기 저장과 가공을 담당한다.
- 휴대전화는 수집, 짧은 암호화 전송 큐와 최근 표시용 cache만 담당한다.
- HealthMes 노드가 꺼져 있으면 휴대전화는 전송 대기열에 보관한다.
- 노드가 다시 접근 가능해지면 오래된 데이터부터 batch로 전송한다.
- 초 단위 실시간 동기화는 요구하지 않는다. 기본 목표는 15~30분 주기 또는
  앱이 열렸을 때의 best-effort 전송이다.
- iCloud relay, multi-node 복제와 휴대전화 on-device agent는 MVP 이후다.

### 휴대전화 단독 사용자

소유자 결정은 다음과 같다.

```text
휴대전화 단독 전체 제품
  = Hosted Personal Data Node 사용
  != 휴대전화 안에 전체 HealthMes stack을 억지로 상주시킴
```

MVP self-hosted 제품은 사용자가 노트북, 데스크톱 또는 홈서버에 HealthMes
노드를 설치한다고 가정한다. Hosted Personal Data Node의 가격, 운영과 모바일
오프라인 fallback은 future work다.

## 3. 무엇을 "활동 중"으로 볼 것인가

클릭, 키 입력이나 화면 내용을 수집하지 않는다. OS가 알려 주는 foreground
앱/창과 사용자의 active/idle 상태를 이용해 시간 구간을 만든다.

```text
foreground app/window
        +
screen unlocked
        +
not idle
        =
active activity interval
```

### 데스크톱과 노트북

- 현재 포커스를 가진 foreground 앱 또는 창을 기준으로 한다.
- 마우스·키보드 이벤트는 내용이나 좌표를 저장하지 않고 idle 여부 판정에만
  사용한다.
- 기본 idle 임계값은 5분으로 시작한다.
- 앱 전환, idle 진입, 잠금, 절전 또는 종료 시 현재 interval을 닫는다.
- ActivityWatch를 사용할 때 window title은 기본 폐기하고 app/category와
  시간만 HealthMes에 가져온다.

예:

```text
09:00-09:24  editor, active
09:24-09:31  idle
09:31-09:48  browser, active
```

### Android

- `UsageStatsManager`가 제공하는 foreground/resumed와 background/paused
  전환을 기준으로 한다.
- 모든 화면 탭이나 클릭을 수집하지 않는다.
- MVP는 기존 시간별 package, foreground seconds, launches, category 전송을
  canonical activity storage에 연결한다.
- screen unlocked/locked와 더 정밀한 closed interval은 MVP 이후 확장한다.

### iPhone과 iPad

- Android와 같은 상세 foreground 앱 timeline을 제품 전제로 약속하지 않는다.
- OS와 entitlement가 허용하는 aggregate 또는 threshold만 adapter를 통해
  받을 수 있다.
- 지원되지 않는 환경은 사용시간 `0`으로 저장하지 않고
  `capability=unavailable`로 표시한다.
- private API, 화면 캡처 또는 접근성 우회로 상세 활동을 만들지 않는다.

## 4. 시간대별 집계 기준

가능한 source는 먼저 닫힌 interval로 저장하고, 시간별 데이터는 그 interval을
시간 경계에서 잘라 계산한다.

```text
source interval: 09:55-10:10

09시 bucket: 5분
10시 bucket: 10분
```

- 앱 실행이나 전환 횟수는 이벤트가 발생한 시간 bucket에 기록한다.
- idle 또는 잠금 시간은 active minutes에 포함하지 않는다.
- 데이터가 없는 구간과 실제 사용시간 0분을 구분한다.
- 모든 summary는 source coverage와 collector capability를 포함한다.
- 기존 Android hourly bucket은 호환 source로 받고, source가 더 정밀한
  interval을 제공하는 플랫폼에서는 interval을 정본으로 사용한다.

## 5. 웨어러블·캘린더·식사와의 관계

Open Wearables는 이미 웨어러블 수집과 정규화를 담당한다. Activity collector가
웨어러블을 다시 수집하지 않는다.

```text
computer/phone activity collectors
        |
        v
activity context -------------------┐
                                    │
Open Wearables -> wearable context -┤
                                    │
Calendar -> schedule context -------┼-> HealthMes cross-domain decision
                                    │
Nutrition -> nutrition context -----┤
                                    │
user state -> subjective context ---┘
```

따라서 "기존 캘린더·웨어러블 -> Activity Ingest"라는 표현은 잘못됐다.
캘린더와 웨어러블은 Activity Ingest의 입력이 아니라, 저장·가공이 끝난
activity context와 나중에 결합되는 독립 context다.

## 6. MVP 프라이버시와 권한 계약

MVP는 복잡한 rule engine을 만들지 않고 다음 세 가지 제어만 구현한다.

```text
1. device collection on/off
2. per-app exclude list
3. pause until a time or manual resume
```

- 제외 규칙은 source device에서 event 생성 전에 적용한다.
- raw window title, full URL, keystroke, click coordinate, clipboard,
  notification body와 화면 픽셀은 수집하지 않는다.
- 수집기 권한이 취소되면 즉시 중지하고 `permission_revoked` 상태를 노출한다.
- 설정 API는 권한 요청 상태, 마지막 수집, 마지막 업로드, queue age와
  coverage를 반환한다.
- 설정 화면, 권한 버튼과 플랫폼 UI는 별도 device-team branch가 소유한다.

## 7. 최소 파생 activity context

HealthMes는 원시 행을 모델에 직접 전달하지 않고 다음 작은 특징을 계산한다.

```text
total_active_minutes
category_minutes
app_launches_or_switches
longest_active_block_minutes
idle_and_break_minutes
late_activity_minutes
first_activity_at / last_activity_at
seven_day_baseline_delta
source_coverage
```

MVP 판단 scope는 세 가지다.

```text
focus
  집중 블록, 전환 빈도, 개인 baseline 대비 분절

overwork
  총 활동시간, 긴 연속 활동, 야간 사용, 휴식 부족

recovery
  활동과 idle 균형, 최근 수면·스트레스 context와 함께 볼 제한된 회복 맥락
```

상관관계를 원인으로 단정하지 않는다. coverage가 부족하면
`insufficient_data`를 반환한다.

## 8. HealthMes 인터페이스와 skill

MVP 엔진 인터페이스는 다음 세 read-only context로 제한한다.

```text
get_activity_summary(date)
get_focus_context(start, end)
get_overwork_context(date, lookback_days)
```

계산과 정책은 HealthMes 엔진에 둔다. `healthmes-activity-wellness` skill은
이 context를 어떤 질문에 사용할지 정의하는 얇은 HealthMes-owned 계약이다.

```text
HealthMes activity engine
        |
        v
HealthMes MCP/context contract
        |
        v
healthmes-activity-wellness skill contract
        |
        v
agent runtime adaptation, including Hermes (future)
```

이번 MVP는 skill 계약과 fixture를 정의할 수 있지만 Hermes bootstrap,
gateway, memory, channel과 vendored code adaptation은 하지 않는다.

## 9. 교차 영역 질문

HealthMes의 강점은 질문이 어느 한 입력에만 속하지 않을 때 필요한 context를
선택해 함께 판단하는 것이다.

예:

```text
"집중하려고 이 커피를 마셔도 될까?"

nutrition/caffeine
  오늘 확정 카페인 + 사진 후보 카페인

wearable
  수면과 현재 회복 context

time/calendar
  현재 시각, 목표 취침과 남은 일정

activity
  연속 작업, 앱 전환과 휴식 부족

HealthMes decision
  카페인 정책의 경계는 유지하면서 휴식 대안과 업무 맥락을 함께 설명
```

Activity policy가 카페인 용량을 계산하지 않고, caffeine policy가 집중도를
추측하지 않는다. HealthMes의 상위 context resolver가 각 전문 정책의 결과와
근거를 결합한다.

## 10. Issue 기반 구현 순서

### Stage 1 - canonical storage

**`ACT-MVP-01 Canonical Activity Ingest`**

- `activity.app-hour.v1`과 선택적 `activity.app-interval.v1`
- source device, observed time, timezone, category, duration, launches
- source identity 기반 멱등 ingest

완료 조건: Android와 desktop fixture가 같은 `WellnessEvent` envelope에 저장된다.

**`ACT-MVP-02 Activity Retention`**

- raw activity, hourly summary, daily summary 보존 클래스
- 기존 `1/7/14/30/90일/무기한` 설정과 purge 연결

완료 조건: 만료와 수동 삭제 뒤 raw와 파생 데이터의 dependency가 일관된다.

### Stage 2 - privacy control

**`ACT-MVP-03 Collection Control Contract`**

- 기기별 on/off
- per-app exclude
- pause/resume
- permission, queue와 coverage status

완료 조건: 제외 앱 fixture의 identity와 시간이 ingest payload에 나타나지 않는다.

### Stage 3 - collectors

**`ACT-MVP-04 Android Canonical Adapter`**

- 기존 `/v1/app-usage/batch`를 canonical event로 투영
- 기존 cognitive-energy read model과 결과 호환

완료 조건: 현재 Android collector를 바꾸지 않아도 canonical store에 쌓인다.

**`ACT-MVP-05 ActivityWatch Desktop Adapter`**

- localhost incremental import
- app/AFK normalization
- title 폐기와 cursor

완료 조건: macOS, Windows와 Linux fixture가 같은 activity schema를 만든다.

**`ACT-MVP-06 iOS Capability Adapter`**

- supported aggregate 입력 계약
- unsupported/permission denied 상태

완료 조건: 지원하지 않는 환경에서 상세 timeline이나 가짜 0분을 만들지 않는다.

### Stage 4 - HealthMes processing

**`ACT-MVP-07 Activity Aggregation`**

- hourly/daily summary
- focus, overwork와 recovery 최소 특징
- 7일 개인 baseline과 coverage

완료 조건: raw fixture로부터 같은 summary를 결정적으로 재생성한다.

**`ACT-MVP-08 Activity Context API and MCP`**

- activity summary
- focus context
- overwork context

완료 조건: raw app identity 없이 근거, coverage와 limitation을 반환한다.

**`ACT-MVP-09 Activity Wellness Skill Contract`**

- 질문 routing
- observation/evidence/proposal/boundary 응답 계약
- 직접 계산과 인과 단정 금지

완료 조건: agent runtime과 무관한 contract fixture가 통과한다.

### Stage 5 - cross-domain moat

**`CTX-MVP-01 Cross-domain Context Resolver`**

- activity, wearable, calendar와 nutrition context를 요청별로 선택
- 각 전문 정책의 숫자와 안전 경계를 보존
- 근거 ID, freshness, coverage와 충돌을 함께 반환

완료 조건: 단일 영역 질문은 불필요한 데이터를 읽지 않고, 복합 질문은 각
영역의 독립 근거와 한계를 유지한 하나의 HealthMes decision context를 만든다.

## 11. 명시적 MVP 제외

- 휴대전화 안에서 전체 HealthMes/Hermes 상시 실행
- 휴대전화 단독 self-hosted 장기 저장
- 실시간 phone-desktop 양방향 동기화
- iCloud relay와 multi-node replication
- 클릭, 키 입력, 화면 내용과 알림 본문 수집
- 브라우저 domain과 window title 저장
- 정밀 phone-desktop interruption reconstruction
- 자동 앱 차단과 강제 행동교정
- iOS/Android/macOS/Windows/Web UI
- Hermes adaptation과 `vendor/hermes-agent/` 변경

## 12. MVP 종료 조건

```text
Android hourly usage
        +
ActivityWatch desktop activity
        |
        v
privacy-filtered canonical events
        |
        v
retention-aware local storage
        |
        v
focus/overwork/recovery summaries
        |
        v
HealthMes context API/MCP + skill contract
```

이 경로가 소유자 실데이터로 동작하고, 누락을 0으로 가장하지 않으며, 제외 앱이
저장·context·로그에 나타나지 않을 때 MVP를 완료한 것으로 본다.

## 13. 구현 계약

```text
healthmes/activity/
  canonical contracts + privacy + adapters
  aggregation + retention + context resolver
  REST + MCP interfaces

tests/activity/
  deterministic engine and contract fixtures

tests/api/ and tests/mcp_server/
  public interface and composition fixtures
```

구현은 공통 `WellnessEvent` 저장소를 사용하며 별도 activity silo를 만들지 않는다.
기존 Android hourly collector는 compatibility table을 유지하면서 같은 입력을
canonical activity event로 투영한다. ActivityWatch와 iOS capability adapter도
같은 envelope를 사용한다.

agent가 따라야 할 질문 routing, 응답 shape와 전문 정책 경계는
[`HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md`](contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md)
를 canonical contract로 사용한다. 실제 UI, device dogfood와 Hermes adaptation은
이 구현 완료 조건과 분리된 후속 작업이다.

## 14. 엔진 구현 상태

2026-08-09 기준으로 UI를 제외한 이 문서의 MVP 엔진 범위는 구현되어 있다.

- Android, ActivityWatch와 iOS capability 입력은 같은 `WellnessEvent` 저장소를
  사용한다.
- 기본 보존은 raw 14일, hourly summary 90일, daily summary 무기한이며 기존
  `1/7/14/30/90일/무기한` storage setting으로 변경할 수 있다.
- hourly와 daily summary의 provenance는 raw ID 전체를 복제하지 않고
  `raw_event_count + SHA-256 digest`로 크기가 제한된다.
- daily coverage는 데이터가 존재한 hour만이 아니라 local day 전체
  23/24/25시간을 분모로 사용한다.
- focus coverage는 요청한 전체 구간을 분모로 사용하며 부분 hour는 비례값과
  limitation을 함께 반환한다.
- 수동 raw 삭제는 summary 직접 삭제 옵션과 관계없이 영향을 받은 날짜를
  재집계해 삭제된 활동이 파생 context에 남지 않게 한다.
- collection on/off, 앱 제외, pause/resume, permission, queue, coverage와
  capability 상태는 UI 독립 REST 계약으로 제공한다.
- Activity summary, focus, overwork와 bounded cross-domain resolver는 REST와
  MCP에서 같은 결정론적 엔진을 사용한다.

실제 iOS/Android/macOS/Watch 화면, 디바이스 dogfood, 실시간 동기화와 Hermes
adaptation은 이 완료 선언에 포함되지 않는다.
