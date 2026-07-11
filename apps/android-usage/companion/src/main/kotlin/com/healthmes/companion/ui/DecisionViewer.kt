package com.healthmes.companion.ui

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.view.ViewGroup
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.net.toUri
import com.healthmes.companion.R

/**
 * Decision viewer plumbing (issue #10): the tokenized `decision_url` /
 * `report_url` links are browser-ready as-is (the read-only viewer token is
 * already embedded by the server). Preferred surface is Custom Tabs (native
 * back/share for free); [DecisionViewerScreen] is the in-app WebView fallback
 * for devices without any browser.
 */
object DecisionOpener {

    /** Try Custom Tabs; on browserless devices hand the URL to [fallback]. */
    fun open(context: Context, url: String, fallback: (String) -> Unit) {
        try {
            CustomTabsIntent.Builder()
                .setShowTitle(true)
                .build()
                .launchUrl(context, url.toUri())
        } catch (_: ActivityNotFoundException) {
            fallback(url)
        }
    }
}

/**
 * Full-screen WebView fallback with native back + share. JavaScript stays ON
 * because the decision pages render Mermaid trees client-side. The WebView
 * only ever loads the paired instance's tokenized viewer URLs — enforced,
 * not assumed: URLs from the paired server's own payloads are trusted, and
 * deep-link URLs (exported-activity extras) must pass
 * [DecisionUrlPolicy.isAllowedViewerUrl] in [CompanionApp] before reaching
 * either viewer surface.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DecisionViewerScreen(url: String, onClose: () -> Unit) {
    val context = LocalContext.current
    BackHandler(onBack = onClose)

    Column(modifier = Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(stringResource(R.string.viewer_title), style = MaterialTheme.typography.titleMedium) },
            navigationIcon = {
                IconButton(onClick = onClose) {
                    Icon(
                        Icons.AutoMirrored.Filled.ArrowBack,
                        contentDescription = stringResource(R.string.viewer_back),
                    )
                }
            },
            actions = {
                IconButton(onClick = { shareLink(context, url) }) {
                    Icon(
                        Icons.Filled.Share,
                        contentDescription = stringResource(R.string.viewer_share),
                    )
                }
            },
        )
        AndroidView(
            modifier = Modifier
                .fillMaxWidth()
                .fillMaxSize(),
            factory = { ctx ->
                WebView(ctx).apply {
                    layoutParams = ViewGroup.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT,
                    )
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    webViewClient = object : WebViewClient() {
                        override fun shouldOverrideUrlLoading(
                            view: WebView,
                            request: WebResourceRequest,
                        ): Boolean = false // viewer pages stay in this WebView
                    }
                    loadUrl(url)
                }
            },
            update = { webView ->
                if (webView.url != url) webView.loadUrl(url)
            },
        )
    }
}

private fun shareLink(context: Context, url: String) {
    val send = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_TEXT, url)
    }
    try {
        context.startActivity(Intent.createChooser(send, null))
    } catch (_: ActivityNotFoundException) {
        // Nothing can handle SEND — nothing to do.
    }
}
