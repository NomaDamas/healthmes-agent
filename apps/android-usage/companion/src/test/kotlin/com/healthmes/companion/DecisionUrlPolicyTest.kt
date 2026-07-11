package com.healthmes.companion

import com.healthmes.companion.ui.DecisionUrlPolicy
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The deep-link trust boundary (Android mirror of iOS
 * `AppRouter.isAllowedViewerURL`): MainActivity is exported, so
 * EXTRA_DECISION_URL arrives from arbitrary apps and only http(s) URLs on the
 * paired host may reach the Custom Tabs / WebView decision viewer.
 */
class DecisionUrlPolicyTest {

    private val paired = "http://192.168.1.20:8100"

    @Test
    fun `allows tokenized viewer urls on the paired host`() {
        assertTrue(
            DecisionUrlPolicy.isAllowedViewerUrl(
                "http://192.168.1.20:8100/decisions/0b8f3e0a?token=viewer-abc123", paired
            )
        )
        // https upgrade of the same host is fine (host-only rule, like iOS).
        assertTrue(
            DecisionUrlPolicy.isAllowedViewerUrl("https://192.168.1.20/decisions/x", paired)
        )
        // Host comparison is case-insensitive.
        assertTrue(
            DecisionUrlPolicy.isAllowedViewerUrl(
                "http://MyBox.local:8100/decisions/x", "http://mybox.local:8100"
            )
        )
    }

    @Test
    fun `rejects third-party hosts`() {
        assertFalse(
            DecisionUrlPolicy.isAllowedViewerUrl(
                "http://attacker.example/fake-token-expired", paired
            )
        )
        assertFalse(
            DecisionUrlPolicy.isAllowedViewerUrl("https://evil.example/pair", paired)
        )
    }

    @Test
    fun `rejects non-http schemes`() {
        assertFalse(
            DecisionUrlPolicy.isAllowedViewerUrl("javascript:alert(1)", paired)
        )
        assertFalse(
            DecisionUrlPolicy.isAllowedViewerUrl("file:///etc/hosts", paired)
        )
        assertFalse(
            DecisionUrlPolicy.isAllowedViewerUrl("intent://scan/#Intent;end", paired)
        )
    }

    @Test
    fun `rejects malformed or empty urls`() {
        assertFalse(DecisionUrlPolicy.isAllowedViewerUrl(null, paired))
        assertFalse(DecisionUrlPolicy.isAllowedViewerUrl("", paired))
        assertFalse(DecisionUrlPolicy.isAllowedViewerUrl("   ", paired))
        assertFalse(DecisionUrlPolicy.isAllowedViewerUrl("http://[malformed", paired))
        // Scheme-only / hostless URLs never pass.
        assertFalse(DecisionUrlPolicy.isAllowedViewerUrl("http://", paired))
    }

    @Test
    fun `rejects everything while unpaired`() {
        assertFalse(
            DecisionUrlPolicy.isAllowedViewerUrl("http://192.168.1.20:8100/decisions/x", null)
        )
        assertFalse(
            DecisionUrlPolicy.isAllowedViewerUrl("http://192.168.1.20:8100/decisions/x", "")
        )
    }
}
