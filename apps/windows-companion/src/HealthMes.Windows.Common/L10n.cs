using System.Globalization;
using System.Resources;
using HealthMes.Glance.Core;

namespace HealthMes.Windows.Common;

/// <summary>
/// Strongly-named access to Resources/Strings.resx (+ the Korean satellite
/// Strings.ko.resx). CLI-first project, so no Visual Studio designer codegen:
/// this thin wrapper is the accessor. The satellite is picked by
/// <see cref="CultureInfo.CurrentUICulture"/> as usual.
/// </summary>
public static class L10n
{
    private static readonly ResourceManager Resources = new(
        "HealthMes.Windows.Common.Resources.Strings", typeof(L10n).Assembly);

    public static string Get(string key) =>
        Resources.GetString(key, CultureInfo.CurrentUICulture) ?? key;

    public static string Format(string key, params object[] args) =>
        string.Format(CultureInfo.CurrentUICulture, Get(key), args);

    /// <summary>Localized confidence word for screen readers / labels.</summary>
    public static string ConfidenceWord(GlanceConfidence confidence) => confidence switch
    {
        GlanceConfidence.High => Get("Confidence_High"),
        GlanceConfidence.Medium => Get("Confidence_Medium"),
        _ => Get("Confidence_Low"),
    };

    /// <summary>
    /// Screen-reader line for the energy slot, per the worksheet's §1.4
    /// mandate ("인지에너지 58점, 신뢰도 높음" — numbers must be *read*, not
    /// just shown).
    /// </summary>
    public static string EnergyAccessibleText(GlancePayload payload)
    {
        var confidence = ConfidenceWord(payload.Energy.Confidence);
        return payload.Energy.Score is { } score
            ? Format("Ax_EnergyScore", score, confidence)
            : Format("Ax_EnergyScoreMissing", confidence);
    }
}
