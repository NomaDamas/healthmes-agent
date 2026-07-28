plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

// Shared glue for the issue #7 companion surfaces (:companion phone widget,
// :wear tile/complication): the GET /v1/briefing/glance contract model +
// parser, the ETag-aware fetch client, the display-state mapper, and the
// encrypted pairing prefs pattern (base URL + bearer token) copied from the
// :app collector — deliberately duplicated, not imported, so the collector
// module stays untouched.
android {
    namespace = "com.healthmes.briefing"
    compileSdk = 35

    defaultConfig {
        // Must stay <= every consumer's minSdk (:companion 26, :wear 30).
        minSdk = 26
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
    implementation("androidx.core:core-ktx:1.19.0")
    // EncryptedSharedPreferences for base URL + token at rest (same artifact
    // and pattern as the :app collector's CollectorPrefs).
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
}
