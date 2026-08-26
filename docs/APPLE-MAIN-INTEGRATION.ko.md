# HealthMes Apple UI / Main 통합 아키텍처

> **결정일:** 2026-08-24
>
> **상태:** iPhone, macOS, Apple Watch UI를 기존 Main runtime에 연결하는
> canonical 책임 경계. 이 문서는 Main 엔진을 다시 구현하는 계획이 아니다.
>
> **범위:** 일반 데이터 조회, 자연어 wellness 판단, one-page 설정,
> first-party HealthKit 수집과 Pane `%44` 산출물 통합.
>
> **관련 문서:**
> [`HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md),
> [`INPUT-CONTROL-PLANE.ko.md`](INPUT-CONTROL-PLANE.ko.md)

## TLDR

Apple UI는 Main 위에 놓이는 client layer다. 화면 종류에 따라 다음 세 경로를
섞지 않는다.

```text
1. 일반 데이터와 설정

iPhone / macOS / Watch UI
            |
            v
Main REST API + /v1/inputs
            |
            v
Main의 저장소, 일정, 알림, 입력 설정


2. 자연어 wellness 판단

iPhone / macOS voice·text
            |
            v
POST /v1/wellness-decisions
            |
            v
HealthMesDecisionService
            |
            v
Hermes -> filtered HealthMes MCP


3. Apple Health 수집

Apple Watch -> iPhone HealthKit
            |
            v
first-party collector
            |
            v
pairing별 encrypted outbox
            |
            v
POST /v1/ingest/healthkit
            |
            v
durable ACK + accepted forward status -> anchor 확정
```

대시보드 카드 하나를 갱신하기 위해 Hermes를 호출하지 않는다. 반대로 사용자의
자유 형식 질문을 일반 데이터 GET 조합만으로 임의 해석하지 않는다. HealthKit
수집기는 Hermes 또는 MCP를 호출하지 않고 ingest API에만 쓴다.

## 1. 책임 경계

| 컴포넌트 | 소유 책임 | 소유하지 않는 것 |
|---|---|---|
| Apple UI | 화면, 사용자 입력, 기기 권한, 로컬 cache와 안전한 queue | Main의 데이터 정본, agent reasoning |
| Main REST API | 일반 데이터 조회·command, 인증, 서버 정본 | 자연어 질문의 자율 해석 |
| Input Control Plane | source 상태, instance 설정, retention, Decision 접근 동의 | 기기 권한 sheet와 로컬 secret |
| Decision Service | 제품 ingress, 요청 정책, 결과 검증과 조건부 기록 | HealthKit 수집, 일반 화면 조회 |
| Hermes | 한 turn의 자연어 해석과 허용된 MCP tool loop | 제품 인증, DB 정본, Apple UI |
| HealthKit collector | 증분 수집, 삭제 감지, encrypted outbox, 재시도 | wellness 판단, Open Wearables 직접 호출 |
| HealthKit ingest | exact-byte idempotency, raw durability, tombstone, ACK | iPhone background scheduling |
| Open Wearables | 정규화된 wearable data plane | Apple UI, 사용자 자연어 ingress |

`vendor/hermes-agent/`는 이 통합에서 수정하지 않는다. Main의 Decision Engine,
storage, provider와 MCP 아키텍처도 Apple UI에 맞춰 복제하거나 우회하지 않는다.
필요한 작업은 기존 공개 계약을 소비하는 Apple adapter와 UI wiring이다.

## 2. 일반 데이터는 Main REST API로 읽는다

iPhone과 macOS의 dashboard, 계획, 알림, 기록 화면은 Main이 이미 제공하는
결정론적 REST 응답을 직접 표시한다.

| 화면 데이터 | 대표 계약 |
|---|---|
| 현재 상태와 다음 일정 | `GET /v1/briefing/glance` |
| 알림 | `GET /v1/alerts` |
| 목표와 할 일 | `GET /v1/goals`, `GET /v1/tasks` |
| 일정과 제안 | `GET /v1/schedule/events`, `GET /v1/schedule/proposals` |
| 판단 기록 | `GET /v1/decisions` |
| 주간 리포트 | `GET /reports/weekly.json` |
| 입력과 설정 | `GET /v1/inputs`, `GET /v1/inputs/{source_id}` |

이 경로의 규칙은 다음과 같다.

1. Main 응답이 데이터의 정본이다. Apple 앱 안에 별도 wellness DB를 만들지 않는다.
2. UI cache는 pairing fingerprint에 묶고, 다른 HealthMes instance의 cache를
   재사용하지 않는다.
3. UI는 Hermes, HealthMes MCP 또는 Open Wearables를 직접 호출하지 않는다.
4. 서버에 없는 provider/device inventory를 앱이 추측해 만들지 않는다.
5. 쓰기는 일정 승인, capture, 설정 변경처럼 Main이 제공하는 bounded command만
   사용한다.

## 3. 자연어 판단만 Decision Service를 사용한다

사용자가 "오늘 운동해도 돼?", "왜 피곤하지?", "일정을 어떻게 바꿀까?"처럼
자연어 해석과 여러 domain의 종합을 요구할 때만 다음 경로를 사용한다.

```text
Apple voice 또는 editable text
              |
              v
POST /v1/wellness-decisions
              |
              v
HealthMesDecisionService
              |
              v
Hermes /v1/responses 한 번
              |
              v
filtered HealthMes MCP search_*
              |
              v
검증된 DecisionResult
```

Apple UI가 Hermes `/v1/responses`를 직접 호출하면 Main의 owner, consent,
retention, source reference 검증과 persistence 경계를 우회하므로 금지한다.
일반 dashboard refresh를 `/v1/wellness-decisions`로 대신하는 것도 금지한다.

## 4. iPhone과 macOS의 one-page setup

두 앱은 각각 하나의 Settings 진입점에서 연결과 입력 상태를 끝까지 확인할 수
있어야 한다. 한 페이지라는 뜻은 모든 secret을 한 폼에 노출한다는 뜻이 아니라,
사용자가 여러 앱과 숨겨진 설정 화면을 찾아다니지 않는다는 뜻이다.

```text
Mac one-page setup
  -> Main runtime 설치·시작
  -> GET /v1/setup/readiness
  -> one-time iPhone pairing grant
  -> GET /v1/inputs

iPhone one-page setup
  -> pairing grant 교환
  -> GET /v1/setup/readiness
  -> GET /v1/inputs
  -> Apple Health 등 기기 권한 action
```

한 페이지는 다음 영역을 함께 보여 준다.

| 영역 | 정본과 동작 |
|---|---|
| HealthMes 연결 | paired base URL, 인증 상태, HTTPS 도달 가능성 |
| 전체 준비 상태 | `GET /v1/setup/readiness` |
| 데이터 source | `GET /v1/inputs` |
| source 설정 | 상세 GET의 `ETag`를 사용한 `If-Match` PUT |
| Apple Health | iPhone의 로컬 HealthKit 권한과 수집 상태 |
| Wearable | Main이 제공하는 aggregate/provider 상태와 실제 instance만 표시 |
| Calendar | Main의 Google/iCloud 연결 상태와 기기 EventKit 권한을 구분 |
| Privacy | domain별 Decision 접근 동의와 data-class retention |
| 알림 | 각 기기의 로컬 notification 권한 |

Mac에서 설정한 값을 iPhone으로 직접 복사하지 않는다. Mac과 iPhone이 같은
Main의 `/v1/inputs`를 읽기 때문에 동기화되는 구조다. Mac이 서버 설정을 바꾸면
iPhone은 foreground, pull-to-refresh 또는 pairing/settings change 시 최신
descriptor를 다시 읽는다.

API token, Open Wearables API key, Hermes provider key와 calendar credential은
서버 소유 secret이다. pairing grant는 짧은 수명의 일회용 code만 전달하며, QR에
장기 bearer token을 넣지 않는다. 앱은 server readiness와 configured/not
configured 상태만 표시한다.

동시 편집은 last-write-wins가 아니다. 두 앱 모두 상세 GET의 strong `ETag`를
`If-Match`로 보내며, `409`에서는 최신 descriptor를 다시 읽고 사용자가 바꾼
필드만 재적용한다.

## 5. First-party HealthKit 수집 계약

목표는 별도 Health Auto Export 앱 설치를 필수로 하지 않는 것이다.

```text
Apple Watch sample
      |
      v
iPhone HealthKit store
      |
      +-- HKAnchoredObjectQuery: 증분 sample + deletion
      +-- HKObserverQuery: 변경 감지
      +-- Background Delivery: best-effort wake-up
      |
      v
healthmes.healthkit.v1 exact bytes
      |
      v
encrypted outbox
      |
      v
POST /v1/ingest/healthkit
```

수집기는 심박수, 안정시 심박수, HRV, 호흡수, SpO2, 걸음, 활동 에너지, 거리,
손목 온도, 수면 단계와 workout을 읽는다. Apple Watch가 서버에 직접 쓰는 것이
아니라, Watch 데이터가 반영된 iPhone HealthKit store를 iPhone collector가
읽는다.

### Pairing fingerprint별 queue 격리

outbox와 anchor는 반드시 pairing fingerprint별 namespace를 사용한다.

```text
pairing A -> queue A + anchors A
pairing B -> queue B + anchors B
```

pairing 변경 시 진행 중 upload를 취소하고 현재 operation이 여전히 같은
fingerprint인지 다시 확인한다. A에서 만든 payload를 B로 보내거나 A의 anchor를
B의 수집 시작점으로 사용하는 것은 금지한다.

pairing 교체와 해제는 persisted transition journal을 사용한다. candidate
credential을 별도 Keychain slot에 먼저 저장한 뒤 journal을 `prepared`로
확정한다. journal을 쓰기 전 `PairingRelayGate`가 새 Watch/notification relay를
차단하고 이미 lease를 얻은 이전 pairing relay가 끝날 때까지 기다린다. 그 뒤
이전 pairing의 queue와 anchor 정리를 시작하기 직전에
`cleanupStarted`를 기록한다. journal이 존재하는 동안 iPhone, Watch, widget과
macOS reader는 active pairing과 cache identity를 노출하지 않는 fail-closed
상태다. 앱이 종료되어도 다음 foreground/background lifecycle이 정리를
재실행하고 전환을 commit한다. `cleanupStarted` 이후에는 일반 clear/abort가
journal을 지울 수 없다.

사용자가 명시적으로 연결 해제를 요청했는데 손상된 pairing record에서 이전
fingerprint를 검증할 수 없다면 특정 namespace만 안전하게 고를 수 없다. 이때는
HealthKit encrypted outbox 전체와 모든 HealthKit anchor, pause, last-upload
로컬 상태를 privacy purge한다. 자동 복구나 pairing 교체에서는 이 전역 삭제를
수행하지 않고 fail-closed 상태를 유지한다.

outbox payload는 기기 저장소에 평문으로 두지 않는다. 현재 outbox primitive는
AES-GCM sealed file과
`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` Keychain key를 사용한다.
queue에는 최소한 다음 값을 원자적으로 보존한다.

- exact request bytes
- 최초 생성한 stable `Idempotency-Key`
- pairing fingerprint
- batch가 성공할 때 확정할 candidate anchors
- attempt 수, 다음 retry 시각과 생성 시각

재시도할 때 JSON을 다시 encode하지 않는다. 같은 outbox item은 같은
`Idempotency-Key`와 같은 exact bytes를 사용한다.

### Durable ACK 뒤 anchor 확정

안전한 순서는 다음과 같다.

```text
1. HealthKit에서 next batch와 candidate anchors를 읽음
2. exact bytes + Idempotency-Key + candidate anchors를 encrypted outbox에 저장
3. 저장된 exact bytes를 POST /v1/ingest/healthkit으로 전송
4. HTTP 202, durable=true, sha256와 size_bytes 및 accepted forward status를 검증
5. candidate anchors를 해당 pairing namespace에 확정
6. outbox item 삭제
```

응답 유실이나 앱 종료가 4와 5 사이에 발생하면 같은 item을 다시 보낸다. 서버는
같은 key와 같은 exact bytes에 저장된 ACK를 반환하므로 anchor를 안전하게
확정할 수 있다.

서버의 exact-byte 의미는 다음과 같다.

- 같은 `Idempotency-Key` + 같은 bytes: 원래 저장된 ACK 재사용
- 같은 `Idempotency-Key` + 다른 bytes: `409 idempotency_conflict`
- native `healthmes.healthkit.v1`: `Idempotency-Key` 필수

`durable=true`는 raw payload가 서버의 durability 경계를 통과했다는 뜻이다.
그러나 iPhone은 `queued` 또는 `nothing_mapped`만 accepted 상태로 보고
anchor를 확정한다. 현재 서버가 삭제가 포함된 native batch에 반환하는
`503 healthkit_deletion_pending`은 raw payload와 tombstone이 durable하더라도
canonical Open Wearables 데이터까지 삭제하지 못했다는 뜻이다. iPhone은 이
응답에서 deletion anchor를 확정하거나 outbox item을 삭제하지 않고 같은 exact
bytes와 key로 재시도한다. 구버전 서버가 반환할 수 있는 `202
forward_status=deletions_recorded`도 같은 이유로 retryable로 처리하며 accepted
상태로 사용하지 않는다. `forward_failed`와 `skipped_no_user`에서도 서버 receipt와
iPhone outbox를 미완료로 유지하고 같은 exact bytes와 key로 재시도한다.

`forward_failed`, `skipped_no_user`, 일반 네트워크·`5xx` 실패는 모두 candidate
anchor를 확정하지 않고 encrypted outbox에서 exponential backoff로 재시도한다.
영구적인 client-side 거부는 terminal 항목으로 보존하고 해당 HealthKit lane만
차단한다. 다른 lane은 계속 수집·전송할 수 있으며 사용자는 Settings에서 terminal
항목을 확인하고 수동 재시도하거나 queue를 제거할 수 있다. 폐기된 구버전 정책이
남긴 미확정 terminal 항목은 다음 drain에서 retryable 상태로 마이그레이션한다.
삭제 보류(`503 healthkit_deletion_pending` 또는 legacy
`deletions_recorded`)도 deletion anchor를 건너뛰지 않고 같은 exact bytes와 key로
계속 재시도한다.

## 6. Legacy Health Auto Export 호환

외부 Health Auto Export 계열 앱은 **선택적 legacy adapter**다. HealthMes의
필수 설치 항목이나 제품 의존성이 아니다.

```text
권장 경로
HealthMes iPhone collector -> healthmes.healthkit.v1 -> /v1/ingest/healthkit

호환 경로
외부 exporter legacy payload ----------------------> /v1/ingest/healthkit
```

서버는 schema가 `healthmes.healthkit.v1`인 native payload와 기존 headerless
legacy payload를 같은 endpoint에서 구분한다. legacy payload는 기존
`transform_hae()` 경로와 raw-first 계약을 유지해야 한다. first-party collector를
추가한다고 기존 사용자의 exporter 자동화를 깨뜨리지 않는다.

## 7. Pane `%44` 통합 경계

Pane `%44`의 산출물은 first-party Apple Health collector다. Main 엔진이나
Hermes 구현을 교체하는 산출물이 아니다.

Pane `%44`가 제공해야 하는 범위:

- HealthKit authorization과 지원 type 목록
- anchored query, observer와 background delivery
- sample, sleep, workout, deletion의 native payload
- pairing fingerprint별 encrypted outbox
- stable idempotency key와 exact-byte retry
- retry/backoff, 재시작 복구와 ACK 뒤 anchor 확정
- 권한 해제, pause, unpair와 queue 삭제 동작

Apple UI/Main 통합 브랜치가 소유하는 범위:

- 기존 one-page Settings에 collector action과 상태 연결
- 기존 pairing과 Main REST client 재사용
- `/v1/ingest/healthkit` native ACK 계약 연결
- Main의 `/v1/inputs`와 Apple Health 상태 표현 정렬
- iPhone/macOS/Watch 화면이 기존 Main 데이터를 표시하도록 adapter 연결

산출물은 dirty worktree 복사가 아니라 검토 가능한 commit 또는 cherry-pick으로
통합한다. 같은 파일을 다른 세션이 수정 중이면 소유권을 먼저 정리한다.

## 8. 2026-08-24 감사 기준 현재 상태

현재 integration worktree에는 first-party sync의 repository 경로가 연결되어 있다.

- Main의 native/legacy 공용 `POST /v1/ingest/healthkit`
- native receipt, exact body hash, stored ACK와 deletion tombstone
- Swift native payload/ACK 모델과 anchored HealthKit query
- AES-GCM encrypted `HealthKitSyncOutbox`와 격리·재시도 단위 테스트
- manager의 outbox-first 저장, stable `Idempotency-Key` 재전송과 ACK hash/size 검증
- durable ACK와 accepted forward status 뒤 pairing별 anchor 확정과 queue 삭제
- forwarding 실패에서 anchor를 확정하지 않는 fail-closed retry와 lane별 차단
- 구버전 미확정 terminal journal을 retryable 상태로 복구하는 encrypted migration
- pairing relay lease/fence와 fingerprint 불명 명시적 unpair의 전체 privacy purge
- Settings의 pending 수, retry, pause/resume와 현재 pairing queue 삭제
- idempotency header가 있는 excessive-depth JSON의 raw-first 보존 회귀 테스트
- receipt/tombstone migration head, metadata parity와 populated downgrade test

Open Wearables 전달은 raw durability 뒤의 후처리다. 전달 실패나 사용자 연결
미완료 ACK에서는 같은 receipt와 iPhone outbox를 유지해 재시도한다. Open
Wearables SDK endpoint가 HealthMes idempotency key를 받지 않으므로 외부 `202`
직후 프로세스 종료까지 포함한 upstream exactly-once를 이 adapter만으로
주장하지 않는다. 원문은 Main raw store에 남고, iPhone은 durable ACK와 accepted
forward status를 모두 collector anchor 확정 조건으로 사용한다.

저장소 검증과 제품 enablement도 구분한다. simulator contract/outbox test와
unsigned iOS/watchOS/macOS build는 repository 범위에서 검증하지만, 실제 HealthKit
권한 prompt, Apple Watch-origin sample, background delivery와 잠금 상태 복구는
signed hardware QA가 필요하다.

## 9. 통합 완료 조건

다음 증거가 모두 있어야 first-party HealthKit 동기화를 완료로 표시한다.

1. 같은 batch 재시도에서 `Idempotency-Key`와 body bytes가 동일하다.
2. 같은 key에 다른 whitespace 또는 key order의 body를 보내면 `409`가 난다.
3. ACK가 유실되어도 raw graph와 upstream enqueue가 중복되지 않는다.
4. `durable=true`, hash/size와 accepted forward status가 일치하기 전에는
   어떤 retry 횟수에서도 anchor가 이동하지 않는다.
5. pairing 변경 뒤 이전 queue가 새 server로 전송되지 않는다.
6. outbox 파일만으로 HealthKit 원문을 평문 복구할 수 없다.
7. 앱 종료, 네트워크 단절과 재실행 뒤 pending batch가 복구된다.
8. 정상 및 malformed/deep legacy Health Auto Export fixture가 raw-first 계약을
   지키며 계속 수용된다.
9. iPhone과 macOS 설정이 같은 `/v1/inputs` 정본을 표시한다.

1, 2, 4, 5, 6, 8, 9는 focused Swift/API/migration test와 unsigned build로
검증한다. 3의 Main raw/receipt 중복 방지는 검증하지만 idempotency를 받지 않는
외부 Open Wearables enqueue의 crash-window exactly-once는 보장하지 않는다.
7의 앱 재시작 복구는 encrypted outbox reload test로 검증하며 실제 OS background
복구는 signed hardware QA로 최종 확인한다.
