# HealthMes Decision Agent 아키텍처 결정

> **결정일:** 2026-08-10
>
> **상태:** 승인된 목표 아키텍처. 현재 코드의 고정 `question_kind` resolver는
> 호환용 구현이며 이 문서의 구조로 마이그레이션한다.
>
> **범위:** 엔진, 데이터 조회, LLM 판단, Hermes adaptation과 의사결정 기록.
> 실제 iOS, Android, 데스크톱 UI는 포함하지 않는다.

## TLDR

HealthMes의 두뇌는 Skill 문서도, Hermes 자체도, 고정 질문 표도 아니다.

```text
사용자 질문
    |
    v
HealthMes Decision Agent
  LLM이 질문을 이해하고 필요한 도구를 선택한다.
    |
    v
Context Access Layer
  권한, 보존기간, 시간대, 개인정보와 조회 한도를 검사한다.
    |
    v
Activity / Nutrition / Wearable / Calendar provider
  정확한 수치와 전문 context를 계산하거나 조회한다.
    |
    v
HealthMes Decision Agent
  여러 영역을 종합하고 설명한다.
    |
    v
Decision Finalizer
  사용한 source_refs를 검증하고 DecisionRecord를 저장한다.
```

Hermes는 위 흐름을 실행하는 첫 번째 runtime adapter다. HealthMes는 판단 계약과
데이터 경계를 소유하고, Hermes는 모델 호출, tool-calling loop, 세션과 전달 채널을
제공한다.

## 1. 왜 바꾸는가

현재 구현에는 다음 문제가 있다.

1. 호출자가 `activity_summary`, `focus`, `overwork`, `recovery`,
   `caffeine_for_focus` 중 하나를 먼저 골라야 한다.
2. 선택된 `question_kind`가 조회할 영역을 고정한다.
3. resolver가 "무엇을 볼지 선택"과 "안전하게 자료를 가져오기"를 동시에 맡는다.
4. 최종 자연어 판단을 실행하는 HealthMes-owned LLM 계층이 없다.
5. 핵심 절차 일부가 Skill 문서에만 있어서 Skill이 로드되지 않으면 보장이 약해진다.
6. `evidence`가 의료적 증거처럼 들리지만 실제로는 데이터 행의 추적 ID다.
7. 원본 데이터는 질문과 권한에 따라 필요할 수 있는데 전면 금지로 표현돼 있다.
8. `record_decision` 도구는 있지만 최종 판단 뒤 반드시 저장되도록 강제되지 않는다.

고정 질문 표는 데모와 회귀 테스트에는 유용하지만 HealthMes의 목표인 모호한 복합
질문을 처리하기에는 부족하다.

```text
"왜 오늘 집중이 안 되지?"

고정 표
  focus -> activity + wearable + calendar

필요한 실제 동작
  LLM이 activity부터 확인
  -> 수면 신호가 필요하면 wearable 조회
  -> 회의 영향이 보이면 calendar 조회
  -> 늦은 카페인이 언급되면 nutrition 조회
  -> 자료가 부족하면 사용자에게 질문
```

## 2. 목표 아키텍처

```text
┌──────────── HealthMes Decision Agent ────────────┐
│ 질문 해석 · 도구 선택 · 반복 조회 · 종합 · 설명   │
│ HealthMes-owned system policy와 결과 계약         │
└─────────────────────┬─────────────────────────────┘
                      │ ContextToolGateway
                      ▼
┌────── Context Access Layer / Source Gateway ─────┐
│ auth · retention · timezone · privacy · query cap │
│ 허용된 context와 source_refs만 반환               │
└────────┬──────────┬──────────┬──────────┬─────────┘
         ▼          ▼          ▼          ▼
     Activity   Nutrition   Wearable   Calendar
     provider   provider    provider   provider
         └──── HealthMes 통합 저장·인덱스 ────┘

Runtime implementation

HealthMesDecisionAgent
        |
        +-- HermesRuntimeAdapter
        +-- FutureNativeRuntimeAdapter
        +-- TestRuntimeAdapter
```

중요한 분리는 다음과 같다.

```text
LLM
  무엇을 알아봐야 하는지 결정한다.

Context Access Layer
  요청한 자료 중 무엇을 실제로 제공할 수 있는지 강제한다.

Domain provider
  정확한 값과 전문 파생값을 계산한다.

Decision Finalizer
  실제 사용한 자료와 최종 답변을 검증하고 저장한다.
```

## 3. 각 부품의 책임

### 3.1 전문 도메인 provider

기존의 "전문 도메인 엔진"은 모든 영역이 동일한 형태의 계산 엔진이라는 오해를
줄 수 있으므로 `Domain Context Provider`를 상위 이름으로 사용한다.

| Provider | 책임 | 하지 않는 일 |
|---|---|---|
| Activity | 사용시간, active/idle, 연속 활동, 분절, 야간 사용, baseline 계산 | 최종 휴식 명령 또는 카페인 판단 |
| Nutrition | 섭취 관찰, 사용자 확인, 영양소, 카페인 ledger와 후보 식품 context | 수면이나 집중 상태 추측 |
| Wearable | Open Wearables 수면, HRV, 스트레스, 회복 context 조회와 정규화 | 다른 영역을 대신한 최종 판단 |
| Calendar | 일정 시간, busy 구간, 회의 밀도와 가용 시간 계산 | 일정의 의미나 건강 원인 확정 |

숫자 합산, 단위 변환, 시간대 경계, 중복 제거와 누락 데이터 처리는 LLM에게 맡기지
않는다. 재현 가능해야 하는 계산은 provider와 전문 정책이 담당한다.

### 3.2 Context Access Layer

기존 `Context Broker` 또는 cross-domain resolver를 두 역할로 나눈다.

```text
의미 선택
  LLM Decision Agent가 담당

데이터 접근과 검증
  Context Access Layer가 담당
```

Context Access Layer는 다음만 강제한다.

- 요청한 데이터 영역과 기간에 대한 사용자 권한
- 데이터별 retention과 삭제 상태
- 사용자 local timezone과 조회 시간 범위
- 개인정보 공개 단계와 민감도
- 한 번에 읽을 수 있는 기간, 행 수와 원본 크기
- 중복, stale data, coverage와 freshness
- 실제 반환한 record와 summary의 `source_refs`

성능이 높은 LLM도 접근 권한이나 삭제된 데이터의 재노출을 보장하는 보안 경계가 될
수 없다. 따라서 LLM의 성능과 Context Access Layer의 필요성은 대체 관계가 아니다.

### 3.3 HealthMes Decision Agent

이 계층이 HealthMes가 소유하는 실제 제품 두뇌다.

- 자연어 질문과 사용자 의도를 해석한다.
- 사용 가능한 context tool catalog를 본다.
- 필요한 provider, 기간, granularity와 필드를 선택한다.
- 첫 조회 결과를 보고 추가 도구를 반복 호출할 수 있다.
- 데이터가 부족하면 필요한 사실을 사용자에게 묻는다.
- 전문 정책 결과를 재계산하지 않고 여러 영역의 trade-off를 종합한다.
- 관찰, 불확실성, 대안과 최종 설명을 만든다.

질문을 미리 다섯 종류로 제한하지 않는다. 필요한 도구는 질문과 첫 조회 결과에 따라
달라질 수 있다.

### 3.4 Decision Finalizer

최종 기록을 LLM의 기억이나 Skill 지침에만 맡기지 않는다.

- 최종 답변이 인용한 `source_refs`가 실제 tool 결과에 있었는지 검사한다.
- 존재하지 않는 ID, 만료된 자료와 허용되지 않은 원본 참조를 거부한다.
- 모델, tool trace, limitation, 질문 시각과 답변을 `DecisionRecord`로 저장한다.
- 저장이 필요한 판단인데 기록에 실패하면 성공한 결정으로 표시하지 않는다.

## 4. source_refs의 뜻

`evidence`는 "이 답변이 의학적으로 증명됐다"는 뜻이 아니다. 답변에 사용한 데이터가
어디에서 왔는지 다시 찾기 위한 provenance다. 새 계약에서는 `source_refs`를
기본 이름으로 사용한다.

```json
{
  "domain": "activity",
  "record_id": "wellness-event-uuid",
  "source_provider": "activitywatch",
  "observed_start": "2026-08-10T09:00:00+09:00",
  "observed_end": "2026-08-10T10:00:00+09:00",
  "schema_version": 1,
  "derived_by": "activity.hour-summary.v1",
  "freshness": "current",
  "coverage": 0.87
}
```

모든 필드를 LLM에 길게 넣을 필요는 없다. 모델에는 필요한 최소 참조를 주고,
DecisionRecord에는 감사 가능한 전체 provenance를 보존할 수 있다.

기존 `evidence_ids`와 최상위 `evidence`는 호환 기간 동안 유지하되 내부적으로
`source_refs`로 정규화한다.

## 5. 원본 데이터 정책

원본을 절대 보내지 않는 것도, 모든 원본을 자동으로 보내는 것도 잘못이다.

### Level 1: 기본 집계

일반적인 집중, 과로, 회복 질문의 기본값이다.

- 활동 시간과 category
- 수면, HRV와 회복 summary
- 일정 busy minutes
- 확인된 영양소와 섭취량

앱 이름, 창 제목, URL, 사진과 음성 bytes는 포함하지 않는다.

### Level 2: 제한된 identity

질문에 identity가 필요하고 사용자가 허용한 경우에만 사용한다.

- "어떤 앱 때문에 집중이 끊겼어?"의 앱 이름
- "어떤 일정 뒤에 피곤했어?"의 허용된 일정 제목
- 사용자가 직접 고른 식품 또는 기록 이름

### Level 3: scoped raw

원본 분석 자체가 질문의 목적일 때만 별도 호출로 사용한다.

- 음식 사진을 Nutrition VLM에 전달
- 음성을 로컬 transcription provider에 전달
- 사용자가 명시적으로 요청한 민감 원본 분석

원본은 해당 분석 provider에만 전달하고, 이후 일반 의사결정 turn에는 구조화 결과와
`source_refs`를 사용한다. 창 제목, URL, 화면 pixel과 raw wearable timeseries는
명시적 권한, 목적과 제한된 보존 정책 없이는 Level 3으로 승격하지 않는다.

## 6. Skill의 위치

Skill은 HealthMes의 두뇌나 데이터 엔진이 아니다. 특정 runtime이 HealthMes 계약을
잘 호출하도록 돕는 설명과 workflow adapter다.

```text
잘못된 구조
  Skill 문서가 권한, 안전 규칙, 자료 선택, 최종 기록을 모두 소유

목표 구조
  HealthMes 코드와 계약이 강제할 것을 강제
  Skill은 도구 이름, 표현 방식과 runtime 사용법만 설명
```

HealthMes의 필수 system policy는 Skill을 사용자가 우연히 열어야만 적용되는 방식이
아니라 Decision Agent를 시작할 때 항상 주입한다.

Skill이 담당할 수 있는 내용:

- Hermes에서 HealthMes Decision Agent를 어떻게 호출하는가
- 사용자에게 관찰, 근거, 제안과 한계를 어떻게 보여주는가
- 특정 채널에서 확인 질문을 어떻게 표현하는가

Skill에만 두면 안 되는 내용:

- retention과 권한 검사
- 섭취량 합계와 시간대 계산
- 카페인 전문 안전 경계
- source reference 검증
- DecisionRecord 저장 의무

## 7. Hermes의 위치

Hermes는 범용 Agent Runtime이며 HealthMes와 동등한 제품 계층이 아니다.

Hermes가 제공하는 기능:

- LLM provider와 model 실행
- 대화와 tool-calling 반복 loop
- MCP 도구 발견과 실행
- 세션, gateway, cron과 전달 채널
- Skill 문서 로딩

HealthMes가 소유해야 하는 기능:

- `HealthMesDecisionAgent` 요청과 결과 계약
- HealthMes system policy
- context tool catalog와 privacy scope
- source reference와 finalization
- DecisionRecord와 outcome 연결

MVP에서는 `HermesRuntimeAdapter`가 Hermes의 기존 loop를 사용한다. 새 LLM runtime을
처음부터 만들지 않는다. Hermes에 필수 generic hook이 없다면 HealthMes vendored
tree를 직접 수정하지 않고 별도 Hermes 저장소와 PR에서 다음과 같은 범용 확장만
제안한다.

- 필수 system policy 주입 hook
- turn 완료 후 finalizer callback
- tool trace export
- tool allowlist와 context scope 전달

HealthMes 전용 카페인, 활동 또는 영양 규칙을 Hermes core에 넣지 않는다.

## 8. MCP의 위치

MCP는 판단기가 아니라 runtime과 HealthMes 도구 사이의 통신 규격이다.

```text
LLM
  "활동과 수면 자료가 필요하다"
       |
       v
Hermes tool loop
       |
       v
MCP HealthMes tools
       |
       v
Context Access Layer와 Domain Provider
```

에이전트의 자율성과 MCP는 충돌하지 않는다. LLM이 어떤 도구를 언제 호출할지
자율적으로 선택하고, MCP는 선택한 도구를 안전하게 실행한다.

아키텍처는 MCP에만 고정하지 않는다.

```text
ContextToolGateway
  +-- MCPToolGateway       Hermes용
  +-- InProcessToolGateway 미래 native runtime용
  +-- FakeToolGateway      테스트용
```

MVP는 기존 Hermes 연동을 위해 MCP 구현체만 제공해도 된다.

## 9. 중앙 데이터 조회

에이전트가 중앙 데이터베이스에 자유 SQL을 실행하는 구조는 채택하지 않는다.
대신 하나의 논리적 tool catalog를 통해 모든 웰니스 영역을 탐색한다.

```text
통합 접근
  list_context_capabilities
  search_wellness_context
  get_activity_context
  get_nutrition_context
  get_wearable_context
  get_calendar_context
  specialized policy tools
```

물리적으로 모든 데이터를 한 테이블에 억지로 넣을 필요는 없다.

- Activity와 Nutrition은 공통 `WellnessEvent` envelope를 사용한다.
- Calendar는 외부 일정의 소유권을 유지하는 local mirror를 사용한다.
- Open Wearables raw 저장은 vendor 호환을 위해 분리할 수 있다.
- HealthMes 판단에 사용한 normalized wearable summary와 provenance는 로컬
  source reference 또는 mirror로 남긴다.

즉 "한곳"은 하나의 거대한 테이블이 아니라 하나의 권한, 조회, provenance와
의사결정 기록 체계를 뜻한다.

## 10. 결정론과 LLM의 경계

| 결정론적으로 강제 | LLM이 판단 |
|---|---|
| 권한, consent와 retention | 질문의 목적 |
| 시간대, 기간과 단위 계산 | 필요한 영역과 도구 |
| 중복 제거와 정확한 합계 | 추가 조회 필요 여부 |
| freshness, coverage와 missing data | 여러 영역의 trade-off |
| 전문 정책의 숫자와 hard boundary | 사용자에게 설명할 대안 |
| source reference 검증과 저장 | 자연어 답변 |

질문 종류에 따라 조회 영역을 고정하는 것은 폐기하지만, 계산과 보안까지 LLM에게
넘기지는 않는다.

## 11. 현재와 목표

| 항목 | 현재 코드 | 목표 |
|---|---|---|
| 질문 입력 | 호출자가 `question_kind` 선택 | 자연어 `DecisionRequest` |
| 자료 선택 | 고정 `DOMAIN_SELECTION` | LLM의 반복 tool planning |
| resolver | 선택과 조립을 함께 수행 | 호환 wrapper로 격하 |
| Context layer | 일부 freshness/coverage 조립 | 권한과 privacy를 강제하는 Source Gateway |
| Skill | 일부 제품 workflow와 필수 절차 포함 | 얇은 runtime adapter |
| Hermes | MCP 연결 가능, HealthMes 전용 adapter 없음 | `HermesRuntimeAdapter` |
| 최종 판단 | 구현되지 않음 | LLM 종합 판단 |
| 판단 저장 | LLM이 `record_decision`을 기억해야 함 | finalizer가 자동 저장 |
| wearable provenance | 안정적인 evidence ID 부족 | normalized source reference 또는 mirror |

## 12. 마이그레이션

현재 API와 테스트를 한 번에 깨지 않는다.

```text
현재
  resolve_wellness_context(question_kind, ...)

과도기
  question_kind를 generic ContextQuery preset으로 변환
  기존 응답의 evidence_ids 유지
  새 내부 응답에는 source_refs 추가

목표
  ask_wellness(DecisionRequest)
  -> LLM tool planning
  -> Context Access Layer
  -> Decision Finalizer
```

기존 `get_activity_summary`, `get_focus_context`, `get_overwork_context`와 전문
Nutrition, Wearable, Calendar 도구는 폐기하지 않는다. 새 Decision Agent가 선택할
수 있는 typed tools로 재사용한다.

## 13. 구현 계획

### `DEC-01 Decision contracts`

- `DecisionRequest`, `ContextQuery`, `ContextResult`, `SourceRef`,
  `DecisionResult` 정의
- `question_kind`는 compatibility preset으로 명시
- runtime이나 MCP 이름에 종속되지 않는 계약 작성

**종료 조건:** 자연어 질문과 tool query가 고정 질문 enum 없이 표현된다.

### `DEC-02 Context Provider Registry`

- Activity, Nutrition, Wearable, Calendar provider를 동일 registry에 등록
- provider capability, 지원 기간, granularity와 sensitivity 선언
- broad discovery와 전문 정책 도구를 함께 노출

**종료 조건:** 새 입력 영역을 resolver의 `if/elif` 수정 없이 등록할 수 있다.

### `DEC-03 Context Access Layer`

- authorization, consent, retention과 timezone 검사
- privacy Level 1, 2, 3 강제
- query cap, freshness, coverage와 `source_refs` 정규화

**종료 조건:** 모델이 금지된 원본이나 만료 데이터를 요청해도 반환되지 않는다.

### `DEC-04 HealthMes Decision Agent`

- 자연어 질문을 받는 HealthMes-owned orchestration interface
- LLM이 tool catalog에서 도구를 선택하고 반복 호출
- 부족한 자료에 대한 추가 질문
- 전문 정책 경계를 유지한 최종 종합

**종료 조건:** 같은 문장이라도 실제 context에 따라 호출 도구가 달라질 수 있다.

### `DEC-05 Hermes Runtime Adapter`

- HealthMes system policy를 항상 주입
- HealthMes MCP toolset만 필요한 범위로 노출
- Hermes tool trace를 표준 결과로 변환
- Skill은 얇은 channel/runtime 설명으로 축소

**종료 조건:** HealthMes core가 Hermes 내부 클래스나 tool prefix에 직접 의존하지 않는다.

### `DEC-06 Decision Finalizer`

- tool result에서 반환된 `source_refs` allowlist 생성
- 최종 답변의 참조와 limitation 검증
- `DecisionRecord` 자동 저장
- 저장 실패와 불완전 판단을 명시적 상태로 반환

**종료 조건:** 행동 제안이 포함된 모든 성공 응답에 검증된 DecisionRecord가 있다.

### `DEC-07 Data completeness`

- Open Wearables normalized summary의 안정적 source reference 또는 local mirror
- ActivityWatch 자동 주기 import
- iOS capability 범위 안의 실제 activity 제출 경로
- 여러 기기의 겹친 활동시간 처리 정책

**종료 조건:** 선택 가능한 각 provider가 freshness, coverage와 provenance를 반환한다.

### `DEC-08 End-to-end verification`

- 모호한 질문에서 LLM tool selection 검증
- 첫 결과에 따라 추가 영역을 조회하는 multi-turn tool test
- privacy Level별 허용과 거부
- 누락 데이터를 0으로 바꾸지 않는 테스트
- source reference 위조 거부
- 최종 DecisionRecord 저장 테스트

**종료 조건:** 다음 전체 흐름이 자동 테스트로 증명된다.

```text
자연어 질문
  -> LLM 자율 도구 선택
  -> 권한과 privacy가 적용된 context
  -> 여러 영역 종합
  -> 검증된 source_refs
  -> DecisionRecord 저장
```

## 14. 채택하지 않는 대안

### 고성능 LLM에 DB 직접 개방

권한, retention, SQL 안정성, 개인정보와 재현성을 모델 성능에 의존하므로 기각한다.

### HealthMes 핵심을 하나의 Skill에 구현

Skill 미로드, prompt drift와 runtime 교체 시 핵심 보장이 사라지므로 기각한다.

### Hermes core에 HealthMes 규칙 직접 삽입

업스트림 동기화와 제품 소유권이 꼬이므로 기각한다. 필요한 generic hook만 별도
Hermes PR로 제안한다.

### 새 LLM runtime 전체 재구현

Hermes가 이미 provider, tool loop, session과 channel을 제공하므로 MVP에서는
중복 구현이다. HealthMes-owned interface와 adapter만 만든다.

## 15. 완료 정의

이 개선은 문서나 Skill 추가만으로 완료되지 않는다.

1. 고정 `question_kind` 없이 자연어 질문을 받을 수 있다.
2. LLM이 상황에 따라 서로 다른 도구를 선택하고 추가 조회할 수 있다.
3. Context Access Layer가 권한, retention과 privacy를 코드로 강제한다.
4. Domain Provider가 정확한 수치와 전문 정책을 소유한다.
5. 최종 답변은 실제 tool output의 `source_refs`만 사용할 수 있다.
6. 행동 제안은 자동으로 `DecisionRecord`에 저장된다.
7. Hermes 없이도 계약 테스트가 가능하고 Hermes는 교체 가능한 adapter다.
8. UI 구현 없이 엔진과 runtime 연결의 end-to-end 테스트가 통과한다.
