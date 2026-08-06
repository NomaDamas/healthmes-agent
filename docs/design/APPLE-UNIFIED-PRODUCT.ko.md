# HealthMes Apple 제품과 웹 대시보드 통합 UX

> 상태: issue #108 구현 기준
>
> 목표: 복잡한 HealthMes 아키텍처를 사용자가 `말하기 → 계획 확인 →
> Yes/No 결정 → 결과 확인`의 단순한 제품으로 사용하게 한다.

## 1. 제품 계약

```text
내부
건강 데이터 · 인지에너지 · 목표 · 캘린더 · 규칙 · 모델 · 결과 학습
                              │
                              ▼
사용자
Today · Plan · Decisions · Speak · Settings
```

iPhone, Mac, Web은 동일한 핵심 기능을 제공한다. 플랫폼 차이는 핵심 기능의
유무가 아니라 입력·표현·운영 능력의 추가다.

- iPhone 추가: HealthKit, 잠금화면 알림, Live Activity, 카메라·마이크, Watch 연결
- Mac 추가: 메뉴바, 키보드 음성 호출, 앱 사용·집중 맥락, 넓은 상세 화면
- Web 추가: URL로 열리는 긴 판단 설명, 장기 리포트, Connections, Advanced
- Watch 예외: 화면 크기 때문에 글랜스와 3초 Yes/No 결정만 제공

## 2. 기본 정보 계층

모든 기본 화면은 다음 순서를 지킨다.

```text
NOW       지금 상태 한 문장
NEXT      다음 일정 한 개
DECISION  지금 결정할 제안 한 개
SPEAK     음성 입력
DETAIL    사용자가 요청할 때만
```

기본 화면에서 숨길 것:

- 전체 건강 원시 수치
- 복잡한 그래프
- confidence 계산 과정
- 모델 provider와 토큰
- 서버 주소
- 보존정책
- 진단 로그

## 3. 공통 기능

### Today

- 현재 capacity 상태
- 다음 일정
- 가장 중요한 pending decision
- 오늘 일정 요약
- 주간 목표 진행
- 최신 데이터 시각과 stale 상태

### Plan

- 주간·월간 목표
- 캘린더 일정
- HealthMes가 배치한 블록
- 이동 전·후 미리보기
- 승인·거절·직접 수정

### Decisions

- pending
- accepted / declined / expired
- 다른 기기에서 이미 처리됨
- 캘린더 적용 상태
- 후속 결과 확인 시점

### Speak

- 목표와 할 일을 자연어 음성으로 입력
- 현재 상태와 일정 질문
- 일정 변경 요청
- 판단 이유 질문

텍스트 명령 composer는 두지 않는다. 계정·자격증명·URL처럼 음성 입력이
부적절한 값은 Settings의 안전한 폼을 사용한다.

### Settings

기본:

- 계정
- 건강 데이터
- 캘린더
- 알림
- Apple Watch
- 저장 방식

Advanced:

- self-host URL과 token
- 데이터별 보존기간
- 모델 provider/BYOK
- 원시 데이터
- 알림 정책
- 진단·export·restore

## 4. 알림과 상세 URL

기존 #91의 알림 의미와 버튼은 유지한다.

```text
Deep Work를 오후 4시로 옮길까요?
수면이 평소보다 1시간 40분 부족합니다.

[아니요] [예]
왜? · 웹에서 자세히
```

행동:

- `예`: proposal을 정확히 한 번 승인하고 캘린더에 반영
- `아니요`: proposal을 거절하고 같은 결정을 다시 묻지 않음
- `왜?`: 기기 안의 짧은 근거
- `웹에서 자세히`: 정확한 decision context URL

URL 계약:

```text
/decisions/{decision_id}
```

웹 상세에는 다음을 순서대로 접어서 제공한다.

1. 한 줄 이유
2. 건강 근거
3. 일정 영향
4. 대안
5. 판단 트리
6. 과거 유사 개입과 결과

## 5. 인증 UX

정상 사용자가 bearer token을 직접 보거나 입력하지 않게 한다.

```text
인증된 앱에서 열기
→ 바로 decision context

새 브라우저에서 열기
→ Sign in 또는 이 기기 잠금 해제
→ 원래 decision context로 복귀

self-host
→ Settings > Advanced에서 한 번 pairing
```

human-facing bare URL은 JSON 401 대신 잠금 해제 화면을 표시한다. API와
mutation endpoint의 bearer 보호는 그대로 유지한다.

## 6. 플랫폼별 표현

### iPhone

- 세로 카드
- Today / Plan / Decisions 탭
- 중앙 Speak 액션
- 잠금화면 알림과 Live Activity
- 상세는 native sheet 또는 인증된 web view

### Mac

- Today / Plan / Decisions의 동일 기능
- Speak 버튼과 키보드 호출
- 넓은 화면에서는 일정과 현재 결정을 나란히 표시
- 상세 inspector
- 메뉴바는 glance shortcut이며 전체 앱을 대체하지 않음

### Web

- 반응형 dashboard shell
- 좁은 화면에서도 같은 Today / Plan / Decisions 구조
- History와 긴 분석
- Connections와 Advanced
- 앱의 `웹에서 자세히`가 여는 정본

### Watch

```text
LOW RECOVERY

Deep Work를
4 PM으로 옮길까요?

Sleep -1h 40m

[No] [Yes]

Why? · 18 min
```

지원 상태:

- pending
- applying
- applied
- declined
- expired
- offline, not sent (연결 후 사용자가 재시도)
- already resolved

## 7. 시각 원칙

- warm neutral 배경, 높은 대비의 graphite text
- accept는 moss green, caution은 muted amber
- 색만으로 의미를 전달하지 않음
- glass는 정보 계층을 만드는 최소한의 재료로만 사용
- 한 화면에 primary action은 하나
- 기본 카드 수는 최대 세 개
- 긴 텍스트는 한 줄 요약 후 펼치기
- Dynamic Type과 VoiceOver에서 의미 순서를 유지

## 8. 성공 흐름

```text
음성으로 목표 투입
→ HealthMes가 계획
→ 상태 변화 감지
→ 행동 하나 제안
→ Watch/iPhone/Mac/Web에서 Yes/No
→ proposal 승인 기록
→ calendar sync 후 pushed 상태에서 실제 반영 완료
→ 모든 표면 상태 동기화
→ 실제 결과 기록
→ 다음 판단 보정
```

## 9. Live QA

1. `/dashboard`가 실제 또는 seed 데이터로 열린다.
2. 모바일 폭에서 핵심 카드와 Yes/No가 잘리지 않는다.
3. iPhone과 Mac에 동일한 core navigation이 있다.
4. Watch 42 mm에서 질문과 Yes/No가 첫 화면에 읽힌다.
5. 알림의 `웹에서 자세히`가 해당 decision을 연다.
6. bare URL은 JSON 401 대신 잠금 안내를 표시한다.
7. 어느 기기에서 처리해도 proposal은 한 번만 전이한다.
8. calendar mutation 결과가 모든 표면에 동일하게 보인다.
9. 토큰·서버 주소·진단 설정은 기본 화면에 보이지 않는다.
10. 복잡한 근거는 사용자가 펼치기 전까지 숨겨진다.
