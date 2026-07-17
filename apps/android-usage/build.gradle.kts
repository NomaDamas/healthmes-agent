// Root build file: plugin versions only. Known-good matrix for Gradle 8.9
// (gradle/wrapper/gradle-wrapper.properties): AGP 8.7.x requires Gradle >= 8.9
// and JDK 17+, Kotlin 2.0.x is the matching stable Kotlin release.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("com.android.library") version "9.2.1" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    // Required by :companion for Glance (@Composable) widget code; with
    // Kotlin 2.x the Compose compiler ships as this Kotlin subplugin and its
    // version must match the Kotlin version above.
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
}
