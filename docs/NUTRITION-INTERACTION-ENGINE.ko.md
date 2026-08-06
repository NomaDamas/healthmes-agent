# HealthMes 식사 입력·의사결정 엔진

기준일: 2026-08-06

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

모든 쓰기 호출은 호출자가 만든 UUID `operation_id`를 요구한다. 같은
`operation_id`와 같은 입력을 다시 보내면 기존 결과를 반환하고, 같은 ID에 다른
입력을 보내면 충돌로 거부한다. MCP owner 쓰기의 trusted proof에도 이 ID가
포함된다. raw capture가 삭제된 뒤에도 UUID와 요청 hash만 담은 비민감
`nutrition.operation.v1` tombstone은 남아 과거 UUID가 다른 식사에 재사용되는
것을 막는다. 이미 만료된 raw capture의 동일 재시도는 새 식사를 만들지 않고
명시적 충돌을 반환한다.

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
정정은 `IntakeOutcome.corrected_items`로 기록한다. 검토 이벤트는 원 관찰과 같은
보존정책 및 만료시각을 사용해 원 관찰 삭제 뒤 고아 데이터로 남지 않는다.

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

## 검색과 컨텍스트 보존

`search_intake_records`는 날짜, intent, modality, 실제 섭취 확인 여부, nutrient,
원문·음식 이름으로 기록을 찾는다. 원본 사진·음성 bytes는 반환하지 않는다.
결과 제한에 걸리면 `truncated`와 검색 범위 metadata를 명시한다.

판단 요청은 후보, 비교 후보, 확인된 과거 섭취, 조회 기간, evidence event ID를
요청 생성 시점에 하나의 불변 snapshot으로 묶는다. 이후 섭취 기록이 바뀌어도
같은 request ID의 과거 판단 근거는 바뀌지 않는다. 판단 결과는 같은 evidence
ID와 limitations를 저장하므로 이후 에이전트가 무엇을 근거로 답했는지 다시
찾을 수 있다.

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

## 안전 경계

- 사진·텍스트·음성을 자동으로 실제 섭취로 승격하지 않는다.
- VLM/agent 추정 영양소와 사용자 확인값의 provenance를 유지한다.
- `unknown`을 0으로 바꾸거나 range에서 임의의 한 값을 고르지 않는다.
- 알레르기와 약물 상호작용은 일반 웰니스 엔진에서 제안하지 않고
  `unsupported`로만 기록한다. 서버가 고정된 안전 문구와 limitation을 저장하고
  호출자가 보낸 recommendation은 폐기한다.
- UI는 이 엔진의 구현 범위가 아니다.
- 음성 전사는 외부 클라우드 STT가 아니라 운영자가 로컬에 실행한 whisper.cpp
  loopback sidecar만 허용한다.
- 사진 전체 영양소는 VLM 추정치이며 의료적 측정값이 아니다.
