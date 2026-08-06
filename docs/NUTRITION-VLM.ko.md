# Sake 영양 사진 관찰과 VLM Provider

기준일: 2026-08-06

## 기능 경계

```text
지원하는 것
  사진 1장 -> 음식/음료 후보 + 제공량 추정 + 카페인 추정
  추정값 -> 사용자 확인 -> 확인된 카페인 근거

지원하지 않는 것
  전체 영양소(열량, 탄수화물, 단백질, 지방, 미량영양소)
  재료와 레시피 추론
  텍스트 영양 기록 분석
  음성 영양 기록 전사/분석
```

`NutritionObservation`은 일반적인 음식/음료 후보와 제공량을 담을 수 있지만,
현재 구현된 영양소 필드는 `caffeine` 하나뿐이다. 따라서 이 기능을 "전체 영양
분석"이라고 부르면 안 된다.

음성 파일은 `POST /v1/media`로 저장할 수 있지만
`POST /v1/nutrition-observations/analyze`는 이미지 형식만 받는다. 기존
`FoodLog`도 별도 계약이며, sake 관찰을 `FoodLog`로 평탄화하지 않는다.

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
사용자 카페인 항목 확인 + 하루 전체성 확인
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
- OpenAI와 xAI 요청은 `store: false`를 보낸다.
- 모든 provider 출력은 동일한 Pydantic `VLMExtraction` 스키마로 재검증한다.
- 원격 provider는 JPEG, PNG, WebP, GIF만 전송한다. HEIC는 현재 로컬
  Ollama 경로를 사용하거나 업로드 전에 변환해야 한다.

## Sake 이슈 #96-#101 기록

| Issue | 제안과 구현 범위 |
|---|---|
| #96 | 기존 multipart 사진 업로드를 유지하고 분석 요청에는 media path, 촬영 시각, timezone, source, 위치 provenance, 원격 전송 동의를 넣는다. |
| #97 | 실제 개인정보 사진 대신 privacy-safe synthetic fixture로 계약과 E2E를 검증한다. |
| #98 | Qwen3-VL 기반 로컬 추출을 만들되 범위를 카페인 중심 음식/음료 관찰로 제한한다. 열량·매크로·미량영양소·재료·레시피는 제외한다. |
| #99 | VLM 관찰은 사실이 아니므로 repository와 MCP에서 미확인 관찰과 확인된 근거의 경계를 지킨다. |
| #100 | 사용자가 사진별 카페인 값과 하루 기록의 완전성을 각각 확인한 뒤에만 PR #95의 카페인 제안 로직이 사용할 수 있게 한다. |
| #101 | CLI/Telegram 실제 사용 흐름으로 업로드부터 확인까지 점검한다. 자동 테스트는 synthetic E2E를 다루며 실제 계정·실모델 dogfood는 별도 운영 검증이다. |

## 후속 작업

1. 텍스트와 음성을 공통 `CaptureObservation`으로 정규화하는 계약을 설계한다.
2. 음성은 로컬 전사 후 원문, 전사문, 사용자의 수정본 provenance를 분리한다.
3. 전체 영양소는 sake가 새 버전의 schema와 확인 규칙을 정의한 뒤 추가한다.
4. 실제 provider 자격 증명으로 opt-in integration test를 별도 실행한다.
5. HEIC의 로컬 변환 경로를 추가한 뒤 원격 provider에도 동일 이미지를 보낸다.
