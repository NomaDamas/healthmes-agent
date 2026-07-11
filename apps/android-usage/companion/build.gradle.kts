plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    // Compose UI + Glance widgets are @Composable code; with Kotlin 2.x the
    // Compose compiler is this Kotlin subplugin (version pinned in the root
    // build).
    id("org.jetbrains.kotlin.plugin.compose")
}

// Issue #10 full phone companion (promoted from the issue #7 widget host):
// single-activity Compose app with the briefing home (score + 24h curve +
// next blocks + alert history + latest decision), native weekly report,
// decision viewer (Custom Tabs, WebView fallback), camera/voice capture into
// POST /v1/media + food/medical creates, real §8.5 notification actions
// against the schedule-proposal endpoints, and an ongoing focus-block
// notification. Still local-first: talks only to the paired HealthMes
// instance; the Glance widget + 15-minute ETag-honoring refresh stay as
// before.
android {
    namespace = "com.healthmes.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.healthmes.companion"
        // java.time + Glance + WorkManager + the photo picker fallback are
        // all happy on 26+, matching the :app collector's floor.
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "0.2.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }
}

dependencies {
    implementation(project(":shared"))

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    // 15-minute periodic refresh + one-shot notification-action calls.
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    // Glance AppWidget (composable RemoteViews) for the briefing widget.
    implementation("androidx.glance:glance-appwidget:1.1.1")

    // Compose UI for the single-activity app. The BOM release pairs with the
    // Kotlin 2.0.x Compose compiler (runtime 1.7.x, material3 1.3.x).
    val composeBom = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(composeBom)
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")

    // Decision viewer: Custom Tabs first, in-app WebView fallback.
    implementation("androidx.browser:browser:1.8.0")

    testImplementation("junit:junit:4.13.2")
    // Unit tests exercise the shared org.json-based contract parsers on the
    // JVM; android.jar's org.json is a stub there, so bring the real one.
    testImplementation("org.json:json:20240303")
}
