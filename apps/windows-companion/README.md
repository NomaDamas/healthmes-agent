# HealthMes Windows Companion

Desktop glance surfaces for HealthMes Agent on Windows (GitHub issue #11):
a **system tray app** (score badge, briefing flyout, §8.5-grammar toasts
with the decision deep link) and an ambient **screensaver** (`.scr`) with a
privacy toggle — both fed by `GET /v1/briefing/glance` from **your own
healthmes instance**, plus a Widgets Board **stub** (see `DEFERRED.md`).

Local-first, like every other companion: the paired base URL is the **only**
network destination in this whole directory — no third-party endpoint, no
analytics, no push relay (polling + ETag; Telegram stays the
guaranteed-delivery channel).

**Deliberate non-goal:** rendering is minimal placeholder plumbing. The
final glance/notification UX — wording, urgency, severity colors, night
behavior — is healthcare-domain design reserved for the domain expert
(`docs/design/WATCH-NOTIFICATIONS.ko.md`; design system: `docs/PLAN.md` §8.5
notification grammar).

## Layout

| Project | TFM | What it is |
|---|---|---|
| `src/HealthMes.Glance.Core` | `net8.0` (no Windows deps) | Contract models + strict JSON for `/v1/briefing/glance`, `/v1/alerts`, `/reports/weekly.json` envelope; ETag-aware client; pairing model; §8.5 grammar builder; Adaptive Card builder; `.scr` arg parser |
| `src/HealthMes.Windows.Common` | `net8.0-windows` | DPAPI pairing store, on-disk snapshot cache, shared settings (privacy toggle), en/ko `.resx` strings |
| `src/HealthMes.Tray` | `net8.0-windows` WinForms | Tray icon + flyout + toasts + pairing UI, 15-min ETag polling |
| `src/HealthMes.Screensaver` | `net8.0-windows` WinForms | Ambient briefing `.scr` (`/s /p /c` contract), privacy toggle honored |
| `src/HealthMes.Widgets` | `net8.0` | Widgets Board provider **stub** (MSIX-blocked — `DEFERRED.md`) |
| `tests/HealthMes.Glance.Core.Tests` | `net8.0` xunit | 66 tests over the pinned fixtures, client, grammar, formats |

## Server contract

Everything renders the same payloads the phone/watch surfaces use:

- `GET /v1/briefing/glance` — bearer auth, `Cache-Control: private,
  max-age=300` + strong ETag. The client sends `If-None-Match` on every poll
  and re-serves the cached body on `304`; the tray polls every 15 minutes
  (the same floor as the Android companion), the running screensaver every
  `max-age`.
- `GET /v1/alerts` — recent pushed alerts with the recorded §8.5 lines
  (observation/evidence/proposal) + `decision_url`; the tray toasts unseen
  ones (newest first, one balloon per poll, "+N" for the rest) and remembers
  seen ids. Noise gates (quiet hours/cooldown/daily budget) are server-side
  and cannot be bypassed from here.
- `GET /reports/weekly.json` — envelope only, to obtain the tokenized
  `report_url` the "Open weekly report" menu item hands to the browser.

Contract pinning: `tests/HealthMes.Glance.Core.Tests/Fixtures/glance*.json`
are **byte-identical copies** of the iOS (`apps/ios-companion/Tests/Fixtures`)
and Android (`.../companion/src/test/resources`) fixtures, and the server
suite validates every copy against the live schema
(`tests/api/test_glance_fixtures.py`) — one glance contract, one fixture set.

## Build & test

Requirements: .NET SDK 8.0 (Visual Studio 2022 17.8+ works too — open
`HealthMes.Companion.sln`). **On macOS/Linux the WinForms projects need the
official .NET 8 SDK** (dotnet.microsoft.com / `dotnet-install.sh`) — **not
Homebrew's `dotnet@8`**, which lacks the WindowsDesktop targeting pack and
fails `dotnet build` with MSB4019 (`Microsoft.NET.Sdk.WindowsDesktop.targets`
not found). With Homebrew's SDK only `tests/HealthMes.Glance.Core.Tests`
(plain `net8.0`) builds/runs.

```bash
cd apps/windows-companion
dotnet build HealthMes.Companion.sln -c Release   # whole solution
dotnet test tests/HealthMes.Glance.Core.Tests     # core suite (any OS)

# runnable outputs (Windows)
dotnet publish src/HealthMes.Tray        -c Release -r win-x64 --self-contained false -o artifacts/tray
dotnet publish src/HealthMes.Screensaver -c Release -r win-x64 --self-contained false -o artifacts/screensaver
```

The solution also **compiles on macOS/Linux** (`EnableWindowsTargeting`,
official SDK — see the requirements note above), which is how it was
developed; running the tray/screensaver requires Windows + the .NET 8
Desktop Runtime. CI: the `Windows apps` workflow
(`.github/workflows/windows-apps.yml`) builds, tests and publishes on
`windows-latest` for every PR touching this directory — the workflow ships
in the same change set as this code, so its **first run, on the PR that
introduces it, is the real-Windows compile proof** (trust that run's
status, not this sentence).

## Pairing

Tray menu → **Pairing settings…** → base URL (e.g.
`http://192.168.1.20:8100`) + API token (`HEALTHMES_API_TOKEN`; optional for
token-less loopback instances). The URL lands in
`%LOCALAPPDATA%\HealthMes\settings.json`; the token is **DPAPI-protected**
(`ProtectedData`, CurrentUser scope — this Windows user on this machine
only) before touching disk. The screensaver reads the same pairing
read-only; decision/report links open in the default browser with the
server-derived read-only viewer token already embedded.

## Screensaver

`artifacts/screensaver` contains `HealthMes.Screensaver.scr` next to its
DLLs — right-click → **Install** (the folder must stay together;
framework-dependent). It renders clock + energy score + 24h curve (honest
null gaps, never interpolated) + next block + alert count, and an honest
"not paired — no briefing data" state.

**Privacy toggle** (issue #11): "hide health numbers" — for shared spaces /
screen sharing. Lives in the screensaver's settings dialog (`/c`, i.e. the
"Settings" button in Windows' screensaver panel) and in the tray menu; when
on, the saver shows only the clock and the next block, no score/curve/alert
content.

## Accessibility & localization

- Every value control carries an `AccessibleName` — Narrator reads
  "Cognitive energy 58, confidence high" (Korean per the worksheet §1.4:
  "인지에너지 58점, 신뢰도 높음"), not bare digits; flyout and dialogs are
  fully keyboard-drivable (Tab/Enter/Esc).
- English + Korean via `.resx` resources
  (`src/HealthMes.Windows.Common/Resources/Strings*.resx`); the `ko`
  satellite is selected by the Windows UI culture. Grammar-line *content*
  comes from the server and stays untranslated placeholder by design.

## Honest status — what has NOT been verified

No Windows machine touched this code at authoring time — the
`windows-apps.yml` job ships in the same change set, so **its first PR run
is the first time this code compiles on real Windows** (that run's status
is the proof; this file cannot be). Proven on the authoring Mac: full
solution cross-compiles warning-free (official .NET 8 SDK — see Build &
test), 66/66 core tests green locally, publish commands produce the win-x64
exe/.scr layout. Pending until that first CI run: build+test+publish on
real Windows. **Not** provable in CI at all (needs a human at a Windows
desktop): tray/toast/flyout runtime behavior, DPAPI round-trip, `.scr`
behavior inside the real screensaver host, Narrator/ko-KR runtime, live
end-to-end against a paired instance from Windows. Full list with reasons:
`DEFERRED.md` §3.
