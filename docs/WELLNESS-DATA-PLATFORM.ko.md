# 웰니스 데이터 통합 플랫폼 전략

> **상태:** 2026-08-06 소유자 요청에 따른 해자 전략 보강 및 딥리서치 결과.
>
> 이 문서는 HealthMes가 최대한 많은 웰니스 입력과 맥락을 수용할 수 있는
> 통합 인터페이스가 되는 것을 독립적인 해자 중 하나로 정의한다.

## TLDR

**HealthMes는 Open Wearables뿐 아니라 건강·행동·환경·일정·주관 상태·의료 등
가능한 많은 웰니스 입력과 맥락을 동일한 계약으로 받을 수 있는 로컬-first
통합 인터페이스가 되어야 한다.**

HealthMes의 여러 해자 중 이 문서가 정의하는 해자는 다음 다섯 가지다.

1. **입력 폭:** 새로운 웨어러블·앱·센서·파일·서비스를 계속 연결할 수 있는 능력
2. **맥락 폭:** 생체 데이터뿐 아니라 행동·환경·일정·주관 상태를 함께 받는 능력
3. **통합 계약:** 출처·시간·단위·품질·동의·보존기한을 잃지 않고 정규화하는 능력
4. **결과 연결:** 입력을 `상태 → 판단 → 사용자 반응 → 실행 → 결과`로 연결하는 능력
5. **오픈 앱 커스터마이징:** 앱 기능과 연결 인터페이스를 오픈소스로 제공해 개인과
   조직이 화면·워크플로·입력·출력 방식을 쉽게 바꾸고 다시 생태계에 기여하게 하는 능력

커넥터 하나의 코드는 복제할 수 있어도, 폭넓은 입력군을 안정된 공통 계약과
권한·품질·보존 정책 아래 계속 수용하고 여러 커스텀 앱이 같은 엔진과 호환되는
플랫폼 전체는 복제 비용이 커진다.

저장 정책은 모든 데이터를 7일 또는 14일 뒤 지우는 단일 TTL보다 **원본은 짧게,
요약과 결과 그래프는 길게** 보존하는 계층형 정책이 적합하다.

저장 위치, 모바일·노트북 역할, iCloud, 데이터별 TTL, 클라우드 용량 관리 및
worktree 격리의 기준은
[`STORAGE-ARCHITECTURE.ko.md`](STORAGE-ARCHITECTURE.ko.md)를 따른다.

## 플랫폼 경계

```text
┌─────────────────────────────────────────────────────────────┐
│ 입력 어댑터                                                  │
│ Open Wearables · HealthKit · Health Connect · 앱사용·수동입력│
│ 캘린더 · 설문/음성 · 체중계 · 환경 · 의료문서/FHIR          │
│ sake 분석 결과(HealthMes는 기록·맥락 연결만 담당)           │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ HealthMes Wellness Event Contract                           │
│ provenance · observed_at · unit · confidence · consent      │
│ retention_class · sensitivity · raw_ref · normalized_value  │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ 로컬 개인 데이터 계층                                       │
│ raw hot store → 정규화 시계열 → 일/주 특징 → 실행 그래프    │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ HealthMes 판단 계층                                         │
│ baseline · correlation · proposal · decision · outcome      │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ 선택적 암호화 동기화/백업                                   │
│ iCloud private space · LocalDirectory · RemoteVault         │
└─────────────────────────────────────────────────────────────┘
```

### 책임 분리

| 계층 | HealthMes가 소유하는 것 | 소유하지 않는 것 |
|---|---|---|
| 입력 어댑터 | 권한, 증분 수집, 출처, 품질 표기 | 각 기기 제조사의 원시 API |
| 정규화 | 공통 이벤트 계약, 단위·시간대 변환, 중복 제거 | 모든 지표를 억지로 같은 의미로 만드는 것 |
| 해석 | 개인 baseline, confidence, 상관, 개입 결과 | 의료 진단 또는 임상적 확정 |
| 저장 | 로컬 원본, TTL, 암호화, 삭제 전파, export | 서버가 평문을 읽는 중앙 데이터 레이크 |
| 플랫폼 | 안정된 adapter SDK/API와 capability discovery | 특정 오픈소스 프로젝트에 대한 강한 결합 |
| 앱 표현 계층 | 공개된 앱 기능, UI adapter, MCP/API 계약, 교체 가능한 화면과 workflow | 하나의 공식 UI만 사용하도록 강제하는 폐쇄형 클라이언트 |

## 오픈 앱 커스터마이징 전략

HealthMes는 엔진뿐 아니라 앱에서 사용하는 기능과 연결 계약도 오픈소스로 제공한다.
공식 iOS·Android·데스크톱·웹 앱은 유일한 제품 표면이 아니라, 같은 HealthMes
엔진을 사용하는 **참조 구현**이어야 한다.

```text
공개 HealthMes 엔진 계약
        ↓
공식 앱 · 조직 전용 앱 · 개인 포크 · 접근성 특화 UI
        ↓
동일한 저장소 · 권한 · 판단 근거 · MCP/Skill 인터페이스
```

커스터마이징 가능한 범위에는 화면과 문구뿐 아니라 입력 adapter, 알림 정책,
승인 workflow, 캘린더 표현, 로컬 모델 선택, 조직별 skill 조합이 포함된다. 반면
데이터 출처, 동의, 보존기한, 판단 근거와 안전 경고는 공통 계약을 우회해 숨기거나
변조할 수 없게 한다.

이 전략의 가치는 코드 공개 자체가 아니다. 사용자가 공급자에 종속되지 않고,
외부 개발자가 새로운 입력과 앱 경험을 더 빠르게 만들며, 검증된 개선을 다시
공유할수록 HealthMes 호환 생태계의 범위와 학습 속도가 커지는 것이 보조 해자가 된다.

## 추가할 입력

지원 범위는 가능한 넓게 설계한다. 다만 구현은 한 번에 하지 않고,
**행동을 바꿀 수 있고 결과를 측정할 수 있는가**를 순서 기준으로 둔다.

| 우선순위 | 입력군 | 예시 | HealthMes에서 답할 질문 | 권장 시작점 |
|---|---|---|---|---|
| P0 | 주관 상태 | 에너지, 집중, 기분, 통증, 스트레스, 배고픔 | 센서 추정과 실제 체감이 일치하는가 | 워치/폰 1탭 EMA |
| P0 | 행동 결과 | 완료, 실제 소요시간, 미룸, 중단 이유 | 어떤 상태에서 어떤 일정이 실제로 성공하는가 | 기존 decision/schedule 모델 |
| P0 | 앱·기기 사용 | 앱 전환, 화면 시간, 알림, 키보드/마우스 idle | 집중 저하가 수면 때문인지 방해 때문인지 | Android 수집기 + ActivityWatch |
| P1 | 체성분·활력 | 체중, 체지방, 혈압, 체온, SpO2 | 회복·운동·식사 추세와 장기 변화가 연결되는가 | HealthKit/Health Connect, openScale |
| P1 | 운동 상세 | 종목, 세트, RPE, GPS, 고도, 훈련 부하 | 어떤 운동량이 다음날 회복과 집중에 적절한가 | Open Wearables + wger/OpenTracks |
| P1 | 환경 | 날씨, UV, 일조, 공기질, 소음, 실내 CO2 | 피로·두통·수면 변화에 환경 요인이 있는가 | Open-Meteo + 선택적 로컬 센서 |
| P1 | 생활 습관 | 기상/취침 루틴, 햇빛, 명상, 사우나, 흡연 | 반복 가능한 회복 행동은 무엇인가 | 1탭 습관 이벤트 |
| P1 | 생리 건강 | 주기, 증상, 체온, 임신 가능성 비공개 설정 | 주기 단계별 에너지·수면 변화가 있는가 | HealthKit/Health Connect + 수동 입력 |
| P2 | 검사·의료 | 검사 결과, 약, 증상, 진료 기록, FHIR | 일정·컨디션 판단에 필요한 장기 맥락은 무엇인가 | 사용자 import + FHIR adapter |
| P2 | 사회·업무 맥락 | 회의 유형, 이동, 장소 범주, 함께한 사람 별칭 | 어떤 맥락이 스트레스·완료율에 영향을 주는가 | 캘린더 기반, 원문 최소 보존 |
| P2 | CGM 등 고위험 입력 | 포도당, 인슐린 관련 이벤트 | 식사 후 반응과 컨디션의 관계는 무엇인가 | Nightscout read-only, 명시적 opt-in |

### 반드시 함께 받아야 하는 메타데이터

```text
event_id
user_id
type / schema_version
observed_at / recorded_at / timezone
source_provider / source_device / source_record_id
value / unit / normalized_value
confidence / coverage / quality_flags
capture_method: sensor | photo | voice | text | import | inferred
consent_scope / sensitivity
retention_class / expires_at
raw_ref / derived_from[]
```

`confidence`는 모델의 확률 하나가 아니다. 센서 착용 여부, 측정 조건, 데이터
누락률, 사용자의 확인 여부를 함께 반영해야 한다. 추론값은 관측값과 별도 타입으로
저장하고 `derived_from`으로 근거를 역추적할 수 있어야 한다.

## 오픈소스 도입 후보

별 수는 2026-08-04 조사 시점의 대략적인 GitHub 표시값이다. 인기도 신호일 뿐
품질·보안·라이선스 적합성을 대신하지 않는다.

| 후보 | 대략적 인기 | 사용처 | 통합 방식 | 판단 |
|---|---:|---|---|---|
| ActivityWatch | 17.8k★ | macOS/Windows/Linux 앱·웹 사용과 AFK | 로컬 REST/이벤트 import | **P0 추천** |
| wger | 6.1k★ | 운동 루틴, 세트, 영양 기록 | REST adapter, 별도 서비스 | **P1 추천** |
| Gadgetbridge | 4.5k★ | Android에서 클라우드 없이 다양한 웨어러블 연결 | export/IPC adapter | **P1, Android 보완** |
| OpenTracks | 활발한 OSS | GPS 운동 경로·고도·속도 | GPX/KML import | **P1 선택** |
| openScale | 2.3k★ | Bluetooth 체중·체성분계 | CSV/Health Connect 경유 | **P1 추천** |
| whisper.cpp | 50.2k★ | 로컬 증상·기분·주관 상태 음성 기록 | 로컬 sidecar | **P0 추천** |
| Nightscout | 2.4k★ | 개인 CGM 데이터 | read-only REST adapter | **P2, 의료 경계 필요** |
| Open mHealth schemas | 표준 중심 | 건강 데이터 공통 스키마 참고 | 어휘/단위 매핑 참고 | **계약 설계에 추천** |
| Medplum/Fasten Health | FHIR 생태계 | 의료기관 기록·개인 PHR 연결 | 독립 adapter/service | **P2 검토** |

### 라이선스 원칙

- Apache-2.0/MIT 계열은 라이브러리 또는 sidecar 도입을 우선 검토한다.
- GPL/AGPL 계열은 배포 형태에 따라 의무가 달라지므로 코드를 복사·벤더링하기
  전에 법률 검토가 필요하다.
- GPL/AGPL 프로젝트는 가능하면 사용자가 별도로 실행하는 서비스의 API,
  표준 export 파일, OS health store를 경계로 연결한다.
- 모델 weight와 학습 데이터 라이선스는 코드 라이선스와 별도로 기록한다.

## 음식 입력 담당 경계

음식 사진 인식은 Sake의 카페인 관찰 계약을 하위 호환으로 포함하는 HealthMes
`NutritionObservation` v2 계약을 사용한다. HealthMes는 이를 기존 `FoodLog`로
평탄화하지 않고 `WellnessEvent.payload`에 원형 그대로 저장한다. 사진 원본은
`nutrition_media`, 원문·transcript는 `nutrition_raw_capture`, 구조화 관측값은
`nutrition_observation`, 사용자 확인은 `nutrition_confirmation`으로 분리해 각
보존정책을 독립 적용한다. HealthMes가
관측값을 자동으로 사실로 승격하지 않는다. 일반 영양 검토는
`nutrition.review.v1`, Sake의 정확한 카페인·일일 완전성 확인은 기존 전용
confirmation 이벤트로 각각 남긴다. 일반 영양 검토는 관찰과 같은 보존정책을
사용하고, 카페인·일일 완전성 확인만 장기 confirmation 정책을 사용한다.

HealthMes의 `IntakeInteraction`은 영양 관찰 위의 오케스트레이션 계약이다. 사진,
텍스트, 로컬 음성 transcript를 같은 식사 맥락으로 찾게 하지만 sake payload를
복사하거나 변경하지 않는다. `먹은 기록`, `먹기 전 후보`, `분석 전용`, `계획`,
`비교` intent를 분리하고 실제 섭취는 별도 `IntakeOutcome`이 있어야만 확정한다.
에이전트는 MCP 검색과 `IntakeDecisionRequest`의 evidence ID를 통해 이후 판단에
같은 기록을 재사용한다.

## 저장 및 보존 전략

### 권장 기본값

| 데이터 계층 | 기본 보존 | 이유 |
|---|---:|---|
| 고빈도 원시 센서·상세 앱 이벤트 | 14일 | 재처리 가능성과 로컬 용량 균형 |
| 증상 원본 사진/음성 | 7일 | 민감도와 용량이 큼; 확인 뒤 구조화값 유지 |
| 분/시간 단위 정규화 시계열 | 90일 | 개인 baseline과 계절 전 단기 상관 분석 |
| 일/주 단위 특징과 quality 통계 | 사용자 삭제 전까지 | 크기가 작고 장기 개인화에 필요 |
| 결정·승인·수정·거절·결과 | 사용자 삭제 전까지 | HealthMes의 핵심 실행 그래프 |
| 사용자 작성 의료 기록 | 자동 삭제 안 함 | 일반 웰니스 TTL과 분리, 명시적 삭제 |
| 삭제 tombstone | 30일 | 여러 기기에서 삭제가 다시 살아나는 문제 방지 |
| 암호화 백업 | 7 daily + 4 weekly + 12 monthly | 단순 age TTL보다 복구 지점과 비용 균형 |

사용자는 각 계층을 `1일 / 7일 / 14일 / 30일 / 90일 / 무기한`으로 바꿀 수
있어야 한다. 단, 저장 기간을 늘리는 UI는 예상 용량을 먼저 보여준다.

### 용량 기반 자동 조절

```text
storage_budget_bytes
  → 데이터 클래스별 예약 비율
  → 오래된 raw부터 삭제
  → media 원본 삭제
  → 정규화 시계열 downsample
  → 장기 특징·결정 그래프는 마지막까지 유지
```

- 기본 예산은 기기 여유 공간의 고정 비율과 절대 상한 중 작은 값으로 계산한다.
- 임계값 예시는 soft 70%, hard 90%다. soft에서 경고·downsample, hard에서
  정책에 따라 오래된 raw를 삭제한다.
- 사용자가 기록한 원본과 의료 데이터는 자동 용량 회수 대상에서 제외하거나
  별도 동의를 받는다.
- 삭제 전 `expires_at`, 예상 회수 용량, 남을 파생 데이터가 무엇인지 보여준다.

### iCloud의 역할

iCloud를 로컬 DB의 유일한 정본으로 두지 않는다.

```text
로컬 정본
  ├─ iCloud: 같은 Apple 계정의 기기 간 선택적 암호화 동기화
  ├─ LocalDirectory: 사용자가 소유한 로컬 스냅샷
  └─ RemoteVault: 장기 암호문 백업과 엔터프라이즈 저장
```

- CloudKit private database 또는 iCloud Drive app container는 Apple 기기 간
  동기화 표면으로 사용할 수 있다.
- HealthMes의 민감 payload는 기존 snapshot 원칙처럼 업로드 전에 앱 레벨에서
  암호화한다. iCloud 계정 보안 설정만을 유일한 암호화 경계로 가정하지 않는다.
- 레코드 단위 동기화는 변경 토큰, idempotency key, tombstone, 충돌 규칙이
  필요하다. 스냅샷 백업과 실시간 동기화를 같은 프로토콜로 섞지 않는다.
- macOS의 Python daemon이 임의의 iCloud app container를 직접 다루게 하지
  말고, Apple entitlement를 가진 companion 앱을 sync bridge로 둔다.
- iCloud가 꽉 찼거나 로그아웃되어도 로컬 캡처와 판단은 계속되어야 한다.

## 추천 인터페이스

기존 `BackupProvider`와 별도로 다음 경계를 둔다.

```python
class WellnessSource(Protocol):
    def capabilities(self) -> list[Capability]: ...
    def authorize(self, scopes: list[str]) -> AuthorizationResult: ...
    def pull(self, cursor: str | None) -> EventBatch: ...
    def revoke(self) -> None: ...


class SyncProvider(Protocol):
    def push_changes(self, batch: ChangeBatch) -> SyncCursor: ...
    def pull_changes(self, cursor: SyncCursor | None) -> ChangeBatch: ...


class RetentionPolicy(Protocol):
    def classify(self, event: WellnessEvent) -> RetentionClass: ...
    def compact(self, now: datetime) -> CompactionReport: ...
    def purge(self, now: datetime) -> PurgeReport: ...
```

`WellnessSource`, `SyncProvider`, `BackupProvider`를 분리해야 입력 장애, 동기화
충돌, 백업 실패가 서로 전파되지 않는다.

## 실행 순서

### 1. P0: 입력 계약과 핵심 맥락

- `WellnessEvent` envelope와 provenance/confidence/retention 필드를 먼저 만든다.
- ActivityWatch import와 1탭 주관 상태 입력을 추가한다.
- raw 14일, media 7일, aggregate 장기 보존 compactor를 구현한다.

**종료 조건:** Open Wearables 외 두 종류 이상의 입력이 동일한 계약으로 들어오고,
원본 삭제 후에도 일 단위 맥락과 결과 연결을 재현할 수 있다.

### 2. P1: OS health hub와 주변 입력

- iOS는 HealthKit, Android는 Health Connect를 우선 hub로 사용한다.
- openScale, OpenTracks, wger는 직접 벤더링보다 hub/export/API adapter를 쓴다.
- 날씨·UV·공기질과 사용자의 햇빛/수분/카페인 이벤트를 결합한다.
- iCloud sync bridge를 E2E 암호화와 tombstone까지 포함해 구현한다.

**종료 조건:** 공급자 하나가 사라져도 normalized event와 decision graph가
깨지지 않고, 오프라인 캡처와 재동기화가 반복 가능하다.

### 3. P2: 의료·엔터프라이즈

- FHIR import, Nightscout 등은 별도 동의·권한·감사 로그 경계에서 제공한다.
- RemoteVault에 조직 정책, 보존 잠금, 무결성 검증, 사용량 계측을 얹는다.
- 가격과 상품 정책은 실제 저장량 계측 이후 Future Work로 결정한다.
- 고객별 키 또는 고객 관리 키(BYOK)를 지원하되 평문 분석을 기본값으로 만들지 않는다.

**종료 조건:** 미래 저장 서비스가 HealthMes의 평문 데이터 접근 권한으로
변질되지 않고, 사용자 export/delete가 동기화·백업 전체에 검증 가능하게 전파된다.

## 검증 지표

- 지원 가능한 입력 종류와 실제 연결된 입력의 7일/30일 활성률
- 새 adapter 구현 시간, 계약 적합성 테스트 통과율
- 각 입력이 실제 제안에 사용된 비율과 제안 결과 개선 여부
- source별 누락률, 중복률, 시간대 오류율, confidence calibration
- raw 삭제 후 장기 인사이트 재현 가능 여부
- 기기 분실·iCloud 실패·잘못된 passphrase·삭제 전파 복구 훈련
- 사용자별 저장량, 일별 증가량, compaction 회수량, 예상 잔여 기간

## 조사 출처

- Apple HealthKit: https://developer.apple.com/documentation/healthkit
- Android Health Connect: https://developer.android.com/health-and-fitness/guides/health-connect
- Apple CloudKit: https://developer.apple.com/icloud/cloudkit/
- Apple iCloud security: https://support.apple.com/en-us/102651
- HL7 FHIR: https://hl7.org/fhir/
- Open mHealth schemas: https://github.com/openmhealth/schemas
- ActivityWatch: https://github.com/ActivityWatch/activitywatch
- wger: https://github.com/wger-project/wger
- Gadgetbridge: https://github.com/Freeyourgadget/Gadgetbridge
- OpenTracks: https://github.com/OpenTracksApp/OpenTracks
- openScale: https://github.com/oliexdev/openScale
- whisper.cpp: https://github.com/ggerganov/whisper.cpp
- Nightscout: https://github.com/nightscout/cgm-remote-monitor
- Open-Meteo: https://open-meteo.com/en/docs
