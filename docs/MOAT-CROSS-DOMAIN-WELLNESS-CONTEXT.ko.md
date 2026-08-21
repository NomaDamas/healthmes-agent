# HealthMes 해자: 교차 영역 웰니스 맥락 판단

> **결정일:** 2026-08-09
>
> **지위:** HealthMes의 잠재적 해자에 대한 소유자 결정 기록.
>
> **관련 문서:** `ACTIVITY-WELLNESS-MVP.ko.md`,
> `HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`,
> `contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md`,
> `WELLNESS-DATA-PLATFORM.ko.md`, `COMPETITIVE-LANDSCAPE.ko.md`

## 한 줄

**HealthMes의 잠재적 해자는 입력을 많이 연결하는 데서 끝나지 않고, 모호하거나
여러 영역에 걸친 질문에 필요한 개인 맥락만 선택해 근거와 한계를 유지한 채 하나의
웰니스 의사결정으로 결합하는 능력이다.**

## 1. 무엇이 다른가

개별 기능은 복제할 수 있다.

```text
웨어러블 dashboard
앱 사용 tracker
식사 사진 분석
카페인 계산기
캘린더 비서
LLM chat
```

HealthMes가 만들려는 것은 이 기능들의 단순한 모음이 아니다.

```text
사용자의 질문
    |
    v
HealthMes wellness decision ingress
    |
    +-- Hermes LLM이 필요한 HealthMes MCP tool 선택
    +-- Context Access Layer가 retention과 privacy 검사
    +-- domain provider가 정확한 context 반환
    +-- subjective state
    |
    v
전문 정책별 독립 판단
    |
    v
근거·freshness·coverage·conflict가 보존된 통합 decision
    |
    v
사용자의 행동과 이후 outcome
```

## 2. 대표 질문

### "왜 오늘 집중이 안 되지?"

HealthMes는 처음부터 하나의 원인을 정하지 않는다.

```text
activity
  앱 전환, 긴 연속 작업, 휴식 부족

wearable
  수면, HRV, stress와 recovery

calendar
  회의 밀도와 남은 일정

nutrition
  식사 누락, 카페인 시각과 확정 섭취

subjective
  사용자가 말한 피로, 불안과 통증
```

각 영역의 데이터가 충분한지 확인한 뒤, 관찰된 관계와 대안을 설명한다.
상관관계를 원인으로 확정하지 않는다.

### "집중하려고 이 커피를 마셔도 될까?"

```text
사진 또는 텍스트
  -> 후보 카페인 분석
  -> 사용자 확인

nutrition ledger
  -> 오늘 확정 섭취량

wearable/time
  -> 수면, 현재 시각, 목표 취침

activity
  -> 연속 작업, 집중 분절, 휴식 부족

HealthMes
  -> 카페인 정책의 결과
  -> 지금 필요한 행동 대안
  -> 각각의 근거와 한계
```

카페인 전문 정책은 카페인 한도와 수면 경계를 소유한다. Activity policy는
집중과 과로 맥락을 소유한다. 상위 HealthMes decision은 두 정책의 숫자를
재계산하거나 섞지 않고 함께 설명한다.

이 결합은 "데이터가 하나라도 있다"를 "판단 가능"으로 바꾸지 않는다.
예를 들어 `caffeine_for_focus`는 같은 local day에 연결된 카페인 후보와
사용자가 완료 확인한 당일 섭취 ledger가 모두 있어야 `decision_ready`다.
활동, 웨어러블 또는 일정 맥락은 이 전문 안전 조건을 대신하지 않고 대안과
설명을 보강한다.

## 3. 이 해자가 쌓이는 데이터

입력의 개수만으로 해자가 생기지 않는다. 다음 연결이 반복되어야 한다.

```text
질문과 당시 맥락
    x 선택된 evidence
    x HealthMes 판단
    x 사용자 승인·수정·거절
    x 실제 행동
    x 이후 집중·수면·스트레스·완료 결과
```

이 기록이 쌓이면 다음을 개인별로 검증할 수 있다.

- 어떤 종류의 집중 저하가 휴식으로 개선됐는가
- 카페인 대신 휴식을 선택한 날 이후 상태가 어땠는가
- 야간 활동과 다음날 수면·집중이 어떤 관계를 보였는가
- 어떤 제안은 사용자가 반복해서 거절하거나 수정하는가
- 같은 외부 조건에서도 개인별로 어떤 개입이 달랐는가

이는 범용 LLM prompt나 하나의 sensor dashboard보다 이전하기 어렵다.

## 4. 구현 원칙

### HealthMes가 소유한다

- 공통 `WellnessEvent`와 source provenance
- 데이터별 freshness, confidence와 coverage
- 전문 activity/nutrition/wearable/calendar policy
- context tool catalog와 접근·privacy 계약
- HealthMes wellness 요청·결과와 단일 runtime 계약
- decision과 outcome graph
- privacy, consent와 retention

### LLM이 맡는다

- 자연어 질문의 목적 해석
- 필요한 영역, 기간과 tool 선택
- 첫 조회 결과에 따른 추가 조회
- 여러 영역의 trade-off와 최종 설명

### LLM이나 agent runtime에 맡기지 않는다

- 오늘 섭취량 합계
- 시간과 timezone 경계
- 앱 사용시간과 baseline 계산
- 누락 데이터를 0으로 바꾸는 판단
- 전문 정책의 숫자 재계산
- 근거 없이 원인을 하나로 확정하는 판단

### Agent runtime과 Skill의 위치

```text
HealthMes wellness product API
        |
        v
Hermes autonomous LLM + tool loop
        |
        v
HealthMes MCP + domain tools
```

Skill은 핵심 판단 엔진이 아니라 runtime별 도구 사용법과 표현 방식을 설명하는 얇은
adapter다. 데이터 계산, retention과 source provenance는 Skill에 맡기지 않는다.

Hermes는 HealthMes 제품 전체가 아니라 자연어 질문과 자율 tool loop를 실행하는
교체 가능한 runtime이다. 제품의 공식 진입점, 데이터와 저장 계약은 HealthMes가
소유한다. 상세 경계는
[`HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md)
를 따른다. Hermes 변경이 필요하면 별도 저장소와 별도 작업으로 진행한다.

## 5. MVP 경계

현재 MVP는 다음 호환 질문과 context 도구를 구현했다. 이 목록은 목표 제품의 질문
종류를 제한하는 taxonomy가 아니라 기존 `question_kind` resolver의 지원 범위다.

```text
지원 질문
  focus
  overwork
  recovery
  caffeine-for-focus

지원 입력
  activity summary
  Open Wearables summary
  calendar/time context
  confirmed nutrition/caffeine context

반환
  observation
  evidence IDs
  freshness and coverage
  specialized policy results
  limitations
  bounded behavior proposal
```

목표 구조에서는 LLM이 질문에 필요한 영역을 선택하되 Context Access Layer가
불필요한 영역과 원본을 차단한다. 기본은 집계 context다. 앱 identity나 일정 제목은
질문에 필요하고 사용자가 허용한 경우에만 제한적으로 사용하며, 사진과 음성 bytes는
VLM 또는 transcription처럼 원본 분석 자체가 목적인 scoped provider 호출에만
전달한다. 일반 최종 판단에는 구조화 결과와 `source_refs`를 사용한다.

각 입력 엔진은 독립적으로 수집·정규화·저장된다. Activity Ingest가 Open
Wearables, 캘린더나 식사 데이터를 다시 수집하지 않는다. 교차 영역 해자는
입력 파이프라인을 하나로 뒤섞는 데 있지 않고, 공통 `WellnessEvent` 저장과
Context Access Layer를 통해 필요한 파생 context만 결합하는 데 있다.

```text
activity collector -> activity context --------┐
Open Wearables -> wearable context ------------┤
nutrition engine -> nutrition/caffeine policy -┼-> Context Access Layer
calendar -> calendar/time context -------------┘
                                                    |
                                                    v
                                        Hermes wellness decision turn
```

Context Access Layer는 날짜, freshness, coverage와 source reference가 맞지 않는 영역을
`insufficient_data` 또는 `unavailable`로 남긴다. 유효한 다른 영역의 context는
보존하지만, 빠진 전문 정책 입력을 추측해서 최종 판단을 만들지는 않는다.

## 6. 검증 기준

이것은 아직 검증된 시장 해자가 아니라 잠재적 해자다. 다음이 실사용으로
확인되어야 한다.

- 단일 영역 도구보다 복합 질문의 답변이 더 유용했는가
- 사용자가 근거와 한계를 이해했는가
- 제안을 실제로 따르거나 수정한 이유가 기록됐는가
- 이후 집중, 회복, 수면 또는 완료 결과가 측정됐는가
- 같은 조건에서 개인별 반복 패턴이 나타났는가
- 불필요한 민감 데이터 없이도 판단이 가능했는가

HealthMes의 해자는 "데이터를 많이 모은다"가 아니라
**필요한 데이터를 정확히 선택하고, 전문 경계를 지키면서, 개인의 행동과 결과까지
연결하는 반복 가능한 의사결정 구조**가 될 때 형성된다.

## 7. 보조 해자: 오픈 앱 커스터마이징

교차 영역 판단 엔진과 별개로, 공식 앱의 기능과 UI 연결 계약도 오픈소스로
제공하는 것을 보조 해자로 둔다. iOS, Android, 데스크톱과 웹 앱은 교체 불가능한
단일 클라이언트가 아니라 같은 저장, 권한, provenance, retention과 MCP/Skill
계약을 사용하는 참조 구현이다.

개인과 조직은 화면, 알림, 승인 workflow, 입력 adapter와 출력 채널을 포크하거나
교체할 수 있지만 HealthMes의 privacy와 전문 정책 경계는 우회할 수 없다. 해자는
코드를 숨기는 데 있지 않고, 호환 가능한 입력과 앱이 늘어나도 하나의 검증 가능한
웰니스 context로 결합되는 생태계에 있다.
