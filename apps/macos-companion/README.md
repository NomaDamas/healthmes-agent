# HealthMes macOS Companion

The macOS slice of issue #108 is a full-window HealthMes product with the
same core information architecture as iPhone:

- **Today** — simple Now / Next / Decision hierarchy, with Yes/No proposal
  actions and optional inspector detail.
- **Plan** — current weekly goals, open tasks, mirrored calendar events and
  pending schedule changes.
- **Decisions** — pending decisions, decision history, recent signals and
  exact web-detail links.
- **Speak** — `Shift-Command-Space` voice capture. Navigation stays local;
  tasks and weekly goals require an explicit confirmation before REST writes.
- **Settings** — connection, Google/iCloud calendar entry point, native
  alerts and privacy; self-host URL/token remain under Advanced.

The issue #11 glance surfaces remain part of the same project:

- **`HealthMesMac`** — Dock/full-window SwiftUI app plus `MenuBarExtra`.
  The menu bar remains a fast energy/proposal glance and opens the main app.
- **`HealthMesMacWidgets`** — WidgetKit extension
  (systemSmall/Medium/Large) for the desktop / Notification Center,
  embedded in the main app.
- **`HealthMesSaver`** — `.saver` screensaver bundle: full-screen ambient
  briefing (big score, curve, next block, gentle alert count), honest
  not-paired / no-data states, and the issue-#11 **privacy toggle**.

Local-first, like every companion in `apps/`: the paired base URL is the
**only** network destination in the whole project — no third-party
endpoint, no analytics, no push relay.

## Source reuse (one contract, one client)

`../ios-companion/Sources/Shared` (Foundation+Security only — no UIKit,
no SwiftUI) is compiled **verbatim** into every target here: product,
glance/alerts/report/schedule Codable contracts, the shared `HealthMesAPI`,
the ETag-honoring `GlanceClient`, the on-disk snapshot cache, `PairingStore`
(Keychain token), the §8.5 notification-content builder, the exactly-once
seen-store, and
`CurveGeometry` (the curve gap/dot/segment honesty rules — one geometry for
the iPhone home curve, the mac popover/widgets and the screensaver; it
started life in `Sources/MacCore` and moved into Shared so the platforms
cannot diverge). `../ios-companion/Sources/SharedWidget` supplies the widget
timeline provider. Nothing under this directory duplicates Shared code, with
one documented exception (`SaverDataSource.pairedBaseURLDefaultsKey` mirrors
a private constant — marked for the integrator in the source).

Contract fixtures are shared too: `HealthMesMacTests` decodes the exact
`../ios-companion/Tests/Fixtures/{glance,alerts,weekly_report}.json` files
that the server pins via `tests/api/test_glance_fixtures.py` — one fixture
set across phone, watch and desktop.

## What is real vs placeholder

Real and tested: full-window navigation and REST plumbing, the simple
Now/Next/Decision default, adaptive inspector, menu-bar handoff, shared
goal/task/calendar APIs, decision history, task/weekly-goal voice
confirmation, curve honesty, privacy redaction, ETag refresh, and
accept/decline including the 409 → "already resolved" story.

The warm-neutral/graphite/moss visual direction is implemented, but final
health-language calibration still belongs to the domain worksheet:
`docs/design/WATCH-NOTIFICATIONS.ko.md` (design system:
`docs/PLAN.md` §8.5).

### Delivery honesty (notifications)

Menu bar notifications derive from the app's own **5-minute polling** —
there is deliberately no push relay (local-first), so **Telegram remains
the guaranteed-delivery alert channel**. The Settings toggle says exactly
that. A notification exposes No/Yes only when the alert carries an exact
`proposal_id`; those actions call the real decline/accept endpoints and
confirm with an outcome notification. Plain clicks open the authenticated,
read-only decision viewer in the browser.

### Screensaver data path (by design)

The saver process does **no networking and never touches the Keychain**:
third-party savers run inside Apple's sandboxed `legacyScreenSaver` host,
where a login-keychain read can pop a password prompt *behind* the
full-screen window. Instead it renders the shared on-disk glance snapshot
that the menu bar app / widget keep ≤ 5 minutes fresh, with an explicit
"Updated N min ago" line. Consequence: **the saver needs the menu bar app
(or widget) running to stay fresh** — paired-but-cold-cache renders the
honest "no data yet" state, never a blank.

## Server contracts consumed

| Endpoint | Used by |
|---|---|
| `GET /v1/briefing/glance` (ETag/304, max-age 300) | popover, status item, widgets, saver (via snapshot) |
| `GET /v1/alerts?hours=24` (§8.5 grammar items) | popover alert list, notifications |
| `GET /v1/schedule/proposals?status=proposed` + `POST …/{id}/accept\|decline` | proposal rows, notification actions |
| `GET /v1/goals`, `GET/POST /v1/tasks` | Plan and confirmed voice capture |
| `GET /v1/schedule/events` | Plan calendar |
| `GET /v1/decisions`, `GET /reports/weekly.json` | Decisions and weekly outcome |
| `GET /dashboard`, `/connect`, `/decisions/{id}` | browser detail and calendar setup |

## Generate & build

Requirements: Xcode 26.x and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`). The `.xcodeproj` and `Support/` plists are
generated artifacts (gitignored):

```bash
cd apps/macos-companion
xcodegen generate

# full Mac app (also embeds the widget extension and menu-bar glance)
xcodebuild -project HealthMesMac.xcodeproj -scheme HealthMesMac \
  -destination "platform=macOS" build CODE_SIGNING_ALLOWED=NO

# widget extension / screensaver bundle standalone
xcodebuild -project HealthMesMac.xcodeproj -scheme HealthMesMacWidgets \
  -destination "platform=macOS" build CODE_SIGNING_ALLOWED=NO
xcodebuild -project HealthMesMac.xcodeproj -scheme HealthMesSaver \
  -destination "platform=macOS" build CODE_SIGNING_ALLOWED=NO

# unit tests, natively on macOS (contract fixtures, curve geometry,
# status-item text, §8.5 grammar mapping, privacy redaction,
# ScreenSaverDefaults persistence, snapshot cache round-trip)
xcodebuild test -project HealthMesMac.xcodeproj -scheme HealthMesMac \
  -destination "platform=macOS" CODE_SIGNING_ALLOWED=NO
```

Signing is deliberately untouched (`CODE_SIGNING_ALLOWED=NO` everywhere).

## Install

**Mac app** — run straight from build products, or copy it:

```bash
open <DerivedData>/Build/Products/Debug/HealthMesMac.app        # run once
cp -R <DerivedData>/Build/Products/Debug/HealthMesMac.app /Applications/
```

Add it to System Settings → General → Login Items to keep the menu-bar glance
and the saver's snapshot alive across logins.

**Screensaver** — copy the bundle, then select it:

```bash
cp -R <DerivedData>/Build/Products/Debug/HealthMesSaver.saver \
  ~/Library/Screen\ Savers/
```

System Settings → Screen Saver → "HealthMes Briefing". Unsigned local
builds may need a right-click → Open style Gatekeeper approval the first
time on some machines.

**Widgets** — with the app copied to /Applications and launched once, the
"HealthMes Glance" widget appears in the desktop / Notification Center
widget gallery (unsigned-build caveat below).

## Pairing

1. Open **Settings → Set up this Mac**. HealthMes downloads its managed
   runtime into `~/Library/Application Support/HealthMes/runtime-source`,
   creates a private mode-0600 configuration, installs the local service,
   and pairs the Mac app.
2. Scan the displayed five-minute QR with the iPhone Camera. The QR contains
   a signed one-time code, never the bearer token; the iPhone app exchanges
   it once and passes the pairing to Apple Watch.
3. Existing self-hosted instances can still be entered under
   **Settings → Advanced → self-host pairing**.
4. Storage: base URL in the app-group `UserDefaults` suite
   (`group.com.healthmes.companion` — shared with the widget and read, URL
   half only, by the saver); token in the login **Keychain** via the shared
   `PairingStore`. **Unpair** clears pairing, snapshot cache and the
   alert seen-store.

Transport policy: production pairing requires **HTTPS**. Plain HTTP is
accepted only for same-device loopback hosts (`localhost`, `127.0.0.0/8`,
`::1`). The default one-click install therefore pairs the Mac app with its
loopback runtime. Private-LAN HTTP cannot exchange or store the long-lived
bearer token. If `HEALTHMES_PUBLIC_BASE_URL` is a valid HTTPS URL, setup also
shows the expiring iPhone QR; otherwise it reports that iPhone pairing still
needs an HTTPS endpoint.

## Privacy toggle (issue #11 requirement)

System Settings → Screen Saver → HealthMes Briefing → **Options…** →
*"Hide health numbers (shared spaces / screen sharing)"*. Persisted via
`ScreenSaverDefaults` (module `com.healthmes.saver`). The rule is a tested
data transformation, not a drawing detail: when on, every health-derived
value is **absent** (score, confidence, curve, energy demand, alert count,
alert summary) — nothing blurred, nothing leaked. Schedule facts (next
block time/title) and the freshness line stay, so the saver remains useful
in a shared space.

## Live smoke (what was actually exercised)

Everything below ran on this machine against a seeded
`python -m healthmes serve` instance on `127.0.0.1:8199`:

```bash
# schema + one pending proposal + one pushed §8.5 alert, then serve
HEALTHMES_DATABASE_URL=sqlite:////tmp/hm-smoke/healthmes.db … uv run python -m healthmes serve

# pre-pair (token-less loopback), launch the real unsigned .app
defaults write group.com.healthmes.companion healthmes.pairing.baseURL "http://127.0.0.1:8199"
open <DerivedData>/Build/Products/Debug/HealthMesMac.app
```

Observed: the app polled `GET /v1/briefing/glance`, `GET /v1/alerts?hours=24…`
and `GET /v1/schedule/proposals?…status=proposed` (server access log) and
wrote the shared snapshot; a **separate process** compiled from the saver's
exact sources (`SaverDataSource` + Shared) then read that snapshot back —
briefing state with the seeded alert ("Stress 82 vs baseline 55.",
`alertCount=1`, "updated 1 min ago", score honestly `--` on an empty energy
table) and, with the privacy toggle on, every health value gone. The §8.5
Yes path ran live through the same shared API layer: first accept →
`applied` (and the block then appeared in glance `next_blocks`), second
accept → server 409 `invalid_transition` → rendered "already resolved
(accepted)". Server and app were stopped afterwards.

## Layout

```
project.yml                  # XcodeGen spec (4 targets, 3 schemes)
Sources/MacCore/             # platform-agnostic mac logic (Foundation only)
                             # (CurveGeometry lives in ../ios-companion/
                             # Sources/Shared — shared with the iPhone curve)
  StatusItemText.swift         --/58•/(58•) status-item honesty rules
  SaverBriefing.swift          saver render model + PRIVACY redaction rule
  SaverDataSource.swift        snapshot + pairing-presence reader (no network)
  ProposalOutcome.swift        accept/decline/409 outcome mapping
  MacProductAPI.swift          Mac-only decision history + exact web links
  MacProductContracts.swift    decision summary contract only
  MacVoiceIntent.swift         deterministic voice routing, no side effects
Sources/MacUI/               # SwiftUI curve view (popover + widgets)
Sources/App/                 # full-window Today / Plan / Decisions / Speak /
                             # Settings, adaptive inspector, dashboard store
Sources/MenuBar/             # MenuBarExtra app: store, popover, settings,
                             # UNUserNotificationCenter manager (§8.5)
Sources/MacWidgets/          # WidgetKit bundle (small/medium/large)
Sources/Saver/               # ScreenSaverView (AppKit drawing), Options
                             # sheet, ScreenSaverDefaults store
Resources/Localizable.xcstrings  # all strings, en + ko (71 keys)
Tests/                       # 26 XCTests (run natively on macOS)
```

Accessibility: primary controls and composed glance rows carry VoiceOver
labels; the curve exposes a data-hours summary instead of raw geometry; text
uses system semantic styles. Existing glance/widget/saver strings are
localized in English and Korean. The new issue-#108 full-window copy currently
uses English development-language fallbacks and needs a Korean localization
pass before release.

## Verification status (honest list)

Proven on this machine: `xcodegen generate`; all three schemes build
unsigned for `platform=macOS`; the issue-#108 API/link and voice-intent tests
pass natively; the earlier live smoke above exercised the real server →
app → snapshot → saver path and live accept + 409.

**Not verified here:**

- **The saver inside the real `legacyScreenSaver` host.** Selecting and
  activating a screensaver is a manual System Settings interaction. The
  drawing model, privacy redaction, defaults persistence and the snapshot
  read are unit/live-tested, but the sandboxed host's file-system view of
  the app-group container path (and macOS 15's group-container consent
  behavior) needs one manual pass: if the saver shows "no data" while the
  menu bar app is fresh, that seam is why.
- **Widget gallery registration for unsigned builds.** The extension
  builds and embeds correctly and the provider logic is the shared, tested
  one; whether macOS lists an unsigned widget in the gallery varies by
  OS/security settings. A signed (even ad-hoc, Developer ID for release)
  build is the reliable path.
- **Notification banners end-to-end.** Authorization prompts and banner
  display for unsigned apps aren't scriptable; the grammar mapping,
  category wiring, exactly-once seen-store and the action handler's real
  endpoint calls are unit/live-tested.
- **App-group container on signed builds.** Unsigned processes here fell
  back to `~/Library/Caches` for the snapshot (writer and reader agree in
  every configuration because they share `GlanceSnapshotCache.defaultFileURL()`);
  a signed build with the app-group entitlement will use the real group
  container — behavior verified only in the unsigned configuration.
- **ETag 304 observed from this app specifically** requires a >5-minute
  run; the 304 leg is the shared `GlanceClient` (unit-tested; observed
  live from the iOS app against the same endpoint).
- **CI first run**: `.github/workflows/apple-apps.yml` ships in this same
  change set — its `macos` job builds all three schemes (`HealthMesMac`,
  `HealthMesMacWidgets`, `HealthMesSaver`) and runs the XCTest suite on
  `macos-latest` next to the iOS schemes (issue #11 constraint). Because
  the workflow is new in this change set, its first run — on the PR that
  introduces it — is the CI proof; until that run is green the
  local-machine results above are the only build evidence.
- **Full-window visual and microphone QA.** The SwiftUI app builds, but this
  scoped implementation did not automate signed launch, real microphone
  authorization, VoiceOver traversal, or Korean copy review.
- **Web decision route integration.** Alerts use the exact tokenized URL
  supplied by the server. Decision-history fallback links target the canonical
  `/decisions/{id}` viewer while preserving a reverse-proxy base path.
