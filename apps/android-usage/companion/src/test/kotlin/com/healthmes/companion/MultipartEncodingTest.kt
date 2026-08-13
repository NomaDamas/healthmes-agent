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
 * contract) and the JSON creates for `POST /v1/food-logs` /
 * `POST /v1/medical-records`.
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
    fun `food log body carries description, media path, and source`() {
        val body = JSONObject(
            CaptureRequests.foodLogBody(
                description = "Bibimbap, small bowl",
                mediaPath = "media/2026/07/abc123.jpg",
                source = "android-companion",
            )
        )

        assertEquals("Bibimbap, small bowl", body.getString("description"))
        assertEquals("media/2026/07/abc123.jpg", body.getString("media_path"))
        assertEquals("android-companion", body.getString("source"))
    }

    @Test
    fun `text-only food log omits media_path`() {
        val body = JSONObject(
            CaptureRequests.foodLogBody("Espresso", mediaPath = null, source = "android-companion")
        )

        assertFalse(body.has("media_path"))
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
