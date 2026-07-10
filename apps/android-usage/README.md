# HealthMes Android Usage Collector

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

## Build & install

Requirements:

- JDK 17+ (Android Studio's embedded JDK works).
- Android SDK with platform 35 (`compileSdk = 35`). Point the build at it via
  the `ANDROID_HOME` env var or a `local.properties` file containing
  `sdk.dir=/path/to/Android/sdk`.
- No Android Studio required for CLI builds; the Gradle wrapper (Gradle 8.9,
  AGP 8.7.3, Kotlin 2.0.21) downloads everything else.

```bash
cd apps/android-usage
./gradlew assembleDebug          # builds app/build/outputs/apk/debug/app-debug.apk
./gradlew test                   # pure-JVM unit tests for the hourly bucketing
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Or open `apps/android-usage/` in Android Studio and run the `app`
configuration. Min SDK 26 (Android 8.0), target SDK 35.

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
app/src/main/kotlin/com/healthmes/usagecollector/
├── MainActivity.kt          # pairing + toggle screen (the whole UI)
├── CollectorPrefs.kt        # EncryptedSharedPreferences (URL, token, watermark)
├── UsageAccess.kt           # PACKAGE_USAGE_STATS check + settings deep link
├── usage/HourlyBucketer.kt  # pure event→hourly-bucket fold (JVM unit-tested)
├── usage/UsageSnapshotReader.kt  # UsageStatsManager drain + category lookup
├── net/IngestClient.kt      # POST /v1/app-usage/batch (chunking, outcome classes)
└── work/                    # WorkManager periodic (30 min) + one-shot upload
```

## Verification status

- **Compiles & unit tests pass**: `./gradlew assembleDebug testDebugUnitTest`
  was run at authoring time (Gradle 8.9, AGP 8.7.3, Kotlin 2.0.21, JDK 21,
  SDK platform 35) — APK builds, `HourlyBucketerTest` 13/13 green.
- **Server contract covered**: the payload example above is replayed against
  the real ingest endpoint by `tests/api/test_android_readme_contract.py`
  (plus `tests/api/test_app_usage.py` for upsert/validation edge cases).
- **Not exercised on a device**: the usage-access onboarding flow, WorkManager
  scheduling behavior under OEM battery managers, and an upload against a live
  instance still need a manual pass on real hardware (steps above).
