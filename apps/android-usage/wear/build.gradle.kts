plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Issue #7 Wear OS surface: a ProtoLayout tile (energy score + next block)
// and a complication data source (SHORT_TEXT / RANGED_VALUE energy score)
// fed by the same GET /v1/briefing/glance contract via :shared. Placeholder
// visuals only — the final watch UX is the healthcare domain expert's
// deliverable (docs/design/WATCH-NOTIFICATIONS.ko.md).
android {
    namespace = "com.healthmes.wear"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.healthmes.wear"
        // Wear OS 3 (API 30) is the floor for the tiles/complications stack
        // used here; older Wear hardware is out of scope.
        minSdk = 30
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
}

dependencies {
    implementation(project(":shared"))

    implementation("androidx.core:core-ktx:1.19.0")
    // Tiles 1.4.x builds layouts with the protolayout 1.2.x builders.
    implementation("androidx.wear.tiles:tiles:1.4.1")
    implementation("androidx.wear.protolayout:protolayout:1.2.1")
    implementation("androidx.wear.protolayout:protolayout-material:1.2.1")
    // Watch-face complication data source (energy score).
    implementation("androidx.wear.watchface:watchface-complications-data-source:1.2.1")
    // CallbackToFutureAdapter for the ListenableFuture-based tile callbacks.
    implementation("androidx.concurrent:concurrent-futures:1.2.0")
}
