# HealthMes iOS/watchOS Companion

Full native companion app for HealthMes Agent (issues #7, #10, #91, and
#108). The iPhone presents one bounded wellness control canvas with a
persistent voice/editable-text dock, real calendar blocks, and one primary
decision. Apple Watch remains a deliberately smaller three-second Yes/No
decision remote.

Local-first, like `apps/android-usage`: the paired base URL is the **only**
network destination in the whole project — no third-party endpoint, no
analytics, no push relay. The watch receives the pairing from the phone over
WatchConnectivity and then talks to the instance directly.

The paired instance is expected to run continuously on the user's Mac or
Linux machine. A physical iPhone cannot reach that machine through
`localhost`; production pairing requires a trusted HTTPS
`HEALTHMES_PUBLIC_BASE_URL`.

## Main 통합 책임 경계

Apple 앱은 Main runtime을 복제하지 않는 client layer다. 세 경로를 분리한다.

```text
일반 데이터/설정
iPhone · macOS · Watch -> Main REST API + /v1/inputs

자연어 wellness 판단
iPhone · macOS voice/text -> POST /v1/wellness-decisions
                            -> Decision Service -> Hermes -> HealthMes MCP

Apple Health 수집
Apple Watch -> iPhone HealthKit -> pairing별 encrypted outbox
            -> POST /v1/ingest/healthkit
            -> durable ACK + accepted forward status -> anchor 확정
```

Dashboard, alert, task, schedule와 settings는 Main REST 응답이 정본이다. 앱은
Hermes, HealthMes MCP 또는 Open Wearables를 직접 호출하지 않는다. 자유 형식
질문만 `/v1/wellness-decisions`를 사용한다.

iPhone과 macOS는 각각 하나의 Settings 화면에서
`GET /v1/setup/readiness`와 `GET /v1/inputs`를 함께 렌더링한다. source 변경은
상세 GET의 strong `ETag`를 `If-Match`로 보내며, 두 앱은 같은 서버 정본을 다시
읽어 동기화한다. Mac 설정 blob을 iPhone에 복사하지 않고 Open Wearables,
Hermes와 calendar credential도 서버 밖으로 내보내지 않는다.

HealthKit 통합의 완료 조건은 collector가 exact request bytes와 stable
`Idempotency-Key`, candidate anchors를 encrypted outbox에 먼저 저장하는 것이다.
outbox와 anchor는 `Pairing.cacheFingerprint`별로 격리한다. HTTP `202`,
`durable=true`, 일치하는 `sha256`와 `size_bytes`, 그리고 accepted forward
status를 받은 뒤에만 anchor를 확정하고 queue item을 삭제한다.
`forward_failed` 또는 `skipped_no_user`이거나 응답이 유실되면 같은 key와 같은
bytes로 재시도한다.

외부 Health Auto Export 계열 앱은 optional legacy adapter다. 별도 exporter를
설치하지 않아도 first-party iPhone collector가 동기화하는 것이 기본이며, 서버는
기존 headerless payload 자동화를 깨뜨리지 않기 위해 같은 ingest endpoint의
legacy parsing을 유지한다.

현재 `HealthKitSyncManager`는 exact bytes와 candidate anchors를 AES-GCM
outbox에 먼저 저장하고, stable `Idempotency-Key`로 재전송하며, 서버의 durable
hash/size ACK와 accepted forward status를 검증한 뒤 anchor를 확정한다. queue,
pause와 last-upload 상태는 pairing fingerprint별로 격리되고 Settings에서 retry,
pause/resume와 현재 pairing의 queue 삭제를 제어한다. simulator contract/outbox test와 unsigned build는
저장소 검증 범위이며, 실제 권한 prompt, Watch-origin sample과 background cadence는
signed hardware QA가 필요하다. 상세 계약은
[`APPLE-MAIN-INTEGRATION.ko.md`](../../docs/APPLE-MAIN-INTEGRATION.ko.md)를
따른다.

## What the app does

- **Issue #108 core IA** — one current wellness conclusion, at most one
  useful visualization, Apple/Google calendar blocks, one primary decision,
  and progressive Web detail. Explore/Settings keeps history, diagnostics,
  pairing, and storage out of the daily path.
- **Bounded command canvas** — voice and editable text share one dock.
  Commands return a bounded generated scene rather than an infinite chat
  transcript. Read-only questions cannot leak mutation controls; supported
  writes still require an exact proposal and explicit confirmation.
- **Today** — one Now energy state, one Next calendar block, and one
  explicit decision question. Yes/No calls the real schedule endpoint;
  reasoning and the exact web decision remain progressively disclosed.
  Pull-to-refresh keeps the glance leg ETag-cheap (304).
- **Alert list in §8.5 grammar** (`GET /v1/alerts`) — observation line
  (`summary`), evidence line rendered from the `evidence` facts, proposal
  line, relative fired-time, "Why this?" → in-app decision viewer. Lines the
  payload does not carry are dropped, never invented.
- **Real decision actions** — Yes →
  `POST /v1/schedule/proposals/{id}/accept`, No → `…/decline`; the in-app
  detail sheet retains the longer Apply/Keep wording where context is
  visible. A second tap elsewhere (or in Telegram) surfaces as the server's 409
  `invalid_transition` → rendered "Already resolved (accepted/declined)".
  App actions return the action-scoped `resolution_token` from the authenticated
  pending-proposal response.
- **Weekly report** — native rendering of `GET /reports/weekly.json`:
  per-day energy bars (hollow stubs for missing days), insights with
  confidence badges (high/medium/low/none), schedule adherence, alert
  digest (delivered vs fired vs budget, per rule), the week's decisions.
  The HTML page stays one toolbar tap away.
- **Decision viewer** — `SFSafariViewController` sheet over the tokenized
  viewer links (native Done/share come free). Links always come from the
  paired instance's own payloads; `healthmes://decision?url=…` deep links
  are additionally host-checked against the pairing.
- **Capture** — camera (device only) / photo picker / text / voice memo.
  Nutrition follows analyze → user review → interaction → explicit outcome:
  photos use `POST /v1/nutrition-observations/analyze` and
  `POST /v1/nutrition-observations/{id}/review`; text/voice use
  `POST /v1/intake-interactions/analyze`; only a separate
  `POST /v1/intake-interactions/{id}/outcomes` records consumed,
  not-consumed, or cancelled. Analysis and `log_consumed` intent are not
  consumption proof. Medication/symptom capture keeps
  `POST /v1/medical-records`. Offline retry must retain the same operation
  IDs, timestamp, media token, and stage so it cannot duplicate a meal or
  silently skip review.
- **Native notifications** (issue #91, parity with Android's `AlertNotifier`) —
  BGAppRefreshTask + foreground sync poll `GET /v1/alerts`, diff against a
  seen-store (exactly-once per alert), and post local notifications in the
  §8.5 grammar: observation title, evidence+proposal body, per-rule thread.
  No/Yes actions are attached only to the exact `proposal_id` correlated by
  the server and call the real endpoints from the action
  handler, confirming with an outcome notification. Tap-through opens the
  decision viewer. Badge = unresolved count.
- **Live Activity** — current focus block (from glance `next_blocks`) on
  the lock screen / Dynamic Island with timer progress; started on
  foreground refresh, updated by the background task, `staleDate = block
  end` so iOS dims it when no budget arrives. Polling only — no push token.
- **Apple Health sync** — the iPhone requests read access for supported
  heart, HRV, respiratory, oxygen, activity, distance, wrist-temperature,
  sleep-stage, and workout samples. Incremental anchored queries produce the
  native `healthmes.healthkit.v1` contract. The integration contract queues
  exact bytes in a pairing-scoped encrypted outbox and advances anchors only
  after a durable, hash-matched server ACK. Observer queries, hourly
  background delivery, app activation, and first pairing all request a sync.
  Apple Watch samples are read once through the phone's HealthKit store.
- **Localization & accessibility** — all app strings ko+en via
  `Resources/Localizable.xcstrings` (server-provided text renders
  verbatim); Dynamic Type throughout (verified at accessibility-large);
  VoiceOver labels/hints on the curve, badges, rows and buttons.

### Delivery honesty (iOS background budget)

Native notifications derive from **polling**: iOS decides when — and
whether — a `BGAppRefreshTask` runs (anywhere between ~15 minutes and a few
times a day, tied to usage/battery; simulators never run them). Opening the
app always syncs. There is deliberately **no APNs relay** (local-first), so
**Telegram remains the guaranteed-delivery alert channel**; the Settings tab
says exactly that to the user.

### Placeholder visuals

Rendering (curve geometry, colors, badge vocabulary, Live Activity layout,
widget/complication text) is engineering placeholder over stable contracts.
What a surface should *say* — state words vs numbers, urgency grades,
low-confidence blurring, night behavior — is the healthcare domain expert's
deliverable: `docs/design/WATCH-NOTIFICATIONS.ko.md` (design system:
`docs/PLAN.md` §8.5 notification grammar).

## Server contracts consumed

| Endpoint | Used by |
|---|---|
| `GET /v1/setup/readiness` | one-page iPhone/macOS setup readiness |
| `GET /v1/inputs`, `GET /v1/inputs/{source_id}`, `PUT …/settings` | server-owned input settings shared by iPhone and Mac |
| `GET /v1/briefing/glance` (ETag/304, max-age 300) | home, widgets, watch, Live Activity |
| `GET /v1/alerts?hours=24` (§8.5 grammar items) | home alert list, notifications |
| `GET /v1/schedule/proposals?status=proposed` + `POST …/{id}/accept\|decline` | proposal cards, notification actions |
| `GET /v1/goals`, `POST /v1/goals` | Plan goals, spoken weekly goals |
| `GET /v1/tasks`, `POST /v1/tasks` | Plan tasks, spoken tasks |
| `GET /v1/schedule/events?start=…&end=…` | Plan calendar timeline |
| `GET /reports/weekly.json` | report tab |
| `POST /v1/media` (multipart `file`) + `GET /v1/media/{path}` | capture upload / preview URL |
| `POST /v1/nutrition-observations/analyze` + `POST …/{id}/review` | photo analysis and explicit owner review |
| `POST /v1/intake-interactions/analyze`, `POST /v1/intake-interactions` | text/voice analysis and reviewed photo capture |
| `POST /v1/intake-interactions/{id}/outcomes` | explicit consumed/not-consumed/cancelled result |
| `POST /v1/medical-records` | medication/symptom capture |
| `POST /v1/wellness-decisions` | natural-language wellness reasoning only |
| `POST /v1/ingest/healthkit` (`healthmes.healthkit.v1`) | native HealthKit upload |

HealthKit retries must reuse one outbox item's exact bytes and
`Idempotency-Key`. The server's durable ACK must match those bytes and report
an accepted forwarding state before the pairing-scoped anchors advance.
Forwarding failures keep both the server receipt and encrypted iPhone outbox
retryable without advancing the stored HealthKit anchor. Ordinary transport,
`5xx`, durable `forward_failed`, and `skipped_no_user` responses remain on
exponential backoff. A permanent client-side rejection is retained as an
observable terminal entry and blocks only the affected HealthKit lanes until
the owner retries or removes it; independent lanes may continue. Legacy
uncommitted terminal entries created by the retired cursor-commit policy are
migrated back to retryable state on the next drain. HealthKit deletion
tombstones are retained in the native payload and raw store. The current server
returns `503 healthkit_deletion_pending` for such a batch because the Open
Wearables SDK contract has no deletion endpoint; the iPhone keeps the encrypted
outbox item and deletion anchor pending rather than claiming synchronization.
For compatibility, a legacy `202` with
`forward_status=deletions_recorded` is also treated as retryable and never as an
accepted anchor state. Normalized derivatives may remain until that upstream
contract adds deletion support.

Contracts are pinned twice: Swift decoding tests against
`Tests/Fixtures/{glance,alerts,weekly_report}.json`, and those same three
fixture sets validate against the server's pydantic models in CI —
`tests/api/test_glance_fixtures.py` parametrizes `glance.json` against
`GlanceOut`, `alerts.json` against `Page[AlertOut]` and
`weekly_report.json` against `WeeklyReportOut`. Editing any fixture without
running the Python suite will fail the server-side pinning test.

Datetime note: glance/alerts serialize aware-UTC (`…Z`); some store-backed
endpoints serialize sqlite's **naive** UTC datetimes
(`2026-07-11T14:23:10.355753`). `GlanceJSON.parseISO8601` accepts both —
found live, covered by `testAcceptsNaiveUTCTimestamps`.

## Generate and build

Requirements: Xcode 26.x with iOS **and watchOS** platform components, and
[XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).
The `.xcodeproj` and `Support/` plists are generated artifacts (gitignored):

```bash
cd apps/ios-companion
xcodegen generate

# iOS app + widget extension (incl. Live Activity)
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "generic/platform=iOS Simulator" build CODE_SIGNING_ALLOWED=NO

# Screen Time opt-in request. The script type-checks Apple's export APIs in
# the selected SDK. Unsupported SDKs still build, but select the explicit
# fail-closed adapter instead of pretending the collector is eligible.
bash Scripts/build-screen-time-opt-in.sh build

# watchOS app + complication extension
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesWatchApp \
  -destination "generic/platform=watchOS Simulator" build CODE_SIGNING_ALLOWED=NO

# unit tests (contract decoding, request builders, notification grammar,
# ETag flow, seen-store, focus-block selection) + UI tests (self-skip
# without a live paired instance)
xcodebuild test -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "platform=iOS Simulator,name=iPhone 17 Pro,OS=26.2" CODE_SIGNING_ALLOWED=NO
```

CI and simulator commands remain unsigned. For hardware QA, select one
development team for every app/extension target. If the default identifiers
conflict with another account, override the project settings
`HEALTHMES_BUNDLE_ID_PREFIX` and `HEALTHMES_APP_GROUP_ID`. See
`docs/qa/APPLE-REAL-DEVICE-QA.ko.md`.

## Live smoke test (what "works" means here)

The end-to-end flow the acceptance sketch describes was exercised against a
real instance on this machine (see "Verification status"):

```bash
# 1. serve an isolated instance (simulators share the Mac's loopback)
HEALTHMES_PORT=8199 HEALTHMES_API_TOKEN= \
HEALTHMES_DATABASE_URL="sqlite:////tmp/hm-smoke/healthmes.db" \
HEALTHMES_DATA_DIR=/tmp/hm-smoke HEALTHMES_PUBLIC_BASE_URL=http://127.0.0.1:8199 \
  uv run python -m healthmes serve   # create the schema first (Base.metadata.create_all)

# 2. install + pre-pair the simulator app (token-less loopback pairing)
xcrun simctl boot "iPhone 17 Pro"
xcrun simctl install booted <BUILT_PRODUCTS_DIR>/HealthMesCompanion.app
xcrun simctl spawn booted defaults write group.com.healthmes.companion \
  healthmes.pairing.baseURL "http://127.0.0.1:8199"
xcrun simctl launch booted com.healthmes.companion

# 3. run the UI acceptance tests against it
xcodebuild test … -only-testing:HealthMesCompanionUITests
```

## Pairing flow

1. In the Mac app, choose **Settings → Set up this Mac**.
2. Scan the five-minute QR with the iPhone Camera. iOS opens
   `healthmes://pair`, exchanges the signed one-time code, stores the returned
   token in Keychain, shows the connection result, and starts the first sync.
   The QR never contains the long-lived bearer token.
3. Advanced users can still enter an existing instance's base URL and token
   manually under **Settings → Advanced → Self-host pairing**.
4. Widgets read the pairing through the App Group
   (`group.com.healthmes.companion`); the token lives in the Keychain (App
   Group access group, unsigned-simulator fallback documented in
   `Pairing.swift`). The watch gets it over WatchConnectivity.
5. Input settings are not copied through WatchConnectivity. iPhone and Mac
   read the same server-owned `/v1/inputs` descriptors.
6. **Unpair** clears pairing, snapshot cache, seen-alerts store and the watch.
   It also deletes the old pairing's encrypted HealthKit queue, anchors,
   pause state and last-upload timestamp from this iPhone. Data already
   stored on the server is unchanged.

Transport policy: production pairing requires **HTTPS**. Plain HTTP is
accepted only for same-device loopback hosts (`localhost`, `127.0.0.0/8`,
`::1`) used by local development and the Mac runtime. Private-LAN HTTP is
rejected before a long-lived bearer token can be stored or returned. The
scoped `NSAllowsLocalNetworking` entitlement remains for loopback tooling;
it is not permission to pair over cleartext LAN. A Mac setup QR is offered
to iPhone only when the configured public base URL is HTTPS.

## Layout

```
project.yml                  # XcodeGen spec (6 targets, 3 schemes)
Scripts/                     # SDK capability probe + opt-in unsigned build
Sources/Shared/              # PLATFORM-AGNOSTIC (Foundation+Security only;
                             # no UIKit/SwiftUI/ActivityKit) — compiled into
                             # every target and reusable verbatim by the
                             # macOS glance app (issue #11):
  GlanceContract.swift         glance Codable models + tolerant ISO parser
  GlanceClient.swift           bearer + If-None-Match/ETag + max-age
  GlanceSnapshotCache.swift    App Group cached payload + validator
  Pairing.swift                PairingStore: Keychain token + App Group URL
  GlanceFormat.swift           placeholder text renderers (expert-owned)
  JSONValue.swift              free-form JSON fields (evidence, error detail)
  AlertsContract.swift         GET /v1/alerts models + Page envelope
  ReportContract.swift         GET /reports/weekly.json models
  ScheduleContract.swift       proposals + accept/decline vocabulary
  CaptureContract.swift        media + nutrition staged-write + medical bodies
  HealthMesAPI.swift           request builders + client + error envelope
  NotificationContent.swift    §8.5 grammar → notification content (pure)
  SeenAlertsStore.swift        exactly-once alert notification bookkeeping
  FocusBlock.swift             current/upcoming block selection
  CurveGeometry.swift          curve gap/dot/segment honesty rules (iPhone
                               home curve + mac popover/widgets/saver)
Sources/SharedActivity/      # ActivityKit attributes (iOS app + widgets only)
Sources/App/                 # iOS app: tabs, home, report, capture, viewer,
                             # notifications, BG refresh, Live Activity ctrl
Sources/SharedWidget/        # widget timeline provider + gauge (both platforms)
Sources/Widgets/             # iOS widget bundle + Live Activity UI
Sources/WatchApp/            # watch app + WCSession pairing receiver
Sources/WatchWidgets/        # watch complications
Resources/                   # Localizable.xcstrings (en source + ko)
Tests/                       # host-less XCTest bundle + contract fixtures
UITests/                     # XCUITest daily-loop acceptance (self-skipping)
```

## Verification status

Verified at authoring time on this machine (Xcode 26.3, iOS 26.2 /
watchOS 26.2 simulators, XcodeGen 2.45.4):

- `xcodegen generate`; the normal iOS and watchOS schemes build unsigned.
  `HealthMesCompanionScreenTimeOptIn` also builds on Xcode 26.3/iOS SDK 26.2,
  but intentionally selects `ios_screen_time_export_sdk_unavailable` because
  that SDK does not expose the required App & Website Usage export symbols.
- **The host-less unit-test suite passed** on an iPhone simulator (iOS 26.2):
  glance/alerts/weekly-report contract decoding (incl. empty shapes and the
  naive-datetime variant), multipart/JSON request builders byte-for-byte,
  §8.5 notification-content mapping, error-envelope → "already resolved"
  mapping, seen-store exactly-once semantics, focus-block selection, ETag
  200→304 flow, and Screen Time report serialization, retention-window
  planning, pseudonymization/key-rotation fencing, privacy-aware coverage,
  state-store, and injected sync-service contracts. The compile-gated Apple
  collector path was not compiled or exercised.
- **4 UI acceptance tests passed against a LIVE instance** (`python -m healthmes
  serve` on :8199, seeded alert/proposal/energy rows): briefing home
  rendered live data; Report tab rendered live `weekly.json`; Yes flipped
  the seeded proposal to `accepted` server-side and the accepted block then
  appeared in glance `next_blocks`. The earlier capture smoke predates the
  review-first nutrition contract and is not evidence for the current
  analyze/review/outcome flow; that flow requires a new live QA pass. Tests
  self-skip (never fail) without a live pairing, so plain CI runs stay green.
- **Capture chain proven with the app's own bytes**: `Sources/Shared`
  compiled verbatim into a macOS CLI (also proving the issue-#11 reuse
  claim), which uploaded via `POST /v1/media` (201), created a medical
  record carrying that `media_path` (201, server attached `context.health`
  honestly degraded + `context.capture` from the app), and round-tripped
  the bytes through `GET /v1/media/{path}` (200, `image/jpeg`, identical).
- Launch smoke on the booted simulator: home renders live data (screenshot),
  Korean localization at runtime (`-AppleLanguages "(ko)"`), dark mode +
  accessibility-large Dynamic Type render without clipping; `healthmes://`
  scheme registered (system open-confirmation appears).
- Fixtures validated against the server's own pydantic models
  (`WeeklyReportOut`, `Page[AlertOut]`) via `uv run python`.

## Screen Time activity engine seam

`ScreenTimeActivityRuntime` and `ScreenTimeActivitySyncService` are
UI-neutral. The app lifecycle is connected now:

```text
explicit device-UI opt-in
  -> requestAuthorizationAndSync()
  -> aggregate + granted authorization
  -> register absent stable collector through input-control CAS
  -> first sync

app active / pairing changed / saved input configuration
  / Screen Time BGAppRefreshTask
  -> read-only central-state check
  -> the same single-flight sync + persistent outbox pipeline

persisted opt-in + authorization not yet restored
  -> cold launch / background: no permission sheet, defer upload
  -> active foreground: read-only central fence
  -> single-flight authorization restoration
  -> re-check opt-out + pairing + identity + central revision
  -> sync without collector registration
```

The device team still owns the settings screen. It should call
`requestAuthorizationAndSync()` only after pairing and an explicit user action;
an unpaired call fails before opening Apple's authorization UI. It should call
`approveExcludedAppsAndSync(_:)` after confirming the exact opaque exclusion
set. After a successful input-setting or retention revision, it should call
`inputConfigurationDidChange()` so the saved configuration gets a fresh sync.
It must not duplicate collector registration: successful explicit
authorization bootstraps an absent stable instance through the existing input
settings contract before first sync.
Foreground catch-up, pairing changes, and best-effort background scheduling
are already wired in `HealthMesCompanionApp` and
`ScreenTimeActivityRuntime`. A timezone change is detected on the next
lifecycle sync and also receives a fresh run.

Cold launch, authorization-status notifications, and background refresh never
call Apple's authorization UI. They inspect the persisted opt-in and current
status, then sync only when authorization is already usable. If status cannot
be restored without foreground interaction, they return
`ios_screen_time_reauthorization_required` and defer upload. On the first
active foreground catch-up, a persisted opt-in may restore authorization
through a single-flight request. That path checks central enabled/pause and
revision before and after the request, rechecks pairing and the remembered
collector identity, and never registers or re-enables an instance.
`requestAuthorizationAndSync()` remains the explicit new-opt-in seam and the
only path that can bootstrap an absent collector. Local opt-out cancels and
awaits any in-flight explicit authorization/bootstrap or foreground
restoration task before purging its outbox, state, and key.
A pairing change also cancels bootstrap before first sync; the explicit action
must be retried against the current pairing if that node has no registered
instance.

Each sync fetches the paired HealthMes node's current device collection
settings, removes excluded apps on-device, replaces bundle identifiers with
device-keyed HMAC pseudonyms, and uploads one authoritative snapshot to
`POST /v1/activity/ios/report`. The first authorized sync and the first sync
after a timezone change are deliberately limited to the latest completed
local hour. Later syncs in the same timezone reconcile from that consent
boundary, bounded to the last 48 completed hours and the server retention
cutoff. Denied or unavailable collection does not persist that boundary, so a
later first grant cannot backfill from the earlier denial date.

The collector ID is derived from the same device-only Keychain key as the app
pseudonym namespace rather than `identifierForVendor`. A new
`ios-collector-v1-<40 lowercase hex>` identity is disabled server-side by
default. After explicit authorization returns `aggregate + granted`, the
runtime reads `GET /v1/inputs/activity.ios-screentime` and, only when that
identity is absent, sends a CAS `PUT` containing only `instance_id`,
`platform: "ios"`, and `enabled: true`. A revision conflict causes a bounded
re-read/retry. An instance created disabled or paused by another writer is
authoritative and is never overwritten. Malformed descriptors, ETag mismatch,
transport/auth/server errors, and exhausted conflicts fail closed before
collection. Losing the Keychain key therefore cannot copy exclusions or
silently reactivate an existing centrally disabled collector.

The sync core fingerprints the device-only HMAC key locally and binds approval
to the SHA-256 digest of the exact sorted exclusion-token set. If the key or
set changes, it advances `collection_generation` when appropriate and stops
before reading Screen Time with
`ios_screen_time_exclusions_require_reapproval_after_key_change`. A future UI
must save `ios-app-v2-<key fingerprint>-<app HMAC>` tokens generated under the
current key and then call the UI-neutral `approveExcludedApps(_:)` seam. The
service rejects legacy or stale-key tokens, and clearing the list does not
silently approve a later set. Hours containing excluded or identity-missing
activity never become false zero-usage hours. Valid allowed app rows remain
usable, while a separate identity-free `coverage_only` marker partitions the
observed hour into represented app, privacy-filtered, website-only, and
unknown seconds. Its status is one of `privacy_filtered`,
`website_activity`, `unknown_activity`, or `mixed_partial`; only a genuinely
empty observed hour uses `complete`. Every observed bucket remains
authoritative, so a newer privacy-filtered snapshot deletes previously stored
private rows, and the generation/sequence fence prevents an older snapshot
from restoring them.

The pseudonym Keychain key is not loaded or created before explicit opt-in,
and an SDK-unsupported build does not create a fallback persistent device
identifier. Opt-out cancels collection, purges the dedicated outbox and local
derived state for the remembered key-derived device, then deletes both that
remembered identity and the pseudonym key. The remembered device ID is
created only after opt-in and lets cleanup resume safely after a process
restart without loading or creating a key. If identity deletion fails, a
persistent cleanup-pending fence blocks re-opt-in and key reuse until cleanup
succeeds.

Successful authoritative snapshot fences also retain the first server
response. An identical retry returns that response before evaluating later
mutable collection settings; sequence reuse with different content remains a
conflict.

Concurrent callers share a service-owned task. Authorization, input-setting,
and timezone changes that arrive during an active run coalesce into one
pending fresh run instead of being lost behind the earlier snapshot.
Cancelling a foreground waiter does not abandon an idempotent upload or
outbox write. A `BGAppRefreshTask` expiration cancels the actual shared
pipeline only when no foreground waiter is using it; an attached foreground
waiter continues safely. A pairing destination change remains the explicit
global cancellation boundary.

The local retry outbox is bounded to 8 entries and 16 MiB and has a fixed
14-day TTL. Expired entries are purged when the outbox is reopened after an
app restart and before sync/retry mutation, including before an offline
collection-state request. Its directory and atomic output file are excluded
from device backup. This transport TTL is separate from the configurable
central `activity_raw` retention policy. Retryable transport/server failures
remain in oldest-first backoff. Permanent `422` responses and non-retryable
`409` responses are retained as observable terminal quarantine entries and
are skipped by delivery, so a bad snapshot cannot block later valid snapshots
for the full TTL. `409 activity_write_conflict` remains retryable, while stale
and privacy/generation fence responses keep their explicit server semantics.

The normal build always uses an unavailable adapter. The
`HealthMesCompanionScreenTimeOptIn` scheme expresses user/product intent, not
proof that the current Apple SDK is eligible. Build it through
`Scripts/build-screen-time-opt-in.sh`; the script type-checks
`AuthorizationStatus.approvedWithDataAccess` and
`DeviceActivityData.activityData(filteredBy:using:)` against the selected SDK.
Only a successful probe injects
`HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE`, which is the sole condition that
compiles the real collector. Otherwise the opt-in build remains usable but
reports `ios_screen_time_export_sdk_unavailable` without fake zero usage.
The corresponding `project.yml` entries are required non-UI build contracts:
they select the opt-in entitlement file, inject the capability probe result,
register the Screen Time BGTask identifier, and compile the runtime seams into
the host-less test target. Removing them would make the entitlement and
lifecycle paths unbuildable rather than reduce device-team UI scope.

Repository code completion and Apple/device enablement are separate:

- Repository validation can prove contracts, lifecycle behavior, the
  fail-closed SDK adapter, server snapshot semantics, and unsigned builds.
- Apple entitlement approval, matching signed provisioning, an eligible
  device/account region, user authorization on hardware, exported Screen Time
  data, and real `BGAppRefreshTask` cadence require external approval and
  real-iPhone dogfood.

The opt-in entitlement declaration is present in
`Configurations/HealthMesCompanion-ScreenTimeOptIn.entitlements`. Actual
collection additionally requires a supporting SDK and iOS release, a signed
provisioning profile whose App ID includes both `Family Controls` and
`Family Controls App and Website Usage`, and user authorization that reaches
`approvedWithDataAccess`. Family Controls permission is required before App
Store submission. Both `approvedWithDataAccess` and
`DeviceActivityData.activityData(filteredBy:using:)` are available starting
with iOS 26.4. Customer installations can use the export only while the device
is in the EU and its Apple Account country or region is also in the EU;
Apple-provisioned development/test builds may be exercised in other regions.
Only one app per device can hold `approvedWithDataAccess`; granting it to
another app resets the previous app to `.notDetermined`. None of signing-
profile eligibility, runtime authorization, region eligibility, single-app
authorization ownership, or real-iPhone behavior is proved by this
repository's unsigned builds. The settings UI is also still device-team work.
See `docs/INPUT-CONTROL-PLANE.ko.md`.

**Not yet verified (honest list):**

- **No real device runs.** Everything below the simulator boundary is
  unproven on hardware: App Group + Keychain access-group sharing under
  real signing, WidgetKit budgets, ATS vs LAN IPs, camera capture (the
  simulator has no camera; the code path is device-only by
  `isSourceTypeAvailable`), microphone quality, real BGAppRefreshTask
  cadence (simulators never run BG tasks — the pipeline was exercised via
  the foreground-sync path it shares), Live Activity presentation on the
  lock screen / Dynamic Island (compile- and logic-tested only; simulators
  support them but starting requires app-foreground timing not driven in
  tests), notification banner delivery + action buttons under a real OS
  budget (content builder unit-tested; delivery path not UI-automated).
- **HealthKit hardware behavior is unproven.** Authorization and query code
  compile, but real permission prompts, observer cadence, anchored-query
  recovery, and Apple Watch-origin samples require a signed hardware QA pass.
- **WatchConnectivity pairing sync** still not exercised end-to-end (needs
  a paired phone+watch simulator pair or hardware); the watch app renders
  its "not paired" guidance until the first sync lands. Watch surfaces
  remain #7-era placeholders by design (expert worksheet pending).
- **No push notifications** — polling only; APNs relay is deliberately out
  of scope (local-first). Telegram stays the guaranteed channel.
- **No release signing/distribution** — no repository-owned team, profiles,
  TestFlight, or App Store setup. The Watch app is embedded in the iPhone
  target, and local developer signing is documented for hardware QA.
- Voice-capture transcription is manual (a transcript field) — no on-device
  speech-to-text yet; the server accepts `transcript` when present.
- Notification ✅/✏️/❌ buttons attach only when exactly one proposal is
  pending; a proper alert→proposal link needs a server-side FK (recorded as
  a follow-up need, matches the store's documented placeholder policy).
