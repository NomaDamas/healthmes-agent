package com.healthmes.usagecollector

import android.app.Application

class UsageCollectorApplication : Application() {

    private lateinit var usageAccessMonitor: UsageAccessMonitor

    override fun onCreate() {
        super.onCreate()
        usageAccessMonitor = UsageAccessMonitor(this)
        usageAccessMonitor.start()
    }
}
