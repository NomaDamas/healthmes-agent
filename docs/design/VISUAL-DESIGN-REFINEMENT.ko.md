# HealthMes Visual Design Refinement

> 대상: PR #111
>
> 변경 경계: UI 시각 표현과 디자인 문서만 변경한다. HealthMes 엔진, 판단
> 규칙, 캘린더 승인 의미, API 계약, 저장소 계보는 변경하지 않는다.

## 1. 목표

HealthMes를 일반 Todo, 의료 모니터링, 친환경 SaaS처럼 보이게 하지 않고,
사용자의 현재 가용 능력과 실제 일정의 관계를 빠르게 읽을 수 있는 Wellness
Control System으로 표현한다.

기본 화면에서 사용자는 10초 안에 다음을 확인해야 한다.

1. 지금 사용할 수 있는 Capacity가 어느 정도인가
2. 오늘 일정 중 몸 상태와 충돌하는 블록이 있는가
3. HealthMes가 지금 요구하는 결정이 있는가

## 2. 공통 시각 언어

| 의미 | 색상 역할 | 사용 위치 |
|---|---|---|
| Brand / Voice | 선라이즈 오렌지 | HealthMes 정체성, Speak, 핵심 진입점 |
| Capacity | 블루 | 측정된 가용 에너지와 데이터 |
| Calendar | 블루 | Apple·Google의 확정된 일정 |
| Proposal | 앰버 | 아직 승인되지 않은 HealthMes 변경안 |
| Recovery | 소프트 블루 | 회복 블록과 보호할 시간 |
| Risk | 빨강 | 실제 오류와 위험만 표시 |

녹색은 HealthMes 브랜드 색으로 사용하지 않는다. 색은 장식이 아니라 역할을
전달한다. Calendar와 Proposal을 같은 색으로 표시하지 않으며, 낮은 Capacity를
곧바로 위험으로 표현하지 않는다. Capacity는 신호등처럼 색을 바꾸기보다 같은
블루 계열의 양과 명도로 표현한다.

기본 토큰:

```text
brand        #E34A26
brand-deep   #B73319
data         #3D6FD6
proposal     #B8520F
canvas       #F8F4ED
surface      #FFFDF9
ink          #20242C
```

## 3. 플랫폼 규칙

### iPhone

- Overview는 결론, Capacity, 오늘 일정, 결정 한 건의 순서로 유지한다.
- 카드 반경, 경계선, 그림자를 하나의 Surface 문법으로 통일한다.
- 둥근 Display 서체 남용을 제거하고 시스템 본문 서체의 가독성을 우선한다.
- Agent composer는 화면 하단의 지속적인 제어 장치로 보이게 한다.
- Sidebar는 본문과 분리되는 중성 잉크색 Workspace rail로 표현한다.

### Mac

- iPhone과 같은 의미 색상과 Surface를 사용한다.
- Sidebar는 탐색 영역, Canvas는 판단 영역으로 명확히 분리한다.
- 넓은 화면에서 카드 수를 늘리기보다 Calendar와 Insight의 비교 면적을 확보한다.

### Apple Watch

- 결론 한 개, 핵심 이유 한 줄, 변경 전후, Yes/No를 한 화면 흐름으로 구성한다.
- 승인 버튼은 Decision 블루를 사용한다.
- 알림 액션은 실제 계약인 `No → Yes → Speak` 순서로 등록한다. 첫 액션은
  비파괴적인 `No`로 유지해 우발 승인을 막고, 42 mm 화면에서는 핵심 결정인
  No/Yes가 음성 대안 입력보다 먼저 보이게 한다.
- `Speak`는 선라이즈 브랜드 색을 사용하고, 받아쓴 내용을 검토한 뒤 iPhone
  HealthMes 명령 파이프라인으로 전달한다.
- 추가 근거는 `Why?`를 통해 스크롤 화면으로 분리한다.

### Web

- 웹만 별도 제품처럼 보이지 않도록 Apple 앱과 의미 색상을 공유한다.
- Sidebar는 중성 잉크색 Workspace rail, 본문은 웜 뉴트럴 Canvas로 분리한다.
- Advanced와 원시 데이터는 기본 화면의 시각 우선순위를 침범하지 않는다.
- 계절 풍경, 달, 별, 날씨 이모지는 기본 dashboard에서 제거한다.

## 4. 참고와 독자성

Toss Consumer UX Guide의 명확한 CTA·장식 절제·다크패턴 방지, Radix Colors의
역할별 단계, Material 3와 Adobe Spectrum의 semantic color, Carbon의 데이터
시각화 구분, Apple Human Interface Guidelines의 플랫폼 밀도와 접근성을
참고했다. Wellness 브랜드 사례는 녹색이나 의료 상징을 답습하지 않고 활력,
생활감, 개인성을 표현하는 방향만 참고했다. 외부 제품의 색상, 컴포넌트 코드,
그래픽 자산은 복사하지 않았다.

HealthMes 고유 요소는 다음 조합이다.

```text
Capacity + Calendar Load + Proposal State + Decision Outcome
```

## 5. 롤백

이번 Visual Design Refinement는 하나의 Git commit으로만 제공한다. 기능
변경과 섞지 않기 때문에 해당 커밋 하나를 revert하면 이전 PR #111 UI로
돌아갈 수 있다.

## 6. 검증

- iPhone 13 mini simulator용 앱과 포함된 Watch target을 빌드했다.
- Apple Watch Series 10 42 mm simulator용 앱을 빌드하고 공식 결정
  notification payload를 전달했다.
- macOS 앱을 빌드했다.
- iPhone의 Light/Dark appearance에서 고정된 밝은 배경과 동적 텍스트가
  충돌하지 않도록 Canvas, Surface, 경계선을 동적 토큰으로 검증했다.
- Web은 기존 API와 template 구조를 유지하고 CSS include만 변경했다.
- `git diff --check`로 whitespace 오류가 없음을 확인했다.

UI 자동화 전체 재실행은 로컬 디스크 여유 공간 부족으로 중단되었다. 이는
Swift compile 오류가 아니며, 같은 소스의 iPhone/Watch 및 macOS application
build는 성공했다. 실제 기기의 알림 확장, Dynamic Type, VoiceOver 순서는
기존 Live QA 절차에서 최종 확인한다.
