package com.healthmes.companion

/** Loads contract fixtures from src/test/resources. */
object Fixtures {

    fun load(name: String): String =
        checkNotNull(javaClass.classLoader?.getResource(name)) {
            "missing test fixture: $name"
        }.readText()

    /** Populated payload matching the /v1/briefing/glance contract verbatim. */
    fun full(): String = load("glance_full.json")

    /** Empty-database shape (all-null energy, no blocks/alerts/decision). */
    fun empty(): String = load("glance_empty.json")
}
