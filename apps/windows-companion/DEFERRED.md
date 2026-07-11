# Deferred — Windows companion (issue #11)

Honest list of what this directory does NOT deliver yet, why, and how each
item would be implemented. Everything listed here was a deliberate call, not
an oversight; the corresponding plumbing that *could* be built without the
blocked dependency **is** built and tested.

## 1. Windows 11 Widgets Board provider — stub only

**Why deferred.** A Widgets Board widget cannot be shipped from an unpackaged
exe: the Board discovers providers through the `com.microsoft.windows.widgets`
app-extension declared in an **MSIX `Package.appxmanifest`**, and activates
them as **COM servers** (CLSID in the manifest). That means MSIX packaging +
package signing (or dev-cert sideloading) in CI for every PR — exactly the
complexity issue #11's "windows-latest CI job" scope avoids, and broken/
unsigned packaging would be worse than an honest stub.

**What exists today.**
- `src/HealthMes.Widgets/` — buildable stub project with the provider
  skeleton documented (`WidgetProviderStub.cs`).
- The DATA half is real and unit-tested: `WidgetCard.BuildJson(...)` in
  `HealthMes.Glance.Core` produces the Adaptive Card 1.5 payload (energy
  line, next block, alerts line, `Action.OpenUrl` "Why this?" action) from a
  glance payload — `tests/.../WidgetCardTests.cs`.

**Implementation path (when MSIX is accepted):**
1. Add a Windows Application Packaging project (or `<WindowsPackageType>MSIX`
   with single-project MSIX) wrapping `HealthMes.Tray`.
2. Declare the widget extension + provider CLSID in `Package.appxmanifest`.
3. Implement `IWidgetProvider` (Microsoft.WindowsAppSDK ≥ 1.2):
   `CreateWidget`/`Activate` → `WidgetManager.GetDefault().UpdateWidget`
   with `WidgetCard.BuildJson(latest snapshot)`; `OnActionInvoked` → open the
   decision URL.
4. CI: build the MSIX with a throwaway test cert (`-p:AppxPackageSigningEnabled`)
   or move packaging to a release-only workflow.

## 2. Toast action buttons (✅ 적용 / ✏️ 수정 / ❌ 그대로)

The tray toast is a `NotifyIcon` balloon (Windows 10/11 renders balloons as
toast notifications). It carries the §8.5 grammar lines and a click-through
to the decision viewer ("왜 이 판단?" — the one element no surface may drop,
worksheet §1.1). Action **buttons** are deferred because:
- buttons require `Microsoft.Toolkit.Uwp.Notifications` + COM activation
  registration (unpackaged) or MSIX (packaged) — see §1;
- the §8.5 buttons act on a **schedule proposal id**, which neither
  `/v1/briefing/glance` nor `/v1/alerts` items carry today. Accept/decline
  (`POST /v1/schedule/proposals/{id}/accept|decline`) is wired in the phone
  apps (issue #10) where the proposal list lives; desktop is a glance
  surface. If/when alerts carry a proposal reference, the upgrade path is
  `ToastNotificationManagerCompat` + two buttons + the existing endpoints.

## 3. Not verified on real Windows (no Windows machine touched this code)

Local compile + unit proof exists for everything (macOS cross-build with
`EnableWindowsTargeting` on the official .NET 8 SDK, all-green xunit
suite). Real-Windows compile proof: the windows-latest CI job in
`.github/workflows/windows-apps.yml` builds/tests/publishes on real
Windows — the workflow ships in the same change set as this code, so its
first run, on the PR that introduces it, IS that proof (docs/PLAN.md
phrases it the same way); until that run is green, treat real-Windows
compilation as pending. Runtime behavior that only a human at a Windows
desktop can confirm:

- Tray: NotifyIcon rendering/tooltip, balloon-as-toast appearance, flyout
  placement/focus, Narrator reading order, ko-KR satellite selection.
- DPAPI round-trip of the pairing token (`ProtectedData` CurrentUser) and
  the tray↔screensaver settings handshake via `%LOCALAPPDATA%\HealthMes`.
- Screensaver: `/s /p /c` behavior inside the real screensaver host dialog
  (arg parsing itself is unit-tested), multi-monitor blanking, `.scr`
  right-click Install, preview-pane `SetParent` embedding.
- End-to-end against a live healthmes instance from Windows (the HTTP
  client + contract layer is fully unit-tested and the endpoints are
  live-proven server-side, but not from a Windows host).

## 4. Out of scope by design (not deferred — excluded)

- **Push relay (WNS or any cloud).** Local-first: surfaces poll the paired
  instance; Telegram remains the guaranteed-delivery channel
  (docs/design/WATCH-NOTIFICATIONS.ko.md §1.2).
- **Final visual/UX decisions** — wording, severity colors, state words,
  night behavior are the domain expert's worksheet decisions (Q1–Q6); all
  rendering here is labeled placeholder.
