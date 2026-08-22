# PR #138 구현 및 코드 리뷰 가이드

> **작성일:** 2026-08-22
>
> **대상:** PR #138 `feat: add HealthMes Decision Agent and unified wellness inputs`
>
> **코드 기준점:** `1acd2221` 이후 제품 코드 변경 없이 리뷰 문서만 추가·최신화한 상태
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

## 1. 사용자 요구와 구현 결과

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

## 2. 실제 조회 책임

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

## 3. Skill과 데이터 도구

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

## 4. 여러 입력을 조회할 때

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

## 5. 저장 경계

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

## 6. iPhone Screen Time과 입력 설정

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

## 7. Sake 권장 리뷰 순서

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

## 8. 리뷰 핵심 불변조건

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

## 9. 검증 결과

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

## 10. 후속 작업

- 검색 전용 bounded subagent: #193
- GPS/location input: #158
- Apple entitlement, signing과 실제 iPhone dogfood
- 디바이스 UI
- hosted mobile-only Personal Data Node
- 실시간 multi-master 기기 동기화

검색 subagent는 PR #138의 완료 조건이 아니다. 현재 단일 LLM 반복 조회가 기준선이며,
#193은 복잡한 장기·다중 domain 검색에서 측정 가능한 이득이 있을 때만 선택적으로
도입한다.
