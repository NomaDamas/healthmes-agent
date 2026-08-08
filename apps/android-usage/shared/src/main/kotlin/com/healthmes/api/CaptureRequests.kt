package com.healthmes.api

import org.json.JSONObject

/**
 * Pure request-body builders for HealthMes capture endpoints.
 *
 * Nutrition is intentionally staged:
 *
 * 1. analyze a photo/text/voice capture,
 * 2. let the owner review the structured result,
 * 3. create or reuse the intake interaction,
 * 4. write an outcome only from the owner's explicit consumed/not-consumed
 *    decision.
 *
 * An analyzed capture is never proof of consumption.
 *
 * - `POST /v1/medical-records` (healthmes/api/medical.py
 *   `MedicalRecordCreate`) — the server attaches the deterministic health
 *   snapshot under `context.health` itself; the client's `context` is
 *   capture metadata ONLY (source/surface), never health data.
 *
 * Pure JSON construction so the exact wire bodies are JVM unit-testable.
 */
object CaptureRequests {

    const val NUTRITION_OBSERVATIONS_ANALYZE_PATH =
        "/v1/nutrition-observations/analyze"
    const val INTAKE_INTERACTIONS_PATH = "/v1/intake-interactions"
    const val INTAKE_INTERACTIONS_ANALYZE_PATH =
        "/v1/intake-interactions/analyze"
    const val MEDICAL_RECORDS_PATH = "/v1/medical-records"

    const val INTENT_LOG_CONSUMED = "log_consumed"
    const val INTENT_ASK_BEFORE_INTAKE = "ask_before_intake"
    const val INTENT_INSPECT_ONLY = "inspect_only"
    const val INTENT_PLAN_FUTURE = "plan_future"
    const val INTENT_COMPARE_OPTION = "compare_option"

    const val MODALITY_PHOTO = "photo"
    const val MODALITY_TEXT = "text"
    const val MODALITY_VOICE = "voice"

    const val REVIEW_CONFIRMED = "confirmed"
    const val REVIEW_CORRECTED = "corrected"
    const val REVIEW_REJECTED = "rejected"

    const val OUTCOME_CONSUMED = "consumed"
    const val OUTCOME_NOT_CONSUMED = "not_consumed"
    const val OUTCOME_CANCELLED = "cancelled"

    /** Medical kinds accepted by the endpoint (healthmes MedicalRecordKind). */
    const val KIND_MEDICATION = "medication"
    const val KIND_SYMPTOM = "symptom"

    fun nutritionObservationReviewPath(observationId: String): String =
        "/v1/nutrition-observations/$observationId/review"

    fun intakeOutcomePath(interactionId: String): String =
        "/v1/intake-interactions/$interactionId/outcomes"

    /**
     * Analyze an uploaded image. The returned observation remains
     * unconfirmed until the owner reviews it.
     */
    fun photoAnalyzeBody(
        mediaPath: String,
        capturedAt: String,
        timezone: String,
        source: String,
        allowRemoteVision: Boolean = false,
    ): String = JSONObject().apply {
        put("media_path", mediaPath)
        put("captured_at", capturedAt)
        put("timezone", timezone)
        put("source", source)
        put("location", JSONObject.NULL)
        put(
            "metadata_provenance",
            JSONObject()
                .put("captured_at", "app")
                .put("timezone", "app")
                .put("location", "unavailable"),
        )
        put("allow_remote_vision", allowRemoteVision)
    }.toString()

    /**
     * Store the owner's explicit review of one photo observation.
     *
     * `correctedItems` must contain a complete replacement only when
     * [status] is `corrected`; confirmed/rejected reviews send no items.
     */
    fun photoReviewBody(
        operationId: String,
        status: String,
        source: String,
        correctedItems: List<JSONObject> = emptyList(),
    ): String = JSONObject().apply {
        put("operation_id", operationId)
        put("status", status)
        put("source", source)
        if (correctedItems.isNotEmpty()) {
            put("items", jsonArray(correctedItems))
        }
    }.toString()

    /**
     * Create an interaction from an owner-reviewed photo observation.
     * This still does not create a consumed outcome.
     */
    fun photoInteractionBody(
        operationId: String,
        intent: String,
        nutritionObservationId: String,
        source: String,
        sourceText: String? = null,
    ): String = JSONObject().apply {
        put("operation_id", operationId)
        put("intent", intent)
        put("modality", MODALITY_PHOTO)
        put("source", source)
        sourceText?.takeIf { it.isNotBlank() }?.let { put("source_text", it) }
        put("nutrition_observation_id", nutritionObservationId)
    }.toString()

    /** Analyze owner-entered text and return an unconfirmed interaction. */
    fun textAnalyzeBody(
        operationId: String,
        intent: String,
        observedAt: String,
        timezone: String,
        source: String,
        sourceText: String,
        allowRemoteAnalysis: Boolean = false,
    ): String = JSONObject().apply {
        put("operation_id", operationId)
        put("intent", intent)
        put("modality", MODALITY_TEXT)
        put("observed_at", observedAt)
        put("timezone", timezone)
        put("source", source)
        put("source_text", sourceText)
        put("allow_remote_analysis", allowRemoteAnalysis)
    }.toString()

    /** Analyze a locally uploaded voice memo and return an unconfirmed interaction. */
    fun voiceAnalyzeBody(
        operationId: String,
        intent: String,
        observedAt: String,
        timezone: String,
        source: String,
        mediaPath: String,
        allowRemoteAnalysis: Boolean = false,
    ): String = JSONObject().apply {
        put("operation_id", operationId)
        put("intent", intent)
        put("modality", MODALITY_VOICE)
        put("observed_at", observedAt)
        put("timezone", timezone)
        put("source", source)
        put("media_path", mediaPath)
        put("allow_remote_analysis", allowRemoteAnalysis)
    }.toString()

    /**
     * Store the owner's exact outcome. Call this only after the structured
     * interaction has been shown and the owner explicitly decides.
     */
    fun intakeOutcomeBody(
        operationId: String,
        status: String,
        source: String,
        consumedAt: String? = null,
        correctedItems: List<JSONObject> = emptyList(),
        note: String? = null,
    ): String = JSONObject().apply {
        put("operation_id", operationId)
        put("status", status)
        put("source", source)
        consumedAt?.let { put("consumed_at", it) }
        if (correctedItems.isNotEmpty()) {
            put("corrected_items", jsonArray(correctedItems))
        }
        note?.takeIf { it.isNotBlank() }?.let { put("note", it) }
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

    private fun jsonArray(values: List<JSONObject>) =
        org.json.JSONArray().apply {
            values.forEach { put(it) }
        }
}
