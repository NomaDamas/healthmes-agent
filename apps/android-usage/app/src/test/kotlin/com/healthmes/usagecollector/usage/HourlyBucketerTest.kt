package com.healthmes.usagecollector.usage

import com.healthmes.usagecollector.usage.AppForegroundEvent.Kind
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Pure-JVM tests for the hourly bucketing fold. Timestamps use small epoch
 * values (epoch 0 is hour-aligned), so `H0 = 0ms`, `H1 = 3_600_000ms`, ...
 */
class HourlyBucketerTest {

    private val h0 = 0L
    private val h1 = HourlyBucketer.HOUR_MS
    private val h2 = 2 * HourlyBucketer.HOUR_MS

    private fun resumed(pkg: String, atMs: Long, activity: String? = "Main") =
        AppForegroundEvent(pkg, activity, atMs, Kind.RESUMED)

    private fun paused(pkg: String, atMs: Long, activity: String? = "Main") =
        AppForegroundEvent(pkg, activity, atMs, Kind.PAUSED)

    private fun stopped(pkg: String, atMs: Long, activity: String? = "Main") =
        AppForegroundEvent(pkg, activity, atMs, Kind.STOPPED)

    @Test
    fun `floors timestamps to the hour`() {
        assertEquals(h1, HourlyBucketer.floorToHour(h1))
        assertEquals(h1, HourlyBucketer.floorToHour(h1 + 59 * 60_000L))
        assertEquals(h0, HourlyBucketer.floorToHour(h1 - 1))
    }

    @Test
    fun `single interval within one hour yields seconds and one launch`() {
        val events = listOf(
            resumed("com.slack", 600_000L),
            paused("com.slack", 940_000L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.slack", 340, 1)), buckets)
    }

    @Test
    fun `interval spanning an hour boundary is split across buckets`() {
        val events = listOf(
            resumed("com.maps", h1 - 600_000L), // 0:50
            paused("com.maps", h1 + 600_000L), // 1:10
        )

        val buckets = HourlyBucketer.bucket(events, h0, h2)

        assertEquals(
            listOf(
                UsageBucket(h0, "com.maps", 600, 1),
                UsageBucket(h1, "com.maps", 600, 0),
            ),
            buckets,
        )
    }

    @Test
    fun `overlapping activities of one package count once and launch once`() {
        val events = listOf(
            resumed("com.app", 600_000L, "ActivityA"),
            resumed("com.app", 720_000L, "ActivityB"),
            paused("com.app", 780_000L, "ActivityA"),
            stopped("com.app", 800_000L, "ActivityA"),
            paused("com.app", 1_200_000L, "ActivityB"),
            stopped("com.app", 1_220_000L, "ActivityB"),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 600, 1)), buckets)
    }

    @Test
    fun `anonymous stop consumes its pause while another anonymous activity remains`() {
        val events = listOf(
            resumed("com.app", 0L, null),
            resumed("com.app", 10_000L, null),
            paused("com.app", 20_000L, null),
            stopped("com.app", 21_000L, null),
        )

        val buckets = HourlyBucketer.bucket(events, h0, 60_000L)

        assertEquals(listOf(UsageBucket(h0, "com.app", 60, 1)), buckets)
    }

    @Test
    fun `re-resume of the same activity does not double launch`() {
        val events = listOf(
            resumed("com.app", 600_000L),
            resumed("com.app", 700_000L),
            paused("com.app", 900_000L),
            stopped("com.app", 920_000L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 300, 1)), buckets)
    }

    @Test
    fun `interval still open at window end is clipped to window end`() {
        val events = listOf(resumed("com.app", 1_800_000L))

        val buckets = HourlyBucketer.bucket(events, h0, 2_700_000L)

        assertEquals(listOf(UsageBucket(h0, "com.app", 900, 1)), buckets)
    }

    @Test
    fun `orphan pause and stop count from window start without a launch`() {
        val events = listOf(
            paused("com.app", 300_000L),
            stopped("com.app", 320_000L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 300, 0)), buckets)
    }

    @Test
    fun `repeated orphan lifecycle pairs never double count`() {
        val events = listOf(
            paused("com.app", 300_000L, "ActivityA"),
            stopped("com.app", 320_000L, "ActivityA"),
            paused("com.app", 600_000L, "ActivityB"),
            stopped("com.app", 620_000L, "ActivityB"),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 600, 0)), buckets)
    }

    @Test
    fun `launch is attributed to the bucket containing the resume`() {
        val events = listOf(
            resumed("com.app", h1 - 60_000L), // 0:59
            paused("com.app", h1 + 60_000L), // 1:01
            stopped("com.app", h1 + 61_000L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h2)

        assertEquals(
            listOf(
                UsageBucket(h0, "com.app", 60, 1),
                UsageBucket(h1, "com.app", 60, 0),
            ),
            buckets,
        )
    }

    @Test
    fun `sub-second launch is kept with zero foreground seconds`() {
        val events = listOf(
            resumed("com.app", 100L),
            paused("com.app", 600L),
            stopped("com.app", 700L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 0, 1)), buckets)
    }

    @Test
    fun `events outside the window are ignored`() {
        val events = listOf(
            resumed("com.app", 600_000L),
            paused("com.app", 900_000L),
            stopped("com.app", 920_000L),
            resumed("com.other", h2 + 100L), // beyond window end
        )

        val buckets = HourlyBucketer.bucket(events, h0, h2)

        assertEquals(listOf(UsageBucket(h0, "com.app", 300, 1)), buckets)
    }

    @Test
    fun `unsorted event input is handled`() {
        val events = listOf(
            paused("com.app", 940_000L),
            stopped("com.app", 960_000L),
            resumed("com.app", 600_000L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 340, 1)), buckets)
    }

    @Test
    fun `multiple packages stay separate`() {
        val events = listOf(
            resumed("com.a", 0L),
            paused("com.a", 120_000L),
            stopped("com.a", 121_000L),
            resumed("com.b", 120_000L),
            paused("com.b", 300_000L),
            stopped("com.b", 301_000L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(
            listOf(
                UsageBucket(h0, "com.a", 120, 1),
                UsageBucket(h0, "com.b", 180, 1),
            ),
            buckets,
        )
    }

    @Test
    fun `paused then stopped ends at pause rather than lifecycle cleanup`() {
        val events = listOf(
            resumed("com.app", 0L),
            paused("com.app", 10_000L),
            stopped("com.app", 20_000L),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 10, 1)), buckets)
    }

    @Test
    fun `paused then resumed screen transition keeps one package launch`() {
        val events = listOf(
            resumed("com.app", 0L, "ActivityA"),
            paused("com.app", 10_000L, "ActivityA"),
            resumed("com.app", 11_000L, "ActivityB"),
            stopped("com.app", 12_000L, "ActivityA"),
            paused("com.app", 30_000L, "ActivityB"),
            stopped("com.app", 31_000L, "ActivityB"),
        )

        val buckets = HourlyBucketer.bucket(events, h0, h1)

        assertEquals(listOf(UsageBucket(h0, "com.app", 30, 1)), buckets)
    }
}
