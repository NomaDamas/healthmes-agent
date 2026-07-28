plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.healthmes.usagecollector"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.healthmes.usagecollector"
        // UsageStatsManager.queryEvents + java.time need API 26+ anyway;
        // docs/PLAN.md §7 targets a minimal personal collector, not Play store reach.
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
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.1")
    implementation("com.google.android.material:material:1.12.0")
    // Periodic 30-min upload with constraints + exponential backoff.
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    // EncryptedSharedPreferences for server URL + ingest token at rest.
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // HourlyBucketer is pure Kotlin, testable on the JVM.
    testImplementation("junit:junit:4.13.2")
}
