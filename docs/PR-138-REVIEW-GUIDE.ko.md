# PR #138 구현 및 코드 리뷰 가이드

> **작성일:** 2026-08-22
>
> **대상:** PR #138 `feat: add HealthMes Decision Agent and unified wellness inputs`
>
> **제품 코드 기준점:** `1acd2221`
>
> **이번 갱신 이전 리뷰 문서 기준점:** `c8640707`
>
> **최신 main 비교 기준:** `103b7269`이며 아직 PR #138에 병합하지 않았다.
> 2026-08-22 현재 GitHub 상태는 `CONFLICTING / DIRTY`다.
>
> **목적:** Sake와 후속 리뷰어가 PR의 대목표, 실제 실행 경로, 저장 경계,
> 핵심 불변조건과 검증 지점을 빠르게 확인하도록 한다.

## TLDR

PR #138은 HealthMes의 자유 형식 wellness 판단 경로를 하나로 통합했다.

```text
REST / Channel / Proactive / Scheduled
                  |
                  v
      HealthMesDecisionService
                  |
                  v
       Hermes /v1/responses
                  |
        하나의 LLM/tool loop
                  |
                  v
         HealthMes MCP 6 tools
                  |
      +-----------+-----------+-----------+
      |           |           |           |
  Activity    Nutrition    Calendar    Wearable
      |           |           |           |
      +-----------+-----------+-----------+
                  |
                  v
      source_refs 검증과 조건부 저장
```

LLM은 질문에 필요한 domain, 기간, capability와 추가 조회를 자율적으로 고른다.
HealthMes 코드는 조회 허용 범위, 보존기간, 시간대, 정확한 계산, provenance와
저장을 소유한다. Hermes는 이 판단을 실행하는 교체 가능한 runtime이며
`vendor/hermes-agent/`는 수정하지 않았다.

중요한 경계가 하나 있다. **한 부모 LLM은 MVP의 조회 계획과 종합에는 충분하지만,
엄밀한 데이터 조회를 혼자 보장하지는 않는다.** 정확한 행, 기간, 중복 제거,
freshness, retention과 `source_refs`는 HealthMes의 결정론적 조회 코드가
보장한다.

## 1. 아키텍처가 어떻게 바뀌었나

### #138 이전

```text
경로 A
사용자 -> Hermes + Skill -> 여러 MCP/전용 도구 -> Skill이 판단 저장 요청

경로 B
호출자 -> 고정 question_kind -> 고정 domain resolver -> context만 반환
```

이 구조에서는 질문 해석, 자료 선택, 최종 저장의 주인이 분산됐다. Skill별 전용
도구와 direct Open Wearables 접근을 계속 추가하면 같은 wellness 질문이 서로 다른
검증·저장 규칙을 타게 된다.

### #138 이후

```text
사용자/채널
    |
    v
HealthMesDecisionService
    |
    v
Hermes의 단일 LLM/tool loop
    |
    v
HealthMes MCP의 4개 domain search + 2개 Skill catalog 도구
    |
    v
Context Access Layer -> 결정론적 Domain Provider
    |
    v
ContextResult + source_refs
    |
    v
LLM 종합 -> DecisionFinalizer 검증 -> 필요한 경우만 compact 저장
```

| 경계 | 이전 | #138 |
|---|---|---|
| 자유 형식 판단 입구 | Hermes 경로와 HealthMes resolver가 분리 | `HealthMesDecisionService` 하나 |
| 자료 선택 | Skill별 절차 또는 고정 `question_kind` | 하나의 LLM이 질문별로 자율 선택 |
| 실제 조회 | 혼합 MCP와 전용 도구 | HealthMes MCP의 typed domain search |
| Open Wearables | Hermes에 직접 노출 가능 | Wearable Provider 뒤 bounded reader |
| 판단 저장 | Skill이 범용 writer 호출을 기억해야 함 | `DecisionFinalizer`만 조건부 저장 |
| 출처 검증 | 도구별로 상이 | canonical trace와 `source_refs` 공통 검증 |

## 2. 사용자 요구와 구현 결과

| 요구 | PR #138 구현 |
|---|---|
| 고정 질문 표가 아닌 자연어 기반 판단 | Hermes LLM이 허용된 HealthMes MCP 도구를 자율·반복 선택 |
| Activity·Nutrition·Calendar·Wearable 결합 | 네 domain의 typed search tool과 Provider 구현 |
| HealthMes가 제품 두뇌를 소유 | 단일 `HealthMesDecisionService` ingress와 finalizer 유지 |
| Skill 기반 전문 지침 | 검토된 Skill catalog를 읽기 전용 MCP 도구로 제공 |
| 실제 사용 데이터 추적 | canonical search trace와 `source_refs` 검증 |
| 불필요한 질문 기록 방지 | 단순 조회는 미저장, 행동·위험·명시 추적만 compact 저장 |
| 입력을 한곳에서 설정 | `/v1/inputs` descriptor와 ETag/`If-Match` CAS |
| 데이터별 보존기간 | `1d/7d/14d/30d/90d/forever` data-class policy |
| iPhone Screen Time 수준 activity | eligible build용 aggregate collector/sync/outbox와 activity 저장 연결 |
| UI와 Hermes vendor 격리 | UI 미구현, `vendor/hermes-agent/` 변경 없음 |

## 3. 실제 조회 책임

```text
LLM
  질문의 의미를 해석하고 필요한 자료를 선택
        |
        v
Skill
  필요할 때 읽는 검토된 업무 설명서
        |
        v
MCP search tool
  LLM의 구조화된 조회 요청을 HealthMes로 전달
        |
        v
Context Access Layer
  요청 범위, retention, privacy, timezone와 budget 확인
        |
        v
Provider Registry
  capability를 담당 Domain Provider에 결정론적으로 연결
        |
        v
Domain Provider
  DB/mirror/upstream adapter를 조회하고 정확한 수치 계산
        |
        v
ContextResult + source_refs
        |
        v
LLM
  추가 조회 또는 최종 종합
```

### LLM이 담당하는 것

- 사용자 질문의 목적 해석
- 어느 Skill을 읽을지
- 어느 domain과 capability를 조회할지
- 결과를 본 뒤 다른 자료가 필요한지
- 여러 domain의 trade-off와 최종 설명

### HealthMes 코드가 담당하는 것

- capability와 Provider의 정확한 연결
- 보존기간과 시간대 경계
- domain별 정확한 집계와 단위
- 누락, stale, partial coverage 표현
- 실제 source와 최종 used ref의 일치
- 최종 결과의 조건부 persistence

### 현재 하지 않는 것

- Provider 내부 subagent spawn
- LLM의 직접 SQL/DB/filesystem 접근
- Hermes의 direct Open Wearables MCP 접근
- 각 domain별 별도 최종 판단 agent
- `question_kind -> 고정 domain 목록`을 주 경로로 사용

### 단일 LLM이 충분한 범위

| 항목 | 한 부모 LLM | HealthMes 코드 |
|---|---|---|
| 질문 의도와 필요한 domain 추정 | 담당 | 고정 표로 대신하지 않음 |
| 여러 도구를 어떤 순서로 호출할지 | 담당 | 허용 도구와 호출 예산만 제한 |
| 정확한 DB/API 행 선택 | 직접 담당하지 않음 | Provider가 결정론적으로 담당 |
| retention·timezone·중복·단위 | 신뢰하지 않음 | Access Layer와 Provider가 담당 |
| 사용 출처의 진위 | 최종 ref를 선언 | canonical trace와 finalizer가 검증 |
| 여러 영역의 의미와 최종 설명 | 담당 | strict 결과 계약을 검증 |

따라서 엄밀성은 다음 합성 결과다.

```text
LLM의 조회 계획
  + 결정론적 domain query
  + access/retention/freshness 검사
  + source_refs 재검증
  = 검증 가능한 wellness 판단
```

현재 남은 품질 위험은 **retrieval-plan completeness**다. 즉 임의의 자연어 질문에서
LLM이 관련 domain을 모두 떠올리는지는 확률적이며, 평가 fixture와 tool-call
telemetry로 측정해야 한다. 반면 선택된 도구가 정확한 범위와 출처를 반환했는지는
코드가 검증한다. 측정 결과 실제 누락이 확인되기 전에는 subagent를 필수 구조로
추가하지 않는다.

## 4. Skill과 데이터 도구

Decision profile에 보이는 도구는 정확히 여섯 개다.

```text
데이터:
  search_activity
  search_nutrition
  search_calendar
  search_wearable

지침:
  list_wellness_skills
  read_wellness_skill
```

Skill은 데이터를 담은 별도 저장소가 아니며 도구를 직접 호출하는 실행 코드도 아니다.
LLM은 질문에 전문 절차가 필요하면 Skill 목록에서 관련 Skill을 골라 읽고, 그 내용을
참고해 같은 LLM turn에서 필요한 `search_*` 도구를 선택한다.

```text
"이 커피를 마셔도 될까?"
  -> LLM이 caffeine/nutrition Skill을 읽음
  -> Skill이 필요한 확인 항목을 안내
  -> LLM이 nutrition ledger/candidate 조회
  -> 필요하면 wearable sleep, activity, calendar 추가 조회
  -> LLM이 최종 DecisionDraft 생성
```

간단한 질문은 Skill을 읽지 않고 바로 검색할 수 있다. Skill을 읽었다는 이유만으로
모든 관련 domain을 조회하지도 않는다.

## 5. 여러 입력을 조회할 때

Hermes `/v1/responses` 호출은 한 번이지만 그 내부 transcript에는 여러
`function_call -> function_call_output` 쌍이 들어갈 수 있다.

```text
function_call: search_nutrition
function_call_output: nutrition context

function_call: search_wearable
function_call_output: wearable context

function_call: search_activity
function_call_output: activity context

final message: healthmes.decision-draft.v2
```

도구 호출은 중간 과정이고 최종 출력은 strict `DecisionDraft` 하나다. 현재 search
session은 canonical trace와 policy 일관성을 위해 호출을 직렬화한다. 향후 병렬
retrieval subagent는 #193에서 별도로 검토한다.

## 6. 저장 경계

```text
HealthMes Personal Data Node
|
+-- HealthMes DB
|   +-- Activity WellnessEvent
|   +-- Nutrition/caffeine events와 confirmation
|   +-- CalendarEventMirror
|   +-- normalized wearable snapshots/provenance
|   +-- input settings, retention, cursors, source refs
|   +-- 필요한 경우만 compact DecisionRecord
|
+-- Open Wearables DB
|   +-- 상세 수면, workout, health score, timeseries 원본
|
+-- HEALTHMES_DATA_DIR
|   +-- 사진, 음성, raw ingest와 큰 object
|
+-- Hermes runtime state
    +-- request-scoped transcript와 runtime metadata
```

한 MCP라는 말은 한 DB나 한 테이블이라는 뜻이 아니다. LLM이 보는 제품 조회 입구가
하나라는 뜻이다. Wearable Provider는 보통 HealthMes의 정규화 mirror를 사용하고,
상세 질문에서는 bounded Open Wearables reader를 호출한 뒤 필요한 결과와
provenance만 HealthMes 계약으로 반환한다.

## 7. iPhone Screen Time과 입력 설정

iPhone Screen Time은 별도 wellness domain이 아니라 Activity domain의 collector다.

```text
Apple authorization
  -> 완료된 local-hour aggregate
  -> app identity 가명화와 제외 앱 제거
  -> bounded offline outbox
  -> HealthMes activity ingest
  -> Activity Provider 검색
```

eligible opt-in build의 authorization-triggered first sync, foreground catch-up,
best-effort background refresh와 retry/outbox는 구현되어 있다. 실제 배포에는 Apple
App & Website Usage entitlement 승인, signing/provisioning, 권한 UI와 real-device
dogfood가 필요하다. 이 외부 조건을 완료했다고 주장하지 않는다.

입력 설정은 UI-neutral API로 제공한다.

```text
GET  /v1/inputs
GET  /v1/inputs/{source_id}
PUT  /v1/inputs/{source_id}/settings
```

데스크톱과 모바일의 오래된 설정 덮어쓰기는 ETag/`If-Match` CAS로 방지한다.

## 8. 최신 main 충돌과 통합 방안

### 현재 확인된 충돌

공통 조상 `d89b314a` 기준으로 #138과 최신 `main`이 함께 수정한 파일은 11개다.
Git merge가 직접 표시한 텍스트 충돌은 다음 6개다.

```text
README.md
healthmes/mcp_server/server.py
tests/glue/test_bootstrap.py
tests/glue/test_skills_docs.py
tests/mcp_server/test_tools_store.py
tests/store/test_alembic.py
```

자동 병합되더라도 의미 검토가 필요한 공통 수정 파일은 다음 5개다.

```text
healthmes/store/models.py
skills/healthmes-sleep/SKILL.md
tests/api/test_decisions.py
tests/mcp_server/test_server_app.py
tests/store/test_models.py
```

`main`이 추가한 중요한 제품 변경은 다음과 같다.

- Open Wearables의 WHOOP Cycle `day_strain` 수집·정규화
- WHOOP recovery와 같은 Cycle의 day strain을 결합하는 전용 context
- `healthmes-whoop-recovery` Skill
- 범용 decision writer에 별도 `evidence_refs` 저장
- Alembic revision `f4a5b6c7d8e`
- 이중 라이선스와 README 변경

### 가장 큰 의미적 충돌

```text
최신 main
WHOOP Skill
  -> WHOOP 전용 context 도구
  -> Skill이 범용 decision writer 직접 호출

#138
HealthMesDecisionService
  -> Hermes 단일 LLM loop
  -> search_wearable
  -> 공통 source_refs 검증
  -> DecisionFinalizer만 저장
```

두 방식을 그대로 남기면 같은 질문에 대해 조회 도구와 저장 주체가 둘이 된다.
이는 #138이 제거한 이중 reasoning/persistence 경로를 되살린다.

또한 현재 #138 코드 자체에는 다음 두 WHOOP 공백이 있다.

- bounded wearable health-score category에 `day_strain`이 없다.
- `wearable.recovery`는 HRV, charge와 어제 load를 반환하지만 같은 WHOOP Cycle의
  Recovery + 현재 day strain package를 정확히 반환하지 않는다.

이는 #138의 단일-runtime 설계가 잘못됐다는 뜻이 아니라, #138이 갈라진 뒤
`main`에 추가된 WHOOP 기능을 아직 공통 Provider 계약으로 옮기지 않았다는 뜻이다.

### 병합 시 해결 원칙

```text
healthmes-whoop-recovery Skill
  -> LLM이 Skill catalog에서 읽음
  -> search_wearable(
       capability="wearable.whoop-recovery-package"
     )
  -> WearableContextProvider
  -> Recovery + day_strain + Cycle linkage + source_refs
  -> LLM DecisionDraft
  -> #138 DecisionFinalizer
```

1. Open Wearables의 WHOOP 수집, 정규화, freshness, Cycle matching과 fail-closed
   계산은 유지한다.
2. #138 wearable allowlist와 typed contract에 `day_strain`을 추가한다.
3. WHOOP 전용 계산은 `WearableContextProvider`의 capability로 옮긴다.
4. 일곱 번째 제품 decision tool을 추가하지 않고 기존 `search_wearable`을
   사용한다.
5. WHOOP Skill은 조회 절차를 설명하되 판단을 직접 저장하지 않는다.
6. provenance는 #138의 private `decision_payload.source_refs`와 source
   attestation에 통합한다. 별도 `evidence_refs` 저장 경로를 중복 추가하지 않는다.
7. `e3f4a5b6c7d8`에서 갈라진 Alembic 계보는 후속 adapted revision 또는 merge
   revision으로 단일 head를 만든다.
8. 라이선스와 README 변경은 보존하되 #138의 단일 ingress 설명과 충돌하지 않게
   합친다.

### #138의 현재 문제를 정확히 요약하면

| 문제 | 성격 | 해결 |
|---|---|---|
| 최신 `main`과 Git 충돌 | 통합 미완료 | 위 6개 텍스트 충돌을 의도 기반으로 해결 |
| WHOOP day strain 미지원 | 기능 격차 | 공통 wearable capability로 흡수 |
| main의 Skill 직접 저장 | 아키텍처 회귀 위험 | `DecisionFinalizer` 단일 writer 유지 |
| 별도 `evidence_refs` 컬럼 | provenance 중복 위험 | 기존 private payload/attestation으로 통합 |
| LLM이 관련 domain을 빠뜨릴 가능성 | 모델 품질 위험 | eval·telemetry 추가, 필요 시 #193 |

이 문서 변경에서는 `main`을 실제로 병합하거나 제품 코드를 수정하지 않는다.
따라서 문서 반영 후에도 PR #138의 GitHub merge 상태는 충돌 해결 커밋이 들어가기
전까지 `CONFLICTING`이다.

## 9. Sake 권장 리뷰 순서

1. **단일 제품 진입점**
   - `healthmes/api/wellness_decisions.py`
   - `healthmes/decision/service.py`
   - `healthmes/decision/engine.py`
2. **Hermes 단일 LLM/tool loop**
   - `healthmes/decision/responses.py`
   - `healthmes/decision/hermes_profile.py`
   - `config/hermes-decision-config.yaml.tmpl`
3. **실제 검색 경로**
   - `healthmes/mcp_server/domain_search.py`
   - `healthmes/decision/search.py`
   - `healthmes/decision/access.py`
   - `healthmes/decision/providers.py`
   - `healthmes/decision/domain_providers.py`
4. **Skill catalog**
   - `healthmes/mcp_server/wellness_skills.py`
   - `skills/healthmes-wellness-decision/SKILL.md`
   - `skills/healthmes-caffeine/SKILL.md`
   - `skills/healthmes-nutrition-decision/SKILL.md`
5. **결과 검증과 저장**
   - `healthmes/decision/finalizer.py`
   - `healthmes/store/decision_records.py`
   - `healthmes/store/decision_receipts.py`
6. **입력과 저장 경계**
   - `healthmes/api/inputs.py`
   - `healthmes/inputs/`
   - `healthmes/activity/`
   - `healthmes/nutrition/`
   - `healthmes/wearables/`
7. **대표 테스트**
   - `tests/decision/test_e2e.py`
   - `tests/decision/test_responses.py`
   - `tests/decision/test_search_sessions.py`
   - `tests/decision/test_providers.py`
   - `tests/api/test_wellness_decisions.py`
   - `tests/glue/test_single_wellness_runtime_repository.py`

## 10. 리뷰 핵심 불변조건

- 외부 자유 형식 reasoning ingress는 하나여야 한다.
- Hermes가 LLM/tool loop를 실행하지만 HealthMes가 제품 정책과 데이터를 소유해야
  한다.
- decision profile에는 위 여섯 read-only HealthMes MCP tool만 보여야 한다.
- Skill은 지침이고 Provider는 조회 코드다.
- Provider가 최종 wellness 결론을 만들거나 agent를 spawn하면 안 된다.
- direct Open Wearables MCP, native Hermes tools와 mutation tools가 decision
  profile에 노출되면 안 된다.
- tool transcript와 canonical search trace가 정확히 일치해야 한다.
- 최종 `used_source_ref_ids`는 실제 반환 ref의 부분집합이어야 한다.
- 단순 조회는 DecisionRecord를 만들지 않아야 한다.
- UI와 `vendor/hermes-agent/`는 이 PR에서 변경하면 안 된다.

## 11. 검증 결과

- macOS lifecycle: `166 passed, 1 skipped`
- 격리 Linux/procps-ng: targeted `2 passed`, full lifecycle `167 passed`
- GitHub Ubuntu/PostgreSQL: `4221 passed, 2 skipped`
- GitHub macOS/SQLite: `4123 passed, 100 skipped`
- iOS/watchOS: `141 tests, 2 skipped, 0 failures`
- macOS native: `26 tests, 0 failures`
- Android Gradle build/tests: 성공
- Compose, Ruff, `bash -n`, `git diff --check`, Alembic render: 통과
- 전체 기능 diff 독립 GPT-5.6 Sol xhigh 리뷰:
  `High 0 / Medium 0 / Low 0 / PASS`
- 이번 아키텍처·main 충돌 문서 갱신:
  canonical docs/glue targeted `23 passed`, `git diff --check` 통과

## 12. 후속 작업

- 검색 전용 bounded subagent: #193
- GPS/location input: #158
- Apple entitlement, signing과 실제 iPhone dogfood
- 디바이스 UI
- hosted mobile-only Personal Data Node
- 실시간 multi-master 기기 동기화

검색 subagent는 PR #138의 완료 조건이 아니다. 현재 단일 LLM 반복 조회가 기준선이며,
#193은 복잡한 장기·다중 domain 검색에서 측정 가능한 이득이 있을 때만 선택적으로
도입한다.
