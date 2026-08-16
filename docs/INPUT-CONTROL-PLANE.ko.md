# HealthMes 통합 입력 제어 평면

> **결정일:** 2026-08-14
>
> **지위:** 데스크톱 웹과 모바일 설정 UI가 공통으로 사용하는 엔진 계약.
>
> **범위:** 입력 탐색, 연결 상태, 수집 제어, Decision Agent 접근 동의,
> 데이터 클래스별 보존 정책과 UI action metadata. 실제 화면 구현은 포함하지
> 않는다.

## TLDR

HealthMes의 입력은 서로 다른 수집기를 사용하지만 설정 화면은 하나의 API만
보면 된다.

```text
Desktop Web Settings ─┐
                      ├─> /v1/inputs
iPhone Settings ──────┘         |
                                v
                    HealthMes Input Control Plane
                      |       |       |
                      |       |       +─ retention_policy
                      |       +───────── Decision domain consent
                      +───────────────── source/device collection control
                                |
                                v
                 기존 HealthMes 수집기와 통합 저장소
```

이 제어 평면은 새 저장소가 아니다. 기존 activity collection control,
storage retention, Decision Agent domain policy와 provider 연결 상태를 하나의
UI 독립 계약으로 합성한다.

중요하게도 모든 source에 같은 toggle을 강제로 만들지 않는다. Android,
ActivityWatch와 iPhone Screen Time만 현재 HealthMes가 실제로 강제하는
기기별 `enabled`, `paused_until`, `excluded_apps`를 노출한다. Nutrition,
Wearable과 Calendar는 실제 adapter가 제공하는 connect/disconnect/sync action,
보존정책과 Decision 접근 동의만 노출한다. collector가 읽지 않는 desired-state
행을 저장해 "꺼졌지만 계속 수집되는" 거짓 설정을 만들지 않는다.

## 1. 현재 입력 목록

`GET /v1/inputs`는 다음 source를 안정적인 `source_id`로 반환한다.

| Source ID | Domain | 현재 엔진 capability |
|---|---|---|
| `activity.android` | activity | 시간별 앱·카테고리 사용 |
| `activity.activitywatch` | activity | macOS/Windows/Linux foreground·idle·시간별 집계 |
| `activity.ios-screentime` | activity | 조건부 iPhone Screen Time 시간별 집계 계약; 일반 빌드는 unavailable |
| `nutrition.capture` | nutrition | 사진 VLM, 텍스트, 음성 transcript, 영양소, 카페인 |
| `wearable.healthkit-bridge` | wearable | 외부 HealthKit exporter의 raw-first 수신과 Open Wearables 전달 |
| `wearable.open-wearables` | wearable | 수면, 회복, HRV, 스트레스, 운동 |
| `calendar.google` | calendar | 일정 mirror, 가용 시간, 일정 밀도 |
| `calendar.icloud` | calendar | 일정 mirror, 가용 시간, 일정 밀도 |

새 GPS/location 입력은 이 목록에 아직 포함하지 않는다. Issue #158에서 iOS와 Android의
권한·백그라운드 제약, coarse 기본값, 장소 제외와 원본 좌표 보존 정책을 별도
구현 범위로 추적한다.

## 2. 공개 API

```text
GET /v1/inputs
GET /v1/inputs/{source_id}
PUT /v1/inputs/{source_id}/settings
```

모든 endpoint는 HealthMes의 기존 API 인증 경계를 그대로 사용한다.

### 목록과 상세

각 `InputSourceDescriptor`는 UI가 화면을 하드코딩하지 않도록 다음을 반환한다.

```json
{
  "source_id": "activity.ios-screentime",
  "domain": "activity",
  "display_name": "iPhone Screen Time",
  "platforms": ["ios"],
  "capabilities": ["hourly_app_usage", "hourly_category_usage"],
  "connection_state": "not_configured",
  "collection_state": "unavailable",
  "decision_access_enabled": true,
  "instances": [],
  "retention": [],
  "settings": [],
  "actions": [],
  "privacy": {},
  "limitations": [],
  "revision": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}
```

- `instances`: 기기별 enable, pause, 권한, capability, 마지막 수집과 제외 앱
- `settings`: UI가 노출할 수 있는 설정 key, 타입, scope와 허용값
- `actions`: authorize/connect/sync 같은 동작의 실행 위치와 기존 endpoint
- `privacy`: 원본 수집 여부, source-side 제외, 기본 LLM 노출 수준
- `limitations`: OS, entitlement 또는 아직 구현되지 않은 기능
- `revision`: descriptor 전체의 안정적인 SHA-256 digest. UI는 값이 바뀌면
  현재 화면을 갱신한다.

`actions`는 UI 명세이지 모든 동작을 대신 실행하는 범용 RPC가 아니다. iPhone
authorize/sync action은 향후 gate-enabled·entitled 기기 빌드가 수행할 계약이며
일반 저장소 빌드에서는 사용할 수 없다. HealthKit bridge의 sync action은 외부
exporter가 기존 `POST /v1/ingest/healthkit` endpoint를 호출한다는 뜻이며,
HealthMes가 iOS 백그라운드 실행을 대신 예약한다는 뜻이 아니다. Google Calendar
connect는 기존 브라우저 OAuth endpoint를 사용한다.

### 설정 변경

```json
{
  "instance_id": "iphone-owner",
  "enabled": true,
  "excluded_apps": [
    "ios-app-v2-1111111111111111111111111111111111111111-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ],
  "paused_until": "2026-08-14T12:00:00Z",
  "decision_access_enabled": true,
  "retention": {
    "activity_raw": "14d",
    "activity_hourly": "90d",
    "activity_daily": "forever"
  }
}
```

지원 보존 preset은 `1d`, `7d`, `14d`, `30d`, `90d`, `forever`다.
`paused_until: null`은 pause를 해제한다.

## 3. 설정 scope

설정은 모두 같은 범위가 아니다.

| 설정 | Scope | 의미 |
|---|---|---|
| `enabled` | activity instance | 특정 iPhone, Android 또는 desktop activity collector를 켜거나 끔 |
| `excluded_apps` | activity instance | Android는 UsageStats package name, ActivityWatch는 window event의 `data.app` 값, iPhone은 같은 기기의 HMAC `ios-app-v2-*` token |
| `paused_until` | activity instance | 특정 activity 기기의 수집을 절대 UTC 시각까지 일시중지 |
| `decision_access_enabled` | domain | Decision Agent가 activity/nutrition/wearable/calendar 영역을 조회할 수 있는지 |
| `retention` | data class | 중앙 저장소의 실제 보존 정책 |

새 ActivityWatch instance처럼 한 source가 여러 desktop platform을 지원하면
최초 설정 PUT에 `platform=macos|windows|linux`를 함께 보낸다. 이 값은 힌트로
버리지 않고 collection config에 저장한다. 이미 등록된 instance의 platform과
다른 값은 `input_platform_conflict`, source가 지원하지 않는 값은
`input_platform_unsupported`로 거부한다.

따라서 iPhone source에서 `activity_raw=7d`로 변경하면 iPhone 데이터에만 적용되는
것이 아니다. Android와 ActivityWatch도 같은 `activity_raw` 데이터 클래스에
저장되므로 세 activity source가 동일한 값을 보여 준다.

마찬가지로 `activity.ios-screentime`에서 activity Decision 접근을 끄면
Android와 ActivityWatch descriptor도 꺼진 값으로 보인다. Decision Agent
동의는 수집기별이 아니라 domain별 보안 경계이기 때문이다.

## 4. iPhone Screen Time 연결 seam

현재 일반 앱은 이 seam을 lifecycle에서 호출하지 않으므로 자동으로 report를
전송하지 않는다. 일반 빌드의 unavailable adapter와 향후 지원 SDK, 승인
entitlement 및 compile condition을 갖춘 조건부 collector는 모두 다음 엔진
seam을 통해 연결한다.

```text
사용자 authorize 선택
        |
        v
ScreenTimeActivitySyncService.requestAuthorization()

앱 foreground / 등록된 Screen Time background task
        |
        v
ScreenTimeActivitySyncService.sync(pairing:)
        |
        +─ Keychain HMAC key에서 ios-collector-v1-* device_id 파생
        |
        +─ GET /v1/activity/devices/{device_id}/collection?platform=ios
        |     최신 enable/pause/excluded_apps/revision/retention cutoff 확인
        |     최초 등록 전 enabled=false로 fail closed
        |
        +─ 최초·timezone 변경: 최신 완료 1시간
        |  이후: 동의 경계부터 최대 최근 48시간 집계
        |
        +─ HMAC key 경계 검사 + source-side 앱 제외
        |
        +─ bundle ID HMAC 가명화
        |
        └─ POST /v1/activity/ios/report
                 |
                 v
          activity.* WellnessEvent
```

2026-08-16 현재 코드는 주입 가능한 service core와 transport/report contract를
테스트하지만 Apple collector 경로는 일반 빌드에서 컴파일하지 않고 앱 lifecycle도
연결하지 않는다. PR #138의 Issue #168은 UI를 수정하지 않고 권한 승인 직후 첫
sync, foreground catch-up, Screen Time 전용 best-effort background task와
offline outbox를 연결한다. 설정 화면, entitlement 승인, 실제 배포 서명과 실기기
검증은 device-team 또는 Apple 외부 조건이다.

`GET /v1/activity/devices/{device_id}/collection`의
`raw_retention_cutoff`는 중앙 `activity_raw` 보존 정책으로부터 계산된다. 최초
승인 직후와 timezone 변경 직후에는 최신 완료 local-hour 1개만 조회해 이전
권한·시간대의 과거 활동을 소급 수집하지 않는다. 이후 같은 timezone에서 실행되는
sync는 이 최초 경계 이후를 최대 최근 48시간으로 제한하고, 그중 cutoff 이후의
완전한 local-hour만 조회·업로드한다. 수집과 업로드 사이에 cutoff가 이동해 전체
snapshot이 거부되지 않도록, cutoff를 올림한 뒤 추가로 완료 1시간의 안전 여유를
둔다.

권한이 denied/unavailable인 collect 결과는 이 최초 경계를 확정하지 않는다.
며칠 뒤 처음 승인돼도 거부 시점의 과거 48시간을 소급하지 않고 승인 시점의 최신
완료 1시간에서 시작한다. collector identity는 설치별 IFV가 아니라 Screen Time
가명화 Keychain key에서 결정적으로 파생한다. key가 유지되면 재설치 후에도 같은
ID를 사용하고, key를 잃어 새 `ios-collector-v1-*` ID가 생기면 서버는
`/v1/inputs/activity.ios-screentime/settings`에서 명시적으로 enable하기 전까지
수집을 차단한다.

### Apple capability 경계

실제 App & Website Usage export 경로는 다음 조건을 모두 만족할 때만 활성화한다.

- 지원 OS와 SDK
- Apple이 승인한 App & Website Usage entitlement
- 데이터 접근을 포함한 사용자 승인
- Apple이 API를 제공하는 사용자·지역 조건

빌드가 이 capability를 갖지 않으면 가짜 0분을 전송하지 않는다.
`capability=unavailable`과 구체적인 reason을 서버에 보고한다.

현재 collector 구현은 `HEALTHMES_IOS_26_4_SCREENTIME_EXPORT` 조건에서만 실제
Apple API를 컴파일한다. 현재 저장소의 일반 빌드는 안전한 unavailable adapter를
제공하지만 앱 lifecycle은 아직 sync seam을 호출하지 않는다. Issue #168이
lifecycle을 연결한 뒤에도 일반 빌드는 unavailable report를 전송한다. 지원
SDK에서 entitlement와 이 build condition을 device-team target에 함께 설정하고
실제 기기로 검증해야 한다.

## 5. Screen Time 데이터 의미

iPhone은 다음 최소 정보만 업로드한다.

```text
completed local-hour bucket
foreground seconds
controlled category
device-local keyed app pseudonym
coverage
```

업로드하지 않는 정보:

```text
bundle identifier / app display name
screen pixel / screenshot
tap / keystroke
URL / notification body
pickup count
```

Screen Time pickup은 HealthMes의 `app launch`와 같은 개념이 아니다. 따라서
iOS sample에는 launch 값을 만들지 않는다.

```json
{
  "app_launches_or_switches": 0,
  "app_launches_or_switches_range": {
    "lower_bound": 0,
    "upper_bound": null,
    "precision": "unknown"
  },
  "limitations": ["launches_unavailable_for_some_sources"]
}
```

이 `0`은 관찰된 0회가 아니라 알려진 최소값이다. focus fragmentation 계산은
launch가 unknown인 source를 횟수 계산에서 제외한다.

완료된 한 시간 동안 사용량이 실제 0이었다면 빈 snapshot으로 의미를 잃지 않고
`coverage_only=true`, `foreground_seconds=0`, 양수 `coverage_seconds`인 identity
없는 record를 보낸다. 이 record는 "관찰된 0분"과 "권한이 없어 관찰하지 못함"을
구분하며 앱·카테고리 식별자를 포함할 수 없다.

제외 앱의 활동이 있었던 시간은 이 zero-usage record를 만들지 않는다. 제외된
앱의 시간과 identity는 저장하지 않되, 그 시간을 "실제 0분"으로 오인하지 않고
coverage 결측으로 남긴다. 같은 시간에 허용 앱과 제외 앱 또는 bundle ID가 없는
활동이 함께 있으면 허용 앱의 시간은 저장할 수 있지만 그 sample도
`coverage_seconds=null`이다. 즉 알려진 일부 활동을 전체 시간의 완전한 관찰로
과장하지 않는다.

앱 가명화 HMAC key가 바뀌면 과거 `ios-app-*` 제외 token은 더 이상 같은 앱을
가리키지 않는다. v2 token은
`ios-app-v2-<key fingerprint>-<app HMAC>` 형식이라 현재 기기 key namespace와
일치하는지도 로컬에서 검증한다. legacy v1 token과 여러 key namespace가 섞인
목록은 서버에서도 거부한다. 기기는 key 변경을 새 `collection_generation`으로
기록한다.
로컬 승인은 현재 key ID와 정렬된 정확한 제외 token 집합의 SHA-256 digest에
묶인다. key나 token 집합이 달라지면 Screen Time 조회 자체를
`ios_screen_time_exclusions_require_reapproval_after_key_change`로 차단한다.
목록을 비운 것만으로 미래 목록을 승인하지 않으므로 key 손실이나 재설치가
private 앱을 조용히 다시 수집하게 만들 수 없다.

## 6. 저장과 보존

서버는 세 activity source를 중앙 저장소의 같은 논리 파티션으로 정규화한다.

```text
Android ─────────────────────┐
ActivityWatch ───────────────┼─> WellnessEvent(event_type="activity.*")
조건부 iPhone snapshot ──────┘         |
                          +─ activity_raw
                          +─ activity_hourly
                          └─ activity_daily
```

gate-enabled iPhone collector는 최초·timezone 변경 경계 뒤에서 완료된 최대
최근 48시간을 authoritative snapshot으로 다시 보내도록 설계됐다. 일반 빌드는
snapshot을 보내지 않는다. 서버는
`collection_generation + snapshot_sequence` fence로 늦게 도착한 과거 snapshot이
최신 삭제·수정 결과를 되살리지 못하게 한다. 데이터 보존과 삭제는 공통
storage maintenance가 실행하며 휴대전화가 별도 장기 정본이 되지 않는다.

성공한 authoritative snapshot은 manifest와 최초 성공 응답을 fence에 함께
저장한다. 네트워크에서 응답만 유실돼 같은 manifest를 재전송하면 이후 pause,
exclude, revision, tombstone 변경보다 먼저 저장된 응답을 그대로 돌려준다.
같은 sequence를 다른 manifest로 재사용하는 요청은 계속 거부한다.

권한 상태가 denied에서 granted처럼 바뀌면 기기는 단조 증가하는
`collection_generation`을 새로 만든다. 서버가 generation 변경을 감지하면
`409 activity_snapshot_fence_reset_required` machine code를 반환하고, 기기는 같은
snapshot을 `reset_snapshot_fence=true`로 한 번 재전송한다. 이전 generation이나
재사용된 sequence가 최신 데이터를 덮어쓸 수는 없다.

Open Wearables의 HealthMes mirror는 범용 `normalized`와 섞지 않고 전용
`wearable_normalized` 데이터 클래스를 사용한다. 따라서 웨어러블 snapshot의
`1d/7d/14d/30d/90d/forever` 보존 설정은 다른 정규화 데이터의 보존기간을 바꾸지
않는다.

`wearable.healthkit-bridge`는 정규화 전 원문을 `raw_payload` 정책으로 먼저
저장한다. 첫 `healthkit-bridge` raw event가 도착하기 전에는 `configured`,
도착한 뒤에는 `connected`로 표시한다. 이 상태는 exporter가 현재도 주기적으로
실행 중이라는 보장이 아니므로 collection state는 `idle`로 유지하고 freshness를
과장하지 않는다. raw payload의 기본 LLM 노출은 `none`이며, 판단에는 별도로
정규화된 wearable context만 사용한다.

## 7. UI 구현자가 지켜야 할 계약

1. source 목록과 설정 항목을 앱에 별도로 복제하지 말고 `/v1/inputs`를 렌더링한다.
2. `scope`를 표시해 기기 설정과 domain/data-class 공유 설정을 구분한다.
3. `limitations`를 숨기지 말고 unavailable 이유로 표시한다.
4. iPhone 제외 앱은 기기가 bundle ID로부터 만든 opaque token만 서버 설정에
   저장하고 bundle ID를 서버로 보내지 않는다.
5. 가명화 key 또는 token 집합 변경 차단 reason을 받으면 기존 제외 목록을
   그대로 재사용하지 말고, 현재 기기 key로 앱을 다시 선택해 서버 PUT이 성공한
   뒤 `approveExcludedApps(_:)`를 호출하는 명시적 재승인 흐름을 제공한다.
6. 새 `ios-collector-v1-*` instance는 설정 PUT으로 `enabled=true`를 명시하기
   전까지 수집되지 않는다. 기기 ID 변경을 자동 등록이나 기존 exclude 복사로
   처리하지 않는다.
7. 설정 변경 후 PUT 응답 descriptor를 즉시 정본으로 사용한다.
   PUT에는 마지막 GET에서 받은 `revision`을 함께 보내야 하며 서버가 stale
   revision을 `409`로 거부하면 최신 descriptor를 다시 읽고 사용자가 변경을
   재적용하게 한다.
8. 실제 collection permission과 HealthMes Decision 접근 동의를 하나의 toggle로
   합치지 않는다.
9. UI가 없어도 API와 수집 엔진은 독립적으로 테스트 가능해야 한다.

## 8. 비범위와 후속

- iOS/Android/desktop 실제 설정 화면
- iPhone 권한 설명·설정 UI와 실제 distribution signing
- Apple entitlement 신청과 실제 기기 dogfood
- hosted/mobile-only Personal Data Node
- GPS/location 수집

iPhone collector의 **UI-neutral lifecycle 연결**은 비범위가 아니다. 지원 조건에서
권한 승인 직후 첫 sync, foreground catch-up, best-effort background task와 offline
outbox 재전송까지가 #168의 코드 범위다.
- 가격과 cloud storage 과금

GPS/location은 Issue #158에서 opt-in, coarse-first, excluded places, raw 좌표의
짧은 보존, 파생 장소/이동 context와 Decision Agent SourceRef까지 추적한다.
