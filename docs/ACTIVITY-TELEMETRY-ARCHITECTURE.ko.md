# HealthMes 컴퓨터·휴대폰 활동 텔레메트리 아키텍처

> **기준일:** 2026-08-08
>
> **문서 지위:** 후속 구현 PR의 계약 기준. 이 문서 자체는 새 수집기를
> 구현하지 않는다.
>
> **목표:** Rize와 비슷하게 컴퓨터·휴대폰 사용 시간과 전환 맥락을 수집하되,
> 모든 입력을 HealthMes의 통합 저장소와 보존정책 안에 넣고 웰니스 판단에
> 재사용한다.

## 0. 기능 경계

```text
┌───────────────────────────────────────────────────────────────┐
│ 수집하는 것                                                   │
│ 앱/웹 카테고리, 활성 구간, idle/lock, 실행·전환 횟수,        │
│ 선택적 앱 식별자·registrable domain, 기기 간 동시 사용       │
├───────────────────────────────────────────────────────────────┤
│ 기본적으로 수집하지 않는 것                                  │
│ 키 입력, 클립보드, 알림 본문, 메시지 내용, 화면 픽셀,        │
│ 전체 URL/query, 문서 내용, 비밀번호                           │
├───────────────────────────────────────────────────────────────┤
│ HealthMes가 만드는 것                                        │
│ 집중 세션, 분절도, 휴식 간격, 야간 사용, 기기 전환,          │
│ 수면·스트레스·일정과의 상관관계 및 근거가 붙은 웰니스 맥락   │
└───────────────────────────────────────────────────────────────┘
```

“모든 활동”은 사용자가 어떤 앱·기기에서 언제 활동했는지를 최대한 빠짐없이
관찰한다는 뜻이다. 사용자가 입력한 글이나 화면 내용을 감시한다는 뜻이 아니다.
웰니스 판단에는 콘텐츠보다 시간, 전환, 휴식, 사용 카테고리, 수면 전 사용 패턴이
더 직접적으로 필요하다.

## 1. 현재 구현 상태

### 구현되어 있음

```text
Android UsageStatsManager
        │
        │ 시간별 package / foreground seconds / launches / category
        ▼
POST /v1/app-usage/batch
        │
        ▼
app_usage_sample
        │
        ├─ cognitive-energy fragmentation penalty
        └─ stress timeline likely_context
```

- `apps/android-usage/`가 Android 앱 사용 이벤트를 시간별로 묶는다.
- WorkManager가 대략 30분마다 HealthMes에 재전송한다.
- 서버는 `(device_id, bucket_start, app_package)`를 natural key로 upsert한다.
- 현재 수집기는 창 제목, 알림 내용, 키보드 입력, 앱 내부 내용을 읽지 않는다.

### 아직 구현되지 않음

- `app_usage_sample`을 공통 `WellnessEvent`와 보존정책에 연결하는 canonical ingest
- macOS, Windows, Linux의 active-app·idle 수집
- 브라우저 domain 어댑터
- Android screen-on, unlock, notification-count 같은 추가 신호
- iOS의 지역·entitlement별 Screen Time 경로
- 기기 간 겹침, 중복, 세션 연결
- raw → session → hourly/daily aggregate lifecycle
- 텔레메트리별 수집 동의, privacy level, pause, delete 제어
- 활동 데이터를 읽는 전용 MCP 인터페이스

따라서 현재 Android 기능은 유용한 첫 수집기이지만, 아직 HealthMes의 통합
웰니스 입력 플랫폼 전체 구조는 아니다.

## 2. 채택 아키텍처

```text
┌──────────────────── Device Edge ────────────────────┐
│ OS collector / ActivityWatch / browser extension   │
│       │                                             │
│       ▼                                             │
│ local sanitizer + session segmenter                │
│       │                                             │
│       ▼                                             │
│ encrypted durable upload queue                     │
└──────────────────────┬──────────────────────────────┘
                       │ at-least-once batch
                       ▼
┌──────────── Personal Data Node: canonical store ─────────────┐
│ WellnessEvent                                                │
│   activity.app-interval.v1                                   │
│   activity.device-state.v1                                   │
│   activity.notification-count.v1                             │
│   activity.browser-context.v1                                │
│   activity.collection-gap.v1                                 │
│                                                              │
│            normalization + immutable derivation              │
│                       │                                      │
│             ┌─────────┼─────────┐                            │
│             ▼         ▼         ▼                            │
│          sessions   hourly    daily                          │
│             │         │         │                            │
│             └─────────┴────┬────┘                            │
│                            ▼                                 │
│                  MCP wellness context                        │
└────────────────────────────┬─────────────────────────────────┘
                             ▼
                 Hermes/HealthMes decision skill
                             ▼
                  calendar / app / web surfaces
```

### 책임 분리

**Device Edge**

- OS 권한을 요청하고 취소 상태를 감지한다.
- 아직 끝나지 않은 foreground interval을 로컬에서만 유지한다.
- 원시 window title과 URL을 privacy policy에 따라 삭제·축약·해시한다.
- 업로드 성공 확인 전까지 암호화 큐에 보존하되, 기기별 용량·최대 14일의
  transport budget을 넘기지 않는다.
- queue overflow나 OS 삭제로 전송하지 못한 구간은 조용히 0분으로 만들지 않고
  `activity.collection-gap.v1`으로 손실 범위와 원인을 남긴다.

**Personal Data Node**

- 유일한 쓰기 정본이다.
- source key 중복을 제거하고 공통 이벤트 envelope로 저장한다.
- 세션, 시간별 집계, 일별 특징을 불변 파생 이벤트로 만든다.
- 데이터 클래스별 보존정책과 삭제를 실행한다.

**Hermes/Wellness Agent**

- 저장소를 직접 추측하지 않고 MCP의 제한된 aggregate/context만 읽는다.
- raw title, 전체 URL, 키 입력, 알림 본문을 모델 context로 가져오지 않는다.
- 상관관계를 원인으로 단정하지 않고 coverage와 confidence를 함께 설명한다.

## 3. 공통 이벤트 계약

기존 `WellnessEvent` envelope를 그대로 사용한다.

```text
event_type
schema_version
observed_at / recorded_at / timezone
source_provider / source_device / source_record_id
capture_method
quality_flags / confidence / coverage
sensitivity / consent_scope
retention_policy_id / expires_at
payload
raw_object_id
derived_from
```

### 3.1 `activity.app-interval.v1`

하나의 기기에서 한 앱이 실제 foreground였던 닫힌 시간 구간이다.

```json
{
  "interval_start": "2026-08-08T09:00:00+09:00",
  "interval_end": "2026-08-08T09:12:42+09:00",
  "active_seconds": 742,
  "app": {
    "stable_id": "hmac:...",
    "platform_id": null,
    "display_name": null,
    "category": "productivity"
  },
  "state": {
    "idle": false,
    "locked": false
  },
  "privacy_level": "category_only",
  "collector_version": "activity-edge-v1",
  "sequence": 1842
}
```

- 기본값은 `category_only`다.
- 앱별 분석을 켜면 `stable_id`를 기기별 HMAC으로 저장한다.
- package name, bundle ID, 실행 파일 경로의 평문 저장은 별도 opt-in이다.
- 진행 중인 구간은 서버에 덮어쓰지 않는다. app switch, idle, lock, shutdown,
  최대 heartbeat gap에서 구간을 닫은 뒤 immutable event로 전송한다.

### 3.2 `activity.device-state.v1`

```text
screen_on | screen_off | unlocked | locked | idle | active
```

세션 경계와 “휴대폰을 집어 든 횟수”, “컴퓨터에서 실제로 자리를 비운 시간”을
계산하는 데 쓴다. 생체 인증 결과나 잠금 비밀번호는 저장하지 않는다.

### 3.3 `activity.notification-count.v1`

```json
{
  "bucket_start": "2026-08-08T09:00:00+09:00",
  "bucket_minutes": 60,
  "delivered_count": 18,
  "interacted_count": 3,
  "category_counts": {
    "communication": 9,
    "work": 5,
    "other": 4
  }
}
```

- 알림 제목, 본문, 발신자 이름은 저장하지 않는다.
- Android에서만 명시적 Notification Access opt-in으로 고려한다.
- iOS에서 OS가 제공하는 Screen Time aggregate가 있으면 그 aggregate만 사용한다.

### 3.4 `activity.browser-context.v1`

```json
{
  "interval_start": "2026-08-08T09:12:42+09:00",
  "interval_end": "2026-08-08T09:28:10+09:00",
  "parent_app_interval_source_record_id": "v1:...",
  "browser_id": "hmac:...",
  "registrable_domain": "github.com",
  "category": "development",
  "privacy_level": "domain"
}
```

- 기본 비활성이다.
- 허용하더라도 scheme, path, query, fragment는 edge에서 버린다.
- private/incognito window는 수집하지 않는다.
- 사용자는 domain denylist와 “category만 저장”을 선택할 수 있어야 한다.
- browser context는 같은 시간의 `activity.app-interval.v1`을 설명하는 enrichment다.
  별도 active minutes로 합산하지 않는다.
- `parent_app_interval_source_record_id`가 없거나 부모와 시간이 맞지 않으면
  격리하고 aggregate에 포함하지 않는다.

## 4. 파생 데이터 계층

```text
원시·정규화 interval
        │
        ├─ app switch / idle / lock / heartbeat gap
        ▼
activity.session.v1
        │
        ├─ 시간대별 합산
        ▼
activity.hourly-summary.v1
        │
        ├─ 기기·날짜·timezone별 합산
        ▼
activity.device-daily.v1
        │
        ├─ 같은 날짜의 기기별 contribution 결합
        ▼
activity.daily-summary.v1
        │
        └─ 수면·스트레스·일정과 결합
           activity.wellness-insight.v1
```

### 세션 종료 규칙

- foreground 앱이 바뀜
- idle 상태가 기본 5분 이상 지속됨
- 화면 잠금 또는 사용자 로그아웃
- collector heartbeat가 기본 5분 이상 끊김
- timezone 또는 local date가 바뀜
- collector가 정상 종료됨

### 시간별 집계

- active minutes
- idle minutes
- app/category별 minutes
- app switches
- device switches
- notification counts
- longest uninterrupted block
- multi-device overlap minutes
- coverage와 collector gap

### 일별 특징

- 총 active time
- 집중 카테고리 시간과 가장 긴 집중 세션
- 시간당 context switch 분포
- 22:00 이후 screen time
- 첫 사용·마지막 사용 시각
- 25/50/90분 연속 사용 뒤 휴식 비율
- 일정 회의 시간 대비 실제 집중 시간
- phone ↔ desktop 전환 횟수

### 장기 집계의 기기별 contribution

raw interval이 만료된 뒤에도 특정 기기의 데이터를 삭제할 수 있어야 한다.
따라서 장기 `activity.daily-summary.v1` 하나에 총합만 저장하지 않는다.

```text
activity.device-daily.v1
  local_date
  timezone
  pseudonymous_device_id
  device_type
  per-device metrics
  compressed attention_segments[]
  source coverage

activity.daily-summary.v1
  local_date
  timezone
  contributing_device_ids[]
  device_contributions[]
  combined metrics
```

`device_contributions`는 raw title이나 URL이 아니라 active minutes, category
minutes, switches, idle, coverage와 압축된 `attention_segments`만 담는다.
`attention_segments`는 하루의 minute offset, active/idle/unknown, coarse category를
run-length encoding한 작은 시간축이다. 앱 이름, title, URL, 입력 내용은 없다.

단순 일별 총합만으로는 두 기기의 겹침과 phone → desktop 전환 순서를 다시 계산할
수 없다. combined daily와 insight는 이 기기별 압축 시간축에서 결정적으로 다시
만들 수 있어야 하며, raw source event ID나 합계만 남기는 방식에 의존하지 않는다.
같은 minute 안의 순서를 구분할 수 없으면 `ambiguous_transition`으로 표시하고
정확한 순서를 만들어내지 않는다.

### 파생 이벤트 identity와 교체

session, hourly, device-daily, combined daily, insight는 재계산할 때 기존 행을
덮어쓰거나 같은 결과를 중복 추가하지 않는다.

```text
aggregate_key
  = event_type + user_id + local/window boundary + timezone
    + pseudonymous_device_id(optional)

source_provider
  = healthmes-activity-derived

source_record_id
  = v1:<event_type>:<aggregate_key_sha256>:<algorithm_version>:<input_watermark_sha256>

payload
  algorithm_version / revision / content_sha256 / input_watermark

derived_from
  source event IDs 또는 바로 아래 contribution event IDs

supersedes_event_id
  같은 aggregate_key의 직전 active revision
```

- 동일 input watermark와 동일 payload 재계산은 기존 event를 반환한다.
- input이 바뀌면 새 immutable revision을 만들고 직전 revision을
  `supersedes_event_id`로 연결한다.
- reader는 aggregate key별 최신 non-quarantined revision만 사용한다.
- 기기·기간 삭제 뒤에는 영향받은 revision을 tombstone 처리하고 남은 contribution로
  새 revision을 만든다. 남은 근거가 없으면 replacement를 만들지 않는다.

## 5. 교차 기기 결합

동시에 켜진 두 기기의 raw event를 삭제하거나 하나로 합치지 않는다.

```text
Mac active:    09:00 ─────────────────── 09:30
Phone active:        09:08 ─ 09:11

원본:
  Mac interval과 Phone interval을 모두 보존

파생:
  09:08-09:11 = multi_device_overlap
  desktop → phone → desktop = interruption candidate
```

### primary-attention 파생 규칙

1. 한 기기만 active면 그 기기를 primary로 둔다.
2. desktop이 active이고 phone이 짧게 켜지면 `possible_interruption`으로 표시한다.
3. desktop이 idle이고 phone이 active면 phone을 primary로 둔다.
4. 두 기기가 모두 장시간 active면 하나를 임의 선택하지 않고
   `ambiguous_multi_device`로 남긴다.
5. 이 결과는 관측값이 아니라 derived event이며 모든 source event ID를
   `derived_from`에 남긴다.

시계 오차가 큰 기기는 `quality_flags=["clock_skew"]`로 표시하고 교차기기
세션화 confidence를 낮춘다.

## 6. 개인정보 수집 레벨

```text
Level 0  off
         어떤 활동도 수집하지 않음

Level 1  category_only                          기본값
         category + active duration + switches + idle/lock

Level 2  app_identity
         HMAC app identity + category + duration

Level 3  domain_or_title                        별도 opt-in
         registrable domain 또는 redacted title

Level 4  content_capture                        지원하지 않음
         keylogging, clipboard, notification body, screenshot, full URL
```

Level 3의 window title은 기본적으로 서버에 저장하지 않는다. edge에서 사용자가
정한 규칙으로 `meeting`, `coding`, `writing`, `entertainment` 같은 category를 만든
뒤 원문을 버리는 방식을 우선한다.

### 권한 변화 처리

- 권한이 없어지면 수집을 즉시 중지한다.
- “데이터 없음”과 “0분 사용”을 구분한다.
- 이벤트에 collector capability와 coverage를 기록한다.
- 설정 화면은 기기별 마지막 수집 시각, 권한 상태, gap, queue 크기를 보여준다.

## 7. OS별 수집 전략

### Android

**현재 유지**

- `UsageStatsManager.queryEvents`
- package, foreground seconds, launches, category
- WorkManager를 통한 best-effort 주기 업로드

**후속 추가**

- screen interactive/non-interactive, keyguard shown/hidden 이벤트
- 종료된 5분 또는 앱 전환 interval
- NotificationListenerService의 내용 없는 시간별 count
- encrypted SQLite upload queue와 cursor

WorkManager 주기는 정확한 타이머가 아니다. OS 배터리 정책에 따라 지연될 수
있으므로 `recorded_at`이 아니라 OS event의 `observed_at`으로 시간대를 복원한다.

### iOS/iPadOS

2026년에는 두 경로를 분리해야 한다.

**EU + iOS/iPadOS 26 이상**

- `FamilyActivityData.activityData(filteredBy:using:)` 경로를 검토한다.
- `com.apple.developer.family-controls.app-and-website-usage` entitlement가 필요하다.
- 사용자가 `approvedWithDataAccess` 권한을 명시적으로 승인해야 한다.
- Apple 문서상 EU에서만 제공되고 한 기기에서 한 앱만 이 접근을 가질 수 있다.
- app, web domain, category, activity duration, pickups, notifications aggregate를
  받을 수 있다.
- Family Controls 사용 조건은 개인·자기관리 목적을 강하게 제한하므로 관리형
  기업 모니터링 기능과 섞지 않고 별도 법률·App Review 검토가 필요하다.

**미국 등 비EU 또는 entitlement 미승인**

- off-device Screen Time export를 제품 전제로 두지 않는다.
- HealthMes telemetry 수집 상태를 `unavailable`로 표시한다.
- 사용자의 별도 수동 회고 입력은 일반 wellness journal 계약으로 받을 수 있지만,
  screenshot·Screen Time export를 activity telemetry로 가장하지 않는다.
- HealthMes 엔진은 iOS 활동 신호가 없을 때 해당 factor를 제외하고 confidence를
  낮춘다.

런타임 feature gate는 최소한 `OS version × region × entitlement × authorization`
네 조건을 모두 확인해야 한다.

### macOS

**P0: ActivityWatch sidecar adapter**

- `aw-watcher-window`의 active application/window event
- `aw-watcher-afk`의 AFK event
- localhost REST API에서 cursor 기반 증분 import
- HealthMes edge adapter에서 title을 즉시 삭제·redact

**P1: HealthMes native collector**

- `NSWorkspace.didActivateApplicationNotification`으로 app switch 관찰
- `CGEventSource.secondsSinceLastEventType`으로 idle 추정
- lock/sleep/wake notification으로 세션 종료
- window title은 Accessibility 권한을 별도로 받은 경우에만 opt-in

Screen Recording 권한으로 화면 픽셀을 수집하는 방식은 MVP 범위에서 사용하지
않는다.

### Windows

**P0: ActivityWatch sidecar adapter**

- macOS/Linux와 같은 import 계약을 사용한다.

**P1: HealthMes native collector**

- `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`으로 foreground 변경 구독
- `GetForegroundWindow`와 `GetWindowThreadProcessId`로 process 연결
- `QueryFullProcessImageName`으로 app identity 생성
- `GetLastInputInfo`로 session-local idle 계산
- `WTSRegisterSessionNotification`으로 lock/unlock/session change 관찰
- `GetWindowText` title은 별도 opt-in이고 edge redaction 후 폐기

Windows의 `GetLastInputInfo`는 전체 조직이나 원격 사용자의 활동을 측정하는
API가 아니라 현재 session 기준 idle 신호로만 사용한다.

### Linux

- 첫 구현은 ActivityWatch API adapter를 채택한다.
- X11/Wayland/desktop environment별 지원 차이는 collector capability에 기록한다.
- Wayland 보안 경계를 우회하기 위한 화면 캡처나 전역 입력 후킹은 하지 않는다.
- native collector는 ActivityWatch로 충족되지 않는 배포 요구가 생긴 뒤 검토한다.

### Browser

- Chrome/Chromium·Firefox extension은 별도 설치와 명시적 tabs/host permission이
  필요하다.
- 기본은 active tab의 registrable domain과 체류 interval만 전송한다.
- optional permission으로 시작하고 사용자가 도메인 수집을 켤 때만 요청한다.
- 브라우저 history 전체를 가져오지 않는다.

## 8. ActivityWatch 도입 결정

2026-08-08 확인 기준으로 `ActivityWatch/activitywatch`는 약 18.5k GitHub star,
MPL-2.0이며 macOS/Windows/Linux의 window·AFK 수집기를 제공한다. 저장소 개발은
계속 진행 중이지만 최신 정식 release는 `v0.13.2`이므로 API를 그대로 신뢰하지
말고 adapter contract test로 고정한다.

### 채택 방식

```text
ActivityWatch process
      │ localhost only
      ▼
HealthMes ActivityWatchAdapter
      │ validate / redact / normalize / checkpoint
      ▼
HealthMes WellnessEvent
```

- ActivityWatch 코드를 HealthMes에 복사하거나 DB를 정본으로 사용하지 않는다.
- 사용자가 별도로 실행하는 sidecar의 localhost API만 읽는다.
- ActivityWatch API에는 인증이 없으므로 LAN 또는 인터넷에 노출하지 않는다.
- adapter는 아래 canonical identity 표의 bucket ID와 event ID를 source key로
  사용하고 timestamp는 payload의 관측 시각으로 검증한다.
- 원본 title은 기본 폐기하고 app/category/active duration만 canonical event로
  보낸다.
- HealthMes의 redaction·삭제는 HealthMes가 가져온 사본에만 적용된다.
  ActivityWatch 자체 DB의 원본 title과 history는 별도 신뢰 경계이며, 설정 화면은
  이 사실과 source-side 삭제 방법을 명확히 보여준다.
- MPL-2.0 코드를 직접 수정·배포하는 경우 파일 단위 의무를 별도로 검토한다.

### 자체 collector와 비교

```text
ActivityWatch adapter
  장점: 검증된 다중 OS 수집기, 빠른 출시, 기존 사용자 데이터 import
  단점: 별도 프로세스, API/version 차이, title privacy를 우리가 다시 통제해야 함

HealthMes native collector
  장점: 정확한 계약·권한·업로드 큐·privacy UX
  단점: OS별 유지보수와 서명·배포 비용이 큼

결정
  Desktop P0 = ActivityWatch adapter
  Desktop P1 = 필요 플랫폼부터 native collector
```

## 9. 저장·보존정책

사용자 activity 데이터 클래스는 기존 선택지
`1일 / 7일 / 14일 / 30일 / 90일 / 무기한`을 사용한다. 삭제 ledger는 데이터
보존 설정이 아니라 부활 방지를 위한 최소 시스템 메타데이터다.

```text
activity_raw
  기본 14일
  OS event, ActivityWatch import revision, collector diagnostic

activity_sensitive_context
  기본 7일
  opt-in domain, redacted title, app identity mapping

activity_session
  기본 30일
  닫힌 app/device focus session

activity_hourly
  기본 90일
  시간별 app/category/switch/idle aggregate

activity_device_daily
  기본 무기한
  기기·날짜별 작은 contribution, 압축 attention timeline, coverage

activity_daily
  기본 무기한
  기기별 contribution에서 재생성 가능한 교차기기 일별 특징

activity_insight
  기본 무기한
  근거 event ID와 confidence가 붙은 결정·인사이트

activity_deletion_ledger
  최소 opaque metadata 무기한
  삭제 device generation, source watermark, account/device hash만 보존
```

`activity_daily` 또는 `activity_insight`가 재계산 가능하다고 표시되는 기간에는
그 근거인 `activity_device_daily`도 남아 있어야 한다. 사용자가
`activity_device_daily` 보존기간을 더 짧게 줄이면 설정 서비스는 dependent
daily/insight 기간도 함께 줄이거나, 재계산할 수 없게 된 오래된 dependent
aggregate를 삭제해야 한다. 총합만 남겨 놓고 “기기 삭제 후 재계산 가능”이라고
표시하지 않는다.

### 저장 위치

```text
Postgres / SQLite
  WellnessEvent envelope, session, hourly/daily aggregate, cursor, policy

HEALTHMES_DATA_DIR
  대량 import 원본 또는 압축 chunk가 실제로 필요할 때만

Mobile/Desktop edge SQLite
  아직 전송되지 않은 닫힌 interval과 cursor
```

현재 `app_usage_sample`은 보존정책에 직접 연결되지 않는다. 후속 마이그레이션에서
이를 canonical source가 아니라 기존 cognitive-energy 엔진을 위한 projection으로
낮춘다.

```text
WellnessEvent(activity.*) = 정본
app_usage_sample           = 호환 read model
```

기존 Android endpoint는 즉시 제거하지 않는다. 새 canonical ingest가 같은 시간별
projection을 만든 뒤 두 경로의 결과가 일치하는 shadow test를 통과하면 전환한다.

## 10. 중복·재전송·삭제

### 이벤트 식별

`source_device`는 Personal Data Node에 등록된 물리 기기의 pseudonymous UUID다.
앱 재설치나 browser profile은 별도 collector installation ID를 갖지만 물리 기기
identity를 대신하지 않는다.

| Producer | `source_provider` | canonical `source_record_id` |
|---|---|---|
| 기존 Android 시간 bucket | `android-usage` | `v1:<collector_installation_id>:<bucket_start_utc>:<package_hmac>` |
| Android native interval/state | `android-native` | `v1:<collector_installation_id>:<boot_id>:<sequence>` |
| iOS FamilyActivity aggregate | `ios-family-activity` | `v1:<collector_installation_id>:<interval_start_utc>:<interval_end_utc>:<filter_sha256>` |
| ActivityWatch window | `activitywatch-window` | `v1:<collector_installation_id>:<bucket_id_hmac>:<event_id>` |
| ActivityWatch AFK | `activitywatch-afk` | `v1:<collector_installation_id>:<bucket_id_hmac>:<event_id>` |
| macOS native | `macos-native` | `v1:<collector_installation_id>:<boot_session_id>:<sequence>` |
| Windows native | `windows-native` | `v1:<collector_installation_id>:<boot_session_id>:<sequence>` |
| Linux native | `linux-native` | `v1:<collector_installation_id>:<boot_session_id>:<sequence>` |
| Browser enrichment | `browser-extension` | `v1:<profile_installation_id>:<browser_session_id>:<sequence>` |
| HealthMes derived | `healthmes-activity-derived` | `v1:<event_type>:<aggregate_key_sha256>:<algorithm_version>:<input_watermark_sha256>` |

- closed interval은 immutable이다.
- 같은 source key와 같은 payload 재전송은 기존 event를 반환한다.
- 같은 source key에 다른 payload가 오면 conflict로 거부한다.
- 진행 중인 interval 수정은 edge DB에서만 일어난다.
- collector installation ID와 device generation은 서버 등록 시 발급한다. 삭제된
  generation의 source key는 다시 받아들이지 않는다.

### cursor

- collector별 마지막 성공 sequence와 source timestamp를 저장한다.
- 재연결 시 cursor 이전의 작은 lookback을 다시 읽고 source key로 dedup한다.
- collector clock과 server clock 차이를 quality flag로 기록한다.
- 늦게 업로드된 event의 `expires_at`은 upload 시각이 아니라 interval/bucket
  종료 시각과 해당 retention policy로 계산한다.
- edge queue가 transport budget을 넘으면 오래된 구간을 조용히 버리지 않는다.
  허용된 coarse aggregate로 축약하거나 gap event를 남기고 coverage를 낮춘다.

### 삭제

- 원본 event가 만료되기 전에 필요한 aggregate를 만든다.
- 원본 삭제 뒤 aggregate에는 raw text가 아니라 source event ID/hash와 작은
  기기별 contribution만 남긴다.
- 사용자가 특정 기기를 삭제하면 한 transaction에서 collector credential을
  폐기하고 `device_generation` cutoff를 올린 뒤 deletion ledger를 먼저 기록한다.
- 삭제 generation에서 이미 대기 중이던 edge/cloud queue event와 지연 upload는
  ingest에서 거부한다.
- 그 기기의 raw, `activity_sensitive_context`, browser enrichment, session,
  hourly, `activity.device-daily.v1`, cursor, collector registration, object/chunk를
  삭제한다.
- 영향받은 `activity.daily-summary.v1`과 insight는 남은 device-daily
  contribution의 압축 attention timeline으로 새 immutable revision을 만들고
  이전 revision을 supersede한다.
- 과거 schema처럼 기기별 contribution이 없는 combined aggregate는 일부 값을
  임의로 빼지 않는다. 영향받은 날짜의 combined daily와 dependent insight
  전체를 삭제한다.
- 최소 deletion ledger는 content, domain, app name을 담지 않고 무기한 보존한다.
  모든 등록 replica·queue·backup generation이 deletion watermark를 넘었다는
  증거가 있을 때만 compact할 수 있다.
- backup 복원은 data replay보다 deletion ledger 적용을 먼저 수행한다.
- ActivityWatch 같은 외부 source DB 삭제는 별도 작업이다. HealthMes 기기 삭제가
  외부 DB까지 삭제했다고 표시하지 않는다.

## 11. MCP 인터페이스

UI와 에이전트가 같은 계약을 사용하도록 다음 read-only MCP를 후속 구현한다.

```text
get_activity_timeline(start, end, detail_level)
  detail_level = category | app | domain

get_attention_summary(date)
  active, focus, idle, switches, late_screen, coverage

get_fragmentation_context(start, end)
  switches, short sessions, notification counts, device overlaps

get_activity_wellness_context(date, lookback_days)
  수면·스트레스·일정과 결합 가능한 aggregate + confidence
```

### 반환 원칙

- 기본 `detail_level`은 `category`다.
- permission이 없으면 `unavailable`, 수집 gap이면 `insufficient_data`를 반환한다.
- 0분과 미수집을 구분한다.
- raw window title, full URL, 알림 본문은 MCP로 반환하지 않는다.
- 모든 insight는 evidence event IDs, coverage, limitations를 포함한다.

## 12. 만들 수 있는 웰니스 인사이트

- “90분 이상 연속 사용 뒤 휴식이 거의 없었다.”
- “수면이 짧은 날 오후 앱 전환이 개인 baseline보다 많았다.”
- “회의가 많은 날 실제 집중 세션이 짧아졌다.”
- “취침 2시간 전 screen time이 길었던 날 수면 시작이 늦었다.”
- “phone ↔ desktop 전환이 많은 시간대에 집중 블록 완수율이 낮았다.”
- “주말에도 업무 앱 사용이 이어져 회복 시간이 줄었다.”
- “알림 수가 많았지만 collector coverage가 낮아 결론 confidence는 낮다.”

다음 표현은 금지한다.

- “이 앱이 스트레스의 원인이다.”
- “생산성이 낮다.”
- “직원이 일하지 않았다.”
- coverage가 없는 시간대를 0분으로 처리한 비교

HealthMes는 개인 웰니스 도구다. 조직의 직원 감시·성과평가 제품으로 같은
텔레메트리를 재사용하지 않는다.

## 13. 설정 계약

설정 정본은 Personal Data Node이며 모바일·데스크톱 UI는 같은 API를 사용한다.

```text
Collection
  device enabled
  app usage enabled
  idle/lock enabled
  notification count enabled
  browser domain enabled

Privacy
  level 0/1/2/3
  app allow/deny rules
  domain allow/deny rules
  private browsing excluded

Retention
  activity_raw
  activity_sensitive_context
  activity_session
  activity_hourly
  activity_device_daily
  activity_daily
  activity_insight

Operations
  pause collection
  upload now
  delete one device
  delete date range
  show last sync / queue / coverage / permission status
```

기기의 OS 권한과 서버 설정이 다르면 더 제한적인 쪽이 이긴다.

## 14. 구현 순서

### PR A — canonical activity storage

- `activity.*` schema와 batch ingest
- activity retention classes
- source-key idempotency와 cursor
- 기존 Android payload를 canonical event로 변환
- `app_usage_sample` compatibility projection
- SQLite/PostgreSQL migration과 retention tests

**완료 조건:** 기존 cognitive-energy 결과가 유지되고 Android 후보 데이터가
`WellnessEvent`와 보존정책에서 조회·삭제된다.

### PR B — desktop ActivityWatch adapter

- bucket/event cursor
- window + AFK normalization
- loopback-only validation
- title redaction
- macOS/Windows/Linux fixture contract tests

**완료 조건:** 세 OS fixture가 같은 `activity.app-interval.v1`을 만든다.

### PR C — session·aggregate·MCP

- sessionizer
- cross-device overlap
- hourly/device-daily/combined-daily aggregate
- 압축 attention timeline, contributing device provenance, 기기 삭제 재계산
- activity MCP tools
- 기존 fragmentation engine을 aggregate read model로 전환

**완료 조건:** raw를 삭제해도 장기 aggregate 기반 판단이 가능하고, 특정 기기
삭제 뒤 남은 기기만으로 daily와 insight를 결정적으로 재생성할 수 있다.

### PR D — native collectors

- macOS native
- Windows native
- Android interval·device-state·notification count
- encrypted edge queue

**완료 조건:** 권한 취소, offline queue, 재전송, clock skew, shutdown 복구 테스트.

### PR E — iOS conditional path

- EU entitlement 승인 여부 확인
- iOS/iPadOS 26 `FamilyActivityData` adapter
- non-EU·권한 미승인 `unavailable` state
- Apple DPLA/App Review 검토 기록

**완료 조건:** 지원되지 않는 지역에서 기능을 가장하거나 private API를 쓰지 않는다.

## 15. 최종 결정

```text
정본
  Personal Data Node의 WellnessEvent

Desktop 시작점
  ActivityWatch sidecar adapter

Android
  기존 collector 유지 후 canonical event로 단계적 migration

iOS
  EU/iOS 26 entitlement 경로만 조건부 지원
  비EU는 off-device 수집을 전제로 하지 않음

기본 개인정보 수준
  category + time + switches + idle/lock

금지
  keylogging, clipboard, notification body, screenshot, full URL

장기 저장
  raw는 짧게, session/hourly는 중기, daily/insight는 작게 장기 보존
```

## 공식 자료

### Apple

- [FamilyActivityData](https://developer.apple.com/documentation/familycontrols/familyactivitydata)
- [Requesting activity data](https://developer.apple.com/documentation/familycontrols/familyactivitydata/activitydata(filteredby:using:))
- [App and website usage entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.family-controls.app-and-website-usage)
- [Family Controls capability](https://developer.apple.com/help/account/capabilities/family-controls)
- [NSWorkspace app activation](https://developer.apple.com/documentation/appkit/nsworkspace/didactivateapplicationnotification)
- [CGEventSource idle time](https://developer.apple.com/documentation/coregraphics/cgeventsource/secondsincelasteventtype(_:eventtype:))

### Android

- [UsageStatsManager](https://developer.android.com/reference/android/app/usage/UsageStatsManager)
- [UsageEvents.Event](https://developer.android.com/reference/android/app/usage/UsageEvents.Event)
- [NotificationListenerService](https://developer.android.com/reference/android/service/notification/NotificationListenerService)
- [WorkManager periodic work](https://developer.android.com/develop/background-work/background-tasks/persistent/getting-started/define-work)

### Windows

- [SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
- [EVENT_SYSTEM_FOREGROUND](https://learn.microsoft.com/en-us/windows/win32/winauto/event-constants)
- [GetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow)
- [GetLastInputInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo)
- [WTSRegisterSessionNotification](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/nf-wtsapi32-wtsregistersessionnotification)

### Browser·ActivityWatch

- [Chrome tabs API](https://developer.chrome.com/docs/extensions/reference/api/tabs)
- [Chrome permission model](https://developer.chrome.com/docs/extensions/develop/concepts/declare-permissions)
- [ActivityWatch repository](https://github.com/ActivityWatch/activitywatch)
- [ActivityWatch REST API](https://docs.activitywatch.net/en/latest/api/rest.html)
- [ActivityWatch watchers](https://docs.activitywatch.net/en/latest/watchers.html)
- [ActivityWatch security](https://docs.activitywatch.net/en/latest/security.html)
