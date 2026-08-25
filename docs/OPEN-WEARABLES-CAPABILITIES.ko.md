# Open Wearables Capability Route Coverage와 Provider Catalog

## 목적

이 문서는 vendored Open Wearables v1 FastAPI route와 HealthMes wearable
capability 경계, 그리고 사용자별 provider/data-source catalog의 기계 검증 가능한
기준을 정의한다. 기준 소스는 다음 디렉터리의 route decorator 전체다.

```text
vendor/open-wearables/backend/app/api/routes/v1/
```

정확한 분류 데이터는
`healthmes/wearables/open_wearables_routes.py`에 있다. 문서가 아니라 해당
manifest가 source of truth다.

각 exposed route에는 실제 HealthMes runtime capability도 함께 기록한다.
이 매핑은 문서 표에 그치지 않고 `WEARABLE_DETAIL_CAPABILITIES`를 구성하며,
focused test가 upstream route와 capability 연결의 drift를 검사한다.

route가 존재한다는 사실만으로 해당 capability가 LLM에 노출되지는 않는다. 실제
세션 catalog는 통합 Input Settings와 현재 사용자의 active connection,
data source, provider coverage, data inventory의 교집합으로 만든다.

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

## Provider/data-source별 capability catalog

source 전체가 ON이라는 사실과 특정 provider의 데이터를 실제로 사용할 수 있다는
사실은 다르다. HealthMes는 다음 순서로 사용자별 catalog를 만든다.

```text
통합 Input Settings
  source_enabled + decision_access_enabled
        |
        v
Open Wearables 사용자 매핑
        |
        +-- active connections
        +-- data sources
        +-- provider coverage
        `-- 사용자별 data inventory
        |
        v
provider/data-source binding 검증
        |
        v
capability와 parameter value별 provider ownership 계산
        |
        v
owner-scoped frozen session catalog
```

provider가 usable하려면 다음 조건을 만족해야 한다.

1. active connection에 실제로 묶인 data source가 있어야 한다.
2. connection이 없는 일회성 import는 해당 provider의 양수 inventory가 있어야 한다.
3. Open Wearables coverage metadata가 해당 provider와 metric/data family를
   지원한다고 선언해야 한다.
4. imported source는 실제 inventory에 존재하는 series/workout/sleep 범위만
   capability로 만들 수 있다.

따라서 active connection만 있고 data source가 없거나, revoke된 connection을
가리키는 오래된 data source만 있으면 `disconnected`다. 반대로 detached import는
실제 inventory가 확인될 때만 읽을 수 있다.

| 실제 연결 상태 | 세션 catalog |
|---|---|
| WHOOP만 연결 | WHOOP가 실제 제공하는 Recovery, Cycle day strain, 수면, timeseries 등 |
| Garmin만 연결 | Garmin coverage와 inventory가 검증된 stress, body battery, workout 등 |
| WHOOP + Garmin | 두 provider가 실제 제공하는 capability와 parameter value의 합집합 |
| Polar 미연결 | Polar 전용 provider workout, samples, zones, route 제외 |
| 모든 연결 해제 | Open Wearables-backed capability 전체 제외 |

capability 이름만 provider에 묶는 것으로는 충분하지 않다. 같은 capability 안에서도
구체적인 parameter value별 ownership을 동결한다.

```text
wearable.health-scores
  category=recovery   -> whoop
  category=day_strain -> whoop
  category=stress     -> garmin

wearable.timeseries
  series_type=heart_rate         -> whoop + garmin
  series_type=garmin_stress_level -> garmin

wearable.provider-workouts
  provider=garmin -> garmin
  provider=polar  -> Polar 연결이 검증된 경우에만 노출
```

`wearable.body-summary`는 upstream endpoint가 provider filter 없이 여러
body/vital series를 집계하므로 더 보수적으로 처리한다. 현재 inventory에서 body
series가 정확히 한 provider에만 귀속되거나, body data가 아직 없고 body coverage를
가진 direct provider가 정확히 하나일 때만 노출한다. 둘 이상의 provider가 body
집계에 섞일 수 있으면 잘못된 provider attribution을 만들지 않고 capability를
숨긴다.

LLM에는 허용된 provider 이름, capability, parameter value만 보인다. connection ID와
data-source ID는 model-visible catalog에 넣지 않는다. 대신 이 private identity와
catalog 전체를 canonical JSON으로 묶어 `sha256:` binding digest를 만든다. 이
digest는 retained snapshot, cursor와 실행 결과가 동일한 실제 source binding에서
생성됐는지 검증하는 데만 사용한다.

### Source lineage 신뢰 계약

`provider_source_lineage_verified`는 호출자가 임의로 올리는 권한 플래그가 아니다.
실제 bounded Open Wearables adapter가 다음 작업을 끝냈을 때만 내부 attestation을
발급한다.

```text
Open Wearables live response
  -> provider 정규화
  -> 명시적 data_source_id가 있으면 frozen allowlist와 exact 비교
  -> ID가 없으면 해당 live 행을 폐기
  -> raw data_source_id 제거
  -> public record sanitization
  -> 내부 live-lineage attestation 발급
```

Open Wearables의 일부 summary, sleep, workout, timeseries 응답은 provider와
device만 반환하고 `data_source_id`를 반환하지 않는다. device/display label은
진단 정보일 뿐 실제 source ownership의 증거가 아니다. 따라서 strict decision
session에서는 source identity map에 유일한 device가 있거나 허용 source가 하나뿐인
경우에도 ID 없는 live 행을 사용하지 않는다. Open Wearables가 인증된 source
lookup을 제공하는 별도 계약을 추가하기 전까지는 해당 행을 폐기하고
`open_wearables_source_lineage_unverified` 경계로 처리한다.

따라서 같은 provider에 같은 device label을 가진 source가 둘 이상이거나 행의
device가 어느 source와도 일치하지 않으면 물론이고, device가 유일하게 일치하는
경우에도 provider 이름이나 device label만으로 추정하지 않고 fail-closed한다.
WHOOP HealthScore처럼 upstream row가 명시적 `data_source_id`를 제공하는 경로는
기존 exact ID 검증을 그대로 사용한다.

따라서 custom reader가 boolean만 `true`로 설정해도 provider-bound 실행을
통과하지 못한다. attestation이 없으면
`open_wearables_source_lineage_unverified`로 실패하며, 해당 payload,
`source_refs`와 snapshot을 저장하지 않는다.

retained snapshot은 raw `data_source_id`를 복구하지 않는다. snapshot event의
private `provider_binding_digest`가 현재 session binding과 정확히 일치할 때만
이미 검증·정제된 public row를 재사용한다. digest가 없거나 다르면 legacy row를
provider-bound 결과로 추정하지 않고 fail-closed한다.

`no_data`는 별도 의미다. provider binding이 정상이고 bounded adapter가 빈 live
응답을 정확히 검증한 경우 capability는 켜진 채 `status=no_data`와 정상
`SourceRef`를 반환할 수 있다. 반면 source lineage를 확인할 수 없는 응답은
`no_data`로 위장하지 않고 실패한다.

### Capability별 lineage matrix

각 capability의 catalog 노출 여부와 실제 live 행의 source 증명 방식은 별개다.
현재 계약은 다음과 같다.

| Lineage mode | 적용 capability | 의미 | strict live query |
|---|---|---|---|
| `EXPLICIT_ROW_SOURCE_ID` | `wearable.health-scores`, `wearable.whoop-recovery-package` | 각 행이 frozen `data_source_id`를 직접 가져야 함 | exact ID가 allowlist에 있을 때만 허용 |
| `PROVIDER_ROUTE_AUTHORITATIVE` | `wearable.provider-workouts`, `wearable.provider-workout-detail` | provider 전용 route가 source 경계를 제공하고, 명시 ID가 있으면 direct source와 대조함 | route의 provider가 frozen direct binding과 일치할 때 허용 |
| `LINEAGE_UNAVAILABLE` | 그 밖의 summary, sleep, body, timeseries capability | upstream 응답만으로 source 소유권을 증명할 수 없음 | exact source ID가 있는 행만 허용; 없으면 fail-closed |

`LINEAGE_UNAVAILABLE`은 capability를 catalog에서 반드시 숨긴다는 뜻이 아니다.
coverage와 inventory가 정상이라면 모델에는 capability와 허용 parameter가 보일 수
있지만, strict live query는 exact source proof가 없는 행을 사용할 수 없다. 따라서
catalog가 `available`이어도 특정 요청 결과가
`open_wearables_source_lineage_unverified` 또는 `no_data`가 될 수 있다. 이 차이를
없애기 위해 lineage가 없는 행을 임의의 provider에 귀속시키거나 `no_data`로
조용히 바꾸지 않는다.

### Catalog 가용성과 실제 데이터 가용성은 별개

metadata로 capability를 계산할 수 있는지와 요청한 기간에 검증된 retained 행이
있는지는 서로 다른 상태다.

```text
metadata 정상
  -> capability catalog 계산 가능
  -> 요청 기간의 검증된 행이 없으면 no_data

metadata 장애 + last-known-good catalog
  -> catalog는 degraded로 복원 가능
  -> 동일 binding digest와 query scope의 retained snapshot이 있을 때만 조회 가능
  -> snapshot이 없거나 digest가 다르면 unavailable
```

즉 `degraded` catalog가 있다고 해서 모든 기간의 데이터가 자동으로 있다고
간주하지 않는다. catalog는 "어떤 capability를 시도할 수 있는가"를 나타내고,
retained snapshot은 "이번 요청을 실제로 답할 검증된 데이터가 있는가"를 나타낸다.

### Imported-only와 mixed provider 실행

provider binding에 imported-only source가 하나라도 선택되면 해당 실행은
`retained_only=true`가 된다. direct provider와 imported-only provider가 한 query에
함께 선택되는 mixed binding도 동일하다.

```text
direct provider만 선택
  -> frozen direct source allowlist
  -> bounded live REST 허용

imported-only provider 선택
  -> retained-only
  -> HealthMes에 보존된 동일 binding/query snapshot만 사용
  -> Open Wearables live REST 금지

direct + imported provider 혼합
  -> retained-only
  -> 전체 query에서 live REST 금지
  -> 직접 행을 live로 가져와 imported 행과 섞지 않음
```

이 규칙은 query 일부가 direct source라는 이유로 imported-only source의 보존
정책을 우회하지 않게 한다. retained snapshot은 원래 bounded adapter가 검증한
immutable event이며, snapshot의 private binding digest가 현재 실행 binding과
같고 event가 보존기간 안에 있을 때만 재사용한다.

WHOOP 전용 `wearable.whoop-recovery-package`는 Recovery와 Cycle
`day_strain`을 같은 live WHOOP source에서 다시 검증해야 하므로 active direct
connection과 direct data source가 모두 있을 때만 catalog에 노출한다. WHOOP
import만 있는 사용자는 inventory로 확인된 일반 wearable capability를
retained-only로 사용할 수 있지만, import 행만으로 WHOOP package를 새로 계산하거나
live package처럼 노출하지 않는다.

### 정상 빈 응답과 lineage 검증 실패

upstream 응답의 형태도 구분한다.

| upstream 상태 | 검증된 행 | 결과 |
|---|---:|---|
| 정상 빈 응답 | 0개, reject 없음 | `status=no_data`; 정상 빈 결과의 provenance 유지 |
| 행이 있었고 일부만 source 검증 통과 | 1개 이상 | 검증된 행만 반환하고 `wearable_rows_discarded`를 표시 |
| 행이 있었지만 모두 source 검증 탈락 | 0개, reject 있음 | `open_wearables_source_lineage_unverified`; payload/source_refs/snapshot 저장 금지 |
| metadata/API 장애 | 해당 없음 | 유효한 동일 scope retained snapshot이 있을 때만 degraded fallback |

마지막 두 상태를 같은 빈 배열로 합치지 않는다. 전자는 "해당 기간에 데이터가
없음"이고 후자는 "데이터는 왔지만 누구의 데이터인지 증명할 수 없음"이다.

### Retained snapshot 검증

retained snapshot에는 원본 전체 행을 다시 복제하지 않고, 이미 정제된 public
payload와 private execution-scope/binding digest, immutable event identity를
보존한다. 재사용 시 다음을 모두 확인한다.

1. owner, capability, query window/scope가 현재 요청과 일치한다.
2. private binding digest가 현재 frozen binding과 exact match한다.
3. event가 삭제·만료되지 않았고 immutable provenance 검증을 통과한다.
4. digest가 누락되거나 변경되면 snapshot을 provider-bound 결과로 승격하지 않는다.

검증에 실패한 snapshot은 `unavailable` 또는 `failed`로 처리하며, 최신 snapshot이나
다른 provider의 행을 임의로 대체하지 않는다.

### Legacy direct-read 경계

기존 `get_health_scores`, `get_personal_baselines`, `get_stress_timeline`,
`compare_impact` MCP 도구와 trigger/energy의 `OwHealthReader`,
`OwEnergyReader`는 역사적 호환 경로다. 이들은 기존 standalone 호출에서는
계속 동작하지만, 공식 Decision Agent가 frozen provider binding을 가진 동안에는
실행을 거부하거나 빈/`unavailable` 결과로 fail-closed한다.

```text
frozen Decision Agent session
  -> legacy direct-read 호출
  -> binding 우회 가능성 감지
  -> upstream Open Wearables 호출 전 거부
  -> HealthMes의 bounded search_wearable 경로만 사용
```

공식 Hermes Decision profile에는 legacy 도구가 포함되지 않는다. 따라서 이 경계는
기존 standalone 사용성을 보존하면서도, Decision Agent가 provider/source binding을
우회해 Open Wearables를 직접 읽는 두 번째 경로를 만들지 않도록 한다.

### Readiness의 legacy calendar mirror 경계

기존 `CalendarEventMirror`의 실제 수면 행에는 Open Wearables의
`data_source_id`가 저장되지 않는다. 따라서 strict provider-bound 세션에서는
행의 `sleep_provider` 이름만으로 현재 WHOOP/Oura/Garmin source라고 추정하지
않는다.

```text
strict session
  calendar mirror(provider 이름만 있음)
        -> 사용하지 않음
  live sleep summary(data_source_id가 frozen allowlist에 있음)
        -> 실제 수면 관측으로 사용
  둘 다 없음
        -> insufficient_data
```

기존 provider-only/legacy 호출은 하위 호환을 위해 calendar mirror를 계속 사용할
수 있다. 공식 Decision Agent의 frozen binding 경로는 exact source ID가 없는
mirror를 SourceRef로 남기지 않는다.

Open Wearables의 `internal` sleep/resilience score도 예외가 아니다. 파생
알고리즘과 category 의미는 유지하지만, strict 세션에서
`data_source_id`가 frozen provider source에 정확히 매핑되지 않으면 해당 score를
계산에 포함하지 않는다. 매핑된 경우 public 결과는 `internal` 계산임을 유지하고,
SourceRef의 `upstream_provider`에는 실제 contributing provider를 기록한다.

## Runtime availability와 세션 capability

Open Wearables route가 manifest에 있다는 사실과 LLM이 그 capability를
현재 사용할 수 있다는 사실은 다르다. 모델에 보이는 catalog는 통합 Input
Settings와 실제 Open Wearables 연결 상태를 함께 평가해 세션 시작 시 만든다.

```text
통합 Input Settings
        +
Open Wearables endpoint/API key/user mapping
        +
active connection + data source
        +
provider coverage + 사용자 inventory
        |
        v
availability 평가
        |
        v
provider/value별 frozen capability catalog
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
| `disconnected` | 검증된 provider/data-source binding이 없음 | 제외 | 금지 |
| `available` | provider/data-source/coverage/inventory 교집합이 있음 | 해당 교집합만 포함 | 허용 |
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

catalog는 한 decision session 동안 고정하며, availability,
`source_policy_revision`, provider/value ownership과 private binding digest도 함께
동결한다. 세션 시작 후 새 연결이 생겨도 그 세션에는 capability를 추가하지 않는다.
반대로 세션 시작 뒤 source가 OFF되거나 provider 연결/data source/coverage가
바뀌면 catalog에 capability가 남아 있어도 결과를 반환하지 않는다.

```text
세션 시작
  -> availability + source_policy_revision 동결

각 tool 호출 직전
  -> availability metadata 조회 전·후 exact source binding 재검사
  -> 현재 availability, revision, provider/value ownership 재검사
  -> 불일치하면 provider를 호출하지 않음

provider 실행 직후
  -> availability, revision과 private binding digest 다시 검사
  -> 실행 중 바뀌었으면 payload/source_refs 폐기

snapshot commit 직전
  -> write lock 안에서 exact source와 provider binding 재검사
  -> 불일치하면 observation/query snapshot 모두 저장하지 않음
```

따라서 source를 OFF했다가 곧바로 다시 ON해 현재 상태가 `available`로
돌아오더라도, OFF 이전에 시작한 fetch의 revision은 더 이상 일치하지 않는다.
이 stale fetch는 `open_wearables_source_policy_changed` 또는
`wearable_source_policy_changed`로 거부하며, 기존 retained snapshot
fallback으로 우회하지 않는다. 최초 명시적 설정 행 생성도 revision `0 -> 1`
변경이므로 이미 진행 중이던 기본 설정 fetch를 무효화한다.

source 설정은 같아도 WHOOP revoke, Garmin 추가, data-source 교체 또는 coverage
변경으로 private binding digest가 달라질 수 있다. 이 경우에는
`open_wearables_provider_binding_changed`로 거부한다. 이전 binding에서 만든
payload, `source_refs`, cursor와 retained fallback은 새 binding에서 재사용하지
않는다.

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

### Retained-only 실행과 snapshot 불변성

availability가 `degraded`이고 frozen provider binding이 유효한 경우에도
upstream을 다시 호출하지 않는다. retained-only binding에서는 daily reader와
detail/search reader를 모두 건너뛰고 HealthMes DB의 동일 query scope snapshot만
읽는다.

```text
degraded + 유효 retained snapshot
        |
        +-- daily query  -> live daily reader 호출 금지
        +-- detail query -> live detail/search reader 호출 금지
        `-- 동일 scope의 retained snapshot 조회
```

retained fallback은 새 결과 행을 저장하는 쓰기 경로가 아니다. 원래 live 조회가
만든 immutable snapshot event의 payload, event UUID, 원본 SourceRef coverage와
collection time을 그대로 재사용하고, 현재 요청이 fallback/partial이라는 사실만
응답 envelope의 `status`, `coverage`, `limitations`에 표시한다. 따라서 fallback
때문에 replacement event나 중복 source row가 생기지 않는다. signed `hmc2`
cursor도 이 fallback 응답 상태를 scope digest에 포함해 다음 페이지에서
`OK`로 되돌아가지 않도록 한다.

daily snapshot의 query identity에는 capability만 넣지 않는다. 다음 실행 범위를
canonical digest로 묶어 snapshot을 분리한다.

```text
execution_scope_digest =
  capability
  + 허용 provider 집합
  + private provider binding digest
```

같은 날짜라도 WHOOP-only 실행과 Garmin-only 실행은 서로 다른 snapshot이다.
provider binding이 없는 legacy snapshot은 provider-bound session의 결과로
재사용하지 않는다.

provider-bound live 결과를 저장할 때는 현재 요청 session과 독립된 writer가
필요하다. writer가 없거나 in-memory `StaticPool`이라 최종 재검증과 commit의
동시성 경계를 만들 수 없으면 `wearable_snapshot_writer_unavailable`로
fail-closed한다. 저장 순서는 다음과 같고, 마지막 검증과 commit 사이에는
`await`가 없다.

```text
write lock 획득
  -> snapshot persist
  -> flush
  -> provider/data-source binding 재검증
  -> input source policy 재검증
  -> commit
```

검증 중 하나라도 바뀌면 transaction을 rollback하며 payload와 SourceRef를
반환하지 않는다.

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
| provider catalog filtering | WHOOP-only, Garmin-only, 복수 provider fixture | provider와 parameter value별 실제 교집합만 노출 |
| provider direct-call denial | WHOOP-only catalog에서 Garmin/Polar 전용 query 요청 | upstream 호출 전 `open_wearables_provider_binding_changed` 또는 catalog denial |
| provider binding race | 실행 또는 snapshot 저장 직전에 connection/data source binding 변경 | stale payload, source ref, cursor와 retained fallback 폐기 |
| binding privacy | session handle, MCP 결과와 model transcript 검사 | connection/data-source ID와 private digest 미노출 |
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

개발 중에는 다음 focused command로 변경 경계를 빠르게 검증한다. PR 완료 전에는
이 focused suite에 더해 저장소의 전체 Ruff, 전체 pytest, `make mac-test`, compose
smoke와 Alembic 양 dialect render를 실행한다.

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
