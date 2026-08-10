# HealthMes iOS/watchOS Companion

Full native companion app for HealthMes Agent (issues #7, #10, #91, and
#108). The iPhone presents a shared product core — **Today, Plan,
Decisions** — around a prominent voice-only **Speak** action. Apple Watch
remains a deliberately smaller three-second Yes/No decision remote.

Local-first, like `apps/android-usage`: the paired base URL is the **only**
network destination in the whole project — no third-party endpoint, no
analytics, no push relay. The watch receives the pairing from the phone over
WatchConnectivity and then talks to the instance directly.

The paired instance is expected to run continuously on the user's Mac or
Linux machine. A physical iPhone cannot reach that machine through
`localhost`; production pairing requires a trusted HTTPS
`HEALTHMES_PUBLIC_BASE_URL`.

## What the app does

- **Issue #108 core IA** — Today defaults to Now / Next / Decision; Plan
  reads real weekly goals, tasks, mirrored calendar events, and pending
  proposals; Decisions separates pending actions from honest resolved
  history. Profile/Settings keeps pairing, notifications, weekly reports,
  and capture out of the daily path.
- **Voice-only Speak** — no text-command composer. On-device speech
  recognition creates a real task (`POST /v1/tasks`) or weekly goal
  (`POST /v1/goals`). General agent conversation remains unavailable until
  the server exposes a supported voice-command contract; the app does not
  fake one.
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
  sleep-stage, and workout samples. Incremental anchored queries upload the
  native `healthmes.healthkit.v1` contract; anchors advance only after a
  successful server response. Observer queries, hourly background delivery,
  app activation, and first pairing all request a sync. Apple Watch samples
  are read once through the phone's HealthKit store.
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
| `POST /v1/ingest/healthkit` (`healthmes.healthkit.v1`) | native HealthKit upload |

HealthKit anchors advance only after the paired personal server confirms that
the verbatim batch is durably stored. Open Wearables normalization remains
asynchronous and replayable from that raw source. HealthKit deletion
tombstones are retained in the native payload and raw store; the current
Open Wearables SDK contract has no deletion endpoint, so normalized
derivatives may remain until that upstream contract adds deletion support.

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

## Generate & build (simulator only)

Requirements: Xcode 26.x with iOS **and watchOS** platform components, and
[XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).
The `.xcodeproj` and `Support/` plists are generated artifacts (gitignored):

```bash
cd apps/ios-companion
xcodegen generate

# iOS app + widget extension (incl. Live Activity)
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "generic/platform=iOS Simulator" build CODE_SIGNING_ALLOWED=NO

# watchOS app + complication extension
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesWatchApp \
  -destination "generic/platform=watchOS Simulator" build CODE_SIGNING_ALLOWED=NO

# unit tests (contract decoding, request builders, notification grammar,
# ETag flow, seen-store, focus-block selection) + UI tests (self-skip
# without a live paired instance)
xcodebuild test -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "platform=iOS Simulator,name=iPhone 17 Pro,OS=26.2" CODE_SIGNING_ALLOWED=NO
```

Signing is deliberately untouched (`CODE_SIGNING_ALLOWED=NO` everywhere).

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
5. **Unpair** clears pairing, snapshot cache, seen-alerts store and the watch.

Transport policy: production pairing requires **HTTPS**. Plain HTTP is
accepted only for same-device loopback hosts (`localhost`, `127.0.0.0/8`,
`::1`) used by local development and the Mac runtime. Private-LAN HTTP is
rejected before a long-lived bearer token can be stored or returned. The
scoped `NSAllowsLocalNetworking` entitlement remains for loopback tooling;
it is not permission to pair over cleartext LAN. A Mac setup QR is offered
to iPhone only when the configured public base URL is HTTPS.

## Layout

```
project.yml                  # XcodeGen spec (6 targets, 2 schemes)
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

- `xcodegen generate`; **both schemes build** (`generic/platform=iOS
  Simulator`, `generic/platform=watchOS Simulator`, `CODE_SIGNING_ALLOWED=NO`).
- **33/33 unit tests green** on an iPhone 17 Pro (iOS 26.2) simulator:
  glance/alerts/weekly-report contract decoding (incl. empty shapes and the
  naive-datetime variant), multipart/JSON request builders byte-for-byte,
  §8.5 notification-content mapping, error-envelope → "already resolved"
  mapping, seen-store exactly-once semantics, focus-block selection, ETag
  200→304 flow.
- **UI acceptance tests against a LIVE instance** (`python -m healthmes
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
- **No signing/distribution** — no team/profiles; the watch app is not
  embedded into the iPhone app for distribution.
- Voice-capture transcription is manual (a transcript field) — no on-device
  speech-to-text yet; the server accepts `transcript` when present.
- Notification ✅/✏️/❌ buttons attach only when exactly one proposal is
  pending; a proper alert→proposal link needs a server-side FK (recorded as
  a follow-up need, matches the store's documented placeholder policy).
