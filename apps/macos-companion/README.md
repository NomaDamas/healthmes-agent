# HealthMes macOS Glance Surfaces

The desktop half of GitHub issue #11 for this Mac: the briefing visible
where deep-work hours actually happen, without opening anything. Three
surfaces over the same `GET /v1/briefing/glance` contract every other
platform uses:

- **`HealthMesMac`** — menu bar app (SwiftUI `MenuBarExtra`, `LSUIElement`:
  no Dock icon). Energy score in the status item; popover with the 24 h
  curve, next blocks, pending proposals with real §8.5 buttons, the alert
  list in §8.5 grammar, decision links that open the default browser;
  pairing Settings window; optional native notifications.
- **`HealthMesMacWidgets`** — WidgetKit extension
  (systemSmall/Medium/Large) for the desktop / Notification Center,
  embedded in the menu bar app.
- **`HealthMesSaver`** — `.saver` screensaver bundle: full-screen ambient
  briefing (big score, curve, next block, gentle alert count), honest
  not-paired / no-data states, and the issue-#11 **privacy toggle**.

Local-first, like every companion in `apps/`: the paired base URL is the
**only** network destination in the whole project — no third-party
endpoint, no analytics, no push relay.

## Source reuse (one contract, one client)

`../ios-companion/Sources/Shared` (Foundation+Security only — no UIKit,
no SwiftUI) is compiled **verbatim** into every target here: glance/alerts/
report/schedule Codable contracts, the ETag-honoring `GlanceClient`, the
on-disk snapshot cache, `PairingStore` (Keychain token), the §8.5
notification-content builder, the exactly-once seen-store, and
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

Real and tested: information architecture and plumbing — curve gap/dot
geometry (`null` hours are gaps, never interpolated), status-item honesty
(`--` unpaired, `58•` alerts pending, `(58•)` stale-from-cache), §8.5 line
order (observation → evidence → proposal → "why this?", missing lines
dropped, never invented), the privacy redaction rule, ETag/max-age refresh
policy, accept/decline including the 409 → "already resolved" story.

Placeholder (clearly marked in source): colors, type scale, badge
vocabulary, layout. The final say on what each surface *says* — numbers vs
state words, urgency grades, low-confidence handling — is the healthcare
domain expert's worksheet: `docs/design/WATCH-NOTIFICATIONS.ko.md`
(design system: `docs/PLAN.md` §8.5).

### Delivery honesty (notifications)

Menu bar notifications derive from the app's own **5-minute polling** —
there is deliberately no push relay (local-first), so **Telegram remains
the guaranteed-delivery alert channel**. The Settings toggle says exactly
that. ✅ Apply / ✏️ Adjust / ❌ Keep actions are attached only when exactly
one proposal is pending (no alert→proposal FK exists — same rule as the
iOS/Android apps); ✅/❌ call the real endpoints from the notification
action handler and confirm with an outcome notification; ✏️ Adjust and
plain clicks open the decision viewer in the browser.

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

## Generate & build

Requirements: Xcode 26.x and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`). The `.xcodeproj` and `Support/` plists are
generated artifacts (gitignored):

```bash
cd apps/macos-companion
xcodegen generate

# menu bar app (embeds the widget extension)
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

**Menu bar app** — run straight from build products, or copy it:

```bash
open <DerivedData>/Build/Products/Debug/HealthMesMac.app        # run once
cp -R <DerivedData>/Build/Products/Debug/HealthMesMac.app /Applications/
```

Add it to System Settings → General → Login Items to keep the status item
(and the saver's snapshot) alive across logins.

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

1. Serve your instance. Same machine: `http://127.0.0.1:8100` works with
   no token (loopback-open). Another machine on your LAN needs
   `HEALTHMES_HOST=0.0.0.0` **and** `HEALTHMES_API_TOKEN=<token>`.
2. Click the status item (shows `--` while unpaired) → **Open Settings…**:
   base URL + token → **Save & test** performs a real glance fetch and
   reports the failure reason if any.
3. Storage: base URL in the app-group `UserDefaults` suite
   (`group.com.healthmes.companion` — shared with the widget and read, URL
   half only, by the saver); token in the login **Keychain** via the shared
   `PairingStore`. **Unpair** clears pairing, snapshot cache and the
   alert seen-store.

Plain-HTTP note: the ATS exception is **scoped to local networking**
(`NSAllowsLocalNetworking`, not a global `NSAllowsArbitraryLoads`): the
typical target `http://<LAN-IP>:8100` works regardless (ATS never applies
to IP-literal URLs), and `.local`/unqualified hostnames are allowed. Plain
HTTP to a qualified public DNS name fails closed — use HTTPS there (a
tokenized viewer URL must never cross an untrusted network in clear text).

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
✅ Apply path ran live through the same shared API layer: first accept →
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
Sources/MacUI/               # SwiftUI curve view (popover + widgets)
Sources/MenuBar/             # MenuBarExtra app: store, popover, settings,
                             # UNUserNotificationCenter manager (§8.5)
Sources/MacWidgets/          # WidgetKit bundle (small/medium/large)
Sources/Saver/               # ScreenSaverView (AppKit drawing), Options
                             # sheet, ScreenSaverDefaults store
Resources/Localizable.xcstrings  # all strings, en + ko (71 keys)
Tests/                       # 26 XCTests (run natively on macOS)
```

Accessibility: every interactive control and composed row carries a
VoiceOver label (status item summarises score/confidence/alert count; the
curve exposes a data-hours summary instead of raw geometry); text uses
system dynamic styles so it follows the system text-size setting. All
user-facing strings live in `Localizable.xcstrings` with English and Korean
values; saver strings resolve against the `.saver` bundle explicitly
(`legacyScreenSaver` is the main bundle at runtime).

## Verification status (honest list)

Proven on this machine: `xcodegen generate`; all three schemes build
unsigned for `platform=macOS`; 26/26 unit tests pass natively; the live
smoke above (real server → real app → shared snapshot → saver data path →
live accept + 409).

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
