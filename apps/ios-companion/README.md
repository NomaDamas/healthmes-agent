# HealthMes iOS/watchOS Companion

Glanceable briefing surfaces for HealthMes Agent (GitHub issue #7): an iPhone
app whose only screen pairs the device with **your own healthmes instance**
(base URL + bearer token), plus WidgetKit surfaces that render
`GET /v1/briefing/glance` — home-screen widgets (systemSmall/systemMedium/
systemLarge), lock-screen widgets (accessoryCircular/accessoryRectangular/
accessoryInline) and watchOS complications (accessoryCircular/accessoryCorner,
plus rectangular/inline).

Local-first, like `apps/android-usage`: the paired URL is the **only**
network destination in the whole project — no third-party endpoint, no
analytics, no push service. The watch receives the pairing from the phone
over WatchConnectivity (Apple's encrypted phone↔watch channel) and then
talks to the instance directly.

**Deliberate non-goal:** the rendering here is minimal placeholder plumbing.
The actual glance/notification UX — wording, urgency, what deserves wrist
space — is healthcare-domain design reserved for the domain expert
(`docs/design/WATCH-NOTIFICATIONS.ko.md`; design system: `docs/PLAN.md` §8.5
notification grammar).

## Server contract

Everything renders one payload: `GET /v1/briefing/glance`
(`healthmes/api/briefing.py`) — energy score/confidence + 24h curve, up to 3
next blocks (calendar + accepted proposals), recent-alert digest, latest
decision link.

- **Auth**: `Authorization: Bearer <HEALTHMES_API_TOKEN>` — same token story
  as the Android collector; required whenever the instance binds beyond
  loopback.
- **Caching**: the client persists the response + strong `ETag` in the App
  Group container and sends `If-None-Match` on every poll; `304 Not
  Modified` re-serves the cached body. `Cache-Control: private, max-age=300`
  lower-bounds the next poll; the timeline provider additionally never asks
  WidgetKit for more than one reload per 15 minutes (WidgetKit grants
  roughly 40–70/day — `Sources/SharedWidget/GlanceTimelineProvider.swift`).
- Contract pinned by `Tests/Fixtures/glance.json` +
  `Tests/GlanceClientTests.swift` (mirrors the server's seeded-payload test
  in `tests/api/test_briefing.py`).

## Generate & build (simulator only)

Requirements: Xcode 26.x with the iOS **and watchOS** platform components
(`xcodebuild -downloadPlatform watchOS` if the watch simulator runtime is
missing), and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`).

The `.xcodeproj` and `Support/` plists are generated artifacts (gitignored):

```bash
cd apps/ios-companion
xcodegen generate

# iOS app + widget extension
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "generic/platform=iOS Simulator" build CODE_SIGNING_ALLOWED=NO

# watchOS app + complication extension
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesWatchApp \
  -destination "generic/platform=watchOS Simulator" build CODE_SIGNING_ALLOWED=NO

# unit tests (GlanceClient contract decoding + ETag/304 flow, network-free)
xcodebuild test -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "platform=iOS Simulator,name=iPhone 17 Pro,OS=26.2" CODE_SIGNING_ALLOWED=NO
```

Signing is deliberately untouched (`CODE_SIGNING_ALLOWED=NO` everywhere);
nothing in this directory configures identities, teams or profiles.

## Run it in the simulator

```bash
# serve healthmes reachable from the simulator (simulators share the Mac's
# loopback, so the default localhost bind works):
cd ../.. && make mac-run     # or: uv run python -m healthmes

xcrun simctl boot "iPhone 17 Pro"
xcrun simctl install booted "$(xcodebuild -project HealthMesCompanion.xcodeproj \
  -scheme HealthMesCompanion -destination 'generic/platform=iOS Simulator' \
  -showBuildSettings build 2>/dev/null | awk '/ BUILT_PRODUCTS_DIR/{d=$3} / FULL_PRODUCT_NAME/{n=$3} END{print d"/"n}')"
xcrun simctl launch booted com.healthmes.companion
```

Then add the "HealthMes glance" widget from the home-screen/lock-screen
widget gallery of the simulator.

## Pairing flow

1. Serve your instance. Same-machine simulator: `http://127.0.0.1:8100`
   works with no token (loopback-open). Real devices need the LAN bind:
   `HEALTHMES_HOST=0.0.0.0` **and** `HEALTHMES_API_TOKEN=<token>` in `.env`
   (the service refuses non-loopback binds without a token).
2. Open the app, enter the base URL (e.g. `http://192.168.1.20:8100`) and
   the token, tap **Save pairing**, then **Test connection** — it performs a
   real glance fetch and shows the energy/alert summary line.
3. Widgets read the pairing through the App Group
   (`group.com.healthmes.companion`): URL in shared `UserDefaults`, token in
   the Keychain (App Group as keychain access group, with a graceful
   fallback for unsigned simulator builds).
4. The pairing is pushed to the watch automatically (WatchConnectivity
   application context, delivered when the watch app next runs); watch
   complications then fetch independently on the watch. **Unpair** clears
   local state, the snapshot cache and the watch.

Plain-HTTP note: like the Android app (`usesCleartextTraffic`), ATS is opened
(`NSAllowsArbitraryLoads`) because the typical target is `http://<LAN-IP>:8100`
on your own network. Prefer HTTPS for anything reachable beyond the LAN.

## Layout

```
project.yml                       # XcodeGen spec (5 targets, 2 schemes)
Sources/Shared/                   # compiled into every target
  GlanceContract.swift            # Codable models pinned to the endpoint schema
  GlanceClient.swift              # bearer + If-None-Match/ETag + max-age parsing
  GlanceSnapshotCache.swift       # App Group cached payload + validator
  Pairing.swift                   # PairingStore: Keychain token + App Group URL
  GlanceFormat.swift              # placeholder text renderers (expert-owned wording)
Sources/SharedWidget/             # both widget extensions
  GlanceTimelineProvider.swift    # entry states + budget-aware refresh policy
  EnergyGaugeView.swift
Sources/App/                      # iOS app: pairing screen + watch sync
Sources/Widgets/                  # iOS home + lock-screen widget bundle
Sources/WatchApp/                 # watch app + WCSession pairing receiver
Sources/WatchWidgets/             # watch complications (circular/corner/...)
Tests/                            # host-less XCTest bundle + contract fixture
```

Widget states are honest by design: **not paired** ("open the iPhone app"),
**no data** (fetch failed, nothing cached), and **cached** (instance
unreachable — last snapshot rendered with a `cached` marker). Missing energy
hours come through as `--`, never invented.

## Verification status

Verified at authoring time (Xcode 26.3 / iOS 26.2 + watchOS 26.2 simulator
platforms, XcodeGen 2.45.4):

- `xcodegen generate` succeeds; both schemes **build** against
  `generic/platform=iOS Simulator` and `generic/platform=watchOS Simulator`
  with `CODE_SIGNING_ALLOWED=NO`.
- `xcodebuild test` on an iPhone 17 Pro (iOS 26.2) simulator: 8/8 green —
  exact-fixture contract decoding, all-null empty-DB shape, timestamp
  variants, URL normalization, Cache-Control parsing, and a stubbed-transport
  200 → ETag → `If-None-Match` → 304 → 401 flow.
- Launch smoke test: the app was installed and launched on the booted
  iPhone 17 Pro simulator; the pairing screen renders and the process stays
  alive (WCSession activation and the Keychain fallback do not crash
  unsigned builds).

**Not yet verified (honest list):**

- **No real device runs** — everything below the simulator boundary is
  unproven on hardware: App Group container + Keychain access-group sharing
  between app and widget extension under real code signing (the Keychain
  store has a documented unsigned-simulator fallback), WidgetKit refresh
  budgeting on-device, ATS behavior against LAN IPs on cellular-assist, etc.
- **WatchConnectivity pairing sync** not exercised end-to-end (needs a
  paired phone+watch simulator pair or hardware); the watch app also renders
  its "not paired" guidance until that first sync lands.
- **No push notifications** — glance surfaces poll; the PLAN §4 alert loop
  stays on Telegram. APNs would need an Apple Developer team and is out of
  scope until the domain expert designs the watch notification UX.
- **No signing/distribution** — no team, no provisioning, no App Store/
  TestFlight artifacts; the watch app is *not* embedded into the iPhone app
  for distribution (add an "Embed Watch Content" dependency once real
  signing exists).
- Lock-screen widget rendering was compile-verified only; visual QA in a
  booted simulator (widget gallery, both color schemes) is pending.
