plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    // Glance widgets are @Composable code; with Kotlin 2.x the Compose
    // compiler is this Kotlin subplugin (version pinned in the root build).
    id("org.jetbrains.kotlin.plugin.compose")
}

// Issue #7 phone companion: Glance home-screen widget (small + medium),
// 15-minute ETag-honoring WorkManager refresh of GET /v1/briefing/glance,
// and a local notification channel that renders the docs/PLAN.md §8.5
// grammar when the unresolved-alert count rises. Local-first: talks only to
// the paired HealthMes instance.
android {
    namespace = "com.healthmes.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.healthmes.companion"
        // java.time + Glance + WorkManager are all happy on 26+, matching the
        // :app collector's floor.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
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
    // 15-minute periodic refresh (WorkManager's minimum periodic interval).
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    // Glance AppWidget (composable RemoteViews) for the briefing widget.
    implementation("androidx.glance:glance-appwidget:1.1.1")
    // Pin a Compose runtime that matches the Kotlin 2.0.x Compose compiler.
    implementation("androidx.compose.runtime:runtime:1.7.5")

    testImplementation("junit:junit:4.13.2")
    // Unit tests exercise the shared org.json-based contract parser on the
    // JVM; android.jar's org.json is a stub there, so bring the real one.
    testImplementation("org.json:json:20240303")
}
