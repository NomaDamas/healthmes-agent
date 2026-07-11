# HealthMes iOS/watchOS Companion

Full native companion app for HealthMes Agent (GitHub issue #10, building on
the #7 glance plumbing): live the whole daily loop on the phone — see the
briefing, act on an alert, capture food/medication, check "why?" — with
Telegram optional rather than required. Plus the #7 surfaces: WidgetKit
home/lock-screen widgets and the watchOS app + complications.

Local-first, like `apps/android-usage`: the paired base URL is the **only**
network destination in the whole project — no third-party endpoint, no
analytics, no push relay. The watch receives the pairing from the phone over
WatchConnectivity and then talks to the instance directly.

## What the app does

- **Briefing home** — energy score + hand-drawn 24 h curve (honest gaps for
  `null` hours, current-hour marker), next blocks, pending schedule
  proposals with a real §8.5 button row, unresolved-alert list, latest
  decision link. Pull-to-refresh; the glance leg stays ETag-cheap (304).
- **Alert list in §8.5 grammar** (`GET /v1/alerts`) — observation line
  (`summary`), evidence line rendered from the `evidence` facts, proposal
  line, relative fired-time, "Why this?" → in-app decision viewer. Lines the
  payload does not carry are dropped, never invented.
- **Real alert actions** — ✅ Apply → `POST /v1/schedule/proposals/{id}/accept`,
  ❌ Keep as is → `…/decline`, ✏️ Adjust → proposal detail sheet. A second
  tap elsewhere (or in Telegram) surfaces as the server's 409
  `invalid_transition` → rendered "Already resolved (accepted/declined)".
- **Weekly report** — native rendering of `GET /reports/weekly.json`:
  per-day energy bars (hollow stubs for missing days), insights with
  confidence badges (high/medium/low/none), schedule adherence, alert
  digest (delivered vs fired vs budget, per rule), the week's decisions.
  The HTML page stays one toolbar tap away.
- **Decision viewer** — `SFSafariViewController` sheet over the tokenized
  viewer links (native Done/share come free). Links always come from the
  paired instance's own payloads; `healthmes://decision?url=…` deep links
  are additionally host-checked against the pairing.
- **Capture** — camera (device only) / photo picker / voice memo →
  `POST /v1/media` (multipart, field `file`; photos re-encoded to JPEG,
  memos AAC-in-m4a = `audio/mp4`) → `POST /v1/food-logs` or
  `POST /v1/medical-records` with a description the user edits first.
  Offline-friendly: a failed step keeps text + attachment + any already-
  uploaded `media_path`, so Retry never re-uploads or loses data. Medical
  captures send capture metadata only (`context.capture`); the server
  attaches its own health snapshot (`context.health`).
- **Native notifications** (parity with Android's `AlertNotifier`) —
  BGAppRefreshTask + foreground sync poll `GET /v1/alerts`, diff against a
  seen-store (exactly-once per alert), and post local notifications in the
  §8.5 grammar: observation title, evidence+proposal body, per-rule thread.
  ✅/✏️/❌ actions are attached **only when exactly one pending proposal
  exists** (no alert→proposal FK exists yet, so that is the only case where
  "Apply" is unambiguous) and call the real endpoints from the action
  handler, confirming with an outcome notification. Tap-through opens the
  decision viewer. Badge = unresolved count.
- **Live Activity** — current focus block (from glance `next_blocks`) on
  the lock screen / Dynamic Island with timer progress; started on
  foreground refresh, updated by the background task, `staleDate = block
  end` so iOS dims it when no budget arrives. Polling only — no push token.
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
| `GET /reports/weekly.json` | report tab |
| `POST /v1/media` (multipart `file`) + `GET /v1/media/{path}` | capture upload / preview URL |
| `POST /v1/food-logs`, `POST /v1/medical-records` | capture save |

Contracts are pinned twice: Swift decoding tests against
`Tests/Fixtures/{glance,alerts,weekly_report}.json`, and those same three
fixture sets validate against the server's pydantic models in CI —
`tests/api/test_glance_fixtures.py` parametrizes `glance.json` against
`GlanceOut`, `alerts.json` against `Page[AlertOut]` and
`weekly_report.json` against `WeeklyReportOut`. Editing any fixture without
running the Python suite will fail the server-side pinning test.

Datetime note: glance/alerts serialize aware-UTC (`…Z`); store-backed
endpoints (proposals, food logs) serialize sqlite's **naive** UTC datetimes
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

1. Serve your instance. Same-machine simulator: `http://127.0.0.1:8100`
   works with no token (loopback-open). Real devices need the LAN bind:
   `HEALTHMES_HOST=0.0.0.0` **and** `HEALTHMES_API_TOKEN=<token>` in `.env`.
2. First launch shows the pairing screen (later: Settings → Instance
   pairing): base URL + token → **Save pairing** → **Test connection**
   performs a real glance fetch. Saving also requests notification
   permission and primes the alert seen-store so old alerts never replay.
3. Widgets read the pairing through the App Group
   (`group.com.healthmes.companion`); the token lives in the Keychain (App
   Group access group, unsigned-simulator fallback documented in
   `Pairing.swift`). The watch gets it over WatchConnectivity.
4. **Unpair** clears pairing, snapshot cache, seen-alerts store and the watch.

Plain-HTTP note: the ATS exception is **scoped to local networking**
(`NSAllowsLocalNetworking`, not a global `NSAllowsArbitraryLoads`): the
typical target `http://<LAN-IP>:8100` works regardless (ATS never applies
to IP-literal URLs), and `.local`/unqualified hostnames — `localhost`
included — are allowed. Plain HTTP to a qualified public DNS name (e.g. a
Tailscale MagicDNS name) fails closed — use HTTPS there; the bearer token
and tokenized viewer URLs must never cross an untrusted network in clear
text. (Android's `usesCleartextTraffic` stays global: its pairing host is
user-typed at runtime, and Android's network-security-config can only
allowlist statically known domains.)

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
  CaptureContract.swift        media upload + food/medical bodies
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
  rendered live data; Report tab rendered live `weekly.json`; Capture form
  saved a real food log (`source: "ios-app"` row verified server-side);
  ✅ Apply flipped the seeded proposal to `accepted` server-side and the
  accepted block then appeared in glance `next_blocks`. Tests self-skip
  (never fail) without a live pairing, so plain CI runs stay green.
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
