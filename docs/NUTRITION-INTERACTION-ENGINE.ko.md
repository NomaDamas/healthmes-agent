# HealthMes 식사 입력·의사결정 엔진

기준일: 2026-08-08

## 기능 경계

이 엔진은 iPhone, Android, Web UI를 구현하지 않는다. 모든 디바이스와 Hermes가
같이 사용할 수 있는 식사 입력, 저장, 검색, 판단 컨텍스트 계층만 제공한다.

```text
디바이스 또는 Hermes
        |
        v
REST / MCP Adapter
        |
        v
Intake Interaction Engine
        |
        +--> sake NutritionObservation
        +--> WellnessEvent 저장 구조
        +--> 이후 웰니스 의사결정 skill
```

## 입력과 목적은 별개다

```text
입력 방식: photo | text | voice
사용 목적: log_consumed | ask_before_intake | inspect_only
           plan_future | compare_option
```

사진을 찍었다는 사실만으로 먹었다고 판단하지 않는다. `log_consumed`도 사용자가
기록하려는 목적일 뿐이며, 실제 섭취는 별도 `IntakeOutcome(status=consumed)`이
저장된 뒤에만 확정된다.

## 공통 흐름

```text
Capture
  |
  +--> photo: NutritionObservation ID 참조
  +--> text: 사용자 원문 -> 선택된 영양 분석 provider
  +--> voice: 로컬 audio token -> whisper.cpp 전사 -> 영양 분석 provider
  |
  v
IntakeInteraction (관찰)
  |
  +--> 모델 영양값 확인/수정/거절
  |        |
  |        v
  |   IntakeInteractionReview
  |
  +--> 섭취 확인 --> IntakeOutcome
  |
  +--> 판단 요청 --> IntakeDecisionRequest
                       |
                       v
                 후보 + 확인된 과거 기록
                 + evidence event IDs
                       |
                       v
                 특화된 판단 skill
                       |
                       v
                 IntakeDecision
```

사진 VLM observation 생성은 업로드 media의 natural key와 분석 request
fingerprint로 멱등 처리한다. 그 밖의 interaction capture/review/outcome,
사진 확인·검토, 일일 완전성 확인, decision request/decision mutation은 호출자가
만든 UUID `operation_id`를 요구한다. 이 ID는 영양 전체에서 하나뿐인 전역 ID가
아니라 **쓰기 종류별 멱등 키**다. 같은 쓰기 종류에서 같은 ID와 같은 입력을 다시
보내면 기존 결과를 반환하고, 같은 ID에 다른 입력을 보내면 충돌로 거부한다.
호출자는 종류가 달라도 논리적 쓰기마다 새 UUID를 만드는 것이 권장된다. MCP
owner 쓰기의 trusted proof에도 이 ID가 포함된다.

사진 기반 확인·review·일일 완전성 결과는 저장 identity에 쓰기 종류 prefix를
붙인다. interaction engine의 review·outcome·판단 요청·판단 결과는 쓰기 종류별
source provider와 UUID-only record ID 조합을 쓴다. 따라서 두 방식 모두 전체
source identity는 쓰기 종류별로 분리된다. 기존 UUID-only 사진 결과 행도 조회,
정확한 재시도, maintenance에서 계속 인식한다.

결과 이벤트가 보존기간에 따라 삭제된 뒤에도 `nutrition.operation.v1` marker는
영구 보존된다. 정상 marker는 원문, 영양값, 판단 scope나 결과 상태를 담지 않고,
쓰기 종류, UUID, 요청 fingerprint, 완료 상태만 담는 기술적 비콘텐츠 metadata다.
따라서 과거 ID를 같은 쓰기 종류에서 새 입력으로 재사용할 수 없다. marker 도입
전에 생성된 legacy 결과는 storage maintenance가 만료 결과를 삭제하기 전에
marker로 backfill한다. 과거 요청 hash를 복원할 수 없는 legacy 결과는 값을
추측하지 않는다. retained 결과가 남아 있는 동안에는 쓰기 종류별 payload
동등성으로 exact retry를 확인하고, 결과가 만료·삭제된 뒤 fingerprint가 없는
marker만 남으면 동일 ID 재시도를 fail-closed로 거부한다. 과거 marker에 남아
있던 interaction, 판단 scope, 상태 metadata는 maintenance가 transition으로
이관한 뒤 제거한다. source ID와 payload 내부 ID가 다르거나 UUID 형식이 잘못된
legacy 결과는 marker로 승격하지 않는다. 대신
`quality_flags.maintenance_quarantine`으로 격리한다. 만료 전에는 복구할 수
있지만 보존기간이 지나면 health payload는 삭제하고, canonical source UUID를
확인할 수 있는 경우 종류·UUID·`invalidated` 상태만 가진 비내용 marker로
대체한다. 깨졌거나 기존 transition과 모순되어 이관할 수 없는 legacy marker도
같은 방식으로 격리하고, 만료 전 복구에 성공하면 격리 표시와 semantic
metadata를 함께 제거한다.
`maintenance_quarantine`은 이 내부 복구 흐름만 쓰는 예약 quality flag이며,
일반 wellness ingest에서는 설정할 수 없다.

interaction review와 outcome은 별도의 영구 wellness 상태
`nutrition.interaction-transition.v1` revision을 공유한다. 같은 interaction에서
동시에 실행되어도 먼저 확정된 transition이 순서를 결정한다. review가 먼저면
outcome은 최신 review 영양값을 snapshot하고, outcome이 먼저면 review는
거부된다. outcome의 `consumed`, `not_consumed`, `cancelled` terminal 상태는
outcome 결과 이벤트의 보존기간과 분리되어 transition에서 계속 확인된다.
transition 도입 전 legacy review/outcome도 maintenance가 삭제 전에 별도
transition으로 이관한다.
최신 review/outcome 조회와 일일 영양 집계도 클라이언트 timestamp가 아니라 이
revision을 사용하므로, 요청 생성 시각과 실제 확정 순서가 뒤집혀도 과거 상태가
다시 최신으로 보이지 않는다. 최신 결과 payload가 만료되면 더 오래된 결과를
현재 상태로 되살리지 않고 fail-closed로 처리한다.

outcome transition에는 영양소나 음식 원문 대신
`interaction_observed_at`과 `outcome_consumed_at`만 최소 projection으로 남긴다.
그래서 retained outcome payload가 삭제된 뒤에도 어느 날짜의 완전성에 영향을
주는 terminal 상태인지는 판단할 수 있다. 다만 영양값을 복원하거나 추측하지는
않는다. 해당 날짜의 latest outcome 결과가 사라졌다면 일일 장부는
`unavailable_outcome_operation_ids`를 반환하고 불완전 상태가 된다. rejected
review나 terminal outcome의 결과 payload가 사라진 경우에도 이전 결과를
되살리거나 같은 interaction을 다시 여는 대신 읽기·재시도를 fail-closed로
거부한다.

파일 기반 SQLite는 process-wide `RLock`과 POSIX advisory sidecar-file lock으로
같은 DB를 쓰는 별도 engine·프로세스의 nutrition transaction을
commit/rollback까지 직렬화한다. PostgreSQL은 영구 interaction operation marker
행을 `FOR UPDATE`로 잠근다. review, outcome, `caffeine_sleep` request, 카페인
proposal 최종 검증이 같은 잠금 경계를 사용한다. proposal은 후보 상태와 오늘
장부를 하나의 최종 DB transaction에서 다시 확인한 뒤에만 반환한다.

자동 텍스트·음성 분석은 모델 호출 전에 operation lease를 예약한다. 같은 ID의
동시 요청은 모델을 다시 호출하지 않고 충돌로 종료한다. 단일 로컬 런타임용
SQLite는 엔진별 프로세스 lease를 사용해 SQLite connection 공유나 write lock이
호출자의 본 작업을 commit하지 않게 한다. PostgreSQL 같은 다중 연결 DB는 별도
트랜잭션과 token compare-and-swap lease를 사용한다. 두 방식 모두 호출자의 본
작업 세션을 commit/rollback하지 않는다. DB lease는 프로세스가 중단되면 provider
timeout보다 긴 만료시각 뒤 같은 입력이 재획득할 수 있다.

## 사진 분석 및 Sake와 결합

사진 입력은 `NutritionObservation`을 ID로 참조하고 이름, 제공량, 핵심
영양소와 추가 영양소를 검색용 `NormalizedIntakeItem`으로 투영한다. 원본
payload와 VLM provenance는 그대로 남는다. Sake 카페인 관찰은 이 일반 영양
관찰의 부분집합이며, 기존 전용 `caffeine` 필드와 확인 도구는 계속 동작한다.

사진 관찰은 별도 `NutritionReview`로 확인, 전체 수정, 거절할 수 있다. 원본 VLM
관찰은 덮어쓰지 않는다. 새 사진 interaction은 최신 검토본을 사용하며, 거절된
관찰에서는 만들 수 없다. 이미 생성된 interaction은 불변 snapshot이므로 이후
정정은 공통 `IntakeInteractionReview`로 기록한다. 이미 저장된 섭취 outcome의
영양값을 다시 고칠 때는 새 `IntakeOutcome.corrected_items`를 저장한다.
`NutritionReview`는 사진 관찰을 interaction으로 만들기 전의 사진 전용 검토이고,
`IntakeInteractionReview`는 사진·텍스트·음성 모두에 적용되는 공통 후보 검토다.
두 검토 이벤트는 각각 원 관찰 또는 interaction과 같은 보존정책 및 만료시각을
사용해 원 데이터 삭제 뒤 고아 데이터로 남지 않는다.

텍스트와 음성은 `POST /v1/intake-interactions/analyze` 또는 MCP
`analyze_intake_capture`로 자동 분석할 수 있다. 음성 bytes는 loopback
whisper.cpp server만 사용해 로컬에서 transcript로 바꾸고, transcript를 사진과
동일한 provider 선택(Ollama/OpenAI/Gemini/Anthropic/xAI)의 텍스트 분석 경로로
보낸다. 원격 provider는 호출마다 `allow_remote_analysis=true`가 있어야 한다.

자동 분석된 구조화 영양소는 `NutrientFact`에 다음 정보를 보존한다.

```text
nutrient
amount: exact | range | unknown
unit
origin: agent
confidence
evidence_text: 응답 검증 중 사용하며 durable 구조화 이벤트에는 원문을 복사하지 않음
```

`analysis_provenance`에는 영양 분석 provider/model/digest/prompt/schema와
분석시각을 저장한다. 음성은 transcription provider/model도 함께 기록한다.
숫자 근거 문구는 원문과 같은 `nutrition_raw_capture` 보존경계로 취급한다.
따라서 구조화 interaction에는 serving·nutrient evidence 문구를 복사하지 않고,
raw capture가 삭제된 뒤에도 이름·수치·단위·confidence·provenance만 남는다.
기존 `POST /v1/intake-interactions`와 MCP `capture_intake_interaction`은
디바이스나 다른 로컬 에이전트가 이미 구조화한 값을 저장하는 수동 adapter로
계속 지원한다.

자동 분석값의 `origin`은 `vlm` 또는 `agent`다. 사용자가 영양값을 확인하면 REST
`POST /v1/intake-interactions/{interaction_id}/review` 또는 MCP
`review_intake_interaction`이 별도 이벤트를 저장하고, 검토된 nutrient origin을
서버에서 `user`로 승격한다. `confirmed`는 원 interaction의 전체 항목을 확인하고,
`corrected`는 전체 구조화 항목을 교체하며, `rejected`는 후보 영양값을 비운다.
원문, transcript, evidence text, warning은 이 durable review에 복사하지 않는다.
한 번이라도 outcome이 저장된 interaction은 다시 review하지 않는다. 이미 섭취한
기록의 영양값을 고치려면 새 `IntakeOutcome.corrected_items`를 저장하고,
`not_consumed` 또는 `cancelled` 후보를 다시 검토하려면 새 interaction을 만든다.

직접 mg 입력도 두 종류를 구분한다.

```text
디바이스가 사용자/라벨의 구조화 exact mg를 전달
  -> origin=user|label
  -> 모델 재분석은 필요 없음
  -> 다만 미래 섭취 판단 전에는 interaction owner review로 값을 재확인

자유 형식 문장을 텍스트 모델이 exact mg로 추출
  -> origin=agent
  -> interaction owner review로 확인/수정해야 후보 판단 가능
```

## 사용자 질문별 경로

### “먹었다”

```text
photo | text | voice | structured input
        |
        v
IntakeInteraction(intent=log_consumed)
        |
        +-- VLM/agent 값이면 review
        |
        v
IntakeOutcome(status=consumed, consumed_at=...)
        |
        v
일일 완전성 확인
```

사진이나 문장을 저장했다는 이유만으로 오늘 섭취량에 들어가지 않는다.
`consumed` outcome이 있어야 한다.
반대로 `intent=log_consumed`인데 아직 outcome이 없는 interaction은 “없던 기록”으로
처리하지 않는다. 이 pending interaction이 하나라도 있으면 일일 완전성 확인과
“오늘 총량”은 fail-closed로 남는다.

### “이걸 먹거나 마셔도 될까?”

```text
photo | text | voice | direct exact value
        |
        v
IntakeInteraction(intent=ask_before_intake)
        |
        +-- 모든 판단 후보는 owner review
            (모델값 확인/수정, 직접값 재확인)
        |
        v
IntakeDecisionRequest(scope=...)
        |
        v
immutable candidate/history/evidence context
        |
        v
specialized wellness policy
```

이 후보는 섭취 장부에 들어가지 않는다. 사진 후보의 원
`NutritionObservation`도 interaction에 연결되는 순간 레거시 사진 섭취 장부에서
제외되고, 이후 `IntakeOutcome(status=consumed)`이 생겼을 때만 실제 섭취량으로
다시 들어간다.

카페인 질문은 specialized caffeine policy가 다음을 비교한다.

```text
오늘 확정 카페인 섭취량
+ 후보를 구성하는 모든 항목의 사용자/라벨 확인 exact mg
+ 수면·섭취시각·목표 취침·개인 한도·금기·제품 형태
```

후보에 음식이 여러 개면 각 항목마다 caffeine fact가 정확히 하나 있어야 한다.
카페인이 없는 항목도 `exact 0 mg`를 명시한다. 한 항목이라도 누락, range,
unknown, VLM/agent origin이면 전체 후보 판단을 중단한다.

후보 판단에는 캘린더 이벤트가 필수가 아니다.
`get_caffeine_proposal(event_id=null, intake_decision_request_id=...)`가 저장된
후보 섭취 예정 시각과 IANA timezone을 사용한다. request 생성 뒤 런타임
timezone이 바뀌면 과거 request를 재해석하지 않고 새 request 생성을 요구한다.
`caffeine_sleep` request는 `intended_consumption_at`을 반드시 포함해야 하며, 그
UTC offset은 interaction의 IANA timezone과 일치해야 한다. proposal 호출자가
나중에 다른 시각을 넣거나, 시각이 없는 과거 request를 보완하는 것은 허용하지
않는다. request 생성 시점보다 5분 이상 과거인 섭취 예정 시각도 거부한다.
proposal 실행은 외부 wearable 조회 전과 최종 저장소 잠금 안에서 같은 시각
조건을 다시 검사하므로, 처리 중 예정 시각이 지나버린 요청도 actionable
proposal로 반환하지 않는다.

request 생성 뒤 후보에 `consumed`, `not_consumed`, `cancelled` outcome이 생기면
과거 request snapshot 자체는 감사용으로 불변 보존하지만, 새 proposal 실행에는
재사용할 수 없다. 특히 이미 섭취한 후보를 현재 일일 장부에 더한 뒤 같은 후보를
한 번 더 더하는 이중 계산을 거부한다. outcome 결과 이벤트가 만료되거나 삭제된
뒤에도 terminal transition이 남으므로 과거 request가 다시 활성화되지 않는다.
request 생성 뒤 후보 review가 확인, 수정, 거절로 바뀐 경우에도 과거 snapshot은
실행할 수 없고 새 request를 만들어야 한다. proposal은 wearable 조회와 장부
계산이 끝난 직후 unified nutrition ledger 잠금을 먼저 잡고 primary와 모든
comparison interaction을 UUID canonical order로 잠근다. 그 transaction 안에서
terminal/review 상태, 후보 version, 오늘 장부, 섭취 예정 시각을 다시 확인한
뒤에만 반환한다. review와 outcome writer도 같은 ledger-first 잠금 순서를
사용하므로 SQLite와 PostgreSQL 모두에서 판단과 수정이 서로 엇갈리지 않는다.

### “오늘 카페인 얼마나 먹었어?”

```text
질문
  -> get_known_caffeine_intake_for_day
  -> confirmed outcomes + legacy confirmed photo records
  -> unresolved log_consumed 없음 검증
  -> operation_id가 있는 complete-day confirmation 검증
```

이 질문은 VLM으로 답하지 않는다. 사용자가 오늘 총량 숫자를 임의로 다시
전달하는 것도 허용하지 않는다. 장부가 불완전하면 숫자를 완전한 하루 총량처럼
말하지 않고 어떤 확인이 부족한지 반환한다.

## 검색과 컨텍스트 보존

`search_intake_records`는 날짜, intent, modality, 실제 섭취 확인 여부, nutrient,
원문·음식 이름으로 기록을 찾는다. 원본 사진·음성 bytes는 반환하지 않는다.
결과 제한에 걸리면 `truncated`와 검색 범위 metadata를 명시한다.

판단 요청은 후보, 비교 후보, 확인된 과거 섭취, 조회 기간, evidence event ID를
요청 생성 시점에 하나의 불변 snapshot으로 묶는다. 이후 섭취 기록이 바뀌어도
같은 request ID의 과거 판단 근거는 바뀌지 않는다. 판단 결과는 같은 evidence
ID와 limitations를 저장하므로 이후 에이전트가 무엇을 근거로 답했는지 다시
찾을 수 있다.

interaction review 뒤에는 새 decision request를 만들어야 한다. review 전에 만든
request는 당시의 미확인 후보 snapshot을 계속 유지한다.

```text
단기 capture event
  source_text / transcript / media_path
              |
              | 사용자 섭취 확인 또는 판단 요청
              v
영구 structured snapshot
  item / nutrient / amount / origin / confidence / outcome
  (source_text, transcript, media_path, free-form note,
   evidence_text 없음)
```

원문·transcript·미디어 참조는 별도 `nutrition_raw_capture` 이벤트에 저장되어
기본 14일 뒤 삭제된다. 같은 interaction의 구조화 영양소와 provenance는
`nutrition_observation` 정책으로 기본 90일 유지된다. 확인된 섭취와 판단 요청은
원문을 복사하지 않은 구조화 snapshot을 별도 저장하므로, 원문과 미디어가 삭제된
뒤에도 식사 사실과 영양소 근거를 재사용할 수 있다.

사진 VLM의 라벨 문자열, `evidence_text`, provider warning, 위치와 미디어 경로는
구조화 observation에 복사하지 않는다. 이 값들은 `nutrition_media` 정책을 따르는
별도 `nutrition.observation-raw.v1` 이벤트에 저장되고 기본 7일 동안만 조회 시
합쳐진다. 사용자가 섭취 결과에 남긴 자유 형식 note도 영구 confirmation에
복사하지 않고 `nutrition.outcome-raw.v1` 이벤트에서 기본 14일 동안만 보존한다.

과거 기록의 coverage는 `captured_records_only`다. 기록이 있다는 사실은 하루
전체 식사가 빠짐없이 기록됐다는 뜻이 아니다. 카페인 총량 판단은 기존 sake의
항목별 확인과 일일 완전성 확인을 계속 사용한다. 일반 엔진의
`caffeine_sleep` 경로는 이 완전성 상태를 context에 노출하지만 `proposal`이나
`noop`을 저장하지 않는다. `insufficient_data`와 `unsupported`도 서버가 고정한
비실행 summary/limitations만 저장하며 recommendation은 항상 제거한다. 실행
가능한 카페인 판단은 별도 검증된 카페인 정책 어댑터가 담당해야 한다.

outcome payload에는 `nutrient_provenance_verified=true|false`를 digest와 함께
저장한다. interaction review, 사진 nutrition review, 또는 outcome의 owner
correction으로 검증된 경우에만 `true`다. 이 표식이 없는 legacy outcome은
`origin=user|label`을 주장하더라도 카페인 합계에서 fail-closed로 제외된다.

## 안전 경계

- 사진·텍스트·음성을 자동으로 실제 섭취로 승격하지 않는다.
- VLM/agent 추정 영양소와 사용자 확인값의 provenance를 유지한다.
- prospective 사진 관찰을 레거시 섭취 장부나 일일 완전성에 섞지 않는다.
- `unknown`을 0으로 바꾸거나 range에서 임의의 한 값을 고르지 않는다.
- 알레르기와 약물 상호작용은 일반 웰니스 엔진에서 제안하지 않고
  `unsupported`로만 기록한다. 서버가 고정된 안전 문구와 limitation을 저장하고
  호출자가 보낸 recommendation은 폐기한다.
- UI는 이 엔진의 구현 범위가 아니다.
- 음성 전사는 외부 클라우드 STT가 아니라 운영자가 로컬에 실행한 whisper.cpp
  loopback sidecar만 허용한다.
- 사진 전체 영양소는 VLM 추정치이며 의료적 측정값이 아니다.
