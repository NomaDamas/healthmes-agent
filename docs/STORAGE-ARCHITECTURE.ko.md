# HealthMes 통합 저장·보존 아키텍처

> **문서 지위:** 2026-08-05 소유자 결정을 구현 가능한 저장 계약으로 구체화한
> 아키텍처 기준 문서.
>
> **범위:** 다입력 웰니스 플랫폼의 로컬·모바일·노트북·iCloud·관리형 클라우드
> 저장, 데이터별 보존기간, 용량 관리, 삭제·복구, 병렬 개발 격리.
>
> 음식 분석·음식 사진 인식은 sake가 담당한다. HealthMes는 그 결과를 공통
> `WellnessEvent`로 받아 저장·맥락 연결만 한다.
>
> Agent runtime, MCP와 저장소 사이의 최신 연결 기준은
> [`HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md)
> 를 따른다.

## 구현 상태 — 2026-08-16

컴퓨터 Personal Data Node의 저장 제어 계층은 구현되었다.

- `WellnessEvent`, `RetentionPolicy`, `StorageObject`, `StorageUsageDaily`,
  `PurgeJob` 모델과 Alembic 마이그레이션
- 데이터별 `1/7/14/30/90일/무기한` 정책 API
- raw ingest와 media 업로드의 중앙 저장 객체 자동 색인
- 파일 사용량 측정, 미등록 raw/media 발견, dry-run, 안전 경로 검증,
  만료 삭제, 미디어 참조 정리, 삭제 감사
- 매시간 storage maintenance scheduler
- `/storage` 컴퓨터 웹 관리 화면과 `/v1/storage/*` API
- 기존 LocalDirectory/RemoteVault 암호화 백업 상태 표시
- sake `NutritionObservation`을 원형 그대로 `WellnessEvent.payload`에 저장
- 음식/음료 사진, 구조화 관측값, 사용자 확인을 서로 다른 보존 클래스로 분리
- Android와 ActivityWatch를 같은 `activity.*` `WellnessEvent` 파티션에
  저장하며, 서버가 조건부 iPhone Screen Time aggregate도 같은 파티션에
  수용할 수 있음
- `/v1/inputs` 통합 입력 제어 평면에서 수집기 capability, 연결 상태,
  activity 기기별 수집 제어, domain별 Decision Agent 동의와 데이터별 보존
  정책을 제공. 비활동 입력은 실제 adapter의 연결·동기화 action만 노출
- 기존 `POST /v1/ingest/healthkit` raw-first 경로를
  `wearable.healthkit-bridge` 입력으로 노출하고, 원문은 `raw_payload`,
  정규화된 Open Wearables mirror는 `wearable_normalized`로 분리
- 조건부 Wellness `DecisionRecord`에 `decision` 보존 클래스를 적용하고,
  compact outcome만 저장. 기존 비-Wellness 판단 기록은 보존정책 대상에서 제외

| 데이터 클래스 | 기본 보존 | 저장 내용 |
|---|---:|---|
| `nutrition_media` | 7일 | 원본 사진과 사진에서 읽은 라벨·근거·warning |
| `nutrition_raw_capture` | 14일 | 식사 원문, 음성 transcript, 미디어 참조, 섭취 결과 note |
| `nutrition_observation` | 90일 | sake VLM 구조화 관측값과 provenance |
| `nutrition_confirmation` | 무기한 | 항목별 사용자 확인과 일일 완전성 확인 |
| `decision` | 무기한 | 조건부 Wellness 판단의 compact outcome, source refs와 runtime |

사진과 원문이 만료되어 삭제되어도 90일 관측값과 무기한 확인 이벤트는 각자의
정책에 따라 남는다. 사용자는 기존 `/storage` 화면 또는
`/v1/storage/settings/{data_class}` API에서 각 클래스를
`1/7/14/30/90일/무기한`으로 바꿀 수 있다.

`decision`도 같은 API에서 `1/7/14/30/90일/무기한`으로 바꾼다. 판단 확정 시각을
보존 기준으로 사용하며 `expires_at <= maintenance 시각`이면 삭제한다. 설정
축소와 finalization은 같은 write-plane fence를 사용하므로 경합 중에도 새 행이
이전 정책으로 남지 않는다. 삭제 대상 DecisionRecord를 참조하는 proposal은
삭제하지 않고 FK만 `NULL`로 바꾸며, correlation ID가 없는 historical/non-wellness
DecisionRecord는 건드리지 않는다.

iOS/Android의 실제 설정 UI는 별도 후속 작업이다. 설정 정본은 컴퓨터
Personal Data Node이며 데스크톱 웹과 모바일 UI는 동일한
`/v1/inputs/{source_id}/settings` 계약을 사용한다. Android는 기존 durable
upload queue를 유지하고, iPhone Screen Time은 최근 완료 48시간을 반복 가능한
authoritative snapshot으로 전송하도록 계약돼 있다. 단 최초 승인 직후와 timezone
변경 직후에는 최신 완료 local-hour 1개에서 시작하고, 이후 같은 timezone에서만
최대 48시간을 재조정한다. 앱 lifecycle은 승인 직후, foreground, pairing 변경과
Screen Time `BGAppRefreshTask`를 sync seam에 연결한다. 저장된 input/retention
변경 뒤 device UI가 호출할 `inputConfigurationDidChange()` seam도 제공하며,
실행 중 authorization/config/timezone 변경은 하나의 pending fresh run으로
합쳐진다. 일반 iOS 빌드의 factory는 unavailable adapter를 반환하고, opt-in
빌드는 SDK capability probe가 성공할 때만 실제 collector를 컴파일한다.

iPhone의 로컬 Screen Time retry outbox는 최대 8개·16 MiB, 고정 14일 TTL이며
재시작 시와 sync/retry 변경 전에 만료 항목을 제거한다. 디렉터리와 atomic output
파일은 기기 backup에서 제외된다. 이 transport TTL은 Personal Data Node의
사용자 설정 가능 `activity_raw` 보존기간과 별도다. BG task 만료는 foreground
waiter가 없을 때만 실제 service pipeline을 취소한다. Apple entitlement 승인,
App ID capability, signed provisioning, 사용자 data-access 승인과 실기기 검증은
여전히 저장소 밖의 배포 조건이며, 실제 설정 화면은 device-team 범위다.

iPhone collector ID는 Screen Time 가명화 Keychain key에서 안정적으로 파생되며
새 `ios-collector-v1-*` instance는 중앙 input 설정에서 명시적으로 활성화하기
전까지 fail closed한다. denied/unavailable 결과는 최초 동의 경계를 저장하지
않는다. Calendar mirror도 retention 변경과 sync를 같은 저장 write fence로
직렬화하고, ingest와 Decision Agent 조회 모두 `calendar_mirror` cutoff를
강제하므로 다음 provider full sync가 삭제된 과거 일정을 되살리지 못한다.

## TLDR

HealthMes는 **하나의 논리적 정본인 Personal Data Node**를 둔다.

```text
휴대폰
센서 접근 + 오프라인 암호화 큐
              │
              │ 증분 업로드
              ▼
┌─────────────────────────────────────────────┐
│ Personal Data Node — 논리적 운영·백업 단위  │
│                                             │
│ postgres service / postgres_data volume     │
│  ├─ healthmes database                      │
│  └─ open-wearables database                 │
│                                             │
│ healthmes_data volume / HEALTHMES_DATA_DIR  │
│ 원본 payload·미디어·압축 시계열 chunk       │
│                                             │
│ ./data/hermes bind mount                    │
│ Hermes local runtime state                  │
└──────────────────┬──────────────────────────┘
                   │
          클라이언트 암호화 후 복제
                   │
          ┌────────┴─────────┐
          ▼                  ▼
    iCloud 선택 복제     RemoteVault
    Apple 사용자용       장기 보존·기업용
```

현재 제품에서는 Personal Data Node가 노트북, Mac mini 또는 홈서버에서 돈다.
휴대폰과 iCloud는 정본이 아니다. 노트북 없는 미래 사용자는 같은 노드를 관리형
클라우드에서 실행할 수 있지만, 이것은 평문을 볼 수 없는 백업 Vault와 구분한다.

## 1. 목표와 비목표

### 목표

1. Open Wearables 외 가능한 많은 웰니스 입력과 맥락을 같은 계약으로 저장한다.
2. 사용자는 데이터 종류별로 `1일`, `7일`, `14일`, `30일`, `90일`, `무기한`을
   선택한다.
3. 로컬 용량이 부족해도 원본부터 안전하게 축약하고 핵심 개인화 기록은 유지한다.
4. 모바일·노트북·클라우드가 같은 논리 모델과 보존 정책을 사용한다.
5. 미래 클라우드 저장소가 현재 로컬 데이터 계약을 바꾸지 않고 붙을 수 있게 한다.
6. HealthMes와 Hermes 개발은 브랜치와 worktree 수준에서 격리한다.

### 비목표

- 휴대폰에서 Postgres, Open Wearables worker, Hermes 전체를 상시 실행하지 않는다.
- 휴대폰과 노트북을 동시에 쓰기 정본으로 만드는 multi-master 동기화를 하지 않는다.
- iCloud를 Android까지 포함하는 공통 저장소로 취급하지 않는다.
- Open Wearables 데이터베이스 스키마를 HealthMes 테이블과 합치지 않는다.
- 백업 Vault가 사용자 건강 데이터의 평문을 읽거나 분석하지 않는다.
- 보존기간 선택만으로 의료적 안전성이나 법적 준수를 주장하지 않는다.

## 2. 왜 하나의 논리적 정본인가

“하나로 한다”는 모든 데이터를 하나의 파일에 넣는다는 뜻이 아니다.

```text
하나의 정본
= 누가 최종 쓰기 권한을 가지는지가 하나

여러 저장 엔진
= 정본 내부에서 데이터 성격에 맞게 분리
```

HealthMes는 현재 Python 서비스, Postgres, Open Wearables worker, Hermes runtime을
사용한다. 이 작업은 휴대폰보다 노트북·Mac mini·홈서버에 적합하다.

`하나의 논리적 정본`은 한 volume이나 한 database라는 뜻이 아니다. 현재 compose는
하나의 Postgres service와 `postgres_data` volume 안에 `healthmes`와
`open-wearables`라는 별도 database를 만든다. Activity, Nutrition과 Calendar는
HealthMes DB 안에서 논리 파티션으로 분리하고, 상세 wearable 원본과 고빈도
시계열은 Open Wearables DB에 둔다. 큰 HealthMes object는 `healthmes_data`,
Hermes runtime state는 `./data/hermes`에 별도로 존재한다. 이들을 합쳐 하나의
논리적 Personal Data Node로 운영하지만, **현재 backup은 전체 노드 복구본이
아니다.** 현재 snapshot은 HealthMes DB, `media/`, `raw_ingest/`를 기본으로 하고
설정된 경우에만 Open Wearables dump와 Hermes home을 포함하는 부분 백업이다.
외부 provider credential, 일부 runtime volume과 연결 상태는 별도로 다시
구성해야 한다. Hermes는 DB를 직접 읽지 않고 HealthMes MCP 하나를 통해 필요한
domain 도구를 선택한다.

### 선택지 비교

| 선택지 | 장점 | 단점 | 결정 |
|---|---|---|---|
| 휴대폰 정본 | 항상 사용자와 함께 있음, 센서 접근이 쉬움 | iOS/Android 백그라운드 제한, 저장 압박, 서버·worker 상시 실행 부적합 | 기각 |
| 노트북 정본 | 현재 스택 그대로 사용, 저장·CPU 여유, 복구 쉬움 | 잠자거나 꺼지면 처리가 지연됨 | **현재 채택** |
| Mac mini/홈서버 정본 | 항상 켜짐, 로컬 프라이버시와 자동화 모두 좋음 | 별도 기기 필요 | **권장 로컬 형태** |
| iCloud 정본 | Apple 기기 동기화 편리 | Android 공통 경로가 아니며 동기화 시점 통제 불가 | 기각 |
| 관리형 클라우드 정본 | 항상 켜짐, 노트북 불필요 | 운영자가 런타임 평문을 다룰 수 있는 신뢰 문제 | 미래 선택 모드 |
| 휴대폰+노트북 multi-master | 오프라인 쓰기 자유 | 충돌·중복·삭제 부활·키 관리가 복잡 | 기각 |

### 채택 구조

- **휴대폰:** 센서 권한, 입력 UI, 임시 파일, 암호화 업로드 큐
- **Personal Data Node:** 유일한 정본, 정규화, 분석, 판단, 보존 정책 실행
- **iCloud:** Apple 사용자의 선택적 암호화 복제 또는 스냅샷
- **RemoteVault:** 장기 암호문 백업
- **관리형 Personal Data Node:** 노트북 없는 사용자를 위한 미래 별도 상품

## 3. 물리 저장 구조

Personal Data Node 내부는 하나의 제품이지만 저장은 세 층으로 나눈다.

```text
┌─────────────────────────────────────────────────────────┐
│ A. 관계형 메타데이터 — Postgres                         │
│ 이벤트 색인·정규화 값·목표·일정·특징·판단·정책·cursor │
├─────────────────────────────────────────────────────────┤
│ B. 객체 저장 — HEALTHMES_DATA_DIR                       │
│ 원본 JSON/ZIP·사진·음성·문서·압축 시계열 chunk          │
├─────────────────────────────────────────────────────────┤
│ C. 불변 암호화 백업 — BackupProvider                    │
│ LocalDirectory · iCloudProvider(미래) · RemoteVault     │
└─────────────────────────────────────────────────────────┘
```

### Postgres에 저장할 것

- `wellness_event`: 모든 입력의 공통 envelope와 작은 정규화 payload
- 기존 목표·일정·결정·인사이트·의료 기록 테이블
- `retention_policy`: 사용자와 데이터 클래스별 보존 선택
- `storage_object`: 큰 파일의 경로, 크기, 해시, 암호화, 만료 시각
- `derived_feature`: 시간·일·주 단위 특징과 baseline
- `device_cursor`: HealthKit/Health Connect/adapter 증분 수집 위치
- `sync_tombstone`: 삭제가 다른 기기에서 되살아나지 않게 하는 표식
- `storage_usage_daily`: 데이터 클래스별 실제 사용량과 증가량
- `purge_job`: 축약·삭제 실행 및 실패 감사 기록

### 객체 저장에 둘 것

- 원본 HealthKit/Open Wearables/vendor payload
- 사진, 음성, 의료문서
- 큰 import ZIP
- 1초·1분 단위 고빈도 시계열 chunk
- export와 암호화 snapshot

큰 payload를 Postgres `JSONB` 한 행에 계속 넣지 않는다. Postgres에는 검색과
정합성에 필요한 메타데이터만 두고, 본문은 해시 기반 객체로 둔다.

```text
objects/
  raw/2026/08/05/<sha256>.bin
  media/2026/08/<sha256>.bin
  timeseries/<source>/<metric>/2026/08/05/<chunk>.parquet.zst
  exports/
  backups/
```

객체 경로에 사용자 이름, 증상명, 지표명 같은 민감한 평문을 넣지 않는다.

### 고빈도 시계열

심박처럼 표본이 많은 데이터는 샘플 한 개당 Postgres 행 하나로 무기한 저장하면
인덱스와 행 overhead가 실제 숫자보다 커진다.

권장 방식은 다음과 같다.

```text
최근 데이터
→ Postgres 행 또는 짧은 chunk로 빠른 조회

고빈도 원본
→ 시간 단위 Arrow/Parquet + Zstd chunk

장기 데이터
→ 시간·일 단위 min/max/mean/median/count/coverage 특징
```

Open Wearables의 데이터베이스는 업스트림 호환을 위해 분리 유지한다. 단, 같은
Postgres 인스턴스와 같은 Personal Data Node에 두고 HealthMes의 공통 이벤트 색인과
`source_record_id`로 논리적으로 연결한다.

## 4. 공통 이벤트 계약

모든 입력은 다음 envelope로 들어온다.

```text
event_id
user_id
type
schema_version
observed_at
recorded_at
timezone
source_provider
source_device
source_record_id
capture_method
quality_flags
confidence
coverage
sensitivity
consent_scope
retention_policy_id
expires_at
payload
raw_object_id
derived_from[]
```

### 저장 원칙

- 측정값과 AI 추론값은 같은 타입으로 덮어쓰지 않는다.
- 사용자 수정값은 원본을 삭제하지 않고 새 revision으로 연결한다.
- 같은 source record는 idempotency key로 중복 인입하지 않는다.
- 시간대 원문과 UTC 정규화 시각을 모두 보존한다.
- 원본이 삭제되어도 어떤 파생값이 어떤 원본에서 왔는지 해시는 남긴다.
- `expires_at`은 서버가 임의로 정하지 않고 사용자 정책에서 결정한다.

## 5. 모바일 저장 구조

휴대폰은 정본이 아니라 **durable capture edge**다.

```text
HealthKit / Health Connect / 사용자 입력
                    │
                    ▼
          암호화 SQLite 업로드 큐
                    │
       성공 확인 전까지 로컬 유지
                    │
                    ▼
        Personal Data Node /v1/ingest
```

### 휴대폰에 저장할 것

- 마지막 동기화 cursor 또는 anchor
- 아직 전송되지 않은 이벤트
- 아직 전송되지 않은 미디어
- 최근 화면 표시용 요약 cache
- 기기 식별자와 암호화 키

### 휴대폰에 저장하지 않을 것

- 전체 사용자 이력
- Open Wearables 데이터베이스 복제본
- Hermes memory 전체
- 장기간 원시 센서 복제본
- 모든 암호화 snapshot

### 휴대폰 기본 용량 정책

- 기본 queue 보존: 7일
- 노드가 오프라인일 때 최대 queue: 14일
- iPhone Screen Time aggregate retry outbox: 고정 14일, 최대 8개·16 MiB,
  backup 제외
- 기본 cache 예산: `min(2GB, 사용 가능 공간의 5%)`
- 업로드 성공 후 미디어는 24시간 grace 뒤 삭제
- HealthKit/Health Connect에 원본이 남아 있는 데이터는 cursor만 유지하고 재조회한다.
- 원본 재조회가 불가능한 사진·음성은 서버 확인 전 삭제하지 않는다.

### 노트북이 꺼져 있을 때

휴대폰은 계속 큐에 쌓는다. 노트북이 다시 켜지면 오래된 순서대로 전송한다.
14일을 넘기기 전에 다음 중 하나를 사용자에게 알린다.

1. Personal Data Node를 켠다.
2. 큐 예산을 늘린다.
3. 선택적으로 iCloud 암호화 staging을 사용한다.
4. 미래의 관리형 Personal Data Node로 전환한다.

## 6. 보존 정책

사용자에게는 단순한 선택지를 제공한다.

```text
1일 | 7일 | 14일 | 30일 | 90일 | 무기한
```

내부적으로는 데이터의 단계마다 별도 정책을 적용한다.

| 데이터 단계 | 권장 기본값 | 설명 |
|---|---:|---|
| 원본 vendor payload | 14일 | 파서 오류 수정과 재처리용 |
| 고빈도 센서 원본 | 14일 | 용량이 크므로 특징 생성 후 삭제 |
| 앱 사용 상세 이벤트 | 14일 | 시간별 집계 후 원본 삭제 |
| 사진·음성·문서 | 7일 | 민감하고 크므로 확인 후 빠르게 삭제 |
| 분 단위 정규화 값 | 30일 | 최근 상세 분석 |
| 시간 단위 특징 | 90일 | 개인 baseline과 상관 분석 |
| 일·주 단위 특징 | 무기한 | 작고 장기 개인화에 필요 |
| 목표·일정·사용자 결정·결과 | 무기한 | 개인 실행 그래프의 핵심 |
| 사용자 작성 의료 기록 | 명시적 선택 | 일반 TTL 자동 적용 금지 |
| 삭제 tombstone | 30일 | 다른 기기의 삭제 부활 방지 |

### 핵심 원칙

```text
raw-first
≠ 원본 영구 보관

raw-first
= 원본을 먼저 안전하게 저장
  → 정규화와 특징 계산
  → 검증 완료
  → 정책에 따라 원본 삭제
```

### 삭제 실행 순서

```text
1. 원본 durable 저장 확인
2. 파싱·정규화 완료
3. 필요한 특징·baseline 계산
4. derived_from 연결 확인
5. 선택적 snapshot 완료
6. 삭제 예정 목록 생성
7. grace 기간
8. 원본 삭제
9. 사용량·감사 기록 갱신
```

파싱 실패, 특징 계산 실패, snapshot 실패가 있다고 무조건 원본을 지우지 않는다.
정책별로 `safe_to_purge` 조건을 통과해야 한다.

### 정책 변경

- 보존기간을 늘리면 아직 남아 있는 데이터부터 새 기간을 적용한다.
- 이미 삭제된 원본은 백업이 없으면 복구할 수 없음을 UI에 명확히 표시한다.
- 보존기간을 줄이면 예상 삭제량을 보여주고 24시간 grace를 둔다.
- “즉시 삭제”는 grace 없이 파일 삭제와 키 폐기를 수행한다.
- `무기한`은 무제한 무료가 아니라 사용자 로컬 예산 또는 클라우드 quota 안에서 동작한다.

## 7. 용량 관리

보존기간 선택만으로는 용량을 예측할 수 없다. 사진 7일과 심박 7일은 크기가
다르기 때문이다.

HealthMes는 사용자별로 다음을 측정한다.

- 데이터 클래스별 현재 bytes
- 하루 평균 증가량
- 압축 전·후 크기
- 선택한 정책에서 30일 뒤 예상 크기
- 로컬 남은 공간
- 클라우드 quota
- snapshot 예약 공간

### 예상 용량 공식

```text
예상 활성 데이터
= Σ(데이터 클래스의 일평균 증가량 × 보존일)
  + 무기한 데이터의 누적량
  + snapshot 예약량
```

### 로컬 기본 예산

- 노트북 Personal Data Node: `min(50GB, 사용 가능 공간의 10%)`
- Mac mini/홈서버: 설치 시 사용자가 절대 상한 선택
- 휴대폰 queue/cache: `min(2GB, 사용 가능 공간의 5%)`
- 디스크 여유 공간 15%는 HealthMes가 사용하지 않는 안전 구간으로 둔다.

### 용량 부족 시 순서

```text
1. 만료된 raw 삭제
2. 업로드 완료된 모바일 cache 삭제
3. 만료된 미디어 삭제
4. 분 단위 데이터를 시간 단위로 downsample
5. 오래된 snapshot rotation
6. 사용자에게 정책 변경 또는 저장 공간 확장 제안
```

목표·결정·사용자 수정·결과 그래프는 자동 공간 회수의 마지막 대상이며 기본적으로
삭제하지 않는다.

## 8. iCloud 구조

iCloud는 Apple 생태계의 선택적 provider다.

### 권장 용도

- 앱 설정과 작은 암호화 이벤트의 Apple 기기 간 동기화
- 암호화 snapshot 보관
- Personal Data Node가 꺼진 동안 제한된 암호화 staging

### 사용하지 않을 용도

- Android 공통 저장소
- Postgres 파일 직접 동기화
- 실행 중인 SQLite 파일 직접 동기화
- 즉시 전달이 필요한 트리거 큐
- 유일한 복구 수단

### 구현 분리

```text
SyncProvider
→ 작은 증분 이벤트와 tombstone

BackupProvider
→ 불변 age snapshot
```

동기화는 삭제도 복제하므로 백업이 아니다. iCloud용 `SyncProvider`와
`ICloudBackupProvider`는 별도 구현이어야 한다.

HealthMes payload는 iCloud 업로드 전에 앱 수준에서 암호화한다. 사용자의 iCloud
보안 설정이나 Advanced Data Protection 사용 여부만을 유일한 보안 경계로 두지 않는다.

## 9. 관리형 클라우드 구조

첫 클라우드 상품은 **Zero-Knowledge RemoteVault**다.

```text
사용자 노드
  → snapshot 또는 immutable chunk 생성
  → 로컬에서 암호화
  → ciphertext 업로드

클라우드
  → object key
  → ciphertext size
  → ciphertext hash
  → expires_at
  → tenant/quota
```

서버가 보지 않는 것:

- 건강 지표명
- 사진과 음성 내용
- 판단 내용
- 사용자 일정
- 암호화 키

### 클라우드 내부

```text
Control plane
계정·기기·quota·복구 상태·감사

Data plane
암호문 object·무결성 hash·만료 시각

Lifecycle worker
expires_at 삭제·snapshot rotation·quota 계산
```

보존 버킷은 `1d`, `7d`, `14d`, `30d`, `90d`, `forever`로 표준화한다.
서버는 데이터 의미 대신 만료 버킷만 알 수 있게 한다.

### 별도 미래 상품

노트북이 없는 사용자를 위해 Hosted Personal Data Node를 제공할 수 있다. 이 상품은
서버에서 HealthMes/Hermes가 데이터를 처리하므로 Zero-Knowledge Vault와 같은
보안 약속을 할 수 없다. 이름, 동의, 보안 설명을 반드시 분리한다.

## 10. Future Work — 가격과 상품 정책

가격, 상품 tier, 무료 용량, 초과요금은 현재 아키텍처 범위에서 결정하지 않는다.
먼저 실제 사용량과 보존 패턴을 측정해야 한다.

```text
현재 확정
데이터별 1/7/14/30/90일/무기한 보존
데이터 클래스별 실제 bytes 측정
30일 뒤 예상 저장량 표시
로컬·iCloud·RemoteVault provider 경계

Future Work
무료 용량
유료 tier
초과요금
Enterprise 계약
```

### 가격 결정 전에 필요한 데이터

- 사용자별 하루 평균 raw·정규화·미디어 증가량
- 압축과 compaction 전·후 크기
- `1/7/14/30/90일/무기한` 선택 비율
- snapshot 평균 크기와 복구 빈도
- iCloud/BYOS/RemoteVault 사용 비율
- 저장소 운영비, 데이터 이동량, 지원 비용

### 지금 구현할 용량 경계

- 사용자가 정책을 바꾸기 전에 예상 저장량을 보여준다.
- 로컬·iCloud·RemoteVault 각각 현재 사용량과 quota를 표시한다.
- quota 70%에서 경고, 90%에서 정책 변경이나 공간 확장을 제안한다.
- quota에 도달해도 로컬 캡처와 이미 저장된 데이터 조회는 계속 동작한다.
- `무기한`은 무제한이라는 뜻이 아니라 현재 저장 위치의 용량 한도 안에서 동작한다.

가격 정책은 위 계측이 실사용으로 검증된 뒤 별도 제품 문서에서 결정한다.

## 11. 암호화와 키

### 로컬

- 휴대폰 키는 Keychain/Android Keystore에 보관한다.
- 노트북 데이터 디렉터리는 FileVault/BitLocker 등 전체 디스크 암호화를 권장한다.
- 앱 수준 민감 객체는 별도 data-encryption key로 암호화할 수 있다.

### Vault

- 계정 root key는 사용자 기기에서 생성한다.
- 데이터 key는 root key로 감싸고 서버에는 암호화된 key만 보낸다.
- 새 기기는 기존 기기의 승인 또는 recovery key로 등록한다.
- 서버는 복구 key를 평문으로 보관하지 않는다.
- 키 분실 시 복구 불가임을 setup에서 명확히 알린다.

기존 `age` snapshot은 불변 백업 포맷으로 유지한다. 실시간 sync chunk의
암호화와 snapshot 암호화를 억지로 같은 프로토콜로 합치지 않는다.

## 12. 삭제와 복구

### 일반 삭제

```text
사용자 삭제 요청
→ 로컬 tombstone 기록
→ 다른 기기에 삭제 전파
→ cloud ciphertext 삭제 예약
→ grace 종료 후 실제 삭제
→ backup 보존 정책에 따라 snapshot rotation
```

### 즉시 삭제

- 활성 객체 삭제
- 관련 data key 폐기
- sync tombstone 유지
- 복구 불가 확인
- 법적 보존 대상이 있다면 사전에 사용자·조직 정책으로 명시

### 기기 분실

- 새 기기 설치
- recovery key 또는 기존 기기 승인
- 최신 snapshot 다운로드
- 무결성 검사
- Personal Data Node 복원
- 마지막 snapshot 이후 이벤트를 sync replay

## 13. Worktree와 브랜치 격리

HealthMes와 Hermes는 동시에 개발될 수 있으므로 파일 소유권을 Git 수준에서 분리한다.

```text
기본 저장소
/Users/.../healthmes-agent
다른 작업이 진행 중이면 읽기 전용

HealthMes 저장 작업
/private/tmp/healthmes-storage-architecture
branch: codex/storage-architecture-20260805

Hermes 작업
별도 hermes repository + 별도 worktree + 별도 branch
```

### 강제 규칙

1. 작업마다 새 branch와 새 worktree를 만든다.
2. 한 branch를 두 worktree 또는 두 agent가 동시에 사용하지 않는다.
3. `vendor/hermes-agent/`는 HealthMes branch에서 수정하지 않는다.
4. Hermes 변경은 Hermes 저장소의 독립 commit/PR로 만든다.
5. 통합은 검토된 commit의 merge 또는 cherry-pick으로만 한다.
6. 다른 worktree의 dirty 파일을 덮어쓰거나 reset하지 않는다.
7. 같은 파일을 다른 agent가 수정 중이면 즉시 멈추고 소유권을 조정한다.

이 규칙은 루트 `AGENTS.md`에도 기록한다.

## 14. 단계별 구현

### Phase A — 정책과 관측

- `retention_policy`
- `storage_object`
- `storage_usage_daily`
- 데이터 클래스별 크기 측정
- UI 예상 용량 계산

**종료 조건:** 삭제 없이도 실제 7일 증가량과 정책별 예상 용량을 계산할 수 있다.

### Phase B — 안전한 compaction

- 원본 → 정규화 → 특징 생성 상태 머신
- `safe_to_purge`
- batch purge와 감사 로그
- tombstone
- 1/7/14/30/90일/무기한 정책

**종료 조건:** 원본 삭제 후에도 일·주 특징과 결정 근거를 재현할 수 있다.

### Phase C — 모바일 edge

- 암호화 SQLite queue
- cursor/anchor
- retry/idempotency
- queue 용량 경고
- 노드 오프라인 14일 복구 시험

**종료 조건:** 노트북을 끈 상태에서 입력을 모은 뒤 중복 없이 전송한다.

### Phase D — iCloud provider

- 작은 이벤트용 `SyncProvider`
- snapshot용 `ICloudBackupProvider`
- 충돌과 tombstone 시험
- iCloud quota/full/logout 시험

**종료 조건:** iCloud 실패가 로컬 캡처나 Personal Data Node를 중단시키지 않는다.

### Phase E — 관리형 Vault

- tenant/device/quota control plane
- ciphertext object lifecycle
- restore drill
- export/delete 감사

**종료 조건:** 서버가 평문과 키 없이 저장·삭제·복구·사용량 계측을 할 수 있다.

## 15. 최종 결정

```text
정본
= 노트북·Mac mini·홈서버의 Personal Data Node

휴대폰
= 센서 접근 + 입력 + 암호화 임시 큐

iCloud
= Apple 사용자의 선택적 암호화 sync/backup

관리형 클라우드
= 암호문 Vault부터 시작

보존
= 데이터별 1/7/14/30/90일/무기한

가격 정책
= 실제 사용량 계측 이후 Future Work

장기 가치
= 원본 전체가 아니라 특징·사용자 수정·판단·실제 결과
```

이 구조는 현재 HealthMes의 로컬-first, Open Wearables 분리, `BackupProvider`,
`RemoteVaultProvider`, `HEALTHMES_DATA_DIR`를 유지하면서 클라우드 상품으로
확장할 수 있다.

## 조사 근거

- Apple CloudKit:
  https://developer.apple.com/icloud/cloudkit/
- Apple CKSyncEngine:
  https://developer.apple.com/documentation/cloudkit/cksyncengine
- Apple Background Tasks:
  https://developer.apple.com/documentation/backgroundtasks
- Apple Platform Security — CloudKit:
  https://support.apple.com/guide/security/advanced-data-protection-for-icloud-sec973254c5f/web
- Android WorkManager:
  https://developer.android.com/topic/libraries/architecture/workmanager
- Android app-specific storage:
  https://developer.android.com/training/data-storage/app-specific
- Android Health Connect incremental reads:
  https://developer.android.com/health-and-fitness/guides/health-connect/develop/sync-data
- PostgreSQL partitioning:
  https://www.postgresql.org/docs/current/ddl-partitioning.html
- PostgreSQL BRIN:
  https://www.postgresql.org/docs/current/brin.html
- Apache Parquet:
  https://parquet.apache.org/docs/
