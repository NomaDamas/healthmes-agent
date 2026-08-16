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
- `revision`: descriptor 전체의 안정적인 SHA-256 digest이자 설정 변경의
  낙관적 동시성 토큰. 상세 GET의 `ETag`는 이 값을 따옴표로 감싼 값이며,
  UI는 모든 설정 PUT에 그 `ETag`를 `If-Match`로 전송해야 한다.

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

### 필수 설정 저장 흐름: GET -> 편집 -> If-Match PUT -> 재조회·재적용

설정 UI는 목록에서 본 오래된 descriptor나 마지막 쓰기 우선 방식으로 저장하면
안 된다. 데스크톱과 iPhone이 같은 domain 또는 data-class 설정을 동시에 바꿀 수
있으므로 다음 compare-and-swap 흐름을 그대로 구현한다.

```text
1. GET /v1/inputs/{source_id}
      base_descriptor = response body
      base_etag       = response ETag
      화면은 base_descriptor로 렌더링

2. 사용자가 편집
      pending_patch = 사용자가 실제로 바꾼 필드만 보존

3. PUT /v1/inputs/{source_id}/settings
      If-Match: <base_etag>
      body: <pending_patch>

4-a. 200
      current_descriptor = response body
      current_etag       = response ETag
      dirty state를 지우고 성공 응답으로 화면 상태를 교체

4-b. 428 input_settings_revision_required
      헤더를 빠뜨린 클라이언트 오류로 취급
      최신 descriptor/ETag를 다시 GET
      pending_patch를 최신 descriptor에 재적용한 뒤 새 ETag로 PUT

4-c. 409 input_settings_revision_conflict
      서버 설정은 변경되지 않음
      최신 descriptor/ETag를 다시 GET
      pending_patch를 최신 descriptor에 재적용
      겹친 필드 충돌을 해결한 뒤 새 ETag로 PUT
```

상세 GET 응답 body의 `revision`은 따옴표 없는
`sha256:<64 lowercase hex>`이고, 응답 헤더는 같은 값을 HTTP entity tag로
따옴표 처리한 `ETag: "sha256:<64 lowercase hex>"`다. PUT의 `If-Match`에는
GET에서 받은 `ETag`를 그대로 사용하는 것이 권장된다. API는 정확히 일치하는
따옴표 없는 revision도 허용하지만 wildcard, weak ETag, 여러 값, 대문자 hex와
불완전한 digest는 `400 input_settings_revision_invalid`로 거부한다.

```http
GET /v1/inputs/activity.ios-screentime

HTTP/1.1 200 OK
ETag: "sha256:0000000000000000000000000000000000000000000000000000000000000000"

{
  "source_id": "activity.ios-screentime",
  "revision": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}
```

```http
PUT /v1/inputs/activity.ios-screentime/settings
If-Match: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
Content-Type: application/json

{
  "instance_id": "iphone-owner",
  "enabled": true
}
```

`If-Match`가 없으면 서버는 PUT을 실행하지 않고 다음 machine code를 반환한다.

```json
{
  "error": {
    "code": "input_settings_revision_required",
    "message": "If-Match is required for input settings updates.",
    "detail": null
  }
}
```

다른 클라이언트가 먼저 설정을 저장해 revision이 바뀌면 서버는 요청 전체를
원자적으로 거부한다. collection, Decision 접근 동의와 retention 중 일부만
적용되는 일은 없다.

```json
{
  "error": {
    "code": "input_settings_revision_conflict",
    "message": "The input settings changed after the caller read them.",
    "detail": {
      "expected_revision": "sha256:<PUT에 사용한 revision>",
      "current_revision": "sha256:<현재 서버 revision>"
    }
  }
}
```

`current_revision`만 새 `If-Match`로 바꿔 같은 body를 즉시 재전송하면 안 된다.
오류 응답에는 최신 설정값 전체가 없기 때문이다. 반드시 상세 GET으로
`latest_descriptor`와 `latest_etag`를 함께 다시 받고 다음 3-way 규칙으로
사용자 편집을 재적용한다.

1. `pending_patch`에 없는 필드는 `latest_descriptor` 값을 유지한다.
2. 사용자가 바꾼 필드의 최신 값이 `base_descriptor`와 같으면 사용자 값을
   자동으로 다시 적용할 수 있다.
3. 사용자가 바꾼 필드를 다른 클라이언트도 변경했다면 최신 값과 사용자 값을
   보여 주고 명시적으로 선택받는다. stale descriptor 전체로 덮어쓰지 않는다.
4. 재적용한 부분 patch를 `latest_etag`와 함께 PUT한다. 또 409가 발생하면 같은
   과정을 제한된 횟수만 반복하고 사용자에게 최신 상태를 다시 보여 준다.

200 응답을 받으면 요청에 사용한 이전 ETag를 계속 쓰지 않는다. 해당 응답 body를
새 `current_descriptor`로, 응답 `ETag`를 새 `current_etag`로 저장하고 이후
편집의 기준으로 사용한다. 의미상 동일한 no-op PUT은 revision을 바꾸지 않으므로
같은 ETag가 성공 응답으로 돌아올 수 있다. domain 또는 data-class 공유 설정을
바꿨다면 같은 값을 표시하는 다른 source descriptor도 무효화하고
`GET /v1/inputs` 또는 각 상세 GET으로 갱신한다.

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

앱 lifecycle, 권한 승인 직후 sync, foreground catch-up, pairing 변경,
저장된 input/retention 설정 변경과 Screen Time 전용 `BGAppRefreshTask`는 같은
UI-neutral 엔진 seam에 연결되어 있다.
실제 설정 화면은 device-team 범위이며, 사용자가 명시적으로 opt-in한 뒤
`requestAuthorizationAndSync()`를 호출해야 한다.

```text
사용자 authorize 선택
        |
        v
ScreenTimeActivityRuntime.requestAuthorizationAndSync()

앱 foreground / pairing 변경 / 등록된 Screen Time background task
저장된 input 또는 retention 설정 변경
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

cold launch, foreground, authorization status 변경 알림, background task는
Apple 권한 UI를 호출하지 않는다. 이 자동 경로들은 저장된 opt-in을 확인하고
`currentAuthorizationStatus()`로 현재 상태를 읽은 뒤 허용된 경우에만 sync한다.
Apple의 `requestAuthorization()`을 호출할 수 있는 유일한 제품 경로는 사용자가
직접 선택한 `requestAuthorizationAndSync()`이다.

현재 PR은 service core, transport/report contract, lifecycle, bounded outbox,
background registration과 source-side privacy exclusion을 연결한다. 설정 화면은
추가하지 않는다. Apple API를 실제 컴파일할 수 있는지는 아래 SDK capability
probe가 결정하며, distribution signing과 실기기 검증은 저장소의 unsigned
빌드가 대신 증명하지 않는다.

authorization, input 설정 또는 timezone 변경이 기존 sync 실행 중 도착하면 해당
요청을 앞선 snapshot에 합쳐 버리지 않는다. 중요한 변경 요청들을 하나의 pending
fresh run으로 합쳐 기존 run 직후 최신 authorization/config/timezone으로 다시
조회한다. 설정 UI는 서버 PUT이 성공한 뒤
`ScreenTimeActivityRuntime.inputConfigurationDidChange()`를 호출해야 한다.

수집이 끝났다는 사실만으로 업로드를 허용하지 않는다. 새 aggregate 또는 로컬
outbox 항목을 전송하기 직전에 서버 collection state와 현재 Apple authorization을
다시 읽는 final upload fence를 적용한다. authorization 또는 input configuration
변경 알림은 service-owned control epoch를 증가시켜 실행 중인 이전 generation이
다음 POST를 시작하지 못하게 한다. revision, enabled, pause, exclusions 같은
안정적인 설정이 바뀌었거나 새 retention cutoff가 실제 snapshot window 또는
bucket을 침범했다면 현재 조건과 맞지 않는 outbox 항목을 제거하고 오래된 수집
결과를 버린 뒤 최신 설정으로 다시 수집한다. 서버가 현재 시각으로 계산하여
매 조회마다 조금씩 전진하는 retention cutoff timestamp 자체는 설정 변경으로
간주하지 않는다.

서버가 `stale_collection_revision`, `activity_outside_retention`,
`activity_collection_blocked`, `ios_exclusion_reapproval_required`를 반환하면
같은 private payload를 quarantine하거나 재전송하지 않는다. pending payload는
즉시 삭제하고 fresh collection으로 전환하며, 새로 만든 payload는 outbox에
기록하지 않는다. 일반적인 영구 데이터 충돌만 bounded quarantine을 사용한다.
설정이 계속 흔들리는 경우에는 최대 3회의 fresh attempt 뒤
`ios_screen_time_collection_configuration_changed`로 이번 sync를 종료하여
무한 반복을 막는다.

Apple authorization이 `granted`여도 collector가
`ios_screen_time_activity_data_unavailable` 같은 export capability 오류를
반환할 수 있다. 이 경우를 설정 변경으로 오인하지 않고 unavailable reason을
그대로 보고하되 aggregate는 업로드하지 않는다.

foreground 호출 취소는 이미 시작한 idempotent upload나 retry 저장을 중단하지
않는다. 반면 iOS가 `BGAppRefreshTask`를 만료시키면 background lease가 실제
service-owned pipeline을 취소한다. 같은 pipeline을 기다리는 foreground waiter가
있다면 그 waiter와 pipeline은 보존한다. pairing destination 변경은 모든 waiter에
적용되는 별도의 전역 취소 경계다.

로컬 Screen Time retry outbox는 최대 8개·16 MiB이며 14일 TTL을 가진다. 앱
재시작 시 파일을 읽는 단계와 매 sync/retry 변경 전에 만료 항목을 삭제하므로,
장기 오프라인 뒤 첫 네트워크 조회가 실패해도 14일을 넘긴 aggregate는 먼저
제거된다. outbox 디렉터리와 atomic output 파일은 기기 backup에서 제외된다.
이 14일 transport TTL은 중앙 `activity_raw` 보존기간 설정과 별도다.

재시도 가능한 네트워크 오류, `408`, `425`, `429`, `5xx`,
`409 activity_write_conflict`만 oldest-first backoff에 남는다. 영구 `422`와
재시도 불가 또는 분류되지 않은 `409`는 reason, HTTP status, terminal 시각을
가진 quarantine entry로 보존하되 전송 후보에서는 제외한다. 따라서 잘못된 과거
snapshot 하나가 이후 정상 snapshot을 14일 동안 막지 않는다. stale snapshot,
privacy/exclusion, collection generation fence는 서버가 반환한 구체적 reason을
잃지 않는다.

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

이 Keychain key는 명시적 opt-in 전에는 읽거나 만들지 않는다. SDK가 export
API를 제공하지 않는 빌드도 fallback persistent identifier를 만들지 않는다.
opt-out은 수집 작업을 취소하고 Screen Time 전용 outbox와 파생 상태를 정리한 뒤
기억해 둔 key-derived device ID와 key를 삭제한다. 이 device ID는 opt-in 뒤에만
저장되며 프로세스 재시작 뒤에도 key를 다시 읽거나 만들지 않고 정확한 device
namespace의 cleanup을 재개하는 데만 사용한다. key 삭제가 실패하면
`privacy-cleanup-pending`을 유지하여 재 opt-in과 이전 identity 재사용을 막고,
다음 명시적 opt-in에서 cleanup을 먼저 재시도한다.

### Apple capability 경계

실제 App & Website Usage export 경로는 다음 조건을 모두 만족할 때만 활성화한다.

- 지원 OS와 SDK
- Apple이 승인한 App & Website Usage entitlement
- 데이터 접근을 포함한 사용자 승인
- Apple이 API를 제공하는 사용자·지역 조건

빌드가 이 capability를 갖지 않으면 가짜 0분을 전송하지 않는다.
`capability=unavailable`과 구체적인 reason을 서버에 보고한다.

`HealthMesCompanionScreenTimeOptIn`은 capability를 주장하는 "eligible" 구성이
아니라 사용자가 기능을 요청한 구성이다. 다음 스크립트만 SDK capability
condition을 생성한다.

```bash
cd apps/ios-companion
xcodegen generate
bash Scripts/build-screen-time-opt-in.sh build
bash Scripts/build-screen-time-opt-in.sh test \
  -only-testing:HealthMesCompanionTests/ScreenTimeActivityContractTests \
  -only-testing:HealthMesCompanionTests/ScreenTimeActivityLifecycleTests
```

스크립트는 선택한 SDK에서
`AuthorizationStatus.approvedWithDataAccess`와
`DeviceActivityData.activityData(filteredBy:using:)`를 실제 type-check한다.
성공할 때만 `HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE`을 주입해 Apple collector
코드를 컴파일한다. 실패하면 opt-in 요청은
`ios_screen_time_export_sdk_unavailable` unavailable adapter로 명시적으로 닫힌다.
일반 빌드는 별도의 `ios_screen_time_normal_build_unavailable` 경계를 유지한다.
`test`는 `HEALTHMES_SCREENTIME_DESTINATION`이 없으면 설치된 최신 iOS runtime의
사용 가능한 iPhone simulator를 자동 선택한다. 특정 기기를 고정해야 하면
`platform=iOS Simulator,id=<UDID>`를 환경변수로 전달한다.
`HEALTHMES_SCREENTIME_SDK=iphoneos`의 build/analyze/archive 기본 destination은
`generic/platform=iOS`다. `iphoneos test`는 generic destination에서 실행할 수
없으므로 `HEALTHMES_SCREENTIME_DESTINATION=platform=iOS,id=<실기기 UDID>`를
반드시 명시해야 하며, 누락하면 스크립트가 실행 전에 실패한다.

SDK probe 성공만으로 실제 기기 수집이 보장되지는 않는다. opt-in entitlement
파일은 `com.apple.developer.family-controls`와
`com.apple.developer.family-controls.app-and-website-usage`를 선언한다. 실제
빌드는 이 두 capability가 App ID와 signed provisioning profile에도 포함되어야
하고, Family Controls는 App Store 제출 전에 Apple 사용 허가가 필요하다. 사용자
승인은 `approvedWithDataAccess`까지 도달해야 한다. 고객 설치에서는 기기가 EU에
있고 Apple Account 국가/지역도 EU여야 하며, Apple-provided development/test
profile을 사용하는 개발·테스트만 다른 지역에서 가능하다. unsigned CI는 이러한
서명·계정·지역 조건이나 실제 iPhone 수집을 증명하지 않는다.

따라서 이 저장소에서 "코드 완료"는 계약, 수집 lifecycle, fail-closed adapter,
서버 snapshot 의미, XCTest와 unsigned build를 검증했다는 뜻이다. Apple
entitlement 승인, capability가 포함된 signed provisioning, eligible
기기·계정·지역, 실기기 authorization/export, 실제 `BGAppRefreshTask` cadence는
별도의 외부 승인과 iPhone dogfood 조건이다.

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

제외 앱, website-only, bundle ID가 없는 활동이 있었던 시간은 zero-usage
record를 만들지 않는다. 허용 앱 record는 버리지 않고 저장하되
`coverage_seconds=null`로 두고, 별도의 identity 없는 `coverage_only` marker가
관찰 시간을 다음 값으로 정확히 나눈다.

```text
observed_activity_seconds
  = represented_app_seconds
  + privacy_filtered_seconds
  + website_activity_seconds
  + unknown_activity_seconds
```

marker의 `coverage_status`는 `privacy_filtered`, `website_activity`,
`unknown_activity`, `mixed_partial` 중 하나다. 실제 관찰된 0분만 `complete`를
사용한다. Apple segment total이 app 합보다 커도 유효한 app record를 버리지
않으며, 차이는 website-only 또는 unknown으로 명시한다.

수집기가 관찰한 모든 hour bucket은 authoritative replacement 대상이다. 따라서
새 snapshot에 private app row가 빠지고 privacy marker만 남으면 서버는 과거
private row를 삭제한다. 더 오래된 snapshot은
`collection_generation + snapshot_sequence` fence에서 거부되어 삭제된 row를
되살릴 수 없다.

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
7. 설정 저장은 반드시 상세 GET의 `ETag`를 `If-Match`로 보내고, 428/409에서는
   최신 descriptor와 ETag를 재조회해 사용자 편집을 재적용한다. 성공 후에는 PUT
   응답 descriptor와 ETag를 다음 편집의 정본으로 사용한다.
8. 설정 또는 retention PUT 성공 후
   `ScreenTimeActivityRuntime.inputConfigurationDidChange()`를 호출해 fresh sync를
   요청한다.
9. 실제 collection permission과 HealthMes Decision 접근 동의를 하나의 toggle로
   합치지 않는다.
10. UI가 없어도 API와 수집 엔진은 독립적으로 테스트 가능해야 한다.

## 8. 비범위와 후속

- iOS/Android/desktop 실제 설정 화면
- iPhone 권한 설명·설정 UI
- Apple entitlement 신청과 실제 기기 dogfood
- App ID capability, signed provisioning profile과 distribution 검증
- hosted/mobile-only Personal Data Node
- GPS/location 수집

iPhone collector의 **UI-neutral lifecycle 연결**은 비범위가 아니다. 지원 조건에서
권한 승인 직후 첫 sync, foreground catch-up, best-effort background task와 offline
outbox 재전송까지가 #168의 코드 범위다.
- 가격과 cloud storage 과금

GPS/location은 Issue #158에서 opt-in, coarse-first, excluded places, raw 좌표의
짧은 보존, 파생 장소/이동 context와 Decision Agent SourceRef까지 추적한다.
