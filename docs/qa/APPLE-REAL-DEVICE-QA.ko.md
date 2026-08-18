# Apple 실기기 Live QA

이 문서는 PR #111의 iPhone 13 mini와 Apple Watch Series 10 실기기
검증 절차다. 시뮬레이터 빌드 성공은 실제 서명, HealthKit, 카메라,
알림, WatchConnectivity 동작을 증명하지 않으므로 아래 항목을 별도로
확인한다.

## 1. 검증 경계

```text
Mac 또는 Linux의 HealthMes 인스턴스
             │ HTTPS
             ▼
       iPhone 13 mini
       ├─ HealthKit
       ├─ Apple Calendar 권한
       ├─ 카메라/VLM 식사 입력
       ├─ 잠금화면 Yes/No
       └─ WatchConnectivity
             │
             ▼
   Apple Watch Series 10
       ├─ 앱 설치와 페어링
       ├─ 현재 상태 glance
       └─ 제안 Yes/No
```

물리 iPhone은 Mac의 `localhost`에 접근할 수 없다. iPhone과 Watch를
연결하려면 사용자의 HealthMes 인스턴스에 신뢰할 수 있는 HTTPS 주소와
`HEALTHMES_PUBLIC_BASE_URL`이 필요하다. 같은 Mac 안에서만 사용하는
로컬 데모는 `127.0.0.1`을 계속 사용할 수 있다.

## 2. Xcode 서명 준비

1. iPhone 13 mini와 Apple Watch Series 10을 먼저 서로 페어링한다.
2. iPhone을 Mac에 연결하고 Finder와 iPhone에서 이 컴퓨터를 신뢰한다.
3. iPhone에서 개발자 모드를 활성화하고 재시동한다.
4. Xcode의 `Settings > Accounts`에 Apple Developer 계정을 추가한다.
5. `apps/ios-companion/HealthMesCompanion.xcodeproj`를 연다.
6. `HealthMesCompanion`, `HealthMesWidgets`,
   `HealthMesNotificationContent`, `HealthMesWatchApp`,
   `HealthMesWatchWidgets`의 Signing Team을 같은 팀으로 선택한다.
7. Bundle ID 충돌이 있으면 프로젝트 Build Settings의
   `HEALTHMES_BUNDLE_ID_PREFIX`를 본인 소유 역도메인 값으로 바꾼다.
   예: `com.example.healthmesqa`
8. 같은 화면에서 `HEALTHMES_APP_GROUP_ID`를
   `group.<동일한 prefix>.companion`으로 바꾼다.

앱, 위젯, 알림 확장, Watch 앱은 위 두 설정에서 일관된 식별자를
파생한다. App Group, Keychain, Watch counterpart가 서로 다른 값을
사용하면 설치는 되더라도 페어링 정보가 공유되지 않는다.

## 3. 설치

1. Xcode scheme을 `HealthMesCompanion`으로 선택한다.
2. destination을 연결된 iPhone 13 mini로 선택한다.
3. Run을 실행한다.
4. iPhone의 Watch 앱에서 HealthMes가 설치 중인지 확인한다.
5. 자동 설치되지 않으면 Watch 앱의 `Available Apps`에서 HealthMes를
   설치한다.
6. Watch에서 HealthMes를 한 번 실행한다.

`HealthMesWatchApp`은 iPhone 앱의 `Embed Watch Content` 단계에
포함되어 있다. Watch가 표시되지 않으면 먼저 iPhone/Watch 페어링,
Watch의 개발자 모드, 모든 target의 동일 Signing Team을 확인한다.

## 4. 인스턴스와 페어링

1. Mac 앱의 `Settings > Set up this Mac`을 실행한다.
2. 설치 완료 후 readiness 목록에서 instance, health, calendars,
   notifications, scheduler, HTTPS, storage를 각각 확인한다.
3. HTTPS 공개 주소가 설정된 경우 Mac 앱에 표시된 5분짜리 QR을
   iPhone Camera로 스캔한다.
4. iPhone에서 HealthMes가 열리고 연결 완료 메시지가 나오는지 확인한다.
5. Watch 앱을 열어 `Not paired`가 사라지는지 확인한다.

QR에는 장기 bearer token이 아니라 서명된 일회용 코드만 들어간다.
코드는 한 번 사용하거나 만료되면 다시 쓸 수 없어야 한다.

## 5. 필수 시나리오

### 건강과 캘린더

- Apple Health 읽기 권한을 허용한다.
- iPhone Settings에서 Apple Calendar full access를 허용한다.
- HealthMes의 Google Calendar와 iCloud CalDAV 연결 상태를 각각 본다.
- Apple/Google 실제 일정이 iPhone과 Web 시간 블록에 표시되는지 본다.
- 제안 수락 직후 `accepted`와 실제 외부 캘린더 `applied`가 구분되는지
  확인한다.

### 식사 VLM

- iPhone 카메라로 식사 사진을 찍는다.
- 원격 분석 opt-in을 켠 경우에만 원격 VLM이 호출되는지 확인한다.
- 음식명, 양, 영양소, confidence, warning을 검토한다.
- 수정 후 `Consumed`를 선택하고 Web Today intake에 같은 interaction이
  표시되는지 확인한다.
- 네트워크를 끊었다 켠 뒤 재시도해 중복 기록이 생기지 않는지 확인한다.

### iPhone 알림

- 정확한 `proposal_id`가 있는 제안을 만든다.
- foreground refresh 또는 허용된 background refresh로 알림을 받는다.
- 잠금화면 알림을 길게 눌러 No/Yes가 보이는지 확인한다.
- Yes는 한 번만 적용되고 두 번째 실행은 already resolved로 보이는지
  확인한다.
- 잠금 해제가 필요한 동작은 Face ID 또는 기기 인증을 요구해야 한다.

### Apple Watch

- 대기 상태에서 몸 상태, 계획 영향, 다음 블록, freshness가 보이는지
  확인한다.
- 결정이 오면 첫 화면에서 정확한 변경 전/후 시간과 No/Yes가 보이는지
  확인한다.
- Yes/No를 누른 뒤 applying, applied/declined 또는 honest failure가
  표시되는지 확인한다.
- iPhone이나 Telegram에서 먼저 처리한 제안은 already resolved로
  표시되어야 한다.

## 6. 합격 기준

- 앱과 Watch 앱이 동일 개발팀 서명으로 설치된다.
- App Group과 Keychain을 통해 재실행 후에도 pairing이 유지된다.
- HealthKit의 Watch-origin sample이 iPhone을 통해 서버 raw ingest에
  저장된다.
- Apple/Google 캘린더 읽기와 승인 후 쓰기 결과가 일치한다.
- 카메라/VLM 식사 입력은 분석과 섭취 확정을 분리한다.
- iPhone 잠금화면과 Watch에서 정확한 제안만 한 번 처리한다.
- 실패, stale, offline, expired, accepted, applied 상태를 서로
  혼동하지 않는다.

실기기에서만 재현되는 실패는 기기/OS/Xcode 버전, 대상 화면, 제안 ID,
HTTP status, 서버 로그 시각을 남긴다. 토큰, QR 코드, 건강 원시 데이터,
사진 원본은 이슈나 스크린샷에 포함하지 않는다.
