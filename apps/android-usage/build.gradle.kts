// Root build file: plugin versions only. Known-good matrix for Gradle 8.9
// (gradle/wrapper/gradle-wrapper.properties): AGP 8.7.x requires Gradle >= 8.9
// and JDK 17+, Kotlin 2.0.x is the matching stable Kotlin release.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}
