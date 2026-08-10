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
  +-- Context Access Layer
  +-- HealthMes Decision Agent contract
  +-- decision policies
  +-- REST/MCP/skill contracts
  |
  +-- optional agent/channel adapters
        |
        +-- Hermes adaptation (future)
```

Hermes는 HealthMes와 동등한 데이터·판단 계층이 아니다. 향후 HealthMes
Decision Agent 계약과 MCP 도구를 실행하는 교체 가능한 agent/channel runtime
adapter다. Skill은 핵심 판단 로직이 아니라 이 runtime을 연결하는 얇은 설명이다.
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
- 수집기는 OS 활동을 읽기 전에 서버의 최신 collection config를 조회하고,
  응답의 `config_revision`을 ingest payload의 `collection_revision`으로 넣는다.
  `enabled`, `effective_collecting`, `blocked_reason`, `excluded_apps`,
  `config_revision`, `collection_generation`이 없거나 타입이 다르면 config
  전체를 거부하고 `UsageStats`를 읽지 않는다. 숫자처럼 보이는 문자열도
  revision이나 generation으로 허용하지 않는 fail-closed 계약이다.
- 공개 canonical ingest와 Android legacy ingest는 revision을 생략할 수 없다.
  iOS도 sample을 제출할 때는 revision이 필수다. ActivityWatch adapter는 서버가
  현재 revision을 주입한다.
- `activitywatch`, `android-usage`, `ios-device-activity` provider 이름은 각
  내장 adapter 전용이다. 공개 canonical endpoint에서 같은 이름을 직접
  제출해 provenance를 위조할 수 없다.
- config revision이 바뀌면 Android 수집기는 이전 revision의 watermark와
  backfill 구간을 버리고 변경 시각부터 새 수집 구간을 시작한다.
  `collection_generation + config_revision + collection_since +
  collection_timezone + watermark`는 하나의 동기식 encrypted preference
  commit으로 저장하며 실패하면 OS 데이터를 읽지 않는다. 따라서 crash가 값의
  일부만 남겨 제외·중지 이전 활동을 새 설정이나 새 timezone으로 읽는 일을
  막는다.
- Android의 민감 경계 commit은 먼저 별도 일반 `SharedPreferences`에
  quarantine latch를 동기식으로 arm하고, encrypted state를 commit한 뒤,
  마지막으로 latch를 해제하는 2단계 순서를 따른다. encrypted commit 또는
  latch 해제가 실패하면 이미 durable한 latch가 process 재시작 뒤에도
  periodic/one-shot 활동 upload를 차단하고, `collection_enabled=false`를
  최선 저장한 뒤 WorkManager를 모두 취소한다. latch arm 자체가 실패하면
  encrypted state를 건드리지 않고 현재 process에서 즉시 수집을 중단한다.
  사용자가 명시적으로 다시 켜서 새 경계 commit에 성공한 경우에만 quarantine을
  해제한다.
- 모든 hard boundary 변경은 `collection_generation`을 증가시킨다. worker가
  UsageStats를 읽는 동안 generation이 바뀌면 읽은 snapshot을 폐기한다.
  network I/O 동안 collection-state lock을 잡지 않는다. 대신 각 HTTP chunk
  직전과 최종 watermark commit 직전에 권한과 generation을 다시 확인한다.
  이미 시작된 한 HTTP request는 완료될 수 있지만, 저장된 새 경계 뒤에는 다음
  chunk와 watermark가 진행되지 않으며 permission observer와 앱 thread도 긴
  network timeout에 막히지 않는다.
- Android의 제외 앱은 시간 bucket 생성과 category 조회 전에 source에서
  제거한다. 서버는 같은 제외 규칙과 revision을 다시 검증한다.
- raw window title, full URL, keystroke, click coordinate, clipboard,
  notification body와 화면 픽셀은 수집하지 않는다.
- Android Usage Access 설정을 앱에서 열기 전, process-lifetime AppOps
  listener가 변경을 볼 때, 앱 화면이 resume될 때와 worker가 실행될 때마다
  로컬 수집 경계를 원자적으로 다시 세운다. 관찰한 revoke에는
  `permission_status=revoked`를 보고하고, 관찰한 regrant는 그 시각부터 새
  수집 구간을 시작한다. worker는 OS read 전과 upload 전에도 권한을 다시
  확인한다.
- Android 수집기는 권한이 다시 허용되면 먼저
  `permission_status=granted + status_observed_at + collection_generation`을
  하나의 status boundary로 보고하고, 그 응답에 포함된 최신 collection
  config를 적용한 뒤에만 OS 데이터를 읽는다. config 적용이 새 로컬
  generation을 만들면 새 generation을 다시 등록하며, 로컬과 서버 generation이
  안정적으로 일치한 경우에만 UsageStats를 읽는다. 이 순서로 서버의
  `permission_revoked` gate를 안전하게 해제하며 설정·권한 상태를 우회하지
  않는다.
- Android 서버 상태는 wall-clock 시각보다 `collection_generation`을 먼저
  비교한다. 낮은 generation은 더 늦은 timestamp를 가져도 무시하고, 같은
  generation에서는 `revoked/denied/unavailable`이 `granted`보다 우선한다.
  새 generation만 이전의 차단 상태를 해제할 수 있다.
- `POST /v1/app-usage/batch`는 먼저 등록된 서버 generation과 payload의
  `collection_generation`이 정확히 같을 때만 저장한다. 미등록 generation과
  이전 generation batch는 `409`로 거부한다. activity ingest 성공은
  `last_collected_at/last_uploaded_at` 같은 telemetry만 갱신하며 permission을
  암묵적으로 `granted`로 바꾸지 않는다.
- Android 공개 API는 과거 Usage Access grant interval을 제공하지 않는다.
  따라서 앱 설정 진입도, HealthMes process 실행도 전혀 없는 동안 외부 시스템
  설정에서 revoke와 regrant가 모두 끝난 경우에는 그 짧은 권한 공백을 사후
  탐지할 수 없다. foreground service로 상시 감시하지 않는 MVP의 명시적
  플랫폼 한계이며, 그런 구간까지 절대 탐지한다고 주장하지 않는다.
- 설정 API는 권한 요청 상태, 마지막 수집, 마지막 업로드, queue age와
  coverage를 반환한다.
- 설정, runtime status와 adapter cursor는 서로 덮어쓰지 않는 독립
  `WellnessEvent`로 저장하고 하나의 read contract로 합쳐 반환한다.
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

## 8. HealthMes context 인터페이스

현재 Activity MVP 엔진 인터페이스는 다음 read-only context 도구를 제공한다.

```text
get_activity_summary(date)
get_focus_context(start, end)
get_overwork_context(date, lookback_days)
```

계산과 정책은 HealthMes 엔진에 둔다. 이 도구들은 LLM 대신 질문을 판단하지 않고
정확한 Activity context를 제공한다.

```text
HealthMes activity engine
        |
        v
HealthMes MCP/context contract
        |
        v
HealthMes Context Access Layer
        |
        v
HealthMes Decision Agent
        |
        v
runtime adaptation, including Hermes
```

현재 `resolve_wellness_context(question_kind, ...)`와
`healthmes-activity-wellness` 계약은 구현 호환용 preset이다. 목표 구조에서는 LLM
Decision Agent가 필요한 도구를 자율적으로 선택하고, Context Access Layer는
권한·retention·privacy와 source reference만 강제한다. 상세 개선안은
[`HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md`](HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md)
를 따른다.

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
추측하지 않는다. HealthMes Decision Agent의 LLM이 필요한 전문 도구를 선택하고
각 정책 결과의 경계를 유지한 채 최종 설명을 결합한다. Context Access Layer는
선택된 자료의 권한, freshness, coverage와 source reference를 검사한다.

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
- OS 활동을 읽기 전 collection config/revision 확인과 source-side 앱 제외
- 기존 cognitive-energy read model과 결과 호환

완료 조건: 보강된 Android collector가 privacy revision을 지키면서 compatibility
table과 canonical store에 원자적으로 투영한다.

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
HealthMes context API/MCP contract
```

엔진 MVP는 결정론적 fixture와 public contract로 이 경로를 증명하고, 누락을
0으로 가장하지 않으며, 제외 앱이 저장·context·로그에 나타나지 않을 때 완료한
것으로 본다. 소유자 실데이터와 실제 기기에서의 end-to-end dogfood는 device
UI PR이 이 엔진 계약을 연결한 뒤 수행하는 별도 제품 검증 단계다.

## 13. 구현 계약

```text
healthmes/activity/
  canonical contracts + privacy + adapters
  aggregation + retention + compatibility resolver
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

현재 고정 `question_kind` 호출자와의 호환 계약은
[`HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md`](contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md)
에 남긴다. 목표 자연어 판단, 자율 tool selection, Hermes adapter와 자동
DecisionRecord 저장은
[`HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md`](HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md)
를 canonical target으로 사용한다. 실제 UI와 device dogfood는 이 엔진 구현과
분리된 후속 작업이다.

## 14. 엔진 구현 상태

2026-08-10 기준으로 UI를 제외한 이 문서의 MVP 엔진 범위는 구현되어 있다.

- Android, ActivityWatch와 iOS capability 입력은 같은 `WellnessEvent` 저장소를
  사용한다.
- 기본 보존은 raw 14일, hourly summary 90일, daily summary 무기한이며 기존
  `1/7/14/30/90일/무기한` storage setting으로 변경할 수 있다.
- hourly와 daily summary의 provenance는 raw ID 전체를 복제하지 않고
  `raw_event_count + SHA-256 digest`로 크기가 제한된다.
- daily coverage는 데이터가 존재한 hour만이 아니라 local day 전체
  23/24/25시간을 분모로 사용한다.
- focus coverage는 요청한 전체 구간을 분모로 사용한다. 부분 hour는 보존된
  raw event로 정확히 다시 계산하고, raw가 이미 만료됐다면 비례 추정하지 않고
  `partial_hour_requires_raw`로 실패한다.
- focus 구간에 일부 raw만 남고 이전 구간은 hourly summary에만 남아 있으면
  남은 raw만으로 전체 구간을 `exact`라고 표시하지 않는다. local-day summary의
  raw provenance가 완전할 때만 raw exact 경로를 사용하고, 그렇지 않으면
  장기 hourly summary로 전환하거나 coverage 부족을 명시한다.
- raw 보존기간을 벗어난 late upload는 저장하지 않고 conflict로 거부한다.
  만료된 raw·hourly·daily event는 maintenance가 아직 실행되지 않았어도
  REST와 MCP read path에서 노출하지 않는다.
- activity raw 보존기간을 줄이면 canonical raw의 expiry를 다시 계산하는 것과
  동시에 이미 만료된 Android compatibility row를 즉시 삭제한다. 보존기간을
  더 길게 늘리거나 무기한으로 바꿔도 이전의 더 짧은 정책에서 이미 만료된
  compatibility row는 먼저 물리 삭제해 다시 나타나지 않는다. scheduler가
  멈춰 있어도 cognitive-energy, insight와 MCP의 legacy read path는 같은
  retention cutoff보다 오래된 row를 읽지 않는다.
- retention 변경과 storage maintenance는 activity ingest/delete와 같은
  process-local write lock을 사용하고, REST·웹·scheduler 경로는 database
  commit이 끝날 때까지 lock을 유지한다. 정책 변경 직전의 규칙으로 뒤늦게
  activity row가 commit되는 경쟁을 막는다.
- PostgreSQL에서는 process-local lock에 더해 device별 transaction advisory
  lock을 사용한다. config, permission/status, cursor writer와 모든 canonical
  ingest가 같은 key를 사용하므로 아직 control row가 하나도 없는 최초
  disable/revoke도 최종 ingest 검증과 원자적으로 순서가 정해진다. row가 이미
  있을 때는 `FOR UPDATE + populate_existing`으로 ORM identity map까지 강제
  갱신한다. 호출자가 source 단계에서 이미 privacy filter를 적용했다고 표시한
  batch도 이 최종 경계를 생략하지 않으며, 잠금 뒤의 최신 revision, gate,
  exclude와 tombstone을 다시 검증한다.
- device별 control lock과 별도로 PostgreSQL activity write-plane 전체가 하나의
  transaction advisory lock을 공유한다. 서로 다른 휴대전화와 컴퓨터가 같은
  local hour/day에 동시에 저장되어도 두 transaction이 서로의 미커밋 raw를
  제외한 summary를 번갈아 덮어쓰지 않는다. canonical ingest, ActivityWatch
  최종 저장, Android backfill, summary rebuild와 v2 migration, baseline
  refresh, retention, 사용자 삭제와 activity retention policy 변경은 raw 또는
  summary를 읽기 전에 이 lock을 잡는다. 잠금 순서는 항상
  `write-plane -> device control`이며 transaction commit까지 유지한다.
  config, permission/status와 cursor를 단독으로 갱신하는 control writer도
  같은 순서를 사용하므로 canonical writer와 서로 반대 순서로 기다리는
  deadlock이 없고, 삭제된 control row를 선행 writer가 다시 만드는 경쟁도
  직렬화된다.
- PostgreSQL 앱 startup은 activity policy를 먼저 만들지 않는다. process-local
  lock과 write-plane transaction lock을 차례로 잡은 뒤 Android backfill과
  summary migration이 policy를 초기화한다. non-dry-run 공통 storage
  maintenance도 default policy bootstrap과 legacy migration보다 write-plane
  lock을 먼저 잡는다. 따라서 한 transaction이 미커밋 policy INSERT 뒤
  write-plane을 기다리고, 동시 ingest가 write-plane 뒤 같은 policy commit을
  기다리는 lock-order inversion이 없다. startup과 maintenance 두 경로는 빈
  PostgreSQL schema의 두 session 경쟁 테스트로 이 순서를 검증한다.
- 빈 PostgreSQL 저장소에서 여러 process가 동시에 시작해도 기본 retention
  policy는 `INSERT ... ON CONFLICT DO NOTHING`으로 생성한다. 먼저 성공한
  transaction의 정책을 보존하고 unique-key startup race로 기동을 실패시키지
  않는다.
- activity raw/hourly/daily retention 설정을 줄이면 정책 row만 바꾸고 끝내지
  않는다. 같은 transaction과 같은 기준 시각으로 activity maintenance를 즉시
  실행해 만료 row를 제거하고, 이후 보존되는 daily summary의 개인 baseline도
  바로 다시 계산한다. 설정 응답 직후 REST/MCP가 이전 보존기간의 stale
  baseline을 반환하지 않는다.
- daily summary가 보존기간 만료로 삭제되면 그 날짜를 7일 개인 baseline에
  사용하던 이후 daily summary도 같은 maintenance 기준 시각으로 다시 계산한다.
  이미 삭제된 summary를 raw에서 되살리지 않고, 남아 있는 후속 summary에서
  만료된 날짜의 수치만 제거한다.
- 공통 storage maintenance가 실행돼도 activity event를 일반 만료 삭제기로 먼저
  지우지 않는다. non-dry-run에서는 같은 기준 시각으로 activity maintenance를
  먼저 실행해 baseline 후속 갱신을 끝내고, 일반 event purge는 `activity.%`
  namespace를 제외한다.
- collector 시계 오염이 장기 저장과 summary expiry를 미래로 밀지 않도록
  `collected_at`, hourly bucket 시작과 detailed interval 종료가 서버 시계보다
  1분 넘게 미래면 `activity_future_data`로 거부한다. 기존 버전이 남긴 미래
  row는 device/global 전체 삭제에서 함께 제거한다.
- 수동 raw 삭제는 재전송으로 복구되지 않도록 영구 tombstone을 먼저 만들고,
  summary 직접 삭제 옵션과 관계없이 영향을 받은 local date/timezone scope를
  재집계한다. 범위 삭제와 전체 삭제 모두 실제로 지우는 모든 canonical raw와
  Android compatibility row의 `(provider, device, source_record_id)`를
  SHA-256 identity tombstone에 500개씩 나눠 기록한다. 따라서 같은 source
  identity의 timestamp를 삭제 범위 밖으로 바꿔도 복구되지 않지만, 삭제 이후
  새로운 source identity의 정상 활동은 저장된다.
- 수동 삭제가 확정되기 전에는 대상 device의 ActivityWatch import fence를
  증가시킨다. global 삭제는 현재 존재하는 모든 device fence를 증가시킨다.
  따라서 삭제 전에 localhost snapshot을 읽기 시작한 요청이 삭제 commit 뒤
  늦게 도착해 raw와 summary를 되살릴 수 없다. 이 fence는 공개 collection
  설정·상태·cursor 응답에 포함되지 않는 내부 ordering event이며,
  `include_control=true`로 visible control state를 삭제해도 보존한다. sequence를
  다시 1부터 사용하지 않으므로 삭제 전 prepared import는 영구히 stale하다.
- targeted 삭제에 필요한 raw provenance가 이미 만료돼 기존 장기 summary를
  정확히 다시 만들 수 없으면 삭제와 tombstone 생성을 모두 `409`로
  fail-closed한다.
- 일반 재집계도 현재 raw의 개수만 늘었다는 이유로 provenance가 불완전한
  장기 summary를 덮어쓰지 않는다. 기존 summary의 raw provenance가 이미
  불완전한 local scope를 바꾸려는 ingest는 저장 전 상태로 롤백하고 `409`로
  거부한다. 정확한 duplicate 재전송은 변경이 아니므로 계속 멱등 처리한다.
- ActivityWatch와 Android legacy backfill의 chunk/page는 각 단계마다 summary를
  갱신해 다음 단계가 항상 완전한 provenance에서 시작한다.
- Android legacy startup backfill이 이미 불완전해진 summary provenance를
  만나면 해당 legacy row는 보류하고 기존 summary를 보존한다. 한 scope의
  마이그레이션 충돌 때문에 서비스 기동이나 다른 device/page 마이그레이션을
  중단하지 않는다.
- collection on/off, 앱 제외, pause/resume, permission, queue, coverage와
  capability 상태는 UI 독립 REST 계약으로 제공한다.
- collection device path는 저장 컬럼과 같은 최대 255자 계약을 사용한다.
  256자 이상 ID는 control event를 만들기 전에 API에서 `422`로 거부한다.
- Android compatibility row와 canonical source identity는
  `(device, collection_generation, bucket, app)` 경계를 사용한다. 같은 시간
  bucket 안에서 권한, privacy 설정 또는 timezone이 바뀌어도 이전 generation을
  덮어쓰지 않고 두 구간을 모두 보존한다. 기존 row는 migration에서 generation
  `0`으로 이관되고 기존 canonical ID와 tombstone 호환성을 위해 generation
  `0`의 source ID 형식은 유지한다. 여러 generation을 하나로 합쳐야 하는
  downgrade는 데이터를 자동 삭제하지 않고 명시적으로 거부한다.
- Android가 저장한 IANA timezone 이름이 바뀌면 새 generation과 새 summary
  identity를 만든다. `Asia/Tokyo`와 `Asia/Seoul`처럼 같은 UTC offset을 가진
  서로 다른 IANA timezone도 named summary에서는 섞지 않는다. 호출자가
  `UTC+09:00` 같은 fixed-offset 이름이나 `tzinfo`를 쓰면 해당 관찰 시각의
  offset이 같은 IANA raw event를 호환 조회한다. 이 형식은 내부 호출뿐 아니라
  activity 공개 REST resolver와 MCP runtime timezone 계약에서도 지원한다.
  summary timezone에 fixed offset을 저장한 뒤에도 local-day 경계, retention
  scope, 후속 baseline refresh와 nutrition 일일 ledger 조회는 HealthMes 공통
  parser로 같은 fixed offset을 복원한다. 사진 capture, text/voice interaction,
  일일 섭취 완료 확인과 실제 caffeine candidate decision request도 같은
  parser를 사용하므로 `UTC+09:00`이 ledger-only fallback에서만 동작하는
  불완전한 계약이 아니다. `Settings.timezone`, `HEALTHMES_TIMEZONE`, 앱
  lifespan startup, MCP `set_timezone`과 override 없는 기본 resolver도 같은
  parser를 사용한다. 이 호환 조회는 비대칭이다. fixed-offset summary는 같은
  관찰 시각 offset의 IANA raw를 포함할 수 있지만, IANA named summary는 이름이
  다른 raw를 포함하지 않는다. 따라서 이미 materialize된 fixed-offset summary와
  겹치는 IANA raw가 생성·교체되거나 ActivityWatch authoritative repair 또는
  수동 삭제로 사라지면 해당 fixed-offset local-day도 함께 provenance 검사 후
  강제 재집계한다. raw 수가 `2 -> 1`로 줄어드는 경우에도
  `raw_event_count`와 evidence digest를 남은 raw 기준으로 갱신한다.
- collection status는 `status_observed_at`을 함께 저장한다. iOS처럼
  generation이 없는 status는 관찰 시각이 오래된 update를 무시하고 같은 시각의
  grant/revoke 충돌에서는 차단 상태를 우선한다. Android는 generation을
  1차 순서로 사용해 낮거나 같은 generation의 grant가 최신 차단 상태를
  되돌리지 못하게 한다. 서버 시계보다 1분 넘게 미래인 public status 시각은
  저장 전에 거부한다.
- Android status에 `collection_generation`이 있으면
  `platform=android`, `capability=aggregate`, `permission_status`,
  `status_observed_at`이 모두 있어야 한다. batch 저장 시에도 요청 generation
  일치만 보지 않고, 잠금 재조회한 persisted boundary가 실제
  `android + aggregate + granted`인지 다시 확인해 누락·위조·unknown 상태를
  fail-closed한다.
- 공개 generic canonical ingest의 record/group identity는 source device의
  opaque namespace로 다시 scope한다. 서로 다른 기기가 같은 source-local ID를
  사용해도 한 기기의 row가 다른 기기의 row를 덮어쓰지 않는다. migration 전에
  저장된 unscoped ID가 같은 provider/device에 있으면 해당 ID와 group을 유지해
  기존 재전송을 중복 row로 만들지 않는다. 이미 삭제되어 row가 없더라도 기존
  unscoped identity의 영구 tombstone이 있으면 그 ID를 유지해 재전송을 계속
  억제하고, 다른 기기의 충돌만 새 namespace로 분리한다.
- 한 provider/device가 같은 시간 구간에 hourly aggregate와 detailed interval을
  함께 보내면 중복 계산을 막기 위해 ingest를 거부한다.
- ActivityWatch는 최신 호환 bucket을 선택하고 한 번에 최대 7일만 가져온다.
  AFK bucket이 없거나 AFK/not-AFK event가 요청 구간 전체를 gap 없이 덮지
  못하면 아무것도 저장하지 않고 cursor도 전진시키지 않는다. 반환된 source
  event가 요청 범위를 넘어도 저장 interval은 정확히 `[start, end)`로 자르고,
  source event identity는 import window와 무관하게 안정적으로 유지한다.
  하나의 원본 window event에서 나온 여러 AFK 교차 조각에는 공통
  `source_group_id`를 저장한다. AFK event ID가 바뀌어 canonical row identity가
  교체되어도 launch 소유권은 이 그룹 안에서 정확히 한 번만 유지된다.
- ActivityWatch는 localhost 조회 전에 device별 monotonic import sequence를
  짧은 transaction으로 예약하고 즉시 commit한다. 실제 bucket/event network
  조회 중에는 process lock이나 database lock을 잡지 않는다. 같은 device에서
  나중에 시작한 import, collection config 변경, permission/status 경계 또는
  사용자 삭제는 sequence를 증가시킨다. 최종 저장 단계에서 예약 sequence가
  현재 sequence와 다르면 먼저 시작한 snapshot 전체를
  `409 stale_activitywatch_import`로 거부한다. 이 계약은
  **latest-started-wins**이므로 더 최신 요청이 source 오류로 실패해도 오래된
  snapshot을 자동 복구하지 않고 다음 import를 다시 시도한다.
- ActivityWatch localhost 조회와 normalize가 끝난 뒤에는 raw, summary, status,
  cursor를 쓰기 전에 config/status/cursor와 내부 import fence를 잠금
  재조회한다. 그 사이 disable, pause 또는 revoke가 확인되면 모든 저장을
  중단하고, config revision이 바뀌었으면 stale revision으로 거부한다. 최종
  단계는 process lock, PostgreSQL write-plane lock, device advisory lock
  순서로 진입하고 raw, summary, status와 cursor의 transaction commit까지
  유지한다. 저장에는 재조회한 최신 경계만 사용한다. 따라서 오래된 non-empty
  authoritative snapshot이 최신 empty/corrected snapshot보다 늦게 확정돼
  삭제·수정된 raw, summary 또는 cursor 상태를 되살릴 수 없다.
- ActivityWatch localhost HTTP client는 `trust_env=False`로 생성한다.
  `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` 또는 잘못된 `NO_PROXY`가 설정돼도
  localhost 요청을 외부·로컬 프록시로 우회하지 않으므로, 프록시가 bucket/event
  응답을 위조하거나 private app activity를 중계할 수 없다.
- 자동 탐색 또는 explicit 설정으로 얻은 ActivityWatch bucket ID는 URL에 직접
  이어 붙이지 않고 정확히 하나의 percent-encoded path segment로 만든다.
  bucket 문자열 안의 `/`, `..`, `?`, `#`, `%2F`가 localhost의 다른 endpoint,
  query 또는 fragment로 해석되지 않는다. URL 표준에서 percent-encoding되지
  않는 단독 `.`/`..`와 빈 ID는 event 요청 전에 fail-closed로 거부한다.
- 자동 cursor overlap 안에서 처음 늦게 발견된 source event도 launch를
  보존한다. 중복 방지는 cursor 이전 launch를 무조건 0으로 만드는 방식이 아니라
  기존 canonical identity와 `source_group_id` reconciliation이 담당한다.
- 각 ActivityWatch import 범위는 그 범위의 authoritative snapshot이므로,
  이전 행의 범위 밖 조각만 보존하고 겹치는 부분은 현재 source 결과로
  원자적으로 교체한다. 따라서 자동 cursor의 5분 overlap과 explicit repair
  모두 source event의 연장·축소·삭제 정정을 반영하면서 launch 수의 멱등성을
  유지한다. 동일한 middle repair를 반복할 때 범위 시작과 정확히 맞닿은 왼쪽
  launch-owner 조각은 읽기 전용 ownership context로 조회하며, 그 범위 밖
  조각을 삭제하거나 다시 쓰지 않고 전체 그룹의 launch를 정확히 1회로 유지한다.
- source가 빈 결과를 반환하고 해당 범위의 raw가 이미 만료됐더라도 장기
  summary를 0으로 덮지 않는다. 요청 범위가 덮는 모든 local-day scope의 raw
  provenance를 먼저 확인하고, 불완전하면 repair 전체를 fail-closed한다.
- source JSON 또는 window/AFK event 한 행이라도 malformed면 해당 범위를
  빈 authoritative snapshot으로 해석하지 않는다. reconciliation 전에 import
  전체를 중단하고 REST에서는 `502 activitywatch_error`로 반환한다.
  매우 큰 duration처럼 Python 변환은 통과해도 canonical Pydantic 계약으로
  표현할 수 없는 값도 같은 오류 경계로 변환하며 HTTP `500`으로 새지 않는다.
- 같은 source identity가 7일 reconciliation lookback 밖의 날짜로 이동하거나,
  repair가 다른 timezone의 보존 조각을 만들면 ingest 결과의 이전·신규
  date/timezone scope를 모두 모아 summary를 재생성한다. 과거 날짜 summary나
  새 timezone summary를 stale 상태로 남기지 않는다.
- 처음 명시 범위로 가져올 때는 자동 cursor를 초기화할 수 있지만, cursor가
  생긴 뒤의 explicit repair는 그 cursor를 앞이나 뒤로 움직이지 않는다.
  repair한 과거 시각 때문에 다음 자동 import가 7일 제한을 넘거나, 미래
  repair 때문에 아직 수집하지 않은 구간을 건너뛰는 일을 막는다.
- 명시적 ActivityWatch 범위의 미래 시각, 역전 또는 7일 초과는 source discovery
  전에 검증하고 `422 invalid_activitywatch_range`로 반환한다. deterministic
  caller 오류를 localhost upstream 장애인 `502`로 가장하지 않는다.
- iOS는 OS가 허용한 aggregate 또는 명시적 unavailable 상태만 받으며 detailed
  app timeline이나 가짜 0분을 만들지 않는다.
- Activity maintenance REST는 호출자가 임의의 `now`를 주입할 수 없고 서버
  시계만 사용한다. 테스트와 scheduler 내부 함수만 명시적 기준 시각을 받는다.
- Android uploader는 stale revision, collection race와 concurrent write
  conflict만 재시도한다. 보존기간 초과와 source mode 충돌처럼 반복해도
  해결되지 않는 sample-level `409`는 실패 chunk를 이분 탐색해 실제 거부된
  단일 sample만 버리고 이후 시간순 sample을 계속 보낸다. 불완전 summary
  provenance는 하루 summary scope 전체의 충돌이므로 sample-local로 격리하지
  않고 watermark를 멈춘다. 이 격리는 명시적으로 허용한 conflict code에만
  적용하며, code가 없거나 malformed 또는 unknown인 `409`도 sample을 버리지
  않고 fail-closed한다.
  transient/unknown 오류에서는 이미 성공한 chunk가 있어도 watermark를
  전진시키지 않아 전체 source range가 멱등 재시도된다.
- Android uploader는 network I/O 동안 collection-state lock을 잡지 않는다.
  permission 또는 generation이 바뀌면 다음 chunk 전에 pass를 취소하고
  watermark를 전진시키지 않는다. 마지막 chunk 중 경계가 바뀐 경우에도 성공
  응답 뒤 generation을 다시 확인하므로 이전 snapshot을 완료 처리하지 않는다.
- Android permission status와 batch는 같은 monotonic generation handshake를
  사용한다. 늦게 도착한 이전 `granted` status와 이전 generation batch는 최신
  `revoked` 뒤에 저장되거나 수집 gate를 다시 열 수 없다.
- daily `device_count`는 한 시간의 최대 기기 수가 아니라 그 local day의 모든
  hourly evidence에 나타난 opaque device namespace의 합집합이다. 서로 다른
  시간에 활동한 기기도 빠뜨리지 않는다. 집계 identity는 device 문자열 하나가
  아니라 `(source_provider, source_device)` 쌍이므로, 서로 다른 provider가
  우연히 같은 device ID를 사용해도 한 source의 hourly/interval evidence가
  다른 source를 억제하지 않는다.
- 이 집계 의미 변경은 hourly/daily `derived_from.derivation_version=2`로
  명시한다. startup은 Android canonical backfill 뒤에 legacy summary를
  검사하고, raw count와 digest가 모두 남아 있는 local scope만 v2로 강제
  재집계한다. raw가 일부라도 사라진 legacy summary는 잘못된 수치를 재발행하지
  않고 `legacy_activity_summary_incompatible`로 차단하며, 개인 baseline도
  현재 derivation version만 사용한다.
- Activity summary, focus, overwork와 bounded cross-domain resolver는 REST와
  MCP에서 같은 결정론적 엔진을 사용한다.
- Open Wearables readiness의 freshness는 최상위 필드뿐 아니라
  `sleep_debt.last_night.recorded_at` 같은 중첩 근거까지 재귀적으로 계산한다.
  유효한 수면 시각이 있는데 `unavailable`로 낮추지 않는다. REST와 MCP에
  전달하는 wearable context는 top-level과 6개 readiness block,
  `last_night`, `current`, `charge.entries`, freshness와 coverage 각각의
  명시적 allowlist만 통과시킨다. 등록되지 않은 raw timeseries, sample과
  provider 원문 payload는 어떤 중첩 깊이에서도 agent context에 노출하지 않는다.
- 한 번의 summary rebuild, baseline refresh와 retention maintenance는 호출자가
  주입한 하나의 `now`를 모든 expiry 판정에 끝까지 전달한다. 같은 DB와 입력,
  같은 기준 시각이면 실제 실행 시각과 관계없이 같은 baseline 결과를 만든다.
  bounded resolver의 activity summary, focus hourly fallback, recovery와
  overwork도 요청의 동일한 `now`를 daily expiry와 개인 baseline까지 전달한다.
- `caffeine_for_focus` resolver는 활동 context만 있다는 이유로 판단 가능 상태가
  되지 않는다. 같은 local day와 timezone의 카페인 후보 근거,
  `status=known`인 specialist ledger의 유한한 0 이상
  `confirmed_caffeine_mg`, 완료 확인된 당일 섭취 boundary가 모두 있을 때만
  `decision_ready=true`를 반환한다.

실제 iOS/Android/macOS/Watch 화면, 디바이스 dogfood, 실시간 동기화와 Hermes
adaptation은 이 완료 선언에 포함되지 않는다.
