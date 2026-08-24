# Open Wearables Capability Route Coverage

## 목적

이 문서는 vendored Open Wearables v1 FastAPI route와 HealthMes wearable
capability 경계의 기계 검증 가능한 기준을 정의한다. 기준 소스는 다음
디렉터리의 route decorator 전체다.

```text
vendor/open-wearables/backend/app/api/routes/v1/
```

정확한 분류 데이터는
`healthmes/wearables/open_wearables_routes.py`에 있다. 문서가 아니라 해당
manifest가 source of truth다.

각 exposed route에는 실제 HealthMes runtime capability도 함께 기록한다.
이 매핑은 문서 표에 그치지 않고 `WEARABLE_DETAIL_CAPABILITIES`를 구성하며,
focused test가 upstream route와 capability 연결의 drift를 검사한다.

## 분류 경계

```text
Open Wearables v1 route 115개
        |
        +-- exposed_user_health_read: 11
        |     사용자가 소유한 건강 관측값, 이벤트, 집계 조회
        |
        +-- internal_availability_identity_metadata: 7
        |     연결 여부, provider/data-source coverage, 사용자 식별용 조회
        |
        `-- intentionally_excluded: 97
              인증, 관리자 설정, 쓰기/삭제, 수집, sync 제어,
              webhook, 운영 telemetry
```

`exposed_user_health_read`는 Provider가 즉시 무제한 공개한다는 뜻이 아니다.
HealthMes runtime에서 기간, pagination, payload 크기, 개인정보, 보존기간,
source reference를 별도로 제한해야 한다. 특히 vendor workout route의
sample, zone, GPS route 필드는 명시적으로 요청된 경우에만 privacy gate를
통과시켜야 한다.

`internal_availability_identity_metadata`는 LLM 답변용 건강 데이터가 아니다.
HealthMes가 wearable 입력의 설정·연결·coverage 상태를 판정하고 올바른
Open Wearables user를 선택할 때 내부적으로 사용하는 read-only metadata다.

`intentionally_excluded`는 누락이 아니라 의도적 비노출이다. Decision Agent가
건강 질문에 답하기 위해 API key, OAuth callback, provider sync, webhook,
archival, 사용자 삭제 같은 제어 기능을 호출하지 못하도록 경계를 명시한다.

## 사용자 건강 read

| Module | Method | Path | 이유 |
|---|---|---|---|
| `events` | `GET` | `/users/{user_id}/events/workouts` | 정규화된 workout record |
| `events` | `GET` | `/users/{user_id}/events/sleep` | nap을 포함한 sleep session |
| `events` | `GET` | `/users/{user_id}/events/menstrual-cycles` | menstrual-cycle record |
| `health_scores` | `GET` | `/users/{user_id}/health-scores` | sleep, recovery, readiness 등의 score |
| `summaries` | `GET` | `/users/{user_id}/summaries/activity` | 일별 activity 집계 |
| `summaries` | `GET` | `/users/{user_id}/summaries/sleep` | 일별 sleep 집계 |
| `summaries` | `GET` | `/users/{user_id}/summaries/recovery` | 일별 recovery 집계 |
| `summaries` | `GET` | `/users/{user_id}/summaries/body` | body/vital 집계 |
| `timeseries` | `GET` | `/users/{user_id}/timeseries` | granular biometric/activity sample |
| `vendor_workouts` | `GET` | `/{provider}/users/{user_id}/workouts` | provider 원격 workout 목록 |
| `vendor_workouts` | `GET` | `/{provider}/users/{user_id}/workouts/{workout_id}` | provider 원격 workout 상세 |

### Route와 runtime capability의 연결

```text
GET /events/workouts
    -> wearable.workouts
GET /events/sleep
    -> wearable.sleep-sessions
GET /events/menstrual-cycles
    -> wearable.menstrual-cycles
GET /health-scores
    -> wearable.health-scores
    -> wearable.whoop-recovery-package
GET /summaries/activity|sleep|recovery
    -> wearable.summaries
GET /summaries/body
    -> wearable.body-summary
GET /timeseries
    -> wearable.timeseries
GET /{provider}/workouts
    -> wearable.provider-workouts
GET /{provider}/workouts/{workout_id}
    -> wearable.provider-workout-detail
```

`wearable.readiness`, `wearable.sleep`, `wearable.recovery`,
`wearable.stress`, `wearable.metric-detail`은 HealthMes가 이미 보존한
정규화 snapshot을 읽는 compatibility/domain capability다. upstream route의
직접 노출 capability는 아니지만 Open Wearables source availability가 OFF면
함께 차단한다.

## 내부 availability와 identity metadata

| Module | Method | Path | 이유 |
|---|---|---|---|
| `connections` | `GET` | `/users/{user_id}/connections` | 사용자 provider 연결과 capability |
| `data_sources` | `GET` | `/users/{user_id}/data-sources` | 실제 데이터 source/provider |
| `meta` | `GET` | `/meta/coverage` | provider별 지원 metric |
| `oauth` | `GET` | `/providers` | 설정된 provider availability |
| `summaries` | `GET` | `/users/{user_id}/summaries/data` | 사용자 데이터 inventory와 count |
| `users` | `GET` | `/users` | Open Wearables user 탐색 |
| `users` | `GET` | `/users/{user_id}` | 선택된 user identity 확인 |

97개 제외 route 각각의 정확한 module, method, path, 이유는 manifest에
기록한다. 같은 path라도 module이 다르면 서로 다른 route identity다.

## Runtime availability와 세션 capability

Open Wearables route가 manifest에 있다는 사실과 LLM이 그 capability를
현재 사용할 수 있다는 사실은 다르다. 모델에 보이는 catalog는 통합 Input
Settings와 실제 Open Wearables 연결 상태를 함께 평가해 세션 시작 시 만든다.

```text
통합 Input Settings
        +
Open Wearables endpoint/API key/user mapping
        +
active connection 또는 data source
        |
        v
availability 평가
        |
        v
세션별 frozen capability catalog
        |
        +-- Hermes instructions에 허용 capability만 전달
        |
        `-- MCP 실행 시 catalog 밖 호출 거부
```

availability 상태의 의미와 노출 규칙은 다음과 같다.

| 상태 | 의미 | 세션 catalog | upstream 호출 |
|---|---|---|---|
| `disabled` | Input Settings에서 Open Wearables source가 OFF | 제외 | 금지 |
| `unconfigured` | endpoint, API key 또는 user mapping이 없음 | 제외 | 금지 |
| `disconnected` | active connection과 data source가 모두 없음 | 제외 | 금지 |
| `available` | active connection 또는 data source가 있음 | 포함 | 허용 |
| `degraded` | metadata transport가 실패했지만 유효한 retained snapshot이 있음 | 포함 | retained snapshot 사용 가능 |
| `unavailable` | metadata transport가 실패했고 유효한 retained snapshot도 없음 | 제외 | 금지 |

`decision_access_enabled`는 wearable 도메인 전체의 판단 사용 여부다.
Open Wearables만 끄는 스위치는 source-level `source_enabled`다. 두 스위치
중 하나라도 접근을 허용하지 않으면 Open Wearables capability를 catalog에
넣지 않는다. HealthKit 등 다른 wearable source를 Open Wearables OFF와
함께 끄면 안 된다.

`source_enabled`는 `{owner_principal_id, source_id}` 단위로 저장한다. 다른
사용자의 설정 변경은 현재 사용자의 세션이나 수집 결과를 무효화하지 않는다.
명시적 설정 행이 없으면 하위 호환을 위해 `enabled=true, revision=0`으로
해석한다. 최초 설정 생성 또는 실제 ON/OFF 변경은 revision을 증가시킨다.
동일 값을 다시 저장하는 것은 revision을 증가시키지 않는다.

catalog는 한 decision session 동안 고정하며, availability와
`source_policy_revision`도 함께 동결한다. 세션 시작 후 새 연결이 생겨도 그
세션에는 capability를 추가하지 않는다. 반대로 세션 시작 뒤 source가 OFF,
disconnected 또는 다른 revision으로 바뀌면 catalog에 capability가 남아
있어도 결과를 반환하지 않는다.

```text
세션 시작
  -> availability + source_policy_revision 동결

각 tool 호출 직전
  -> availability metadata 조회 전·후 exact source binding 재검사
  -> 현재 availability와 동결 revision 재검사
  -> 불일치하면 provider를 호출하지 않음

provider 실행 직후
  -> availability와 revision 다시 검사
  -> 실행 중 바뀌었으면 payload/source_refs 폐기

snapshot commit 직전
  -> write lock 안에서 exact source binding 재검사
  -> 불일치하면 observation/query snapshot 모두 저장하지 않음
```

따라서 source를 OFF했다가 곧바로 다시 ON해 현재 상태가 `available`로
돌아오더라도, OFF 이전에 시작한 fetch의 revision은 더 이상 일치하지 않는다.
이 stale fetch는 `open_wearables_source_policy_changed` 또는
`wearable_source_policy_changed`로 거부하며, 기존 retained snapshot
fallback으로 우회하지 않는다. 최초 명시적 설정 행 생성도 revision `0 -> 1`
변경이므로 이미 진행 중이던 기본 설정 fetch를 무효화한다.

FastMCP tool schema 자체는 프로세스 단위의 정적 계약이므로 세션마다 도구를
물리적으로 등록·삭제하지 않는다. 대신 HealthMes가 다음 두 경계를 모두
강제한다.

1. Hermes에는 frozen catalog에 포함된 capability만 알려준다.
2. `search_wearable`은 frozen catalog와 호출 시점 availability를 모두
   검사한다.

## 빈 결과와 degraded fallback

연결 상태와 조회 결과 상태를 혼동하면 안 된다.

```text
available + 정상 조회 + 해당 기간 행 없음
        -> capability 유지
        -> ContextResult.status = partial
        -> payload.status = no_data

degraded + live metadata/API 일시 실패 + 유효 retained snapshot 있음
        -> capability 유지
        -> retained snapshot 반환
        -> freshness/limitations에 fallback 사실 기록
```

`no_data`는 top-level `ContextStatus` enum 값이 아니다. 연결이 끊겼다는
뜻도 아니며, `ContextResult.status=partial`과
`payload.status="no_data"`로 표현하는 "요청 기간에 일치하는 건강 행이
없다"는 정상 결과다. 새 live 빈 결과는 `no_data`로 기록한다. 과거 snapshot에
저장된 `empty_success`는 마이그레이션 없이 계속 읽되, 외부 결과에서는
동일한 빈 결과 의미로 정규화한다.

degraded fallback은 아무 snapshot이나 반환하는 우회가 아니다. 보존기간,
owner, capability, query scope가 일치하고 아직 읽을 수 있는 retained
snapshot만 사용할 수 있다. 반환 결과에는 원래 source reference와 snapshot
freshness가 유지되어야 한다.

source policy revision이 바뀐 fetch에는 degraded fallback을 적용하지 않는다.
transport 장애는 과거의 허용된 snapshot으로 안전하게 대체할 수 있지만,
사용자가 source를 끈 경우나 OFF 후 다시 켠 경우에는 이전 권한 상태에서
시작된 결과를 되살리면 안 되기 때문이다.

`unavailable`은 최초 metadata 장애를 임의로 degraded로 승격하지 않는
fail-closed 상태다. 연결이 해지된 뒤 과거 `data_source`가 남아 있어도
현재 active connection ID와 일치하지 않으면 연결로 인정하지 않는다.
`user_connection_id=null`인 일회성 import는 upstream 계약상 연결에 묶이지
않으므로 별도 유효 source로 인정한다.

## 완전성 및 granular 데이터 표시

HealthMes는 "일부 행을 명확히 잘랐다"와 "upstream 내부 동작 때문에 전체인지
증명할 수 없다"를 서로 다른 limitation으로 표현한다.

| 표시 | 의미 | 대표 사례 |
|---|---|---|
| `wearable_upstream_page_limit_reached` | continuation 또는 page/row 상한 때문에 남은 행이 있음을 확인함 | cursor/offset/token pagination 중단 |
| `wearable_upstream_completeness_unverified` | 반환된 행을 잘랐다고 단정할 수는 없지만 요청 기간 전체를 upstream이 완전히 반환했는지 검증할 수 없음 | Garmin의 24시간 초과 summary window 내부 chunk |
| `wearable_granular_data_truncated` | 한 workout 안의 sample, zone, route 또는 sleep-stage 세부 행을 HealthMes 상한으로 제한함 | Polar sample/zone/route, sleep stage interval |

Garmin workout 조회가 24시간을 초과하면 Open Wearables가 내부적으로 기간을
chunk 처리하지만 그 내부 chunk의 완전성을 HealthMes가 관찰할 수 없다.
따라서 실제 continuation이나 row 상한이 없는 경우
`truncated=true`로 오표시하지 않고
`wearable_upstream_completeness_unverified`만 반환한다. 반대로 replay할 수
없는 continuation token, offset/page 상한 또는 최대 행 수 도달은 실제
`truncated`로 처리한다.

Polar의 `samples`, `zones`, `route`는 사용자가 명시적으로 요청하고 identity
privacy를 허용한 경우에만 조회한다. 세부 행이 잘리면 정규화 record의
`granular_truncated=true`와 결과 limitation을 함께 유지해 LLM이 부분
데이터를 전체 데이터처럼 설명하지 못하게 한다.

## 국소 verification 계약

세션/availability focused test는 다음 경계를 독립적으로 검증한다.

| 검증 | 준비 상태 | 기대 결과 |
|---|---|---|
| catalog omission | `disabled`, `unconfigured`, `disconnected` 각각으로 세션 시작 | wearable capability가 handle/snapshot/runtime catalog에 없음 |
| stale call rejection | `available`로 세션 시작 후 호출 전에 OFF 또는 disconnected로 변경 | provider/upstream 미호출, 안정된 denial code 반환 |
| source race rejection | provider 실행 중 source가 OFF 또는 disconnected로 변경 | 계산된 provider 결과와 source refs를 반환하지 않고 denial |
| revision race rejection | 세션 또는 provider 실행 중 source가 OFF 후 ON되어 다시 `available` | revision 불일치로 stale payload/source refs 폐기 |
| snapshot write fence | fetch와 snapshot commit 사이 source revision 변경 | observation/query snapshot 미저장, stale 정책 limitation 반환 |
| owner isolation | 다른 owner의 source 설정 변경 | 현재 owner의 fetch와 snapshot에는 영향 없음 |
| available empty | `available` 상태에서 정상 API가 빈 행 반환 | capability 유지, `ContextStatus.PARTIAL` + payload `no_data` |
| degraded fallback | retained snapshot 저장 후 metadata/live transport 실패 | capability 유지, retained 결과와 provenance 반환 |
| metadata unavailable | retained 이력 없이 metadata transport 실패 | capability 제외, stable unavailable denial |
| revoked source | 해지 connection에 연결된 과거 data source만 존재 | capability 제외, `disconnected` |
| completeness semantics | Garmin 장기 window 또는 Polar granular 상한 | `truncated`와 `completeness_unverified`를 구분해 limitation 전파 |

테스트는 내부 구현 필드가 아니라 다음 공개 동작을 기준으로 작성한다.

1. `DecisionSearchSessionHandle` 또는 동등한 공개 session contract에서 모델에
   허용된 capability를 확인할 수 있어야 한다.
2. `DecisionContextSearchSessionService.search()`는 session의 frozen catalog
   밖 capability를 provider 호출 전에 거부해야 한다.
3. catalog에 있던 capability도 호출 시점 source availability가 바뀌면
   provider 호출 전에 거부해야 한다.
4. 빈 live 응답과 transport 실패를 구분하고, transport 실패에서만 조건을
   만족하는 retained fallback을 사용해야 한다.

전체 저장소 CI 대신 변경 경계에 한정한 focused command를 실행한다.

```bash
uv run pytest tests/wearables/test_open_wearables_availability.py
uv run pytest tests/decision/test_search_sessions.py
uv run pytest tests/wearables/test_open_wearables_route_manifest.py
uv run pytest tests/mcp_server/test_ow_client.py
uv run pytest tests/wearables/test_search.py
uv run pytest tests/wearables/test_provenance.py
uv run pytest tests/decision/test_wearable_search.py
uv run pytest tests/decision/test_responses.py
uv run pytest tests/mcp_server/test_domain_search.py
uv run pytest tests/api/test_inputs.py
uv run pytest tests/store/test_alembic.py
```

## Drift 검증

집중 테스트는 vendor Python 파일을 import하지 않고 AST로 읽는다.

테스트는 다음 조건에서 실패한다.

1. Open Wearables에 새 route decorator가 추가됐지만 manifest에 분류하지 않은 경우
2. upstream route의 module, HTTP method, path가 변경된 경우
3. 삭제된 upstream route가 manifest에 남은 경우
4. route identity가 중복되거나 분류 이유가 비어 있는 경우
5. 전체 또는 분류별 route 수가 감사 기준과 달라진 경우
6. exposed health route가 runtime capability에 연결되지 않은 경우
7. metadata/excluded route에 runtime capability가 실수로 연결된 경우

route manifest 테스트는 route 분류와 runtime capability 연결을 담당한다.
Provider adapter 동작, Input Settings ON/OFF, 세션 capability catalog, MCP
schema와 runtime availability는 각각 별도 focused test로 검증한다.
