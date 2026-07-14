# 도메인 전문가 온보딩 가이드 (한국어)

이 문서 하나로 시작할 수 있게 만든, 헬스케어 도메인 전문가용 안내서입니다.
당신의 역할은 코드가 아니라 **판단 지식**입니다: 중요한 지표를 고르고, 그
지표를 읽는 규칙을 정의하고, 의사결정 절차를 스킬로 말고, 실기기로 검증하는 것.

## 0. 30초 아키텍처 — 내 작업이 어디에 꽂히는가

```
[워치/폰 11개 프로바이더] → open-wearables (데이터 플레인, vendor/ 수정금지)
                                  │ REST (원시·점수 데이터)
                                  ▼
                    HealthMes 플레인  ←←← ❷ 메트릭(도구) 추가는 여기
                    (결정론: baseline·z-score·confidence 계산,
                     MCP 도구 14종, 트리거, 에너지 엔진)
                                  │ MCP (해석된 값만)
                                  ▼
                    Hermes 에이전트 (LLM 판단 루프)  ←←← ❶ 스킬 추가는 여기
                                  │
                    Telegram 알림/대화 + 의사결정 뷰어 웹
```

**철칙 — 결정론 경계**: 숫자 계산(지표·baseline·신뢰도)은 전부 HealthMes의
파이썬 도구가 하고, LLM(스킬)은 그 결과를 **읽고 판단만** 합니다. LLM이
직접 계산하게 하는 스킬은 리뷰에서 반려됩니다 (환각·재현성 문제).

## 1. 지금 사용 가능한 지표 카탈로그 (코드로 검증된 사실)

### 시계열 (~100+ 타입, `get_timeseries`로 조회)

| 카테고리 | 대표 지표 |
|---|---|
| 심혈관 | heart_rate, 안정심박, **HRV(sdnn/rmssd)**, 1분 심박회복, 보행심박 |
| 혈액/호흡 | SpO2, 혈당, 혈압, 호흡수, 폐활량 |
| 체성분 | 체중, 체지방, 골격근, 체온, **수면 중 손목체온** |
| 체력/활동 | VO2max, 걸음, 활동에너지, 운동시간, 6분보행 |
| 보행 품질 | 보행 안정성·비대칭·속도 (낙상 위험 계열) |
| 환경/행동 | **햇빛 노출 시간, 소음 노출, 음주량, 수분 섭취**, 흡입기 사용 |
| 기타 | 심방세동 부담, 인슐린, 생리주기 단계 |

전체 어휘: `vendor/open-wearables/backend/app/schemas/enums/series_types.py`

### 프로바이더별 점수 커버리지 (중요 — 없는 기기가 많음)

| 점수 | 제공 기기 |
|---|---|
| SLEEP | Garmin, Oura, Whoop, Polar, Suunto (5개사) |
| **STRESS / BODY_BATTERY** | **Garmin만** — 타 기기는 내부 회복탄력성 점수로 프록시 |
| READINESS | Oura, Polar |
| RECOVERY | Whoop, Suunto, Polar |
| STRAIN | Whoop, Polar |
| 내부 계산 (기기 무관) | **수면 점수**(시간/단계/일관성/각성 4요소), **회복탄력성**(야간 HRV-CV) |

### 신뢰도 경계 (스킬 작성 시 반드시 반영)

- 손목 HRV는 **야간(수면 중) 측정만** 신뢰 — 주간 스팟 측정은 노이즈
- SDNN(Apple/Ultrahuman)과 RMSSD(그 외)는 **절대 혼용 금지** — baseline 분리됨 (도구가 처리)
- 소비자기기 칼로리는 부정확, Fitbit/Strava는 운동 데이터만(수면·시계열 없음)
- 모든 도구가 `confidence`/`coverage`/`insufficient_data`를 돌려줌 —
  **confidence 낮으면 단정적 조언 금지**가 스킬의 의무

## 2. 스킬 말기 (코드 불필요 — 당신의 주 무기)

임상 질문 하나 = 스킬 하나. 여러 개를 말아 넣는 걸 권장합니다.

1. `docs/templates/SKILL.md`를 복사 → `skills/<스킬-이름>/SKILL.md`
2. 절차 안에서 도구를 **등록명으로** 부르기: `mcp__healthmes__<도구>`,
   `mcp__open_wearables__<도구>` (밑줄 두 개)
3. 필수 규칙: ① REST 직접 호출 지시 금지(MCP만) ② 권고 후 반드시
   `record_decision` ③ confidence 게이트 ④ 알림 문법(관찰→근거→제안) 준수
4. 설치: `uv run python scripts/bootstrap.py` (재실행하면 내용 재동기화)
5. 선제 알림에 연결하려면 `config/hermes-config.yaml.tmpl`의 route
   `skills:` 목록에, 브리핑에 연결하려면 `scripts/bootstrap.py`의
   `BRIEFING_JOBS`에 추가 (엔지니어와 함께)

기존 예시 4종이 최고의 교재입니다: `skills/healthmes-planner/`(가장 풍부),
`healthmes-capture/`, `healthmes-sleep/`, `doctor-visit-summary/`.

## 3. 새 메트릭(도구) 추가 (파이썬 — 엔지니어와 페어 가능)

"기존 도구로 답할 수 없는 임상 질문"이 생기면 Layer B 도구를 추가합니다.
방법은 `docs/EXTENDING.md` §2 — 핵심 계약만 요약:

- 순수 함수 + 해석된 델타 + confidence 반환, 원시 시계열 반환 금지
- 결측은 `insufficient_data`로 정직하게
- vendor가 이미 계산하는 점수(수면·회복탄력성) 재발명 금지
- 손계산 검증 벡터를 `tests/mcp_server/`에 — 이게 영구 계약

직접 구현이 부담이면 **이슈 폼 "Metric proposal"**에 지표 정의·해석
규칙·신뢰 조건만 적어주세요. 구현은 엔지니어링에서 받습니다.

## 4. QA — 두 단계

### 4a. 로컬 QA (크리덴셜 불필요, 오늘 바로 가능)

```bash
make mac-setup && make mac-run     # sqlite로 전체 서비스 기동 (:8100)
```

- **도구 직접 호출** (LLM 없이 지표 검증 — 가장 빠름): `docs/EXTENDING.md`
  §4의 fastmcp 스니펫으로 `get_daily_readiness_context` 등 호출, 픽스처
  데이터로 출력 구조·confidence 동작 확인
- **에이전트 판단 QA**: 터미널에서 `hermes chat -q "..."` (LLM API 키 필요,
  Claude 외 프로바이더도 가능 — `HERMES_MODEL`/`HERMES_PROVIDER`)
- **판단 감사**: 모든 권고는 `http://localhost:8100/decisions`에 트리로
  남음 — "왜 이 판단?"을 지표 근거까지 따라가며 반박하는 것이 QA의 본질
- **회귀 고정**: 맞다고 확인한 케이스는 손계산 벡터로 테스트에 박제

### 4b. 실기기 QA (본인 워치로)

1. **기기 연결**: open-wearables 백엔드에 본인 프로바이더 연동 —
   `make mac-ow`로 기동 후 vendor 문서(`vendor/open-wearables/docs/`,
   개발자 포털·프로바이더 OAuth 설정) 따라 연결. 프로바이더 앱 등록
   (Garmin/Oura 등 developer 계정)이 필요한 경우가 있음 — 엔지니어와 1회 셋업
2. **인입 확인**: 하루 뒤 `GET :8000/api/v1/...` 또는 MCP
   `get_health_scores`로 본인 데이터가 흐르는지 확인
3. **체감 대조 프로토콜** (핵심): 매일 아침 ① 도구가 말하는 readiness/에너지
   점수 기록 ② 본인 체감(1–10) 기록 ③ 2주 후 상관·불일치 케이스 분석 —
   불일치가 곧 지표 개선 이슈
4. **알림 소음 일지**: 받은 선제 알림마다 유용/무시/방해 태깅 — 트리거
   룰·쿨다운 튜닝의 근거 (계획 §11이 지정한 최대 리스크)
5. Android 폰이면 `apps/android-usage/` 수집 앱 설치(README 참조) —
   앱 사용 단편화가 인지에너지 요인으로 활성화됨

## 5. 기여 절차 (GitHub)

- 브랜치 → PR → CI 자동(리눅스+맥) → **상호 리뷰 1명 필수**(main 보호
  규칙) → rebase 머지. 자세한 건 `CONTRIBUTING.md`
- 제안은 이슈 폼으로: **Metric proposal / Skill proposal**
- 주의: `gh` 쓸 때 반드시 `--repo NomaDamas/healthmes-agent`

## 6. 시작 과제 추천 (첫 2주)

1. 기존 스킬 4종 정독 → planner의 배치 룰에 임상 관점 리뷰 코멘트 (이슈로)
2. 본인 워치 연결 + 4b 체감 대조 프로토콜 시작
3. 본인 전문 영역에서 임상 질문 1개 골라 스킬 1개 말기 (예: 수면무호흡
   스크리닝, 과훈련 감지, 혈당 안정성 — 데이터 커버리지는 §1 표에서 확인)
