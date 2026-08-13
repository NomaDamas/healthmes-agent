# HealthMes Decision Agent 아키텍처 결정

> **결정일:** 2026-08-10
>
> **상태:** 승인된 아키텍처의 `DEC-01`부터 `DEC-07`까지 구현되었다.
> `DEC-08`의 공개 엔진 경계와 실제 4-domain E2E도 구현되었으며, 전체 회귀와
> 독립 리뷰를 거쳐 merge-ready 상태를 검증한다. 고정 `question_kind` resolver는
> 새 core 경로가 아니라 기존 호출자를 위한 호환 구현으로만 남는다.
>
> **범위:** 엔진, 데이터 조회, LLM 판단, Hermes adaptation과 의사결정 기록.
> 실제 iOS, Android, 데스크톱 UI는 포함하지 않는다.

## TLDR

HealthMes의 두뇌는 Skill 문서도, Hermes 자체도, 고정 질문 표도 아니다.

```text
사용자 질문
    |
    v
HealthMes Decision Agent
  LLM이 질문을 이해하고 필요한 도구를 선택한다.
    |
    v
Context Access Layer
  권한, 보존기간, 시간대, 개인정보와 조회 한도를 검사한다.
    |
    v
Activity / Nutrition / Wearable / Calendar provider
  정확한 수치와 전문 context를 계산하거나 조회한다.
    |
    v
HealthMes Decision Agent
  여러 영역을 종합하고 설명한다.
    |
    v
Decision Finalizer
  사용한 source_refs를 검증하고 DecisionRecord를 저장한다.
```

Hermes는 위 흐름의 모델 iteration을 실행하는 첫 번째 runtime adapter다. HealthMes는
판단 계약, 반복 loop와 데이터 경계를 소유하고, Hermes는 모델 호출, 세션과 전달
채널을 제공한다. Hermes의 범용 autonomous tool loop가 HealthMes loop를 대신하지
않는다.

## 1. 왜 바꾸는가

이 작업을 시작하기 전 구현에는 다음 문제가 있었다.

1. 호출자가 `activity_summary`, `focus`, `overwork`, `recovery`,
   `caffeine_for_focus` 중 하나를 먼저 골라야 한다.
2. 선택된 `question_kind`가 조회할 영역을 고정한다.
3. resolver가 "무엇을 볼지 선택"과 "안전하게 자료를 가져오기"를 동시에 맡는다.
4. 최종 자연어 판단을 실행하는 HealthMes-owned LLM 계층이 없다.
5. 핵심 절차 일부가 Skill 문서에만 있어서 Skill이 로드되지 않으면 보장이 약해진다.
6. `evidence`가 의료적 증거처럼 들리지만 실제로는 데이터 행의 추적 ID다.
7. 원본 데이터는 질문과 권한에 따라 필요할 수 있는데 전면 금지로 표현돼 있다.
8. `record_decision` 도구는 있지만 최종 판단 뒤 반드시 저장되도록 강제되지 않는다.

고정 질문 표는 데모와 회귀 테스트에는 유용하지만 HealthMes의 목표인 모호한 복합
질문을 처리하기에는 부족하다.

```text
"왜 오늘 집중이 안 되지?"

고정 표
  focus -> activity + wearable + calendar

필요한 실제 동작
  LLM이 activity부터 확인
  -> 수면 신호가 필요하면 wearable 조회
  -> 회의 영향이 보이면 calendar 조회
  -> 늦은 카페인이 언급되면 nutrition 조회
  -> 자료가 부족하면 사용자에게 질문
```

## 2. 목표 아키텍처

```text
┌──────────── HealthMes Decision Agent ────────────┐
│ 질문 해석 · 도구 선택 · 반복 조회 · 종합 · 설명   │
│ HealthMes-owned system policy와 결과 계약         │
└─────────────────────┬─────────────────────────────┘
                      │ ContextToolGateway
                      ▼
┌────── Context Access Layer / Source Gateway ─────┐
│ auth · retention · timezone · privacy · query cap │
│ 허용된 context와 source_refs만 반환               │
└────────┬──────────┬──────────┬──────────┬─────────┘
         ▼          ▼          ▼          ▼
     Activity   Nutrition   Wearable   Calendar
     provider   provider    provider   provider
         └──── HealthMes 통합 저장·인덱스 ────┘

Runtime implementation

HealthMesDecisionAgent
        |
        +-- HermesRuntimeAdapter
        +-- FutureNativeRuntimeAdapter
        +-- TestRuntimeAdapter
```

실행 loop의 소유권은 다음처럼 고정한다.

```text
HealthMesDecisionAgent
  for step in bounded_steps:
    runtime.next_step(read_only_turn_snapshot)
      -> tool_calls 또는 final draft 중 하나

    tool_calls:
      HealthMes가 Context Access Layer를 통해 순차 실행
      -> 검증된 RuntimeToolExchange를 다음 snapshot에 추가

    final draft:
      실제 반환된 source_refs의 부분집합인지 검사
      context에 source_refs가 있으면 완료 답변이 최소 하나를 명시했는지 검사
      -> Decision Finalizer로 전달
```

Runtime에는 provider callback, DB session, registry, `consume_step()`을 주지 않는다.
또한 전체 `DecisionRequest`를 넘기지 않는다. 모델에는 질문, 질문 시각, 시간대,
요청 privacy level, 조회 기간 hint, 관련 기록 존재 여부와 허용된 관련 domain만
담은 `RuntimeDecisionRequest`를 제공한다. 선택된 record가 필요한 도구에는
turn-scoped `rr_...` alias만 보이며 HealthMes가 내부 실제 ID로 치환한다.
`principal_id`, `session_id`, channel과 실제 related record ID는 runtime 경계를
넘지 않는다. 외부 `DecisionRequest.request_id/turn_id`도 넘기지 않고, agent가
turn마다 새로 생성한 opaque runtime correlation UUID만 전달한다. 이 UUID는
응답 correlation과 감사에만 사용되며 caller/session/record identity를 담지 않는다.
도구 결과도 runtime 전용 최소 계약으로 다시 변환한다. 전체 `SourceRef`,
raw-source handle, query UUID와 cursor는 내부 trace에만 남고 모델에는 검증된
`source_ref_ids`, freshness, coverage, limitation과 질문에 필요한 payload만
전달한다. payload 안에 선택 record ID가 반복되어도 동일한 `rr_...` alias로
치환한다. UUID의 hyphen/hex/URN 및 대소문자 변형은 하나의 canonical identity로
합쳐 한 alias만 사용한다. 권한이나 domain 불일치로 조회 도구에 연결되지 않은
related record도 질문 문자열에서는 익명화하되 tool catalog에는 노출하지 않는다.
따라서 step 수와 tool 실행은 모델의 자발적 준수에 의존하지 않는다.

중요한 분리는 다음과 같다.

```text
LLM
  무엇을 알아봐야 하는지 결정한다.

Context Access Layer
  요청한 자료 중 무엇을 실제로 제공할 수 있는지 강제한다.

Domain provider
  정확한 값과 전문 파생값을 계산한다.

Decision Finalizer
  실제 사용한 자료와 최종 답변을 검증하고 저장한다.
```

### 2.1 공개 엔진 경계와 수명주기

UI, API와 MCP adapter가 공통으로 호출할 canonical Python 경계는 다음과 같다.

```text
build_healthmes_decision_engine(...)
  같은 ContextAccessLayer와 policy_resolver를
  HealthMesDecisionAgent와 DecisionFinalizer에 주입
        |
        v
await engine.ask_wellness(DecisionRequest)
        |
        +-- agent.ask(): 질문 해석과 반복 context tool loop
        |
        +-- finalizer.finalize(): source_refs 재검증과 DecisionRecord 저장
        |
        v
DecisionResult
```

`ask()`는 기존 내부 호출자를 위한 호환 wrapper이고, 새 UI와 service adapter는
`ask_wellness()`를 사용한다. production composition root에는 broad-consent
기본값이 없다. 인증된 사용자의 현재 정책을 반환하는 `policy_resolver`를 명시하지
않으면 엔진을 구성할 수 없다.

종료 경계도 제품 계약의 일부다.

```text
수락된 요청 ──> LLM/provider 실행 ──> finalization ──> record 저장
                         |
caller 취소 -------------+  내부 작업은 취소하지 않음

await engine.aclose()
  1. 새 요청 거부
  2. 이미 수락한 요청의 finalization 대기
  3. agent runtime 종료
```

따라서 HTTP 연결이나 UI task가 먼저 취소돼도 이미 생성된 행동 제안의 감사 기록이
중간에 유실되지 않는다. async application은 `await aclose()`를 사용하며, 실행 중인
event loop 안에서 동기 `close()`를 호출하면 명시적으로 거부한다. 애플리케이션
lifespan 자체가 반복 취소되더라도 Decision Engine 종료를 끝낸 뒤 MCP와 DB를
닫으므로 finalization이 이미 dispose된 저장소를 참조하지 않는다. composition이
agent worker 생성 뒤 실패하면 worker도 즉시 회수한다.

finalization 자체도 무기한 기다리지 않는다.

```text
HEALTHMES_DECISION_FINALIZATION_TIMEOUT_SECONDS=5

하나의 총 deadline
  -> preflight 정책 조회
  -> process write lock
  -> SQLite file lock 또는 PostgreSQL advisory lock
  -> transaction 안의 정책 재검사
  -> source row lock과 재검증
  -> result와 private payload 생성
  -> DecisionRecord flush
  -> commit 직전 마지막 deadline 검사
  -> 제한된 retry
```

SQLite는 HealthMes process/file lock뿐 아니라 protocol 밖의 외부 writer가 잡은
`BEGIN IMMEDIATE`도 임시 `busy_timeout`으로 제한하고, 끝난 뒤 기존 30초 설정을
복원한다. PostgreSQL은 advisory lock polling과 transaction-local `lock_timeout`,
`statement_timeout`을 같은 남은 deadline으로 제한한다. Deadline 결과는 commit
경계에 따라 다르게 처리한다.

```text
PRE_COMMIT에서 deadline 초과
  -> commit 진입을 영구 차단
  -> rollback
  -> decision_finalization_timeout
  -> HTTP 503 decision_service_unavailable

COMMITTING에서 deadline 초과
  -> 저장 성공/실패를 추측하지 않음
  -> persistence_status=unknown
  -> HTTP 202 + Location: /v1/wellness-decisions/{request_id}
  -> 같은 request_id로 저장 결과 재조회
```

이 제한 덕분에 저장소가 잠겨 있어도 `aclose()`가 수락된 작업을 무기한 기다리지
않는다. Python payload 생성이나 SQLAlchemy `flush()`가 deadline을 넘긴 경우에는
`commit()`을 호출하지 않고 rollback한다. 반대로 DB driver가 이미 commit을 시작한
뒤 응답만 늦는 상황에서는 실패나 성공으로 단정하지 않는다. commit이 실제 완료되면
복구 endpoint가 저장된 private payload와 digest를 다시 검증한
`PersistenceStatus.PERSISTED` 결과를 반환하고, 아직 행이 없으면 `404`이므로 호출자는
같은 request ID로 다시 조회할 수 있다.

### 2.2 운영 설정, 실행 위치와 domain 동의

Decision runtime은 세 설정이 모두 있을 때만 활성화된다.

```text
HEALTHMES_DECISION_HERMES_BASE_URL
HEALTHMES_DECISION_HERMES_MODEL
HEALTHMES_DECISION_HERMES_PROVIDER
```

일부만 설정하면 startup validation이 실패한다. Hermes가 전용 single-iteration
계약을 광고하지 않으면 일반 chat endpoint로 fallback하지 않고 Decision REST
호출을 `503 decision_runtime_unavailable`로 종료한다.

실행 위치는 서버가 소유하는 명시적 설정이다.

```text
HEALTHMES_DECISION_EXECUTION_SCOPE=local
  -> Hermes origin은 loopback이어야 함

HEALTHMES_DECISION_EXECUTION_SCOPE=hosted
  -> 질문과 허용된 aggregate context가 외부 모델로 갈 수 있음
```

loopback Hermes가 cloud model을 대신 호출하는 경우에도 운영자는 `hosted`를
명시해야 한다. 클라이언트는 request body에서 실행 위치, principal, privacy level,
budget 또는 허용 domain을 바꿀 수 없다.

domain 동의는 `decision_domain_policy`에 저장하며 서버 시작 시 누락된 네 domain만
로컬 기본값인 enabled로 만든다. 이미 사용자가 바꾼 값은 재시작 뒤에도 보존한다.
`hosted`로 바꾸는 행위는 현재 enabled인 domain의 aggregate context를 외부 runtime에
보내도록 운영자가 명시적으로 승인하는 설정 변경이다.

```text
GET /v1/wellness-decisions/settings
PUT /v1/wellness-decisions/settings/{domain}

domain = activity | nutrition | wearable | calendar
```

두 endpoint는 전체 REST/MCP와 같은 bearer 또는 loopback-only 인증 경계 안에 있다.
Decision Agent는 planning 때만 정책 snapshot을 믿지 않는다. 각 context tool 호출
직전과 provider 실행 직후에 현재 정책을 다시 읽는다. domain의 `enabled` 상태,
revision, 실행 범위, privacy 또는 query limit이 실행 중 바뀌면 provider 결과를
버리고 `domain_consent_changed`로 turn을 중단한다. 따라서 사용자가 실행 중
`off -> on`으로 빠르게 되돌려도 revision 변화가 감지된다. Finalization
transaction 안에서도 정책을 다시 읽고 row lock을 건다. 이미 철회된 consent는
`domain_consent_denied`로 기록 저장을 거부한다. Calendar는 별도로 현재 연결된
credential source만 읽으며, 연결 해제된 source의 오래된 mirror row를 판단에
재사용하지 않는다.

Google과 iCloud/CalDAV credential 저장·삭제도 finalization과 같은 process 및
database write fence를 사용한다. OAuth나 CalDAV 네트워크 통신은 잠금 밖에서 하고,
owner-only 임시 파일을 atomic replace하는 짧은 credential 변경만 fence 안에서
수행한다.

```text
finalization이 먼저 fence 획득
  -> 검증된 Calendar source로 DecisionRecord commit
  -> 그 뒤 disconnect

disconnect가 먼저 fence 획득
  -> credential 삭제
  -> 그 뒤 finalization은 calendar_source_disconnected로 실패
```

따라서 두 작업이 동시에 실행돼도 "연결 해제된 캘린더를 사용했지만 판단 기록은
나중에 저장된" 중간 상태가 생기지 않는다.

Calendar 원격 실행 경로는 credential 파일의 secret-safe digest를 connection
generation으로 사용한다.

```text
Calendar poll / sleep scheduler / sleep web preview·apply
  -> source별 calendar connection lock
  -> 현재 credential generation 확인
  -> generation이 바뀌면 cached backend 폐기
  -> 연결이 없으면 원격 호출 없이 실패
  -> 현재 generation으로 새 backend 생성
  -> lock을 유지한 동안에만 원격 read/write
```

sync 도중 disconnect가 시작되면 disconnect는 현재 원격 작업이 끝날 때까지
기다린다. disconnect가 완료된 다음 실행은 이전 cached backend를 호출하지 않는다.
재연결 뒤에는 새 generation으로 새 backend를 만든다. Open Wearables 수면 조회는
이 lock 밖에서 먼저 수행하고, 캘린더 preview/apply에 필요한 원격 작업만 lock
안에서 수행하므로 async event loop를 wearable 네트워크 I/O 동안 막지 않는다.
Google token refresh는 credential 파일 digest compare-and-swap도 사용하므로
refresh 도중 파일이 삭제되거나 교체되면 오래된 refresh 결과를 저장하지 않는다.

`decision_domain_policy` migration의 downgrade도 동의 상태를 조용히 잃지 않는다.
offline downgrade는 현재 행을 확인할 수 없어 항상 거부하고, 하나라도
`enabled=false`인 행이 있으면 online downgrade도 거부한다. PostgreSQL은 검사와
table drop 사이에 사용자가 동의를 철회하는 race까지 막도록 table을
`ACCESS EXCLUSIVE`로 잠근다. 모든 행이 enabled일 때만 명시적 downgrade를 허용한다.

요청 admission도 무제한 queue가 아니다.

```text
HEALTHMES_DECISION_MAX_PENDING_REQUESTS=8
```

실행 중인 요청과 수락된 대기 요청을 합쳐 이 수를 넘으면 새 요청은 runtime에
도달하지 않고 `429 decision_engine_busy`로 즉시 거부된다. runtime contract,
identity 또는 실행 실패는 성공 응답으로 위장하지 않고
`503 decision_runtime_unavailable`로 변환한다. 정책 resolver, provider catalog,
tool 실행, source contract 또는 DecisionRecord 저장과 같은 HealthMes 내부 실패도
`503 decision_service_unavailable`로 변환한다.

반대로 다음은 서비스 장애가 아니라 안전하게 중단한 정상 제품 결과이므로
구조화된 `DecisionResult`와 HTTP `200`을 유지한다.

- 사용자가 허용한 step, tool, source 또는 context byte 예산 소진
- domain consent, privacy 또는 retention 정책에 따른 차단
- 조회 뒤 원본 삭제, 만료, 연결 해제로 source ref 재검증이 실패한 경우
- 자료 부족으로 추가 질문이 필요한 경우

즉 HTTP 상태는 `FAILED` 문자열만 보고 결정하지 않는다. 호출자가 설정이나 질문을
바꿔 해결할 수 있는 안전한 중단과, 운영자가 runtime/provider/storage를 복구해야
하는 내부 장애를 reason code로 분류한다.

## 3. 각 부품의 책임

### 3.1 전문 도메인 provider

기존의 "전문 도메인 엔진"은 모든 영역이 동일한 형태의 계산 엔진이라는 오해를
줄 수 있으므로 `Domain Context Provider`를 상위 이름으로 사용한다.

| Provider | 책임 | 하지 않는 일 |
|---|---|---|
| Activity | 사용시간, active/idle, 연속 활동, 분절, 야간 사용, baseline 계산 | 최종 휴식 명령 또는 카페인 판단 |
| Nutrition | 섭취 관찰, 사용자 확인, 영양소, 카페인 ledger와 후보 식품 context | 수면이나 집중 상태 추측 |
| Wearable | Open Wearables 수면, HRV, 스트레스, 회복 context 조회와 정규화 | 다른 영역을 대신한 최종 판단 |
| Calendar | 일정 시간, busy 구간, 회의 밀도와 가용 시간 계산 | 일정의 의미나 건강 원인 확정 |

숫자 합산, 단위 변환, 시간대 경계, 중복 제거와 누락 데이터 처리는 LLM에게 맡기지
않는다. 재현 가능해야 하는 계산은 provider와 전문 정책이 담당한다.

### 3.2 Context Access Layer

기존 `Context Broker` 또는 cross-domain resolver를 두 역할로 나눈다.

```text
의미 선택
  LLM Decision Agent가 담당

데이터 접근과 검증
  Context Access Layer가 담당
```

Context Access Layer는 다음만 강제한다.

- 요청한 데이터 영역과 기간에 대한 사용자 권한
- 데이터별 retention과 삭제 상태
- 사용자 local timezone과 조회 시간 범위
- 개인정보 공개 단계와 민감도
- 한 번에 읽을 수 있는 기간, 행 수와 원본 크기
- 중복, stale data, coverage와 freshness
- 실제 반환한 record와 summary의 `source_refs`

성능이 높은 LLM도 접근 권한이나 삭제된 데이터의 재노출을 보장하는 보안 경계가 될
수 없다. 따라서 LLM의 성능과 Context Access Layer의 필요성은 대체 관계가 아니다.

### 3.3 HealthMes Decision Agent

이 계층이 HealthMes가 소유하는 실제 제품 두뇌다.

- 자연어 질문과 사용자 의도를 해석한다.
- 사용 가능한 context tool catalog를 본다.
- 필요한 provider, 기간, granularity와 필드를 선택한다.
- 첫 조회 결과를 보고 추가 도구를 반복 호출할 수 있다.
- 데이터가 부족하면 필요한 사실을 사용자에게 묻는다.
- 전문 정책 결과를 재계산하지 않고 여러 영역의 trade-off를 종합한다.
- 관찰, 불확실성, 대안과 최종 설명을 만든다.
- 각 model iteration과 전체 deadline을 코드에서 직접 계산한다.
- runtime에는 개인정보를 제거한 읽기 전용 `RuntimeDecisionRequest`, 허용 tool
  catalog와 이전 tool 결과 snapshot만 제공한다.
- runtime이 반환한 tool 요청을 직접 검증하고 Context Access Layer를 통해
  실행한다.

질문을 미리 다섯 종류로 제한하지 않는다. 필요한 도구는 질문과 첫 조회 결과에 따라
달라질 수 있다.

#### hard deadline과 실행 격리

하나의 장기 실행 `HealthMesDecisionAgent`는 하나의 daemon worker event loop를
소유한다. runtime과 provider coroutine은 이 안정된 loop에서 실행하므로 loop-bound
client를 요청마다 다른 event loop에서 재사용하지 않는다. 같은 runtime instance의
전체 decision turn은 직렬화되어 동시 요청의 임시 상태가 서로 섞이지 않는다. API
event loop는 `time.sleep()` 같은 동기 블로킹 코드와 분리된 wall-clock deadline을
소유한다. worker thread와 event loop는 `HealthMesDecisionAgent` 구성 단계에서
미리 시작하고 ready 상태까지 확인하므로 `ask()`가 `Thread.start()`를 실행하지
않는다. request deadline은 `ask()` 진입부터 외부 request의 strict validation,
동기 policy resolution, provider catalog 검증, access turn 준비와 runtime/provider
실행 전체에 적용된다. worker가 취소를 늦게 처리해도 호출자는 deadline에 맞춰
반환받고 늦게 도착한 결과는 canonical trace에 반영되지 않는다. validation이
끝나기 전에 timeout되면 신뢰할 수 없는 caller ID 대신 새 fallback request/turn
UUID를 반환한다.

hard timeout 뒤 async runtime이 cancellation을 정상 수용해 turn을 종료하면 같은
worker를 재사용한다. cancellation이 끝나지 않은 경우 다음 요청의 짧고 bounded된
확인 구간 뒤 quarantine하고 `runtime_worker_unavailable`로 실패시킨다. 같은
agent는 새 worker를 만들지 않으므로 종료되지 않는 동기 코드가 요청마다 daemon
thread를 하나씩 늘릴 수 없고, 한 agent에서 orphan 가능한 worker는 최대 하나다.
운영자는 agent를 장기 실행 singleton으로 구성하고 정상 shutdown에서 `close()`를
호출한다. active turn 중 `close()`가 호출되더라도 완료 결과를 caller future에
전달한 뒤 loop를 종료한다.

일반 caller cancellation은 hard timeout과 구분한다. 취소가 worker에서 정상적으로
정리되면 다음 turn이 같은 loop를 재사용한다. 취소된 동기 코드가 끝나지 않아 다음
대기 turn까지 timeout되면 worker를 quarantine해 이후 요청을 fail-fast 한다.

이 경계에는 명시적인 호환성 계약이 있다.

- runtime과 provider는 API event loop에 귀속된 `asyncio.Event`, task 또는 client를
  worker 경계 밖에서 만든 뒤 공유하지 않는다.
- async client가 필요하면 첫 worker 호출 안에서 생성해 같은 agent worker에
  귀속하거나 worker-safe한 client factory를 사용한다.
- SQLite session은 worker 안에서 만들며 기존 `check_same_thread=False` 설정을
  사용한다.
- 외부 Pydantic model instance는 신뢰하지 않는다. nested model까지 plain value로
  변환한 뒤 validator를 다시 실행하고 deep copy한다.
- registry는 provider metadata의 검증된 snapshot을 등록 시 보관하며, provider에는
  canonical query가 아닌 별도의 deep copy만 전달한다.
- `source_ref_id`는 source identity에서 다시 계산하며, gateway 정규화 뒤 동일해진
  query는 첫 실제 조회 시각으로 고정한 turn normalization time을 기준으로 중복
  실행하지 않는다.

### 3.4 Decision Finalizer

최종 기록을 LLM의 기억이나 Skill 지침에만 맡기지 않는다.

- 최종 답변이 인용한 `source_refs`가 실제 tool 결과에 있었는지 검사한다.
- 존재하지 않는 ID, 만료된 자료와 허용되지 않은 원본 참조를 거부한다.
- 모델, tool trace, limitation, 질문 시각과 답변을 `DecisionRecord`로 저장한다.
- 저장이 필요한 판단인데 기록에 실패하면 성공한 결정으로 표시하지 않는다.

## 4. source_refs의 뜻

`evidence`는 "이 답변이 의학적으로 증명됐다"는 뜻이 아니다. 답변에 사용한 데이터가
어디에서 왔는지 다시 찾기 위한 provenance다. 새 계약에서는 `source_refs`를
기본 이름으로 사용한다.

```json
{
  "domain": "activity",
  "record_id": "wellness-event-uuid",
  "source_provider": "activitywatch",
  "observed_start": "2026-08-10T09:00:00+09:00",
  "observed_end": "2026-08-10T10:00:00+09:00",
  "schema_version": 1,
  "derived_by": "activity.hour-summary.v1",
  "freshness": "current",
  "coverage": 0.87
}
```

모든 필드를 LLM에 길게 넣을 필요는 없다. 모델에는 필요한 최소 참조를 주고,
DecisionRecord에는 감사 가능한 전체 provenance를 보존할 수 있다.

기존 `evidence_ids`와 최상위 `evidence`는 호환 기간 동안 유지하되 내부적으로
`source_refs`로 정규화한다.

## 5. 원본 데이터 정책

원본을 절대 보내지 않는 것도, 모든 원본을 자동으로 보내는 것도 잘못이다.

### Level 1: 기본 집계

일반적인 집중, 과로, 회복 질문의 기본값이다.

- 활동 시간과 category
- 수면, HRV와 회복 summary
- 일정 busy minutes
- 확인된 영양소와 섭취량

앱 이름, 창 제목, URL, 사진과 음성 bytes는 포함하지 않는다.

### Level 2: 제한된 identity

질문에 identity가 필요하고 사용자가 허용한 경우에만 사용한다.

- "어떤 앱 때문에 집중이 끊겼어?"의 앱 이름
- "어떤 일정 뒤에 피곤했어?"의 허용된 일정 제목
- 사용자가 직접 고른 식품 또는 기록 이름

### Level 3: scoped raw

원본 분석 자체가 질문의 목적일 때만 별도 호출로 사용한다.

- 음식 사진을 Nutrition VLM에 전달
- 음성을 로컬 transcription provider에 전달
- 사용자가 명시적으로 요청한 민감 원본 분석

원본은 해당 분석 provider에만 전달하고, 이후 일반 의사결정 turn에는 구조화 결과와
`source_refs`를 사용한다. 창 제목, URL, 화면 pixel과 raw wearable timeseries는
명시적 권한, 목적과 제한된 보존 정책 없이는 Level 3으로 승격하지 않는다.

## 6. Skill의 위치

Skill은 HealthMes의 두뇌나 데이터 엔진이 아니다. 특정 runtime이 HealthMes 계약을
잘 호출하도록 돕는 설명과 workflow adapter다.

```text
잘못된 구조
  Skill 문서가 권한, 안전 규칙, 자료 선택, 최종 기록을 모두 소유

목표 구조
  HealthMes 코드와 계약이 강제할 것을 강제
  Skill은 도구 이름, 표현 방식과 runtime 사용법만 설명
```

HealthMes의 필수 system policy는 Skill을 사용자가 우연히 열어야만 적용되는 방식이
아니라 Decision Agent를 시작할 때 항상 주입한다.

Skill이 담당할 수 있는 내용:

- Hermes에서 HealthMes Decision Agent를 어떻게 호출하는가
- 사용자에게 관찰, 근거, 제안과 한계를 어떻게 보여주는가
- 특정 채널에서 확인 질문을 어떻게 표현하는가

Skill에만 두면 안 되는 내용:

- retention과 권한 검사
- 섭취량 합계와 시간대 계산
- 카페인 전문 안전 경계
- source reference 검증
- DecisionRecord 저장 의무

## 7. Hermes의 위치

Hermes는 범용 Agent Runtime이며 HealthMes와 동등한 제품 계층이 아니다.

Hermes가 제공하는 기능:

- LLM provider와 model 실행
- 일반 대화와 범용 tool-calling 기능
- MCP 도구 발견과 실행
- 세션, gateway, cron과 전달 채널
- Skill 문서 로딩

HealthMes가 소유해야 하는 기능:

- `HealthMesDecisionAgent` 요청과 결과 계약
- HealthMes system policy
- context tool catalog와 privacy scope
- source reference와 finalization
- DecisionRecord와 outcome 연결

HealthMes 판단에서 Hermes의 전체 autonomous tool loop를 그대로 실행하면 step,
deadline과 gateway 소유권이 다시 Hermes로 넘어가므로 사용하지 않는다.
`HermesRuntimeAdapter.next_step()`은 HealthMes가 준 turn snapshot으로 정확히 한 번의
모델 iteration만 실행하고, `tool_calls` 또는 final draft 중 하나를 반환해야 한다.

HealthMes 쪽 `HermesRuntimeAdapter`와 fail-closed 계약은 구현되어 있다. 다만 현재
vendored Hermes에는 이 single-iteration generic hook이 없다. 따라서 기존 chat
endpoint를 안전한 것처럼 fallback하지 않고 `unavailable`을 반환한다. 실제 Hermes
모델 실행을 활성화하려면 HealthMes vendored tree를 직접 수정하지 않고 별도 Hermes
저장소와 PR에서 다음 범용 확장을 구현해야 한다.

- 필수 system policy 주입 hook
- structured tool-call/final response 한 번 생성
- model usage metadata 반환
- tool allowlist와 이전 exchange snapshot 전달
- 외부 HealthMes driver가 cancellation과 deadline을 소유할 수 있는 호출 경계

HealthMes 전용 카페인, 활동 또는 영양 규칙을 Hermes core에 넣지 않는다.

## 8. MCP의 위치

MCP는 판단기가 아니라 runtime과 HealthMes 도구 사이의 통신 규격이다.

```text
LLM
  "활동과 수면 자료가 필요하다"
       |
       v
HermesRuntimeAdapter.next_step
  tool 요청만 반환
       |
       v
HealthMesDecisionAgent
  요청 검증과 step/tool budget 적용
       |
       v
Context Access Layer와 Domain Provider
```

에이전트의 자율성과 MCP는 충돌하지 않는다. LLM이 어떤 도구를 언제 호출할지
자율적으로 선택하고, HealthMes driver가 선택을 검증한 뒤 MCP 또는 in-process
gateway로 안전하게 실행한다.

아키텍처는 MCP에만 고정하지 않는다.

```text
ContextToolGateway
  +-- MCPToolGateway       Hermes용
  +-- InProcessToolGateway 미래 native runtime용
  +-- FakeToolGateway      테스트용
```

현재 core E2E는 in-process Context Access Layer로 실행한다. MCP는 외부 agent나
channel이 HealthMes 기능을 호출하는 integration surface로 유지하되, 권한 검사와
finalization을 우회하는 별도 core 경로로 만들지 않는다.

## 9. 중앙 데이터 조회

에이전트가 중앙 데이터베이스에 자유 SQL을 실행하는 구조는 채택하지 않는다.
대신 하나의 논리적 tool catalog를 통해 모든 웰니스 영역을 탐색한다.

```text
통합 접근
  list_context_capabilities
  search_wellness_context
  get_activity_context
  get_nutrition_context
  get_wearable_context
  get_calendar_context
  specialized policy tools
```

물리적으로 모든 데이터를 한 테이블에 억지로 넣을 필요는 없다.

- Activity와 Nutrition은 공통 `WellnessEvent` envelope를 사용한다.
- Calendar는 외부 일정의 소유권을 유지하는 local mirror를 사용한다.
- Open Wearables raw 저장은 vendor 호환을 위해 분리할 수 있다.
- HealthMes 판단에 사용한 normalized wearable summary와 provenance는 로컬
  source reference 또는 mirror로 남긴다.

즉 "한곳"은 하나의 거대한 테이블이 아니라 하나의 권한, 조회, provenance와
의사결정 기록 체계를 뜻한다.

## 10. 결정론과 LLM의 경계

| 결정론적으로 강제 | LLM이 판단 |
|---|---|
| 권한, consent와 retention | 질문의 목적 |
| 시간대, 기간과 단위 계산 | 필요한 영역과 도구 |
| 중복 제거와 정확한 합계 | 추가 조회 필요 여부 |
| freshness, coverage와 missing data | 여러 영역의 trade-off |
| 전문 정책의 숫자와 hard boundary | 사용자에게 설명할 대안 |
| source reference 검증과 저장 | 자연어 답변 |

질문 종류에 따라 조회 영역을 고정하는 것은 폐기하지만, 계산과 보안까지 LLM에게
넘기지는 않는다.

## 11. 현재 구현과 남은 경계

| 항목 | 현재 구현 | 남은 경계 |
|---|---|---|
| 질문 입력 | 공개 `ask_wellness(DecisionRequest)`와 호환 `ask()` | API/MCP/UI별 얇은 호출 adapter |
| 자료 선택 | HealthMes-owned 반복 `next_step` loop | 실제 runtime의 모델 품질 평가는 운영 단계 |
| resolver | 고정 `question_kind`를 compatibility preset으로만 유지 | 기존 호출자 migration 뒤 축소 가능 |
| Context layer | 권한, retention, timezone, privacy, query cap과 source ref 강제 | 새 provider 추가 시 동일 계약 준수 |
| Domain provider | Activity, Nutrition, Wearable, Calendar 공통 registry | 새로운 wellness 입력 provider 확장 |
| Skill | 필수 정책을 소유하지 않는 설명 계층 | channel별 표현 지침만 유지 |
| Hermes | `HermesRuntimeAdapter`와 strict/fail-closed 계약 구현 | Hermes upstream single-iteration hook `#139` |
| 최종 판단 | runtime-neutral structured draft loop 구현 | 상용 LLM eval은 이 PR 비범위 |
| 판단 저장 | finalizer가 source ref를 재검증하고 자동 저장 | 저장 backend 운영·관측성 보강 |
| wearable provenance | normalized local snapshot과 stable source ref 구현 | vendor별 장기 호환성 모니터링 |
| activity completeness | ActivityWatch scheduler, iOS aggregate 경계, cross-device dedup 구현 | 실제 device UI와 dogfood |
| production 조립 | FastAPI lifespan singleton, DB policy resolver, REST adapter와 cancellation-safe cleanup 구현 | 다른 channel/UI의 얇은 호출 adapter |
| E2E | 실제 네 domain 저장소에서 자연어 질문부터 DecisionRecord 재조회까지 검증 | 실제 상용 LLM 네트워크 평가는 별도 |

## 12. 마이그레이션

현재 API와 테스트를 한 번에 깨지 않는다.

```text
현재
  resolve_wellness_context(question_kind, ...)

과도기
  question_kind를 generic ContextQuery preset으로 변환
  기존 응답의 evidence_ids 유지
  새 내부 응답에는 source_refs 추가

목표
  ask_wellness(DecisionRequest)
  -> LLM tool planning
  -> Context Access Layer
  -> Decision Finalizer
```

기존 `get_activity_summary`, `get_focus_context`, `get_overwork_context`와 전문
Nutrition, Wearable, Calendar 도구는 폐기하지 않는다. 새 Decision Agent가 선택할
수 있는 typed tools로 재사용한다.

## 13. 구현 계획

### `DEC-01 Decision contracts`

- `DecisionRequest`, `ContextQuery`, `ContextResult`, `SourceRef`,
  `DecisionResult` 정의
- `question_kind`는 compatibility preset으로 명시
- runtime이나 MCP 이름에 종속되지 않는 계약 작성

**종료 조건:** 자연어 질문과 tool query가 고정 질문 enum 없이 표현된다.

### `DEC-02 Context Provider Registry`

- Activity, Nutrition, Wearable, Calendar provider를 동일 registry에 등록
- provider capability, 지원 기간, granularity와 sensitivity 선언
- broad discovery와 전문 정책 도구를 함께 노출

**종료 조건:** 새 입력 영역을 resolver의 `if/elif` 수정 없이 등록할 수 있다.

### `DEC-03 Context Access Layer`

- authorization, consent, retention과 timezone 검사
- privacy Level 1, 2, 3 강제
- query cap, freshness, coverage와 `source_refs` 정규화

**종료 조건:** 모델이 금지된 원본이나 만료 데이터를 요청해도 반환되지 않는다.

### `DEC-04 HealthMes Decision Agent`

- 자연어 질문을 받는 HealthMes-owned orchestration interface
- HealthMes가 model iteration, step budget와 hard deadline을 직접 소유
- runtime은 한 iteration마다 tool 요청 또는 final draft 중 하나만 반환
- LLM이 tool catalog에서 도구를 선택하고 실제 결과에 따라 추가 요청
- runtime에 provider callback, DB와 registry를 노출하지 않음
- runtime history와 canonical trace를 deep snapshot으로 격리
- 실제 tool result에 없는 source reference ID 거부
- source reference가 있는 context를 사용한 완료 답변의 source reference 생략 거부
- runtime request에서 caller/session/record 식별자를 제거하고 turn-scoped record
  alias와 허용 domain만 공개
- runtime/provider를 API event loop와 분리하고 wall-clock deadline 강제
- worker를 구성 단계에서 prewarm하고 request validation, policy/catalog/access
  준비에 하나의 deadline 적용
- cooperative timeout cancellation 뒤 worker 재사용, stuck turn만 quarantine
- active turn shutdown 시 결과 전달 후 worker loop 종료
- 같은 runtime을 사용하는 전체 turn 직렬화와 cancellation-safe 재사용
- provider metadata/query snapshot으로 post-filter allowlist 변조 차단
- tool parameter마다 type, 길이, 범위, enum과 format 계약 강제
- nested Pydantic instance를 포함한 외부 결과 전체 재검증
- gateway normalization 후 동일한 effective query 중복 실행 거부
- configured runtime/model identity 변경 거부
- 부족한 자료에 대한 추가 질문
- 전문 정책 경계를 유지한 최종 종합

**종료 조건:** 같은 문장이라도 실제 context에 따라 호출 도구가 달라지고, runtime의
cancellation 억제, 동기 event-loop blocking, step 우회와 source ref 위조가 성공
결과에 반영되지 않는다.

### `DEC-05 Hermes Runtime Adapter`

- HealthMes system policy를 항상 주입
- request/turn ID, privacy scope, 남은 예산과 deadline을 매 iteration에 전달
- configured model/provider identity를 매 iteration에 고정하고 응답 변경을 거부
- canonical request fingerprint를 응답 correlation과 idempotency key에 결합
- 정책으로 허용된 HealthMes capability만 virtual tool allowlist로 노출
- Hermes 내부 tool prefix를 adapter의 opaque mapping으로 캡슐화
- 정확히 한 번의 model call 뒤 unexecuted tool call 또는 draft만 수신
- correlation, model, usage, tool allowlist와 structured output을 fail-closed 검증
- capability discovery와 model iteration 각각에 남은 turn deadline을 강제
- decoded HTTP response를 streaming으로 읽고 2 MB에서 즉시 중단
- `Accept-Encoding: identity`만 허용해 압축 해제 bomb을 body read 전에 거부
- remote HTTPS runtime은 API key 없이는 구성하지 못하게 차단
- 명시적 capability refresh 실패 시 이전 success cache를 즉시 폐기
- Skill 설치 여부와 무관하게 같은 mandatory policy와 결과 계약을 적용
- Skill은 얇은 channel/runtime 설명으로 제한
- 현재 Hermes가 single-iteration hook을 광고하지 않으면 기존 chat endpoint로
  fallback하지 않고 명시적 unavailable 상태를 반환
- 별도 upstream 요구사항은
  [#139](https://github.com/NomaDamas/healthmes-agent/issues/139)와
  [`HERMES-MODEL-ITERATION-HOOK.ko.md`](contracts/HERMES-MODEL-ITERATION-HOOK.ko.md)
  에서 추적

**종료 조건:** HealthMes core가 Hermes 내부 클래스나 tool prefix에 직접 의존하지
않고, fake transport에서는 전체 iteration 계약이 통과하며, hook이 없는 실제 Hermes는
지원되는 것처럼 가장하지 않는다.

### `DEC-06 Decision Finalizer`

- tool result에서 반환된 `source_refs` allowlist 생성
- 최종 답변의 참조와 limitation 검증
- `DecisionRecord` 자동 저장
- 저장 실패와 불완전 판단을 명시적 상태로 반환

**종료 조건:** 행동 제안이 포함된 모든 성공 응답에 검증된 DecisionRecord가 있다.

### `DEC-07 Data completeness`

- Open Wearables normalized summary의 안정적 source reference 또는 local mirror
- ActivityWatch 자동 주기 import
- iOS capability 범위 안의 실제 activity 제출 경로
- 여러 기기의 겹친 활동시간 처리 정책

**종료 조건:** 선택 가능한 각 provider가 freshness, coverage와 provenance를 반환한다.

### `DEC-08 End-to-end verification`

- 모호한 질문에서 LLM tool selection 검증
- 첫 결과에 따라 추가 영역을 조회하는 multi-turn tool test
- privacy Level별 허용과 거부
- 누락 데이터를 0으로 바꾸지 않는 테스트
- source reference 위조 거부
- 최종 DecisionRecord 저장 테스트

**종료 조건:** 다음 전체 흐름이 자동 테스트로 증명된다.

```text
자연어 질문
  -> LLM 자율 도구 선택
  -> 권한과 privacy가 적용된 context
  -> 여러 영역 종합
  -> 검증된 source_refs
  -> DecisionRecord 저장
```

현재 E2E는 다음 실제 저장 경로를 사용한다.

```text
"오늘 집중이 흐트러지고 피곤한데,
 100mg 카페인 커피를 더 마시면서 계속 일해도 될까?"
        |
        v
Activity 저장 이벤트와 focus 집계
        |
        v
Open Wearables local sleep snapshot
        |
        v
Calendar local mirror
        |
        v
Nutrition confirmed caffeine ledger
        |
        v
네 domain의 content digest가 있는 SourceRef 재검증
        |
        v
DecisionRecord 저장
        |
        v
DB dispose/reopen 뒤 같은 record 재조회
```

Activity 자료가 없으면 첫 결과 뒤 즉시 `needs_clarification`으로 끝내며 Wearable,
Calendar와 Nutrition을 관성적으로 조회하지 않고 record도 저장하지 않는다.
privacy 거부, missing/partial data, source ref 위조와 저장 실패는 같은 production
component를 사용하는 focused failure tests에서 별도로 검증한다.

## 14. 채택하지 않는 대안

### 고성능 LLM에 DB 직접 개방

권한, retention, SQL 안정성, 개인정보와 재현성을 모델 성능에 의존하므로 기각한다.

### HealthMes 핵심을 하나의 Skill에 구현

Skill 미로드, prompt drift와 runtime 교체 시 핵심 보장이 사라지므로 기각한다.

### Hermes core에 HealthMes 규칙 직접 삽입

업스트림 동기화와 제품 소유권이 꼬이므로 기각한다. 필요한 generic hook만 별도
Hermes PR로 제안한다.

### 새 LLM runtime 전체 재구현

Hermes가 이미 provider, tool loop, session과 channel을 제공하므로 MVP에서는
중복 구현이다. HealthMes-owned interface와 adapter만 만든다.

## 15. 완료 정의

이 개선은 문서나 Skill 추가만으로 완료되지 않는다.

1. 고정 `question_kind` 없이 자연어 질문을 받을 수 있다.
2. LLM이 상황에 따라 서로 다른 도구를 선택하고 추가 조회할 수 있다.
3. Context Access Layer가 권한, retention과 privacy를 코드로 강제한다.
4. Domain Provider가 정확한 수치와 전문 정책을 소유한다.
5. 최종 답변은 실제 tool output의 `source_refs`만 사용할 수 있다.
6. 행동 제안은 자동으로 `DecisionRecord`에 저장된다.
7. Hermes 없이도 계약 테스트가 가능하고 Hermes는 교체 가능한 adapter다.
8. UI 구현 없이 엔진과 runtime 연결의 end-to-end 테스트가 통과한다.
9. runtime이 cancellation을 억제하거나 이전 결과 snapshot을 변경해도 반환 결과와
   canonical trace가 deadline 이후 변하지 않는다.
10. runtime 또는 provider가 event loop를 동기적으로 막아도 API 호출은 wall-clock
    deadline 안에 실패로 반환된다.
11. timeout으로 worker가 종료되지 않아도 같은 agent가 새 thread를 계속 만들지 않고
    이후 요청을 fail-fast 한다.
12. 각 tool 호출 전후 consent revision이 달라지면 결과를 버리며,
    `off -> on` 경합도 허용된 것으로 오인하지 않는다.
13. finalization의 payload 생성 또는 flush가 deadline을 넘기면 commit하지 않는다.
14. Calendar sync, sleep scheduler와 sleep web 경로는 disconnect 뒤 stale backend를
    호출하지 않고 reconnect 뒤 새 credential generation으로 backend를 재생성한다.
15. commit 시작 전 timeout은 늦은 `DecisionRecord` 생성을 영구 차단하고, commit
    시작 후 timeout은 `UNKNOWN`으로 반환한 뒤 request ID 조회로만 결과를 복구한다.
