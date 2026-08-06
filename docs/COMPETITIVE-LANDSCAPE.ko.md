# HealthMes 유사 사례, 차별화, 재사용 전략과 해자

> **RECORD ONLY — NOT IMPLEMENTATION AUTHORITY**
>
> 이 문서는 2026-08-04에 소유자와 나눈 경쟁·제품 전략 대화를 보존하는 **기록용
> 메모**다. 승인된 요구사항, 로드맵, 설계 결정, 작업 지시가 아니다.
>
> **개발 에이전트는 이 문서를 근거로 구현·리팩터링·통합·이슈 생성·로드맵 변경을
> 해서는 안 된다.** 특히 이 문서에 언급된 `SchedulingBackend`, Reclaim 연동,
> Notification Policy DSL, Outcome Ledger, 생성형 UI는 자동으로 개발할 대상이 아니다.
> 소유자가 이후 작업에서 명시적으로 지시하거나 권위 문서에 별도로 승인하기 전까지는
> 아이디어 기록으로만 취급한다.
>
> 최상위 철학은 [`OWNER-VISION.ko.md`](OWNER-VISION.ko.md), 현재 구현 계약은
> [`PLAN.md`](PLAN.md)다. 충돌하거나 모호하면 이 문서를 무시하고 두 권위 문서와
> 소유자의 현재 지시를 따른다.
>
> **조사 기준일**: 2026-08-04. 경쟁 제품은 이후 변경될 수 있으므로 중요한 제품
> 결정 전 공식 문서를 다시 확인한다.

## TL;DR

HealthMes와 완전히 동일한 단일 오픈소스 프로젝트는 확인되지 않았다. 그러나
**자동 일정 배치**, **웨어러블 기반 에너지 계획**, **AI 채팅**, **MCP**, **사용자 승인
후 일정 반영**은 이미 각각 Reclaim과 LifeStack 등이 제공한다. 따라서 이 기능들의
조합만으로는 지속 가능한 차별점이 되지 않는다.

대화에서는 HealthMes가 소유할 수 있는 문제를 다음 한 문장으로 좁혀 검토했다.

> **개인의 건강·인지 상태를 수행능력 제약으로 변환하고, 상태 변화에 맞춰 가역적인
> 개입을 제안하며, 어떤 개입이 실제로 효과가 있었는지 장기간 학습하는 로컬-first
> 개인 에이전트.**

당시 검토한 제품 역할은 "더 나은 자동 캘린더"보다 **Personal Capacity Layer**에
가깝다는 가설이었다.

```text
웨어러블 + 앱 사용 + 일정 + 음식/증상 + 사용자 대화
                         │
                         ▼
        HealthMes Personal Capacity Layer
      상태 해석 · confidence · 개입 · 결과 학습
                         │
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
      자체 캘린더     Reclaim 등      Chat/Watch/UI
      (fallback)    scheduling backend   surfaces
```

## 1. 경쟁 경계

### 이미 범용화된 기능

다음 기능은 중요하지만 그 자체로 해자가 아니다.

- 태스크를 빈 시간에 자동 배치
- 충돌 발생 시 일정 재조정
- Focus/Habit/Task 우선순위 관리
- AI 채팅으로 일정 생성·수정
- 변경안 미리보기와 사용자 승인
- MCP를 통한 일정 조작
- 웨어러블 수면·HRV 기반 에너지 예측
- 휴대폰·워치 위젯과 알림
- 생성형 UI 또는 모델 선택 기능

### HealthMes가 소유할 기능

- **Human-capacity conflict 감지**: 캘린더 충돌이 아니라 현재 사람의 수행능력과
  계획된 요구량의 충돌을 감지한다.
- **상태 변화 기반 선제 개입**: 수면·HRV·스트레스·앱 단편화·주관적 맥락이
  변하면 사용자가 묻기 전에 가역적 행동 하나를 제안한다.
- **개입 결과 학습**: 제안을 승인했는지뿐 아니라 이후 집중도·스트레스·완료율과
  사용자의 평가가 어떻게 변했는지 기록한다.
- **근거와 불확실성 공개**: 판단 입력, 규칙, confidence, 누락 데이터와 실행 결과를
  재구성할 수 있다.
- **사용자 소유 실행 환경**: raw-first, local-first, self-host, 암호화 백업,
  교체 가능한 모델·채널·스킬·스케줄링 백엔드를 제공한다.

## 2. 주요 유사 사례

| 프로젝트 | 이미 해결한 문제 | HealthMes와 겹치는 부분 | HealthMes가 남겨야 할 차이 |
|---|---|---|---|
| **Reclaim AI 2.0** | Task, Habit, Focus, Meeting을 우선순위와 가용시간에 따라 배치하고 충돌 시 재조정. AI Assistant, 백그라운드 agent, Preview Mode, MCP 제공 | 자동 배치, 재계획, 채팅, 승인·수정·무시 학습, MCP | 건강 상태를 수행능력 제약으로 변환하는 계층, 상태 기반 개입, 개입 후 건강·업무 결과 학습 |
| **LifeStack** | 수면·회복·집중 패턴과 캘린더를 이용한 Energy Zone, AI 일정, 일정 변경 후 재계획, contextual copilot, 식사·수면 계획 | 건강 기반 시간 추천, 일정 배치, 건강 맥락 채팅 | 더 넓은 데이터 소스, 실시간 상태 변화, 앱 단편화·증상 맥락, 설명가능한 개입-결과 루프, self-host |
| **Lumen** | Garmin 데이터 기반 로컬 건강 코치, HRV·수면·Body Battery 분석, 일일 권고, SQLite/Ollama 경로 | 로컬 건강 해석과 행동 권고 | 실제 캘린더 실행, 선제 트리거, 승인 정책, 장기 개입 결과 |
| **GAIA** | 선제적 개인비서, 캘린더·태스크·메시징 연결, 승인 가능한 자동화 | "먼저 행동하는 비서" UX | 건강 신호의 결정론적 해석과 confidence, 개인 수행능력 모델. PolyForm Strict이므로 OSI 오픈소스가 아닌 source-available로 취급 |
| **OwnChart** | 환자 소유 건강 기록, raw provenance, 장기 건강 맥락, AI 주장과 근거 추적 | 데이터 주권, raw-first, 감사 가능한 건강 판단 | 미래 행동을 조정하는 일정 실행·개입 루프. PolyForm Noncommercial 기반 source-available |
| **HPI** | 여러 개인 데이터 소스를 로컬 Python 객체로 통합하고 건강·일정·환경 간 상관을 탐색 | 데이터 사일로 해체, 개인 데이터 소유, 장기 상관 | 비개발자용 실행 제품, 선제 알림, 승인된 행동과 결과 학습 |
| **ActivityWatch** | 앱·창·브라우저 사용을 로컬에서 수집하는 오픈소스 자동 시간 추적 | 집중 단편화와 앱 사용 맥락 | 건강·캘린더 판단은 HealthMes가 담당. 수집기를 다시 만들기보다 어댑터 후보 |
| **Open Wearables** | 여러 웨어러블의 통합 데이터 모델, 건강 점수, MCP·API | HealthMes 데이터 플레인의 직접 업스트림 | 개인 일정·개입·결과와 연결하는 Capacity Layer |
| **Lorvex** | local-first AI 일정·태스크 관리, MCP, 에너지 패턴, 변경 기록을 지향 | 오픈 에이전트 플래너 철학 | 생체 신호 기반 수행능력, 검증된 결과 루프. 현재 pre-release 성격이 강해 벤치마크로만 사용 |

### Reclaim AI

사용자가 제시한 `How Reclaim manages your schedule automatically` 문서는 Reclaim 1.0
방식을 설명하며, 현재 공식 문서는 Reclaim 2.0을 별도로 안내한다. Reclaim 2.0은
calendar-native chat, Task/Habit/Focus/Meeting agent, Preview Mode를 제공한다. 공식
MCP 서버도 ChatGPT, Claude 등의 외부 클라이언트에서 일정을 조작할 수 있게 한다.
2026-06-11자 공식 overview 기준 Reclaim 2.0은 private beta이지만, 제품 방향은 이미
HealthMes의 범용 스케줄링·채팅·승인 표면과 직접 겹친다. 공식 문서는 제안의 승인,
수정, 무시 행동을 통해 지원 방식을 개선한다고도 설명한다.

**판단**: 범용 일정 최적화 엔진으로 정면 경쟁하지 않는다. Reclaim을 선택적 실행
백엔드로 연동할 수 있도록 경계를 만든다. HealthMes 자체 스케줄러는 독립 실행,
로컬-first fallback, 테스트 가능한 기준 구현으로 유지한다.

### LifeStack

LifeStack은 수면·회복·집중 패턴과 캘린더를 바탕으로 에너지 리듬을 예측하고, 태스크를
적합한 시간에 배치하며, 캘린더가 변하면 계획을 조정한다. 현재 홈페이지는 24/7
contextual copilot, 식사·수면 계획, 캘린더 안의 wearable insight까지 전면에 내세운다.
즉, "웨어러블 데이터에 따라 오늘의 일을 배치하고 건강 맥락으로 대화한다"는 문장만
보면 HealthMes와 직접 겹친다.

**판단**: HealthMes를 "오픈소스 LifeStack"으로 포지셔닝하지 않는다. 차이는 데이터
소스 개수도 아니며, **상태 변화 중 개입**, **개입 효과 추적**, **설명가능성**,
**프로그래밍 가능한 실행 정책**, **사용자 소유 런타임**이어야 한다.

### 공개 형태 구분

- **상용 SaaS**: Reclaim, LifeStack. 공개 제품 문서에서는 self-host 경로를 확인하지
  못했다.
- **source-available**: GAIA, OwnChart. 소스는 공개되어 있지만 PolyForm 계열의
  상업 제한 라이선스이므로 OSI 오픈소스와 구분한다.
- **오픈소스 기반·재사용 후보**: Lumen, HPI, ActivityWatch, Open Wearables, Lorvex.
  실제 도입 전에는 각 저장소의 현재 라이선스와 유지보수 상태를 다시 검토한다.

### 나머지 프로젝트가 주는 교훈

- Lumen은 건강 코치 계산과 로컬 실행을 다시 만들 필요가 없다는 점을 보여준다.
- GAIA는 범용 proactive assistant UX와 채널·도구 연결이 독립 제품군임을 보여준다.
- OwnChart와 HPI는 장기 데이터 소유권과 provenance가 독립적인 사용자 가치임을
  보여준다.
- ActivityWatch는 데스크톱 앱 사용 수집기를 직접 만드는 대신 검증된 로컬 수집기와
  연결하는 경로를 제공한다.
- Open Wearables는 HealthMes가 웨어러블별 SDK와 건강 점수를 재구현하지 않아야 하는
  이유다.

## 3. 차별화된 제품 정의

### Positioning

```text
Reclaim   : 무엇을 언제 할 것인가?         → 일정 최적화
LifeStack : 내 에너지상 언제 하는가?       → 건강 기반 시간 추천
HealthMes : 지금 수행 가능한가, 무엇을
            바꾸면 실제로 나아지는가?      → 상태·개입·결과 학습
```

### 핵심 사용자

일반 생산성 사용자 전체보다 **수행능력 변동성이 큰 사용자**를 우선한다.

- 수면·번아웃으로 날짜별 업무 수용력이 크게 달라지는 사람
- ADHD 등으로 계획과 실제 실행의 괴리가 반복되는 사람
- 만성질환·장기 회복 중 에너지 보존이 필요한 사람
- 데이터와 자동화 규칙을 직접 소유하려는 개발자·파워유저

진단이나 치료를 제공한다는 의미가 아니다. HealthMes의 역할은 사용자가 제공한
건강·행동 증거를 근거로 **가역적인 일정·회복 개입을 제안하고 결과를 기록하는 것**이다.

### 핵심 폐루프

```text
Observation
수면, HRV, 일정 부하, 앱 전환, 증상, 주관적 상태
      │
      ▼
Decision
개인 baseline, confidence, 규칙, 선택지
      │
      ▼
Intervention
이동, 축소, 휴식, 유지, 추가 질문
      │
      ▼
Outcome
완료, 실제 소요시간, 후속 건강 신호, 사용자 평가
      │
      └──────────── 다음 개인 판단 보정 ────────────┘
```

## 4. 바퀴를 재발명하지 않는 방법으로 검토한 후보

### 직접 소유할 것

1. **Capacity Engine**
   - 시간대별 수행능력, 계획 부하와의 불일치, confidence를 계산한다.
   - LLM이 숫자를 만들지 않는 기존 결정론 경계를 유지한다.
2. **Intervention Engine**
   - 지금 알림을 보낼지, 무엇을 제안할지, 어떤 결과를 나중에 확인할지 결정한다.
   - 알림 예산, 쿨다운, quiet hours, 가역성, 승인 수준을 포함한다.
3. **Outcome Ledger**
   - 관찰, 결정, 개입, 승인·수정·거절·무시, 실제 결과를 append-only로 연결한다.
4. **Explanation Contract**
   - 모든 표면이 같은 decision schema와 confidence를 렌더링한다.

### 외부에 위임할 것

| 영역 | 기본 재사용 경로 | HealthMes의 역할 |
|---|---|---|
| 웨어러블 통합·점수 | Open Wearables | 해석된 수행능력과 일정 영향으로 변환 |
| 에이전트 채팅·메모리·채널 | Hermes Agent | 건강 도구·스킬·정책 제공 |
| 범용 일정 최적화 | Reclaim MCP 또는 다른 `SchedulingBackend` | capacity constraint와 승인 정책 전달 |
| 로컬 앱 사용 수집 | ActivityWatch 또는 기존 Android collector | 시간 버킷과 단편화 지표만 수용 |
| 캘린더 프로토콜 | Google Calendar API, CalDAV | 소유권 분할과 확정된 변경만 집행 |
| 모델 실행 | Claude/OpenAI/Ollama 등 provider | 최소 컨텍스트 판단, 숫자 계산 금지 |
| 암호문 스토리지 | S3/R2/MinIO | 클라이언트 암호화 envelope와 복원 계약 |

### `SchedulingBackend` 경계

대화에서는 현재 `CalendarBackend`와 별도로 "언제 배치할지"를 계산하는 전략
인터페이스 아이디어를 검토했다. 이는 승인된 설계가 아니다.

```text
SchedulingBackend
  plan(tasks, fixed_events, capacity_windows, policies) -> proposals
  replan(change_event, current_plan, capacity_windows)   -> proposals
  explain(proposal_id)                                   -> evidence
```

대화에서 검토한 가상 순서(미승인):

1. 현재 HealthMes planner를 `LocalSchedulingBackend`로 감싼다.
2. 계약 테스트로 동일 입력·출력·승인 경계를 고정한다.
3. Reclaim MCP는 `ReclaimSchedulingBackend` 후보로 별도 어댑터화한다.
4. 외부 백엔드가 없거나 프라이버시 정책상 허용되지 않으면 로컬 구현으로 fallback한다.

Reclaim의 내부 최적화 알고리즘을 복제하지 않는다. 외부 서비스가 HealthMes의 raw 건강
데이터를 받을 필요도 없다. 전달값은 "14:00–17:00 high-demand 금지, confidence=medium"
같은 최소 capacity constraint여야 한다.

### 오픈소스 채팅과 알림

새 범용 챗앱을 처음부터 만들지 않는다. Hermes의 대화·메모리·MCP·채널 기능을 사용하고,
HealthMes는 다음을 추가한다.

- 알림 조건·쿨다운·예산·채널·상세도·허용 행동을 설정하는 **Notification Policy DSL**
- 사용자가 직접 수정하고 공유할 수 있는 건강 개입 policy/skill pack
- 모든 채널에서 동일하게 해석되는 observation/evidence/proposal/action schema
- 사용자 반응과 후속 결과를 Outcome Ledger에 연결하는 callback 계약

### 생성형 UI와 diffusion 모델

**핵심 건강 판단 화면을 픽셀 단위 diffusion 모델로 매번 생성하지 않는다.** 버튼 위치와
정보 계층이 달라지면 접근성, 재현성, 승인 안전성, 테스트 가능성이 떨어진다.

권장 구조는 schema-constrained generative UI다.

```text
Decision Record
      ▼
검증된 UI Schema
      ▼
Generative Composer
      ▼
검증된 component 조합
      ▼
Watch / Phone / Chat / Desktop
```

- 모델이 선택할 수 있는 것: 카드 수, 근거 상세도, 차트 종류, 요약 길이, 순서.
- 모델이 바꿀 수 없는 것: 사실값, confidence, 승인 대상, action semantics, 경고 문구.
- diffusion 적용 가능 영역: 배경, 앰비언트 에너지 표현, 비결정적 장식.
- diffusion 적용 금지 영역: 동의 버튼, 위험 경고, 건강 수치, 결정 근거, 캘린더 변경량.

즉 생성형 UI는 차별화된 전달 방식이 될 수 있지만, 제품의 핵심 해자는 아니다.

## 5. HealthMes의 해자

### 해자가 아닌 것

- 웨어러블 11개 연동
- 특정 LLM이나 프롬프트
- MCP 사용
- 자동 시간 배치
- 승인 후 캘린더 반영
- 네이티브 앱·워치·위젯
- 생성형 UI
- 암호화 백업 기능 하나
- 여러 오픈소스 프로젝트를 연결한 코드

경쟁자가 시간과 자본을 투입하면 복제할 수 있고, 오픈소스 프로젝트에서 코드 자체는
더욱 해자가 되기 어렵다.

### 잠재적 핵심 해자: 개인 개입-결과 그래프

가장 방어력 있는 자산은 다음 관계가 개인별로 축적되는 것이다.

```text
건강·인지 상태
    × 상황·사람·업무
    × 에이전트 판단
    × 사용자의 승인·수정·거절
    × 실제 행동
    × 이후 건강·집중·완료 결과
```

이는 단순 웨어러블 시계열이나 일정 이력이 아니다. "이 사용자에게 어떤 조건에서 어떤
개입이 실제로 효과가 있었는가"를 설명하는 장기 실행 그래프다. 다른 서비스로 이동하면
이 개인화된 인과 후보와 신뢰 관계를 다시 쌓아야 한다.

### 보조 해자

1. **신뢰와 권한 보정**
   - 어떤 제안을 사용자가 승인하는지, 언제 알림을 무시하는지, 어느 범위까지 자동화를
     허용했는지가 축적된다.
2. **검증된 전문가 프로토콜**
   - 전문가 스킬, 측정 조건, confidence gate, 실패 사례와 손계산 회귀 테스트가 함께
     쌓인다. 단순 프롬프트보다 복제하기 어렵다.
3. **데이터 주권에서 오는 신뢰**
   - local-first, raw-first, ciphertext-only backup은 네트워크 효과는 아니지만 건강
     자동화에 필요한 신뢰 장벽을 낮춘다.
4. **오픈 확장 생태계**
   - metric, skill, notification policy, scheduling backend뿐 아니라 공식 앱의
     기능과 UI 연결 계약도 오픈소스로 제공한다. 개인·조직이 iOS, Android,
     데스크톱, 웹 표면을 포크하거나 교체하면서도 동일한 저장소·MCP·판단 근거와
     호환되게 한다.
   - 코드 공개 자체는 해자가 아니다. 커스텀 앱, adapter, workflow가 공통 계약
     위에서 재사용되고 개선이 다시 환원되는 생태계가 형성될 때 분배와 학습 속도의
     보조 해자가 된다. 현재는 가능성이지 이미 존재하는 해자는 아니다.

### 현재의 냉정한 상태

HealthMes에는 해자를 만들 **구조와 코드**는 있지만 아직 검증된 해자는 없다.

- 실사용 기간이 짧고 개인별 intervention outcome 데이터가 충분하지 않다.
- 자체 스케줄러, 앱, 백업, 의사결정 트리는 구현 자산이지 시장 방어력의 증거가 아니다.
- 실제 알림 유용성, 계획 완료율, 회복 개선, 사용자 위임 수준이 측정돼야 한다.

해자는 기능 출시가 아니라 다음 데이터가 반복적으로 쌓일 때 생긴다.

- 제안 승인·수정·거절·무시
- 변경 전후의 완료율과 실제 소요시간
- 후속 집중도·스트레스·회복 변화
- 사용자가 직접 평가한 도움 여부
- 같은 조건에서 반복되는 개입 효과
- 틀린 판단과 사용자의 수정 이유

## 6. 대화 당시 제품 우선순위 후보(미승인)

아래 항목은 실행 계획이 아니라 토론 당시의 후보 순서를 기록한 것이다. 개발 에이전트는
이 목록을 작업 큐로 사용하면 안 된다.

### P0 — 해자 데이터 생성

1. `Observation → Decision → Intervention → Outcome` 연결을 저장 모델과 API에서
   일급 개념으로 고정한다.
2. 알림마다 후속 평가 시점과 성공 지표를 기록한다.
3. 승인율보다 **실제 도움 여부와 결과 변화**를 핵심 지표로 삼는다.
4. 소유자 실데이터로 6–8주 dogfood해 반복되는 개인 패턴을 확인한다.

### P1 — 재사용 경계

1. `LocalSchedulingBackend` 계약을 기존 planner 위에 정의한다.
2. Reclaim MCP 어댑터의 가능성과 데이터 최소화 경계를 검증한다.
3. ActivityWatch 인입 어댑터를 검토하고 데스크톱 수집기 재구현을 피한다.
4. Notification Policy DSL을 구현해 알림 커스터마이징을 문구가 아닌 실행 정책으로
   확장한다.

### P2 — 표현 계층

1. decision schema 기반 component generative UI를 만든다.
2. 워치·폰·채팅에서 인지 상태에 따라 정보 밀도를 조절한다.
3. diffusion은 장식적·앰비언트 표현에 한정해 실험한다.

### 중단 조건

- 사용자가 원하는 가치가 일정 정리뿐이면 Reclaim과 경쟁하지 말고 연동한다.
- 웨어러블 기반 시간 추천만 필요하면 LifeStack과 차별성이 약하므로 범위를 늘리지 않는다.
- 6–8주 실사용 후에도 개입 결과를 측정하거나 개인별 반복 효과를 찾지 못하면 "학습하는
  Capacity Layer"라는 가설을 재검토한다.
- 생성형 UI가 승인 시간, 이해도, 접근성을 개선하지 못하면 시각적 데모 이상으로
  확장하지 않는다.

## 7. 공식 자료

### 직접 경쟁·인접 제품

- Reclaim 2.0 overview:
  <https://help.reclaim.ai/en/articles/14846468-reclaim-ai-2-0-overview>
- Reclaim 2.0 FAQ와 MCP:
  <https://help.reclaim.ai/en/articles/15280604-reclaim-2-0-faq>
- Reclaim 자동 일정 관리(1.0 문서, 현재 2.0 문서와 함께 참고):
  <https://help.reclaim.ai/en/articles/6207587-how-reclaim-manages-your-schedule-automatically>
- LifeStack:
  <https://lifestack.ai/>
- LifeStack AI Life Planner:
  <https://lifestack.ai/ai-life-planner>
- LifeStack Google Calendar scheduling:
  <https://lifestack.ai/blog/daily-planner-syncs-with-google-calendar>
- Lumen:
  <https://www.lumenhealth.sh/>
- GAIA:
  <https://github.com/heygaia/gaia>
- OwnChart:
  <https://github.com/nickpdawson/OwnChart>
- Lorvex:
  <https://lorvex.app/>

### 재사용 가능한 기반

- HPI:
  <https://github.com/karlicoss/HPI>
- ActivityWatch:
  <https://activitywatch.net/>
- Open Wearables:
  <https://github.com/the-momentum/open-wearables>
- Vercel AI SDK Generative User Interfaces:
  <https://ai-sdk.dev/docs/ai-sdk-ui/generative-user-interfaces>
