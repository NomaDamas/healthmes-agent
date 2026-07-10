pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "healthmes-android-usage"
include(":app")
// Issue #7 companion surfaces: shared glance-contract parsing + pairing prefs,
// phone home-screen widget + local alert notifications, Wear OS tile +
// complication. The apps talk only to the user's own HealthMes instance.
include(":shared")
include(":companion")
include(":wear")
