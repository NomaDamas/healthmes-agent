package com.healthmes.companion.ui

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AddCircle
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import com.healthmes.companion.MainActivity
import com.healthmes.companion.R

/**
 * Navigation shell of the single-activity app: five tabs plus a full-screen
 * in-app decision-viewer overlay (the Custom Tabs fallback). Hand-rolled tab
 * state — the app is small enough that a navigation library would be more
 * code than this.
 */
enum class AppTab(val route: String, val labelRes: Int, val icon: ImageVector) {
    HOME("home", R.string.tab_home, Icons.Filled.Home),
    REPORT("report", R.string.tab_report, Icons.Filled.DateRange),
    CAPTURE("capture", R.string.tab_capture, Icons.Filled.AddCircle),
    PROPOSALS("proposals", R.string.tab_proposals, Icons.Filled.Edit),
    SETTINGS("settings", R.string.tab_settings, Icons.Filled.Settings),
}

@Composable
fun CompanionApp(
    navRequest: MainActivity.NavRequest?,
    onNavConsumed: () -> Unit,
) {
    val context = LocalContext.current
    val services = remember { AppServices(context.applicationContext) }
    var selectedTab by rememberSaveable { mutableStateOf(AppTab.HOME.route) }
    // Non-null while the in-app WebView viewer overlays the tabs.
    var viewerUrl by rememberSaveable { mutableStateOf<String?>(null) }

    // Custom Tabs first; the in-app WebView screen is the fallback.
    val openDecision: (String) -> Unit = { url ->
        DecisionOpener.open(context, url) { fallbackUrl -> viewerUrl = fallbackUrl }
    }

    LaunchedEffect(navRequest) {
        when (navRequest) {
            is MainActivity.NavRequest.Tab -> {
                if (AppTab.entries.any { it.route == navRequest.route }) {
                    selectedTab = navRequest.route
                }
                viewerUrl = null
            }

            // NavRequests decode from exported-activity intent extras, i.e.
            // from OUTSIDE the app — validate against the paired host before
            // any viewer surface renders the URL (same rule as iOS
            // AppRouter.handle); rejected links fall back to home.
            is MainActivity.NavRequest.Decision ->
                if (DecisionUrlPolicy.isAllowedViewerUrl(
                        navRequest.url, services.prefs.serverUrl
                    )
                ) {
                    openDecision(navRequest.url)
                } else {
                    selectedTab = AppTab.HOME.route
                }

            null -> Unit
        }
        if (navRequest != null) onNavConsumed()
    }

    val overlayUrl = viewerUrl
    if (overlayUrl != null) {
        DecisionViewerScreen(url = overlayUrl, onClose = { viewerUrl = null })
        return
    }

    Scaffold(
        bottomBar = {
            NavigationBar {
                AppTab.entries.forEach { tab ->
                    val label = stringResource(tab.labelRes)
                    NavigationBarItem(
                        selected = selectedTab == tab.route,
                        onClick = { selectedTab = tab.route },
                        // Text label + labeled icon = TalkBack reads the tab.
                        icon = { Icon(tab.icon, contentDescription = label) },
                        label = { Text(label) },
                    )
                }
            }
        }
    ) { padding ->
        val contentModifier = Modifier.padding(padding)
        when (selectedTab) {
            AppTab.HOME.route -> HomeScreen(
                services = services,
                onOpenDecision = openDecision,
                onGoToSettings = { selectedTab = AppTab.SETTINGS.route },
                modifier = contentModifier,
            )

            AppTab.REPORT.route -> ReportScreen(
                services = services,
                onOpenUrl = openDecision,
                modifier = contentModifier,
            )

            AppTab.CAPTURE.route -> CaptureScreen(services = services, modifier = contentModifier)

            AppTab.PROPOSALS.route -> ProposalsScreen(
                services = services,
                modifier = contentModifier,
            )

            AppTab.SETTINGS.route -> SettingsScreen(
                services = services,
                modifier = contentModifier,
            )
        }
    }
}
