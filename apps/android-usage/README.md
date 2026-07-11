# HealthMes Android companions

One Gradle project, four modules, one philosophy: **each app talks only to
the HealthMes instance you pair it with** (base URL + optional bearer token in
`EncryptedSharedPreferences`) — no third-party endpoint, no analytics, no SDKs
that phone home.

| Module | What it is | Docs |
|---|---|---|
| `:app` | Usage collector (docs/PLAN.md §7): hourly app-usage buckets → `POST /v1/app-usage/batch` | [below](#app--usage-collector) |
| `:companion` | Full phone companion app (issue #10, promoted from the issue #7 widget host): Compose briefing home + weekly report + capture + proposal actions, plus the Glance widget and §8.5-grammar alert notifications with REAL buttons | [below](#companion--full-companion-app) |
| `:wear` | Wear OS briefing surfaces (issue #7): ProtoLayout tile + energy-score complication from the same endpoint | [below](#wear--wear-os-tile--complication) |
| `:shared` | Library used by `:companion`/`:wear`: glance contract parser, ETag-aware fetch client, display-state mapper, encrypted pairing prefs, plus the issue #10 app-surface contracts (alerts page, weekly report, proposals, media upload/capture bodies) and the bearer `HealthmesApi` client | — |

`:app` predates the briefing work and stays self-contained; `:shared`
deliberately *duplicates* its pairing-prefs pattern instead of importing it.

## Build matrix

Toolchain for everything: Gradle 8.9 (wrapper), AGP 8.7.3, Kotlin 2.0.21
(+ `org.jetbrains.kotlin.plugin.compose` 2.0.21 for `:companion`'s Glance
code), JDK 17+, `compileSdk = 35`. Point the build at an SDK with platform 35
via `ANDROID_HOME` or `local.properties`.

| Module | Type | minSdk | Key libraries | Build | JVM unit tests |
|---|---|---|---|---|---|
| `:app` | phone app | 26 | WorkManager 2.9.1, security-crypto | `:app:assembleDebug` | `:app:testDebugUnitTest` (hourly bucketing) |
| `:shared` | library | 26 | security-crypto, org.json (platform) | `:shared:assembleDebug` | tested via `:companion` |
| `:companion` | phone app | 26 | Compose BOM 2024.10.01 (material3), activity-compose 1.9.3, browser 1.8.0, Glance 1.1.1, WorkManager 2.9.1 | `:companion:assembleDebug` | `:companion:testDebugUnitTest` (glance/alerts/report/proposals contract parsers, state mapper, notification grammar + action plan, proposal-action logic, multipart upload bodies, curve geometry, focus-block selection) |
| `:wear` | Wear OS app | 30 | tiles 1.4.1, protolayout 1.2.1, watchface-complications-data-source 1.2.1 | `:wear:assembleDebug` | — (logic lives in `:shared`, tested via `:companion`) |

```bash
cd apps/android-usage
./gradlew assembleDebug                      # all four APKs
./gradlew test                               # all JVM unit tests
adb install -r companion/build/outputs/apk/debug/companion-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
# wear-debug.apk installs to a Wear OS emulator/watch the same way
```

## `:companion` — full companion app

The full phone app of issue #10 (single-activity Compose, five tabs), grown
from the issue #7 widget host. Every network call still goes to the paired
HealthMes instance only; there is no push relay by design (notifications are
derived from 15-minute polling — Telegram stays the guaranteed-delivery
channel).

- **Briefing home**: energy score + confidence + relative freshness, the 24 h
  curve drawn from `curve_24h` with real gaps for `null` hours (never
  interpolated), the next blocks (≤3, with energy demand + proposal tag), the
  24 h alert history from `GET /v1/alerts` rendered in §8.5 slots
  (observation / evidence facts / proposal / "why this?" link), and the
  latest-decision link.
- **Weekly report**: native rendering of `GET /reports/weekly.json`
  (energy per-day trend with honest "—" days, insights with confidence
  badges, schedule adherence, alert digest per rule, decision links), plus an
  "open web version" button on the tokenized `report_url`.
- **Decision viewer**: tokenized viewer URLs open in Custom Tabs
  (native back/share); on browserless devices an in-app WebView screen with
  back + share takes over. JavaScript stays on for the Mermaid trees.
- **Capture**: photo (ACTION_IMAGE_CAPTURE via FileProvider, or the photo
  picker — deliberately no CameraX/no CAMERA permission) and voice memo
  (MediaRecorder → audio/mp4) → `POST /v1/media` (multipart `file` field) →
  `POST /v1/food-logs` or `POST /v1/medical-records` with an editable
  description — the same contracts the Telegram capture skill uses. The
  medical health-context snapshot is attached **server-side**; the app sends
  capture metadata only (`context.source = "android-companion"`).
- **Real §8.5 notification actions**: ✅ Apply / ❌ Keep as is enqueue a
  one-shot WorkManager job that resolves the pending schedule proposal and
  calls `POST /v1/schedule/proposals/{id}/accept|decline` with the bearer
  client. Because alerts carry no proposal id yet (server-side linkage gap),
  the worker acts only when exactly ONE proposal is pending; zero or 2+ route
  into the app instead of guessing (PLAN.md §11). Second taps render the
  server's 409 `invalid_transition` as "already resolved". ✏️ Adjust
  deep-links into the proposals screen; notification content tap opens the
  decision viewer in-app. Notification *content* prefers the real fire-time
  grammar lines from `GET /v1/alerts` over the glance-derived filler.
- **Ongoing focus block**: while a `next_blocks` entry is active, a quiet
  ongoing notification shows the block title and counts down to its end.
  Battery-honest: no foreground service — the 15-minute poll posts it, the
  OS chronometer ticks it, and `setTimeoutAfter` clears it at block end even
  if no poll runs. Wear OS bridges it to the watch by default, which is the
  current wrist story for the running block (see the Wear section).
- **Alert *trigger* still placeholder**: a notification fires when
  `alerts.unresolved_count` rises between two polls (first fetch only sets
  the baseline — PLAN.md §11 treats alert noise as the top product risk).
- **Widget + refresh** unchanged from issue #7: cache-only Glance widget,
  15-minute ETag-honoring WorkManager refresh (`If-None-Match`, 304 keeps
  the cache).
- **Settings tab** = the old pairing screen (server URL + token in
  `EncryptedSharedPreferences`, refresh now, clear pairing, status readout).
- **Accessibility & i18n**: TalkBack contentDescriptions on the score
  ("Cognitive energy 72 out of 100, confidence medium"), curve, day rows and
  icon buttons; sp-based Material3 typography follows system font scaling;
  full English (default) + Korean (`values-ko`) string resources.
- **Placeholder visuals**: layout/colors/thresholds are engineering defaults
  labeled in code — the final glanceable grammar belongs to the healthcare
  domain expert (docs/design/WATCH-NOTIFICATIONS.ko.md).

The wire contracts are pinned by fixtures that mirror the server tests'
payload shapes (`glance_full.json`/`glance_empty.json`, `alerts_page.json`,
`weekly_report.json`) — `alerts_page.json`'s top item deliberately agrees
with `glance_full.json`'s top alert, mirroring the server-side pin that
`alerts[0]` and glance `alerts.top` never disagree. JVM suites:
`GlanceBriefingParserTest`, `BriefingDisplayStateTest`,
`NotificationGrammarTest`, `AlertsFeedParserTest`, `WeeklyReportParserTest`,
`ProposalActionLogicTest`, `NotificationActionPlanTest`,
`MultipartEncodingTest`, `FocusBlockLogicTest`, `CurveGeometryTest`.

## `:wear` — Wear OS tile + complication

Standalone Wear app (`com.google.android.wearable.standalone=true`): the watch
pairs directly with the HealthMes instance via its own on-watch pairing
activity (same encrypted-prefs pattern; typing a URL on a watch is tolerable
exactly once — nicer pairing can ride the final UX pass).

- **Tile** (`androidx.wear.tiles` / ProtoLayout): energy score + confidence,
  next block, alert count. Cache-first: a tile request serves the cached
  payload when younger than the endpoint's `max-age=300` and only otherwise
  fetches (on the tile's background executor), so opening the tile never
  blocks on the LAN. `freshnessIntervalMillis` asks the renderer to re-request
  every 15 min. The whole tile is clickable (`LaunchAction`) and opens the
  on-watch briefing view.
- **Complication data source** exposing the energy score as `SHORT_TEXT`
  ("72" titled "NRG") and `RANGED_VALUE` (0–100 gauge); watch faces poll it
  every 15 min (`UPDATE_PERIOD_SECONDS=900`). A null score is honestly
  `NoDataComplicationData`; both data types carry a tap action that opens the
  on-watch briefing view.
- **Tap-through target** (issue #7: "tapping opens the briefing view"): both
  surfaces launch `WearPairingActivity`, whose status readout (energy / next
  block / alerts) doubles as the placeholder briefing view until the domain
  expert designs a dedicated one.

Everything visual on the watch is **placeholder plumbing**. The actual watch
notification/tile UX — what deserves the 3-second glance, thresholds, wording,
haptics — is deliberately reserved for the healthcare domain expert:
**docs/design/WATCH-NOTIFICATIONS.ko.md** (code comments point there).

## Briefing endpoint contract (`:companion`, `:wear`)

- `GET {server}/v1/briefing/glance` with `Authorization: Bearer <token>`
  (same 401 envelope + loopback-open behavior as every `/v1` route; the
  decision-viewer `?token=` credential does **not** authorize this route).
- Clients poll with `If-None-Match` and keep their cached payload on `304`.
- Decision URLs in the payload are browser-tappable as-is.
- Contract source of truth: `healthmes/api/briefing.py` +
  `tests/api/test_briefing.py`; the fixtures under
  `companion/src/test/resources/` must stay in sync with it.

## Device caveats (honest status)

- **The app/widget/tile/complication surfaces have not been exercised on
  real hardware yet.** `assembleDebug` compiles all modules and the JVM
  tests pass, but adding the widget to a launcher, the Compose flows
  (capture launchers, MediaRecorder, Custom Tabs), tile/complication
  rendering on a watch, OEM battery-manager behavior toward the 15-min job,
  and a live fetch against a real instance still need a manual pass (phone +
  Wear OS emulator or device).
- Lock-screen widget availability depends on the host (Android 15+ / certain
  hosts); `home_screen` placement is the baseline.
- Notifications need `POST_NOTIFICATIONS` (requested by the Settings tab on
  API 33+). The §8.5 buttons are REAL now (schedule-proposal
  accept/decline), but because alerts carry no proposal id yet the buttons
  act only on an unambiguous single pending proposal — otherwise they route
  into the app. The alert *trigger* remains the rising-count polling
  heuristic; there is deliberately no push relay (Telegram = guaranteed
  channel).
- The ongoing focus-block notification relies on `setTimeoutAfter` +
  chronometer countdown; some OEM skins render chronometer countdowns
  inconsistently — needs the hardware pass.
- Wear OS enforces its own budgets: complication updates are throttled to the
  manifest period at best, and tile freshness is at the renderer's discretion.
- Issue #7's Wear scope item **"ongoing activity for the current focus
  block"** is now covered at the interaction level by the phone's ongoing
  focus-block notification, which Wear OS bridges to the watch by default
  (no `setLocalOnly` is set on it). A *native* on-watch
  `androidx.wear.ongoing` OngoingActivity (watch-face chip etc.) remains
  deferred to the domain expert's watch UX pass, like its iOS twin
  (docs/design/WATCH-NOTIFICATIONS.ko.md).
- Cleartext HTTP is enabled in all modules because the typical target is
  `http://<LAN-IP>:8100` on your own network; prefer HTTPS beyond your LAN.

---

# `:app` — usage collector

Minimal Android companion app for HealthMes Agent (docs/PLAN.md §7). It has no
UI beyond a single pairing + toggle screen: it buckets
`UsageStatsManager.queryEvents` output into hourly per-app buckets (foreground
seconds, launch counts, app category) and uploads them to your own HealthMes
instance every ~30 minutes via WorkManager. The cognitive-energy engine uses
these samples for its fragmentation term (docs/PLAN.md §3).

There is intentionally no iOS counterpart: Screen Time / DeviceActivity data
cannot leave the device sandbox (docs/PLAN.md §7).

## Privacy

- Data goes **only to the HealthMes instance you pair with** — the server URL
  you enter is the only network destination; there is no third-party endpoint,
  no analytics, no SDKs that phone home.
- Collected fields per hourly bucket: package name, foreground seconds, launch
  count, and Android's coarse app category. No window titles, no notification
  or input content, nothing inside apps.
- The server URL and API token are stored in `EncryptedSharedPreferences`
  (AndroidKeyStore-backed AES-256-GCM), not plain-text XML.
- Cleartext HTTP is enabled (`android:usesCleartextTraffic="true"`) because the
  typical target is `http://<LAN-IP>:8100` on your own network. Prefer HTTPS if
  your instance is reachable beyond your LAN.

## Ingest contract

The app POSTs to `POST {server}/v1/app-usage/batch`
(`healthmes/api/app_usage.py`). Bucket starts are top-of-hour UTC instants.
The token is sent as `Authorization: Bearer <token>` and **is verified
server-side**: when the HealthMes service has `HEALTHMES_API_TOKEN` set
(required for any non-loopback bind, i.e. exactly the LAN setup this app
targets), unauthenticated POSTs are rejected with 401 — set the same token in
the app's server settings.

Example batch (this exact example is round-tripped against the real endpoint
by `tests/api/test_android_readme_contract.py`):

<!-- ingest-payload-example -->
```json
{
  "device_id": "android-3f9c2a7b41e8d05c",
  "samples": [
    {
      "bucket_start": "2026-07-09T10:00:00Z",
      "app_package": "com.slack",
      "foreground_seconds": 1260,
      "launches": 9,
      "category": "productivity"
    },
    {
      "bucket_start": "2026-07-09T10:00:00Z",
      "app_package": "com.google.android.apps.maps",
      "foreground_seconds": 300,
      "launches": 2,
      "category": "maps"
    },
    {
      "bucket_start": "2026-07-09T11:00:00Z",
      "app_package": "com.slack",
      "foreground_seconds": 480,
      "launches": 4,
      "category": "productivity"
    },
    {
      "bucket_start": "2026-07-09T11:00:00Z",
      "app_package": "org.fdroid.fdroid",
      "foreground_seconds": 95,
      "launches": 1,
      "category": null
    }
  ]
}
```

First-time ingest acknowledgement:

<!-- ingest-ack-example -->
```json
{
  "accepted": 4,
  "created": 4,
  "updated": 0
}
```

Upload semantics (why re-sending is safe):

- The worker keeps a **watermark** (top of the hour of the last successful
  upload) in encrypted prefs. Each run re-queries events from
  `watermark − 6 h` (lookback for sessions crossing the watermark; first run
  backfills ~24 h, hard cap 7 days) and re-sends every recomputed bucket,
  including the still-growing current hour.
- The server **upserts** on `(device_id, bucket_start, app_package)` with
  last-write-wins, so repeated uploads are idempotent; a second POST of the
  example above answers `{"accepted": 3, "created": 0, "updated": 3}`.
- Batches are chunked at 500 samples per POST (server cap: 1000).
- `foreground_seconds` is clamped to 3600 per bucket; a `launch` is a
  background→foreground transition attributed to the bucket of the resume.
- `category` is Android's `ApplicationInfo.category` mapped to stable labels
  (`game`, `audio`, `video`, `image`, `social`, `news`, `maps`,
  `productivity`, `accessibility`) or `null` when undefined.
- `device_id` is `android-<ANDROID_ID>` (stable per device + signing key),
  generated once and persisted.

## Pairing & permission onboarding

1. Make sure the phone can reach your HealthMes instance. Mac-native default:
   `uv run python -m healthmes` serves on `http://<your-mac-LAN-IP>:8100`
   (bind/port per repo `.env`); docker compose exposes the same port.
2. Open the app, enter the server URL (e.g. `http://192.168.1.20:8100`) and
   optionally a token, then tap **Save pairing**.
3. Tap **Open usage access settings** — this deep-links to
   *Settings → Special app access → Usage access* — and enable
   **HealthMes Usage**. This is a "special access" permission
   (`PACKAGE_USAGE_STATS`); it cannot be granted via a runtime dialog.
4. Flip **Collect & upload app usage**. This schedules the periodic upload
   (every 30 min, network required, exponential backoff on failure) and fires
   one upload immediately.
5. Verify with **Upload now**, then check the status line and your server:
   `curl http://<server>:8100/docs` → `POST /v1/app-usage/batch`, or query the
   `app_usage_sample` table.

### Permission & platform caveats

- **Usage access** exposes app usage history to this app; grant it consciously.
  Revoking it stops collection (the worker reports "Usage access not granted").
- **QUERY_ALL_PACKAGES** is declared so the app can resolve the category of
  other packages on Android 11+. Fine for a sideloaded personal tool, but it
  is a restricted permission on Google Play — this app is not meant for Play
  distribution.
- **OEM battery managers** (Samsung, Xiaomi, Huawei, ...) may throttle or kill
  periodic WorkManager jobs. If uploads stall, exempt the app from battery
  optimization (*Settings → Apps → HealthMes Usage → Battery → Unrestricted*).
  Missed runs self-heal: the next successful run re-covers the gap (up to the
  7-day cap) thanks to the watermark + upsert design.
- Android only retains detailed usage events for a bounded window (days,
  OEM-dependent); if the collector is off for longer, older hours are lost.
- An app continuously foreground across the query edge with no events inside
  the window is invisible to `queryEvents`; the 6 h lookback makes this rare.

## Project layout

```
shared/src/main/kotlin/com/healthmes/briefing/
├── GlanceBriefing.kt         # GET /v1/briefing/glance contract model + parser
├── GlanceApiClient.kt        # conditional GET (If-None-Match / ETag / 304)
├── BriefingRepository.kt     # cache-through refresh (encrypted prefs cache)
├── BriefingDisplayState.kt   # payload → glanceable state (JVM unit-tested)
└── PairingPrefs.kt           # base URL + token + payload cache (encrypted)

shared/src/main/kotlin/com/healthmes/api/     # issue #10 app surface
├── HealthmesApi.kt           # bearer client (GET / POST json / multipart) + Multipart encoder
├── ApiContracts.kt           # error envelope, Page meta, media-upload result
├── AlertsFeed.kt             # GET /v1/alerts page (§8.5 grammar lines)
├── WeeklyReport.kt           # GET /reports/weekly.json model + parser
├── Proposals.kt              # GET /v1/schedule/proposals + action paths
└── CaptureRequests.kt        # POST /v1/food-logs & /v1/medical-records bodies

companion/src/main/kotlin/com/healthmes/companion/
├── MainActivity.kt           # THE activity (singleTask; deep-link extras)
├── ui/                       # Compose app: home / report / capture /
│                             #   proposals / settings, curve, decision viewer
├── widget/                   # Glance widget (small/medium) + receiver
├── work/                     # 15-min refresh + one-shot proposal actions
└── notify/                   # §8.5 alert channel (real buttons), action
                              #   results, ongoing focus block

wear/src/main/kotlin/com/healthmes/wear/
├── WearPairingActivity.kt    # on-watch pairing
├── tile/BriefingTileService.kt          # ProtoLayout tile (cache-first)
└── complication/EnergyComplicationService.kt  # SHORT_TEXT / RANGED_VALUE

app/src/main/kotlin/com/healthmes/usagecollector/
├── MainActivity.kt           # pairing + toggle screen (the whole UI)
├── CollectorPrefs.kt         # EncryptedSharedPreferences (URL, token, watermark)
├── UsageAccess.kt            # PACKAGE_USAGE_STATS check + settings deep link
├── usage/HourlyBucketer.kt   # pure event→hourly-bucket fold (JVM unit-tested)
├── usage/UsageSnapshotReader.kt  # UsageStatsManager drain + category lookup
├── net/IngestClient.kt       # POST /v1/app-usage/batch (chunking, outcome classes)
└── work/                     # WorkManager periodic (30 min) + one-shot upload
```

## Verification status

- **Compiles & unit tests pass**: `./gradlew clean :companion:assembleDebug
  :companion:testDebugUnitTest :wear:assembleDebug :app:assembleDebug
  :app:testDebugUnitTest` was run at authoring time of the issue #10 wave
  (Gradle 8.9, AGP 8.7.3, Kotlin 2.0.21, Compose BOM 2024.10.01, JDK 21,
  SDK platform 35) — all four APKs build, `HourlyBucketerTest` 13/13 and the
  ten `:companion` JVM suites (62 tests: contract parsers for
  glance/alerts/report/proposals, display mapper, notification grammar +
  action plan, proposal-action logic, multipart upload bodies, curve
  geometry, focus-block selection) green.
- **Server contracts covered**: the ingest payload example above is replayed
  against the real endpoint by `tests/api/test_android_readme_contract.py`;
  the glance fixtures mirror `tests/api/test_briefing.py`; the alerts/report
  fixtures mirror the `GET /v1/alerts` and `GET /reports/weekly.json`
  contracts (healthmes/api/alerts.py, healthmes/api/reports.py), including
  the alerts[0]-equals-glance-top pin.
- **Not exercised on a device**: see
  [Device caveats](#device-caveats-honest-status) for the app/widget/tile/
  complication/notification hardware pass that is still owed, plus the
  collector's usage-access onboarding flow and WorkManager behavior under OEM
  battery managers.
