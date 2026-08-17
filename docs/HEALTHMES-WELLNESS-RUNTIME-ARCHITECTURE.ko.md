# HealthMes 통합 Wellness Runtime 아키텍처

> **결정일:** 2026-08-16
>
> **상태:** PR #138의 canonical 아키텍처이자 현재 구현 기준. 구현과 문서가
> 다르면 이 문서의 책임 경계를 기준으로 수정한다.
>
> **범위:** 자유 형식 wellness 판단, HealthMes MCP, 도메인 저장 경계,
> 조건부 판단 기록, iPhone Screen Time 수집 lifecycle과 입력 설정 계약.

## TLDR

HealthMes의 자유 형식 wellness 질문 경로는 하나다.

```text
REST / Future Channel Wrapper / Proactive / Scheduled
                    |
                    v
        HealthMesDecisionService
                    |
                    v
       POST /v1/wellness-decisions
       또는 같은 내부 service 호출
                    |
                    v
       Hermes /v1/responses 한 번
                    |
          autonomous LLM/tool loop
                    |
                    v
       filtered HealthMes MCP 하나
                    |
     +--------------+--------------+--------------+
     |              |              |              |
 Activity       Nutrition       Calendar       Wearable
     |              |              |              |
     +--------------+--------------+--------------+
                    |
                    v
 source_refs 검증 + 필요한 경우만 compact DecisionRecord
```

`POST /v1/wellness-decisions`가 외부 제품 ingress다. 도식 안의 Hermes
`/v1/responses`는 HealthMes가 내부에서 호출하는 runtime 계약이며, 앱이나
사용자가 선택할 수 있는 두 번째 제품 경로가 아니다.

두 개의 LLM loop를 합치는 방식은 다음과 같다.

- **HealthMes**가 제품 ingress, 데이터, 도구 계약, 저장·보존, 결과 검증을
  소유한다.
- **Hermes**가 한 `/v1/responses` 요청 안에서 질문 해석, 도구 선택, 반복 조회와
  최종 종합을 소유한다.
- HealthMes가 Hermes에게 한 번씩 생각을 요청하고 직접 tool loop를 돌리는
  split-runtime은 사용하지 않는다.
- 폐기된 split-runtime adapter, 공개 builder와 계약 문서는 제거한다.
- MVP는 subagent를 spawn하지 않는다. 하나의 Hermes turn이 필요한 HealthMes
  도구를 직접 선택한다. 향후 위임을 추가해도 외부 ingress와 데이터 접근 계약은
  그대로 유지해야 한다.

## 1. Capability Boundary

### HealthMes가 소유하는 것

```text
제품 REST/internal ingress
DecisionRequest와 DecisionResult 계약
Activity / Nutrition / Calendar 저장과 검색
Open Wearables 접근 adapter와 wearable mirror
입력 설정, 보존기간과 삭제
Context Access Layer와 source_refs
조건부 compact DecisionRecord
VLM/텍스트/음성 intake command 계약
```

### Hermes가 소유하는 것

```text
LLM provider 호출
자연어 질문 해석
허용된 MCP 도구의 자율 선택
도구 결과를 본 뒤 추가 도구 호출
여러 wellness domain의 종합
strict HealthMes 최종 envelope 생성
```

Hermes는 HealthMes와 동등한 상위 제품이 아니다. HealthMes가 사용하는 교체 가능한
agent runtime이다. 현재 runtime이 Hermes라는 뜻이지, HealthMes의 데이터와 제품
정책을 Hermes가 소유한다는 뜻은 아니다.

### Skill이 소유하는 것

Skill은 실행 엔진이나 DB adapter가 아니라 검토된 도메인 지침이다.

```text
도구를 언제 참고할지
전문 영역의 hard boundary
불확실성과 추가 질문 표현
섭취 기록과 섭취 전 질문의 대화 차이
```

권한, 보존기간, 정확한 합계, source 검증과 저장 여부는 Skill 문서에 맡기지 않고
HealthMes 코드가 강제한다.

## 2. 공식 판단 경로는 하나

### 자유 형식 reasoning

다음 입력은 모두 같은 `HealthMesDecisionService`를 호출한다.

```text
REST       -> DecisionIngress.REST
Channel    -> DecisionIngress.CHANNEL
Proactive  -> DecisionIngress.PROACTIVE
Scheduled  -> DecisionIngress.SCHEDULED
```

현재 concrete channel 구현은 UI-neutral `DecisionChannelAdapter`다. 이 adapter는
`source`, `session_id`, privacy, budget과 hints를 보존해 canonical service를
정확히 한 번 호출한다. 실제 Telegram, iOS, Android 또는 웹 inbound는 구현하지
않았으며, 디바이스/채널 팀이 이 계약을 감싸야 한다. 그 wrapper가 별도 LLM loop를
만들거나 Hermes를 직접 호출해서는 안 된다.

서버가 owner, timezone, local/hosted execution scope와 요청 예산을 구성한다.
클라이언트가 owner ID나 보존기간을 임의로 넣어 조회 범위를 바꾸지 않는다.

```text
DecisionServiceRequest
  -> server-owned DecisionRequest
  -> HealthMesDecisionEngine
  -> HermesResponsesDecisionAgent
  -> Hermes POST /v1/responses
  -> HealthMes MCP 반복 호출
  -> strict DecisionDraft
  -> DecisionFinalizer
  -> DecisionResult
```

Hermes CLI나 Hermes `/v1/responses`를 사용자가 직접 호출한 결과는 HealthMes 제품
판단이 아니다. 그것은 HealthMes ingress, source 검증과 조건부 기록을 우회한다.

### 시작 순서와 지연 runtime 검증

HealthMes의 `/mcp`가 먼저 열려야 Hermes가 도구를 등록할 수 있으므로 두 프로세스가
서로의 startup을 기다리게 만들지 않는다.

```text
bootstrap / migration
  -> Open Wearables와 background workers
  -> HealthMes 시작
  -> HealthMes /health와 /mcp 준비
  -> optional Hermes decision runtime 시작
  -> Hermes runtime-health 준비
  -> 첫 wellness 질문에서 profile/model/toolset 검증
```

Docker Compose에서도 `hermes-decision`만 `healthmes: service_healthy`에
의존한다. HealthMes는 Hermes에 역의존하지 않는다. 따라서 Hermes가 아직
설정되지 않았거나 일시적으로 내려가 있어도 core ingest, 저장, `/health`와
`/mcp`는 시작된다.

첫 질문의 lazy 검증이 실패하면 그 요청은 안전하게 `blocked`로 반환한다. 검증
성공 상태를 거짓으로 캐시하지 않으므로 다음 질문은 runtime 검증을 다시 시도한다.
`HealthMesDecisionEngine.astart()`는 배포 도구가 명시적으로 readiness를
확인하고 싶을 때 사용할 수 있는 선택 API로 남지만 FastAPI lifespan의 선행
조건은 아니다.

### bounded command는 두 번째 reasoning 경로가 아니다

사용자가 이미 의도를 확정한 쓰기는 별도 command로 남을 수 있다.

```text
식사 섭취 확정      -> Nutrition ingest command
입력 설정 변경      -> Input settings command
캘린더 제안 승인    -> Calendar confirmation command
```

이 command는 자유 형식 질문을 해석하거나 여러 domain을 자율 검색하지 않는다.
새로운 wellness 판단이 필요하면 같은 `HealthMesDecisionService`를 호출해야 한다.
일반 `/mcp`에는 임의 DecisionRecord를 만드는 범용 mutation tool이 없다.

## 3. HealthMes MCP 하나의 의미

MCP 서버가 하나라는 것은 모든 데이터를 한 DB나 한 테이블에 섞는다는 뜻이 아니다.
Hermes가 보는 **제품 데이터 도구의 입구가 하나**라는 뜻이다.

Decision runtime에 노출되는 도구는 정확히 다음 6개다.

```text
search_activity
search_nutrition
search_calendar
search_wearable
list_wellness_skills
read_wellness_skill
```

앞의 네 도구는 데이터를 읽고, 뒤의 두 도구는 검토된 Skill을 읽는다. decision
profile에는 mutation, raw SQL, terminal, browser, memory, writable Skill,
direct Open Wearables MCP를 노출하지 않는다.

```text
Hermes LLM
   |
   | mcp__healthmes__search_*
   v
DecisionContextSearchSessionService
   |
   v
Context Access Layer
   |
   +-- Activity provider  -> HealthMes DB
   +-- Nutrition provider -> HealthMes DB
   +-- Calendar provider  -> HealthMes DB mirror
   +-- Wearable provider  -> retained mirror 또는 bounded OWClient
```

LLM은 `question_kind -> 고정 domain 표`를 따르지 않는다. 첫 결과의
`coverage`, `freshness`, `limitations`를 보고 다른 domain이나 기간을 추가로
조회할 수 있다.

검색은 기본적으로 한 Hermes agent가 수행한다. 별도 wearable, nutrition 또는
calendar subagent를 만들지 않아도 각 tool이 domain 경계와 source provenance를
보존한다. subagent는 병렬 비용, 격리 또는 장시간 작업이 실제로 필요해질 때의
확장 수단이지 현재 데이터 통합의 전제조건이 아니다.

hosted multi-user 제품에서는 이 단일-owner 계약을 재사용하면 안 된다. 그때는
HealthMes가 서명한 request-scoped principal envelope 또는 사용자별 MCP session이
별도 설계돼야 한다.

### Hermes API server tool surface

`/v1/responses`는 Hermes의 `platform_toolsets.api_server` 설정을 사용한다.
server 이름만 허용하면 그 server의 mutation tool까지 노출되므로 두 단계 필터를
모두 사용한다. 또한 `platform_toolsets.api_server: [healthmes]` 하나만으로
native tool이 절대 노출되지 않는다고 가정하지 않는다. credential과 설정 상태에
따른 toolset 복구를 막기 위해 bootstrap은 API server용 deny-by-default profile을
함께 생성한다.

```yaml
platform_toolsets:
  api_server:
    - healthmes

agent:
  # 실제 목록은 bootstrap이 Hermes native toolset catalog에서 생성한다.
  # healthmes MCP는 native toolset이 아니므로 이 목록에 넣지 않는다.
  disabled_toolsets:
    - web
    - search
    - x_search
    - terminal
    - file
    - browser
    - delegation
    - memory
    - skills

mcp_servers:
  healthmes:
    tools:
      include:
        - search_activity
        - search_nutrition
        - search_calendar
        - search_wearable
        - list_wellness_skills
        - read_wellness_skill
```

실제 include 목록은 코드의 decision-read profile이 정본이며 위 목록은 최소
형태다. `healthmes`는 유일한 제품 MCP server 이름이다. Hermes 내장
`skills_list`, `skill_view`, `skill_manage`, HealthMes mutation tools, terminal,
file write, browser, delegation과 direct `open_wearables` MCP는 wellness decision
turn에 노출하지 않는다. HealthMes response adapter도 설정만 신뢰하지 않고 실제
transcript의 tool name allowlist를 다시 검사한다. 사후 검증은 이미 실행된
mutation을 되돌릴 수 없으므로 **등록 전 include filter가 1차 경계**다.

배포 시작 검사는 다음 두 검증을 모두 통과해야 한다.

1. 렌더된 Hermes config에는 `healthmes` MCP 하나와 정확한
   `mcp_servers.healthmes.tools.include` 목록만 존재한다.
2. 인증된 `GET /v1/toolsets` 결과에서 API server용 native toolset이 하나라도
   `enabled=true`면 HealthMes decision runtime은 fail closed한다.

`GET /v1/toolsets`는 native toolset 검사용이며 MCP 도구 allowlist의 정본은
렌더된 config와 HealthMes의 decision-read profile이다. transcript 검사는
잘못된 배포를 탐지하는 2차 방어이지 실행 전 경계를 대신하지 않는다.

전용 profile은 `compression.in_place: true`도 필수로 선언한다. Hermes가 긴
request-scoped transcript를 압축하더라도 session ID를 회전시키지 않아야
HealthMes가 `/v1/responses`에서 받은 정확한 session을 turn 종료 후 삭제할 수
있다. 이 값이 없거나 boolean `true`가 아니면 profile validation과 runtime
attestation이 시작 전에 실패한다.

### Legacy cron migration

`scripts/bootstrap.py`는 더 이상 일반 Hermes home에 wellness reasoning job,
Skill, snapshot script 또는 채널 설정을 설치하지 않는다. 대신 기존 배포를
단일 reasoning ingress로 옮길 때
`$HERMES_HOME/cron/jobs.json`에서 다음 항목만 제거한다.

1. 과거 bootstrap의 `origin.source=healthmes-bootstrap` 소유권 marker가 있는 job
2. marker 도입 전의 알려진 briefing declaration과 모든 관리 필드가 정확히 같은 job

이름만 같은 사용자 job, 수정된 job, 외부 origin job과 알 수 없는 record는 모두
보존한다. no-op과 dry-run은 파일 bytes를 바꾸지 않으며, malformed/symlinked
storage 또는 compare-before-replace 중 감지된 변경은 fail closed한다. 다만
legacy Hermes scheduler와 공유하는 cross-process lock은 vendor 계약에 없으므로
migration 실행 중에는 해당 scheduler를 중지하거나 일시 정지해야 한다.

### Open Wearables

Open Wearables의 상세 DB는 별도 물리 저장소로 유지한다. 그러나 Hermes에 별도
`open_wearables` MCP 서버를 노출하지 않는다.

예:

```text
"이 커피를 마시고 계속 일해도 될까?"
  -> nutrition/caffeine 후보와 오늘 ledger
  -> 필요하면 wearable 수면/readiness
  -> 필요하면 activity 연속 작업/휴식
  -> 필요하면 calendar 다음 일정
  -> 현재 시각까지 종합
```

고정 `question_kind` resolver는 기존 호출자 호환용 preset일 뿐 공식 자연어
reasoning 경로가 아니다.

### 실행 전·후 검증

1. 렌더된 Hermes decision config는 제품 MCP로 `healthmes` 하나만 가진다.
2. `mcp_servers.healthmes.tools.include`는 위 6개와 정확히 일치한다.
3. Hermes native toolset은 deny-by-default다.
4. runtime 시작 시 live tool profile과 model route를 검증하고 drift가 있으면
   fail closed한다.
5. 실행 후 transcript의 tool name, call/output pair와 source ref를 다시 검증한다.

사후 검증만으로 이미 실행된 mutation을 되돌릴 수 없으므로 config/include 검사가
1차 경계이고 transcript 검사는 2차 경계다.

## 4. LLM, Gateway와 Domain Provider

세 책임은 서로 대체하지 않는다.

| 계층 | 책임 | 하지 않는 일 |
|---|---|---|
| Hermes LLM | 무엇을 알아볼지 결정하고 여러 결과를 종합 | DB 직접 조회, 보존기간 계산 |
| Context Access Layer | 요청한 자료를 실제 제공할 수 있는지 검사 | 질문 의미 분류, 최종 조언 |
| Domain provider | 정확한 수치, 집계, coverage와 source_refs 계산 | “커피를 마셔라” 같은 최종 판단 |

Context Access Layer는 고정 질문 표가 아니다. LLM이 요청한 조회에 대해 다음만
결정론적으로 검사한다.

```text
현재 domain 사용 동의
실행 위치와 privacy level
사용자 retention cutoff
timezone과 기간
row/byte/call budget
현재 source generation과 freshness
```

고성능 LLM을 사용해도 삭제된 행, stale device revision, timezone cutoff, 중복
합산과 DB transaction을 확실하게 지키지는 못한다. 따라서 LLM에 DB나 자유 SQL을
직접 주지 않는다.

## 5. 중앙 Personal Data Node와 저장 경계

HealthMes는 하나의 **논리적 Personal Data Node**를 사용하지만 물리 저장은 데이터
성격에 맞게 분리한다.

```text
Personal Data Node
|
+-- healthmes database
|   +-- Activity WellnessEvent
|   +-- Nutrition/caffeine WellnessEvent와 confirmation
|   +-- CalendarEventMirror
|   +-- normalized wearable snapshot/provenance
|   +-- input settings, retention, cursors, source refs
|   +-- optional compact DecisionRecord
|
+-- open-wearables database
|   +-- provider 상세 수면
|   +-- workout 상세
|   +-- health scores
|   +-- 고빈도 wearable timeseries
|
+-- HEALTHMES_DATA_DIR
|   +-- 사진, 음성, raw ingest, 큰 object
|
+-- Hermes runtime state
    +-- request-scoped transcript와 runtime metadata
```

Activity, Nutrition과 Calendar는 HealthMes DB 안에서 table, event type, index와
retention class로 논리 분리한다. 상세 wearable 원본은 Open Wearables DB에 둔다.
HealthMes는 검색에 필요한 retained mirror와 provenance만 저장하고 필요할 때
`OWClient`로 상세 데이터를 bounded 조회한다.

Hermes는 어느 DB도 직접 읽지 않는다. HealthMes MCP가 각 저장소 차이를 숨기는
통합 인터페이스다.

현재 backup은 Personal Data Node 전체의 완전 복구본이 아니다. HealthMes DB,
`media/`, `raw_ingest/`, 설정된 경우의 Open Wearables dump와 Hermes home을
포함하는 부분 snapshot이다. 외부 OAuth credential과 포함되지 않은 runtime
volume은 다시 연결해야 한다.

## 6. Source Refs

`source_refs`는 의료적 증명이 아니라 답변에 실제 사용한 데이터의 추적 주소다.

```json
{
  "reference_id": "sr_0123456789abcdef0123456789abcdef",
  "domain": "activity",
  "source_id": "wellness-event-uuid",
  "observed_at": "2026-08-16T09:00:00+09:00",
  "derived_by": "activity-hour-summary.v1"
}
```

HealthMes는 다음을 검증한다.

- Hermes가 사용했다고 쓴 ref가 실제 tool output에 있었는가
- 그 source가 아직 보존기간 안에 있는가
- 사용자가 해당 domain을 계속 허용하는가
- provider generation이나 calendar 연결이 바뀌지 않았는가

사진 bytes, 앱 창 제목과 전체 wearable 시계열을 모든 질문에 보내지 않는다.
반대로 질문에 꼭 필요하고 사용자가 허용한 identity detail은 aggregate-only
규칙으로 무조건 막지 않는다. 원칙은 전면 금지가 아니라 **필요한 최소 범위**다.

## 7. 조건부 Compact DecisionRecord

모든 질문을 영구 저장하지 않는다.

| 결과 | 기본 저장 |
|---|---|
| 단순 조회·요약 | 저장하지 않음 |
| 구체적인 행동 변경 제안 | compact record |
| 행동 가능한 중요 위험 경고 | compact record |
| 명시적 추적 요청 | compact record |
| 실제 식사·활동 기록 | 해당 domain event |
| 설정·캘린더 mutation | 해당 command workflow audit |

compact record의 요약은 LLM 자유 텍스트를 저장하지 않는다. HealthMes가 검증된
`action`/`risk`/`explicit_tracking` intent에서 만든 고정된 category-only 문장만
저장한다. runtime/model, intent, confidence, `source_refs`, 안전한 limitation과
시각은 함께 저장하지만 원문 질문, 전체 답변, model-authored `record_summary`,
transcript, 전체 tool payload, 사진·음성 bytes는 복제하지 않는다.

자유 형식 판단의 write authority는 `DecisionFinalizer` 하나뿐이다. 단,
캘린더 confirmation처럼 사용자가 이미 의도를 확정한 bounded internal command는
자기 workflow 안에서만 제한된 감사 레코드를 만들 수 있다. routine capture와
summary는 DecisionRecord를 만들지 않는다.

`DecisionRecord`는 `decision` 데이터 클래스의
`1d/7d/14d/30d/90d/forever` 정책을 따른다. read API도 `expires_at <= now`인
레코드를 반환하지 않아야 하며 maintenance 지연이 privacy 경계 지연으로 이어지지
않아야 한다.

Hermes 호출은 `store=false`이고 장기 memory를 사용하지 않는다. 성공 session은
turn 종료 후 bounded cleanup한다. 현재 Hermes 실패 응답은 session ID를 항상
제공하지 않으므로 실패 session은 전용 state 경로와 짧은 TTL purge로 제한한다.
이 transient state는 DecisionRecord나 사용자 장기 정본이 아니다.

## 8. iPhone Screen Time

iPhone Screen Time은 별도 wellness domain이 아니라 `activity monitoring` 입력이다.

```text
Apple authorization
  -> 완료된 local-hour aggregate
  -> 기기 내 app identity 가명화
  -> 제외 앱 source-side 제거
  -> bounded local outbox
  -> POST /v1/activity/ios/report
  -> HealthMes activity WellnessEvent
```

저장소 코드가 제공하는 lifecycle:

```text
권한 승인 성공 -> absent stable instance CAS 등록 -> 즉시 첫 sync
foreground     -> catch-up sync
background     -> OS가 허용한 기회에 best-effort sync
offline        -> bounded outbox 후 다음 기회에 재전송
설정 변경      -> 같은 single-flight pipeline 재실행
```

등록은 기존 input-control descriptor/ETag 계약으로만 수행하며, 기존
disabled/paused instance는 자동으로 다시 enable하지 않는다. foreground,
background와 observer 경로는 중앙 설정을 읽기만 한다.

일반/미지원 빌드는 사용시간 0을 위조하지 않고 unavailable을 보고한다.

다음은 코드만으로 완료할 수 없는 Apple 외부조건이다.

- App & Website Usage entitlement 승인
- 실제 Team ID와 provisioning/signing
- iOS 26.4 이상, EU 기기·Apple Account와 단일 data-access 앱 조건
- 실제 iPhone dogfood

따라서 “자동 수집”은 지원 조건과 사용자 승인이 충족된 뒤 lifecycle 기회마다
자동 sync한다는 뜻이지, iOS에서 24시간 임의 daemon을 보장한다는 뜻이 아니다.
실제 권한 화면과 설정 UI는 device team 범위다.

## 9. 통합 입력 설정

데스크톱 웹과 미래 모바일 UI는 같은 UI-neutral API를 사용한다.

```text
GET  /v1/inputs
GET  /v1/inputs/{source_id}
PUT  /v1/inputs/{source_id}/settings
```

descriptor에는 capability, 연결/권한 상태, action metadata, privacy limitation,
retention preset과 revision이 포함된다.

설정 저장은 다음 순서를 사용한다.

```text
GET descriptor와 ETag
  -> 사용자가 편집
  -> PUT + If-Match
  -> 성공하면 새 descriptor/ETag
  -> 409이면 최신 GET 후 사용자 변경을 재적용
```

동일 revision을 사용한 경쟁 변경은 하나만 성공한다. input enable/exclusion,
decision domain access와 retention 변경은 같은 write fence 안에서 원자적으로
적용한다.

## 10. 완료 기준과 비범위

PR #138 구현 기준:

- 공식 자유 형식 reasoning ingress가 하나다.
- Hermes `/v1/responses`가 유일한 LLM/tool loop다.
- decision runtime에 HealthMes MCP 6개 읽기 도구만 노출된다.
- Activity, Nutrition, Calendar와 Wearable을 질문에 따라 자율 조합한다.
- 실제 tool output에 없는 source ref를 성공 답변으로 저장하지 않는다.
- 단순 조회는 미저장, 행동·위험·명시 추적만 compact 저장한다.
- iPhone Screen Time lifecycle/outbox code와 입력 설정 CAS가 검증된다.
- UI와 `vendor/hermes-agent/`는 수정하지 않는다.

비범위:

- Apple entitlement 승인과 실제 iPhone dogfood
- hosted multi-user identity와 휴대폰 단독 hosted node
- 실시간 multi-master device sync
- 가격·과금 정책
- 완전한 Personal Data Node disaster recovery
- 디바이스 UI
