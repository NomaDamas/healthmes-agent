// Root build file: plugin versions only. AGP 9.3.x requires Gradle 9.5+
// and JDK 17+; the wrapper is pinned to Gradle 9.6.1.
plugins {
    id("com.android.application") version "9.3.1" apply false
    id("com.android.library") version "9.3.1" apply false
    // Required by :companion for Glance (@Composable) widget code; with
    // Kotlin 2.x the Compose compiler ships as this Kotlin subplugin and its
    // version must match the Kotlin version above.
    id("org.jetbrains.kotlin.plugin.compose") version "2.4.10" apply false
}
