# HealthMes 영양 사진 관찰과 VLM Provider

기준일: 2026-08-06

## 기능 경계

```text
지원하는 것
  사진 1장 -> 음식/음료 후보 + 제공량 + 전체 핵심 영양소 추정
  자유 텍스트 -> 음식/음료 후보 + 제공량 + 전체 핵심 영양소 추정
  음성 파일 -> 로컬 whisper.cpp 전사 -> 텍스트 영양소 추정
  핵심 영양소 -> 열량, 단백질, 탄수화물, 지방, 식이섬유,
                 당류, 나트륨, 카페인
  유용한 추가 영양소 -> 동일한 일반 nutrient 계약으로 보존
  추정값 -> 사용자 확인/전체 수정/거절 -> 검토된 식사 맥락

지원하지 않는 것
  사진에 보이지 않는 재료·알레르기·정확한 레시피의 단정
  원격 음성 전사
```

`NutritionObservation` v2는 핵심 영양소를 모두 같은 `NutrientEstimate`
목록으로 저장한다. 모델이 판단할 수 없는 값은 반드시 `unknown`이고, 읽을 수
있는 라벨이 없는 수치는 근거가 있는 범위로만 저장한다. 카페인은 Sake의 기존
카페인 판단 흐름과 호환되도록 전용 `caffeine` 필드에도 같은 값을 유지한다.

사진은 `POST /v1/nutrition-observations/analyze`, 자유 텍스트와 음성은
`POST /v1/intake-interactions/analyze`를 사용한다. 음성 파일은 먼저
`POST /v1/media`로 저장하며 HealthMes는 등록된 audio object만 로컬
whisper.cpp에 전달한다. 구형 단일 음식 기록 계약으로 평탄화하지 않고,
모든 신규 쓰기는 관찰·검토·interaction·outcome을 분리한다.

사진 분석 후에는 반드시 사용자가 `confirmed`, 전체 `corrected`, 또는
`rejected`로 검토한다. 검토된 사진만 `IntakeInteraction`으로 만들 수 있으며,
텍스트·음성 분석 결과도 먼저 사용자에게 구조화 항목을 보여준다.
`intent=log_consumed`는 기록 목적일 뿐이다. 실제 섭취는 사용자가 별도로
승인해 `IntakeOutcome(status=consumed)`이 생성된 뒤에만 확정된다.

상위 `IntakeInteraction` 엔진은 텍스트 원문을 자동 분석하고, 음성은 로컬
전사 후 같은 분석 경로를 사용한다. 섭취 확인과 판단 요청에는
원문·transcript·미디어 경로를 제외한 구조화 snapshot과 분석 provenance만
장기 보존한다. 상세 계약은
[`NUTRITION-INTERACTION-ENGINE.ko.md`](NUTRITION-INTERACTION-ENGINE.ko.md)를
따른다.

텍스트 원문, 음성 transcript, 미디어 참조는 `nutrition_raw_capture`로 기본
14일 보존하고, 구조화 interaction은 `nutrition_observation`으로 기본 90일
보존한다. 두 기간은 저장 설정에서 독립적으로 바꿀 수 있다.

사진 분석에서 읽은 라벨 문자열, 근거 문구, warning, 위치와 미디어 경로는
`nutrition.observation-raw.v1`으로 분리하며 `nutrition_media`와 같은 기본
7일 보존기간을 적용한다. 90일 구조화 observation에는 이름·수치·단위·confidence와
분석 provenance만 남는다.

## 실행 구조

```text
사용자 사진
    |
    v
POST /v1/media
    |
    +--> StorageObject: nutrition_media (기본 7일)
    |
    v
POST /v1/nutrition-observations/analyze
    |
    +--> ollama (기본, 로컬)
    |
    +--> openai / gemini / anthropic / xai
         조건: API 키 + allow_remote_vision=true + HTTPS
    |
    v
공통 VLMExtraction JSON Schema 재검증
    |
    v
NutritionObservation 원형 보존
    |
    +--> WellnessEvent.payload
    +--> StorageObject: nutrition_observation (기본 90일)
    |
    v
사용자 전체 영양 검토
  confirmed | corrected | rejected
    |
    +--> nutrition.review.v1
    +--> nutrition_observation과 같은 보존정책/만료시각
    +--> corrected는 모든 항목과 핵심 영양소의 완전한 교체본
    |
    v
IntakeInteraction 생성 시 최신 검토본 적용
    |
    +--> 실제 섭취 여부는 별도 IntakeOutcome
    |
    v
카페인 전용 항목 확인 + 하루 전체성 확인
    |
    +--> StorageObject: nutrition_confirmation (기본 무기한)
    |
    v
MCP가 확인된 카페인 데이터만 의사결정 기능에 제공
```

사진 원본, 구조화 관찰, 사용자 확인을 분리했기 때문에 사용자는 원본 사진을
짧게 보존하면서 작은 구조화 데이터와 중요한 확인 기록은 더 오래 유지할 수
있다.

## Provider와 기본 모델

| Provider | 2026-08-06 기본 모델 | API 방식 |
|---|---|---|
| Ollama | `qwen3-vl:4b-instruct` | 로컬 `/api/chat` |
| OpenAI | `gpt-5.6-sol` | Responses API + Structured Outputs |
| Google | `gemini-3.6-flash` | Gemini `generateContent` + JSON Schema |
| Anthropic | `claude-fable-5` | Messages API + Structured Outputs |
| xAI | `grok-4.5` | Chat Completions + Structured Outputs |

모델명은 설정으로 덮어쓸 수 있다. 기본값은 조사 기준일의 최신 공식 모델을
기록한 것이며 provider가 모델을 종료하거나 새 모델을 출시하면 운영자가
명시적으로 바꾸고 회귀 테스트를 수행해야 한다.

공식 조사 근거:

- OpenAI: `https://developers.openai.com/api/docs/guides/images-vision`
- OpenAI Structured Outputs:
  `https://developers.openai.com/api/docs/guides/structured-outputs`
- Gemini models: `https://ai.google.dev/gemini-api/docs/models`
- Gemini image understanding:
  `https://ai.google.dev/gemini-api/docs/image-understanding`
- Gemini structured output:
  `https://ai.google.dev/gemini-api/docs/structured-output`
- Anthropic models:
  `https://platform.claude.com/docs/en/about-claude/models/overview`
- Anthropic vision:
  `https://platform.claude.com/docs/en/build-with-claude/vision`
- Anthropic structured outputs:
  `https://platform.claude.com/docs/en/build-with-claude/structured-outputs`
- xAI image understanding:
  `https://docs.x.ai/developers/model-capabilities/images/understanding`
- xAI structured outputs:
  `https://docs.x.ai/developers/model-capabilities/text/structured-outputs`

## 개인정보와 실패 정책

- 기본 provider는 로컬 Ollama다.
- 원격 provider로 자동 fallback하지 않는다.
- 원격 호출은 API 키만으로 활성화되지 않는다. 각 요청이
  `allow_remote_vision=true`를 보내야 한다.
- 원격 endpoint는 HTTPS만 허용한다.
- API 키와 이미지 bytes는 오류 메시지에 포함하지 않는다.
- 원격 전송본은 Pillow로 새 JPEG/PNG로 재인코딩해 EXIF/GPS/기기
  메타데이터를 제거하고 최대 4096px로 제한한다. 로컬 원본은 바꾸지 않는다.
- OpenAI와 xAI 요청은 `store: false`를 보낸다.
- 모든 provider 출력은 동일한 Pydantic `VLMExtraction` 스키마로 재검증한다.
- 원격 입력은 JPEG, PNG, WebP, GIF, HEIC/HEIF를 받고 메타데이터가 제거된
  JPEG/PNG로 재인코딩해 전송한다.
- provider 응답이 실제 model 또는 fingerprint를 제공하면 저장 provenance에
  설정값 대신 그 값을 기록한다.

## Sake 이슈 #96-#101과 확장 관계

| Issue | 제안과 구현 범위 |
|---|---|
| #96 | 기존 multipart 사진 업로드를 유지하고 분석 요청에는 media path, 촬영 시각, timezone, source, 위치 provenance, 원격 전송 동의를 넣는다. |
| #97 | 실제 개인정보 사진 대신 privacy-safe synthetic fixture로 계약과 E2E를 검증한다. |
| #98 | Qwen3-VL 기반 로컬 카페인 관찰을 최초 부분집합으로 정의했다. HealthMes v2는 이 계약을 깨지 않고 일반 영양소 목록을 추가했다. |
| #99 | VLM 관찰은 사실이 아니므로 repository와 MCP에서 미확인 관찰과 확인된 근거의 경계를 지킨다. |
| #100 | 사용자가 사진별 카페인 값과 하루 기록의 완전성을 각각 확인한 뒤에만 PR #95의 카페인 제안 로직이 사용할 수 있게 한다. |
| #101 | CLI/Telegram 실제 사용 흐름으로 업로드부터 확인까지 점검한다. 자동 테스트는 synthetic E2E를 다루며 실제 계정·실모델 dogfood는 별도 운영 검증이다. |

## 운영 검증

실제 provider 자격 증명은 저장소에 포함하지 않는다. 배포 환경에서 명시적
opt-in integration test를 실행해 계정별 모델 접근 권한과 quota를 확인한다.
