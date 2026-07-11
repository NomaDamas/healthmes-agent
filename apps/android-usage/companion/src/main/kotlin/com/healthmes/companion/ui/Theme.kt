package com.healthmes.companion.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * PLACEHOLDER design tokens — the same day/night palette the briefing widget
 * uses (brand green from the launcher mark), lifted into a Material3 scheme
 * so app and widget agree. Final colors/typography/thresholds are the
 * healthcare domain expert's deliverable
 * (docs/design/WATCH-NOTIFICATIONS.ko.md); everything else in the app should
 * read colors from [MaterialTheme] so the expert pass swaps one file.
 *
 * Typography stays Material3 defaults on purpose: default text styles are
 * sp-based, so Dynamic Type (system font scaling) works everywhere for free.
 */
private val LightColors = lightColorScheme(
    primary = Color(0xFF1B7F5C),
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFD9F0E4),
    onPrimaryContainer = Color(0xFF08331F),
    secondary = Color(0xFF5A6B62),
    onSecondary = Color(0xFFFFFFFF),
    background = Color(0xFFF6FBF8),
    onBackground = Color(0xFF17241D),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF17241D),
    surfaceVariant = Color(0xFFE7F1EA),
    onSurfaceVariant = Color(0xFF5A6B62),
    error = Color(0xFFB3261E),
    onError = Color(0xFFFFFFFF),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF63C79E),
    onPrimary = Color(0xFF06281A),
    primaryContainer = Color(0xFF1D5940),
    onPrimaryContainer = Color(0xFFD9F0E4),
    secondary = Color(0xFF93A69B),
    onSecondary = Color(0xFF14211B),
    background = Color(0xFF14211B),
    onBackground = Color(0xFFE1EFE7),
    surface = Color(0xFF1B2B23),
    onSurface = Color(0xFFE1EFE7),
    surfaceVariant = Color(0xFF243830),
    onSurfaceVariant = Color(0xFF93A69B),
    error = Color(0xFFF2B8B5),
    onError = Color(0xFF3B0907),
)

@Composable
fun HealthmesTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors,
        content = content,
    )
}
