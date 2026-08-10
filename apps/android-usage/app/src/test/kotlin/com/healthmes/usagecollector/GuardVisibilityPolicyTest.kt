package com.healthmes.usagecollector

import android.app.NotificationManager
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class GuardVisibilityPolicyTest {

    @Test
    fun `pre 33 devices do not require runtime notification permission`() {
        assertTrue(
            notificationPermissionAllowsVisibleGuard(
                apiLevel = 32,
                postNotificationsGranted = false,
            )
        )
    }

    @Test
    fun `api 33 and newer require notification permission`() {
        assertFalse(
            notificationPermissionAllowsVisibleGuard(
                apiLevel = 33,
                postNotificationsGranted = false,
            )
        )
        assertTrue(
            notificationPermissionAllowsVisibleGuard(
                apiLevel = 33,
                postNotificationsGranted = true,
            )
        )
    }

    @Test
    fun `app-wide notification disablement fails the visible guard`() {
        assertFalse(
            notificationSettingsAllowVisibleGuard(
                apiLevel = 35,
                postNotificationsGranted = true,
                appNotificationsEnabled = false,
                channelImportance = NotificationManager.IMPORTANCE_LOW,
            )
        )
    }

    @Test
    fun `disabled guard channel fails even when app notifications are enabled`() {
        assertFalse(
            notificationSettingsAllowVisibleGuard(
                apiLevel = 35,
                postNotificationsGranted = true,
                appNotificationsEnabled = true,
                channelImportance = NotificationManager.IMPORTANCE_NONE,
            )
        )
    }

    @Test
    fun `missing channel is allowed so first service start can create it`() {
        assertTrue(
            notificationSettingsAllowVisibleGuard(
                apiLevel = 35,
                postNotificationsGranted = true,
                appNotificationsEnabled = true,
                channelImportance = null,
            )
        )
    }
}
