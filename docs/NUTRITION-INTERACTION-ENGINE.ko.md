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
  +--> photo: 기존 sake observation ID 참조
  +--> text: 사용자 원문을 단기 capture에 보존
  +--> voice: 로컬 audio token + transcript를 단기 capture에 보존
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

## sake와 결합

사진 입력은 새 사진분석 스키마를 만들지 않는다. 기존 sake
`NutritionObservation`을 ID로 참조하고, 이름·제공량·카페인 값을 검색용
`NormalizedIntakeItem`으로 투영한다. 원본 payload와 VLM provenance는 그대로
남는다. 현재 sake 사진 스키마에서 투영 가능한 영양소는 카페인뿐이다.

텍스트나 음성 transcript에 구조화된 영양소가 함께 제공되면
`NutrientFact`에 다음 정보를 보존한다.

```text
nutrient
amount: exact | range | unknown
unit
origin: user | vlm | agent | label
confidence
evidence_text
```

HealthMes는 현재 텍스트에서 영양소를 자동 추출하거나 음성을 직접 전사하지
않는다. 디바이스 또는 로컬 에이전트가 만든 transcript와 구조화값을 검증하고
저장하는 엔진이다.

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

원문 interaction은 `nutrition_observation` 보존정책을 유지하며 섭취나 판단 때문에
영구 정책으로 승격되지 않는다. 확인된 섭취와 판단 요청은 원문을 복사하지 않은
구조화 snapshot을 별도 저장하므로, 원문과 미디어가 삭제된 뒤에도 식사 사실과
영양소 근거를 재사용할 수 있다.

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
- UI, 자동 텍스트 영양 분석, 서버 음성 전사, 전체 영양소 사진 추출은 이
  엔진의 구현 범위가 아니다.
