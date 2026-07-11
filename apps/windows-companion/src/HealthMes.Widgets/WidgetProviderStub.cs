using HealthMes.Glance.Core;

namespace HealthMes.Widgets;

/// <summary>
/// Windows 11 Widgets Board provider — STUB (see DEFERRED.md at the app
/// root for why, and for the step-by-step implementation path).
///
/// What a real provider needs that this repo deliberately does not ship yet:
/// <list type="number">
/// <item>MSIX packaging: a <c>Package.appxmanifest</c> declaring the
/// <c>com.microsoft.windows.widgets</c> uap3 app extension with the widget
/// definitions (name, sizes, template).</item>
/// <item>An out-of-process COM server implementing
/// <c>Microsoft.Windows.Widgets.Providers.IWidgetProvider</c>
/// (CreateWidget / DeleteWidget / OnActionInvoked / OnWidgetContextChanged),
/// activated by the Widgets Board via the CLSID in the manifest.</item>
/// <item>Signing: MSIX must be signed (or sideloaded with a dev cert) —
/// exactly the CI complexity issue #11's Windows job avoids.</item>
/// </list>
///
/// The DATA half is already real and tested: the provider's
/// <c>WidgetManager.GetDefault().UpdateWidget(...)</c> payload is the
/// Adaptive Card JSON built by <see cref="WidgetCard"/> from the same glance
/// snapshot the tray maintains, so wiring the provider later is plumbing
/// only — no new mapping logic.
/// </summary>
public static class WidgetProviderStub
{
    /// <summary>The card a real provider would push for a glance payload.</summary>
    public static string CardJsonFor(GlancePayload? payload) => WidgetCard.BuildJson(payload);
}
