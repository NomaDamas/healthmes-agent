# HealthMes 해자: 교차 영역 웰니스 맥락 판단

> **결정일:** 2026-08-09
>
> **지위:** HealthMes의 잠재적 해자에 대한 소유자 결정 기록.
>
> **관련 문서:** `ACTIVITY-WELLNESS-MVP.ko.md`,
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
HealthMes context resolver
    |
    +-- wearable evidence
    +-- activity evidence
    +-- nutrition/caffeine evidence
    +-- calendar/time evidence
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
- 질문별 context selection
- decision과 outcome graph
- privacy, consent와 retention

### 모델이나 agent runtime에 맡기지 않는다

- 오늘 섭취량 합계
- 시간과 timezone 경계
- 앱 사용시간과 baseline 계산
- 누락 데이터를 0으로 바꾸는 판단
- 전문 정책의 숫자 재계산
- 근거 없이 원인을 하나로 확정하는 판단

### Agent와 skill의 위치

```text
HealthMes engine and policies
        |
        v
HealthMes context/MCP contracts
        |
        v
HealthMes-owned skills
        |
        v
Hermes or another agent runtime adapter
```

Hermes는 제품 전체나 동등한 판단 엔진이 아니라, 향후 HealthMes 계약을 사용하는
교체 가능한 runtime adaptation이다. Hermes 변경은 별도 저장소와 별도 작업으로
진행한다.

## 5. MVP 경계

MVP는 모든 데이터를 자유롭게 LLM에 넣는 범용 context engine을 만들지 않는다.

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

질문에 필요하지 않은 영역은 읽지 않는다. raw 앱 이름, window title, URL,
사진 bytes, voice bytes와 wearable raw timeseries를 agent context에 넣지 않는다.

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
