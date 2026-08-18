# Apple/Web UI 검증 증거

PR #111의 결정적 demo fixture 출력은 다음 파일에 저장한다.

- `artifacts/apple-unified-dashboard/iphone-today.png`
- `artifacts/apple-unified-dashboard/watch-42mm.png`
- `artifacts/apple-unified-dashboard/macos-dashboard.png`
- `artifacts/apple-unified-dashboard/web-dashboard.png`

## 화면별 계약

| 화면 | 기본 정보량 | 핵심 조작 | 상세 정보 |
|---|---|---|---|
| iPhone | 두 줄 결론, 시각화 최대 1개, 실제 시간 블록, 제안 1개 | 음성/텍스트, 카메라, Yes/No | 정확한 Web 문맥 |
| Watch 42 mm | 결론 1개, 이유 1개, 확인 가능한 변경 시간 | No/Yes | Why 또는 iPhone |
| Mac | iPhone과 동일한 핵심 기능, 넓은 일정/비교 | 음성/텍스트, 키보드, Yes/No | inspector와 Web |
| Web | 캘린더, 근거, 추이, 결과 | 승인 전 미리보기 | Advanced |

## 접근성 증거

- 모든 핵심 텍스트는 SwiftUI semantic font를 사용하므로 Dynamic Type에
  참여한다.
- iPhone command dock, generated scene, 식사 입력, 캘린더 블록,
  Watch Yes/No에 명시적 accessibility label 또는 summary가 있다.
- 생성형 시각화는 도형 자체가 아니라 데이터 의미를 읽는 대체 텍스트를
  제공한다.
- Mac은 `Shift-Command-Space` 음성 호출, command field focus,
  action keyboard flow를 제공한다.
- iPhone과 Mac의 scene 전환 애니메이션은 Reduce Motion에서 제거된다.
- Web Advanced는 기본적으로 접혀 있고 키보드 focus와 narrow viewport
  동작을 테스트한다.

실제 VoiceOver 읽기 순서, 실제 기기 Dynamic Type 최대 크기, 색상 필터,
Switch Control은 `APPLE-REAL-DEVICE-QA.ko.md` 절차에서 최종 확인한다.
자동화와 시뮬레이터 증거를 실기기 접근성 검증으로 과장하지 않는다.

## 자동 검증 결과

- Python 전체: `1854 passed, 14 skipped`
- Dashboard/Schedule API 집중 검증: `50 passed`
- iOS unit: `124 passed, 0 failed`
- iPhone UI: `5 total`, `2 passed`, live fixture가 필요한 `3 skipped`,
  `0 failed`
- macOS unit: `57 passed, 0 failed`
- Apple Watch Series 10 42 mm simulator build: 성공
- Pairing/setup 집중 검증: `26 passed`
- Ruff: 성공
- `git diff --check`: 성공

UI 테스트의 skip은 성공으로 위장하지 않는다. 실제 nutrition provider,
실제 actionable proposal, 실제 Apple/Google 계정이 필요한 시나리오는
`APPLE-REAL-DEVICE-QA.ko.md`의 Live QA에서 완료해야 한다.

## 시각 검수 메모

- Visual Design Refinement는 Capacity 청록, Calendar 파랑, Proposal 호박색,
  Recovery 녹색의 의미 색상을 iPhone, Mac, Watch, Web에 공통 적용한다.
- iPhone의 Canvas와 Surface는 Light/Dark appearance에 대응하는 동적
  색상을 사용한다. Mac은 기존 제품 계약대로 Light appearance를 유지한다.
- Watch 알림 액션은 `No → Yes → Alternative` 순서다. 첫 액션은 계속
  비파괴적인 No이므로 Double Tap 우발 승인을 막고, 42 mm에서는 No/Yes를
  대안 입력보다 우선한다.
- iPhone은 `2026-08-10` America/Los_Angeles fixture를 사용하며 실제
  Google 일정 `Team sync`를 시간 블록으로 표시한다.
- iPhone의 마지막 분석 시각은 기기 시간대가 아니라 WellnessScene의
  서버 시간대로 렌더링한다.
- Watch 42 mm는 결론, 이유, 확인 가능한 제안 시간, No/Yes를 첫 화면에
  유지한다. 서버의 correlated decision card에 `before`가 있으면
  `기존 시간 → 제안 시간`을 표시한다. 현재 저장된 demo fixture에는
  `before`가 없어 제안 구간만 표시하며, 이를 변경 전/후 증거로
  과장하지 않는다.
- Web desktop은 실제 캘린더, 에너지 막대, 제안 미리보기와 접힌
  Advanced를 함께 표시한다.
- Web compact breakpoint는 500 px에서 제목, 세 관점, 차트, 고정
  command dock이 가로로 잘리지 않는 것을 확인했다.
- macOS 이미지는 동일한 결정적 demo contract를 보여주는 기존 캡처다.
  최신 바이너리의 57개 unit test는 다시 통과했지만 현재 GUI 캡처
  세션은 사용할 수 없어 새 이미지로 교체하지 않았다.

## Visual Design Refinement 재검증

- iPhone 13 mini + embedded Watch targets: build 성공
- Apple Watch Series 10 42 mm target: build 성공
- macOS target: build 성공
- 42 mm 공식 decision notification payload 전달: 성공
- iPhone Light/Dark appearance 수동 렌더 확인: 성공
- `git diff --check`: 성공
- UI automation 전체 재실행: 로컬 디스크 공간 부족으로 중단

마지막 항목을 통과로 기록하지 않는다. 빌드와 수동 simulator 렌더는
성공했지만, 실제 iPhone·Watch에서 알림을 확장했을 때의 버튼 배치와
VoiceOver 순서는 owner Live QA가 최종 증거다.
