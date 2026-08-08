package com.healthmes.companion

import com.healthmes.api.CaptureRequests
import com.healthmes.api.Multipart
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The exact wire bodies the capture flow sends: the multipart/form-data
 * upload for `POST /v1/media` (single `file` field, per the endpoint
 * contract), staged nutrition analyze/review/capture/outcome requests, and
 * the unchanged `POST /v1/medical-records` request.
 */
class MultipartEncodingTest {

    @Test
    fun `encodes one file field with headers and binary payload intact`() {
        val payload = ByteArray(256) { it.toByte() }
        val body = Multipart.encode(
            boundary = "healthmes-test-boundary",
            fieldName = "file",
            filename = "capture",
            contentType = "image/jpeg",
            bytes = payload,
        )
        val text = body.toString(Charsets.ISO_8859_1)

        assertTrue(text.startsWith("--healthmes-test-boundary\r\n"))
        assertTrue(
            text.contains(
                "Content-Disposition: form-data; name=\"file\"; filename=\"capture\"\r\n"
            )
        )
        assertTrue(text.contains("Content-Type: image/jpeg\r\n\r\n"))
        assertTrue(text.endsWith("\r\n--healthmes-test-boundary--\r\n"))

        // The binary payload survives byte-for-byte between the blank line
        // and the closing boundary.
        val headerEnd = text.indexOf("\r\n\r\n") + 4
        val payloadEnd = body.size - "\r\n--healthmes-test-boundary--\r\n".length
        assertArrayEquals(payload, body.copyOfRange(headerEnd, payloadEnd))
    }

    @Test
    fun `photo analysis carries capture provenance and remains local by default`() {
        val body = JSONObject(
            CaptureRequests.photoAnalyzeBody(
                mediaPath = "media/2026/07/abc123.jpg",
                capturedAt = "2026-08-08T12:40:00+09:00",
                timezone = "Asia/Seoul",
                source = "android-companion",
            )
        )

        assertEquals("media/2026/07/abc123.jpg", body.getString("media_path"))
        assertEquals("2026-08-08T12:40:00+09:00", body.getString("captured_at"))
        assertEquals("Asia/Seoul", body.getString("timezone"))
        assertEquals("android-companion", body.getString("source"))
        assertTrue(body.isNull("location"))
        assertEquals(
            "app",
            body.getJSONObject("metadata_provenance").getString("captured_at"),
        )
        assertEquals(
            "unavailable",
            body.getJSONObject("metadata_provenance").getString("location"),
        )
        assertFalse(body.getBoolean("allow_remote_vision"))
    }

    @Test
    fun `photo review is explicit and separate from capture`() {
        val body = JSONObject(
            CaptureRequests.photoReviewBody(
                operationId = "10000000-0000-4000-8000-000000000001",
                status = CaptureRequests.REVIEW_CONFIRMED,
                source = "android-companion",
            )
        )

        assertEquals(
            "10000000-0000-4000-8000-000000000001",
            body.getString("operation_id"),
        )
        assertEquals("confirmed", body.getString("status"))
        assertFalse(body.has("items"))
        assertEquals(
            "/v1/nutrition-observations/observation-123/review",
            CaptureRequests.nutritionObservationReviewPath("observation-123"),
        )
    }

    @Test
    fun `corrected photo review carries complete structured replacements`() {
        val replacement = JSONObject()
            .put("item_index", 0)
            .put("name", "small bottled latte")
            .put("intake_type", "beverage")
            .put(
                "serving",
                JSONObject()
                    .put("kind", "exact")
                    .put("unit", "ml")
                    .put("exact", 250)
                    .put("estimation_basis", "owner_correction"),
            )
            .put(
                "nutrients",
                org.json.JSONArray().put(
                    JSONObject()
                        .put("nutrient", "caffeine")
                        .put(
                            "amount",
                            JSONObject()
                                .put("kind", "exact")
                                .put("unit", "mg")
                                .put("exact", 95)
                                .put("estimation_basis", "owner_correction"),
                        )
                        .put("confidence", "high"),
                ),
            )
            .put("confidence", "high")
        val body = JSONObject(
            CaptureRequests.photoReviewBody(
                operationId = "11000000-0000-4000-8000-000000000011",
                status = CaptureRequests.REVIEW_CORRECTED,
                source = "android-companion",
                correctedItems = listOf(replacement),
            )
        )

        assertEquals("corrected", body.getString("status"))
        assertEquals(
            "small bottled latte",
            body.getJSONArray("items").getJSONObject(0).getString("name"),
        )
        assertEquals(
            95,
            body.getJSONArray("items")
                .getJSONObject(0)
                .getJSONArray("nutrients")
                .getJSONObject(0)
                .getJSONObject("amount")
                .getInt("exact"),
        )
    }

    @Test
    fun `photo capture references reviewed observation without consumed outcome`() {
        val body = JSONObject(
            CaptureRequests.photoInteractionBody(
                operationId = "20000000-0000-4000-8000-000000000002",
                intent = CaptureRequests.INTENT_LOG_CONSUMED,
                nutritionObservationId = "30000000-0000-4000-8000-000000000003",
                source = "android-companion",
                sourceText = "Lunch photo",
            )
        )

        assertEquals("log_consumed", body.getString("intent"))
        assertEquals("photo", body.getString("modality"))
        assertEquals(
            "30000000-0000-4000-8000-000000000003",
            body.getString("nutrition_observation_id"),
        )
        assertFalse(body.has("status"))
        assertFalse(body.has("consumed_at"))
    }

    @Test
    fun `text analysis carries owner text but no outcome`() {
        val body = JSONObject(
            CaptureRequests.textAnalyzeBody(
                operationId = "40000000-0000-4000-8000-000000000004",
                intent = CaptureRequests.INTENT_LOG_CONSUMED,
                observedAt = "2026-08-08T12:40:00+09:00",
                timezone = "Asia/Seoul",
                source = "android-companion",
                sourceText = "Bibimbap and a fried egg",
            )
        )

        assertEquals("text", body.getString("modality"))
        assertEquals("Bibimbap and a fried egg", body.getString("source_text"))
        assertFalse(body.has("media_path"))
        assertFalse(body.has("status"))
        assertFalse(body.getBoolean("allow_remote_analysis"))
    }

    @Test
    fun `voice analysis references local media without sending transcript`() {
        val body = JSONObject(
            CaptureRequests.voiceAnalyzeBody(
                operationId = "50000000-0000-4000-8000-000000000005",
                intent = CaptureRequests.INTENT_LOG_CONSUMED,
                observedAt = "2026-08-08T12:40:00+09:00",
                timezone = "Asia/Seoul",
                source = "android-companion",
                mediaPath = "media/2026/08/meal.m4a",
            )
        )

        assertEquals("voice", body.getString("modality"))
        assertEquals("media/2026/08/meal.m4a", body.getString("media_path"))
        assertFalse(body.has("source_text"))
    }

    @Test
    fun `consumed outcome is a separate explicit owner decision`() {
        val body = JSONObject(
            CaptureRequests.intakeOutcomeBody(
                operationId = "60000000-0000-4000-8000-000000000006",
                status = CaptureRequests.OUTCOME_CONSUMED,
                source = "android-companion",
                consumedAt = "2026-08-08T12:45:00+09:00",
            )
        )

        assertEquals("consumed", body.getString("status"))
        assertEquals("2026-08-08T12:45:00+09:00", body.getString("consumed_at"))
        assertEquals(
            "/v1/intake-interactions/interaction-123/outcomes",
            CaptureRequests.intakeOutcomePath("interaction-123"),
        )
    }

    @Test
    fun `not consumed outcome omits consumed timestamp`() {
        val body = JSONObject(
            CaptureRequests.intakeOutcomeBody(
                operationId = "70000000-0000-4000-8000-000000000007",
                status = CaptureRequests.OUTCOME_NOT_CONSUMED,
                source = "android-companion",
            )
        )

        assertEquals("not_consumed", body.getString("status"))
        assertFalse(body.has("consumed_at"))
    }

    @Test
    fun `cancelled outcome is also an explicit write without consumed timestamp`() {
        val body = JSONObject(
            CaptureRequests.intakeOutcomeBody(
                operationId = "71000000-0000-4000-8000-000000000007",
                status = CaptureRequests.OUTCOME_CANCELLED,
                source = "android-companion",
            )
        )

        assertEquals("cancelled", body.getString("status"))
        assertFalse(body.has("consumed_at"))
    }

    @Test
    fun `medical record body keeps capture metadata under context`() {
        val body = JSONObject(
            CaptureRequests.medicalRecordBody(
                kind = CaptureRequests.KIND_MEDICATION,
                description = "Ibuprofen 200mg, one tablet",
                mediaPath = "media/2026/07/def456.m4a",
                transcript = "Took one ibuprofen after lunch",
                captureSource = "android-companion",
            )
        )

        assertEquals("medication", body.getString("kind"))
        assertEquals("Ibuprofen 200mg, one tablet", body.getString("description"))
        assertEquals("media/2026/07/def456.m4a", body.getString("media_path"))
        assertEquals("Took one ibuprofen after lunch", body.getString("transcript"))
        // Capture metadata ONLY — the health snapshot is attached server-side.
        assertEquals(
            "android-companion",
            body.getJSONObject("context").getString("source"),
        )
        assertEquals(1, body.getJSONObject("context").length())
    }

    @Test
    fun `blank transcript is omitted`() {
        val body = JSONObject(
            CaptureRequests.medicalRecordBody(
                kind = CaptureRequests.KIND_SYMPTOM,
                description = "Mild headache since 15:00",
                mediaPath = null,
                transcript = "  ",
                captureSource = "android-companion",
            )
        )

        assertFalse(body.has("transcript"))
        assertFalse(body.has("media_path"))
    }
}
