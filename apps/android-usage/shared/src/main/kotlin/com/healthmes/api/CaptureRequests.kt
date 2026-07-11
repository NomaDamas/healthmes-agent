package com.healthmes.api

import org.json.JSONObject

/**
 * Request-body builders for the capture endpoints — the same contracts the
 * Telegram capture skill uses (docs/PLAN.md §8):
 *
 * - `POST /v1/food-logs` (healthmes/api/food.py `FoodLogCreate`)
 * - `POST /v1/medical-records` (healthmes/api/medical.py
 *   `MedicalRecordCreate`) — the server attaches the deterministic health
 *   snapshot under `context.health` itself; the client's `context` is
 *   capture metadata ONLY (source/surface), never health data.
 *
 * Pure JSON construction so the exact wire bodies are JVM unit-testable.
 */
object CaptureRequests {

    const val FOOD_LOGS_PATH = "/v1/food-logs"
    const val MEDICAL_RECORDS_PATH = "/v1/medical-records"

    /** Medical kinds accepted by the endpoint (healthmes MedicalRecordKind). */
    const val KIND_MEDICATION = "medication"
    const val KIND_SYMPTOM = "symptom"

    fun foodLogBody(
        description: String,
        mediaPath: String?,
        source: String,
    ): String = JSONObject().apply {
        put("description", description)
        mediaPath?.let { put("media_path", it) }
        put("source", source)
    }.toString()

    fun medicalRecordBody(
        kind: String,
        description: String,
        mediaPath: String?,
        transcript: String?,
        captureSource: String,
    ): String = JSONObject().apply {
        put("kind", kind)
        put("description", description)
        mediaPath?.let { put("media_path", it) }
        transcript?.takeIf { it.isNotBlank() }?.let { put("transcript", it) }
        // Capture metadata only — the health snapshot is server-attached.
        put("context", JSONObject().put("source", captureSource))
    }.toString()
}
