package com.healthmes.api

import java.util.UUID
import org.json.JSONArray
import org.json.JSONObject

enum class NutritionModality(val wireValue: String) {
    PHOTO("photo"),
    TEXT("text"),
    VOICE("voice"),
}

enum class NutritionReviewStatus(val wireValue: String) {
    CONFIRMED("confirmed"),
    CORRECTED("corrected"),
    REJECTED("rejected"),
}

enum class NutritionOutcomeStatus(val wireValue: String) {
    CONSUMED("consumed"),
    NOT_CONSUMED("not_consumed"),
    CANCELLED("cancelled"),
}

/**
 * Stable idempotency keys and capture metadata for one owner-reviewed
 * nutrition flow. Each possible review/outcome action has its own operation
 * ID so retrying the same action is stable without reusing an ID for a
 * different payload.
 */
data class NutritionCaptureSession(
    val modality: NutritionModality,
    val observedAt: String,
    val timezone: String,
    val sourceText: String?,
    val analyzeOperationId: String,
    val confirmedReviewOperationId: String,
    val correctedReviewOperationId: String,
    val rejectedReviewOperationId: String,
    val interactionOperationId: String,
    val consumedOutcomeOperationId: String,
    val notConsumedOutcomeOperationId: String,
    val cancelledOutcomeOperationId: String,
) {
    fun reviewOperationId(status: NutritionReviewStatus): String =
        when (status) {
            NutritionReviewStatus.CONFIRMED -> confirmedReviewOperationId
            NutritionReviewStatus.CORRECTED -> correctedReviewOperationId
            NutritionReviewStatus.REJECTED -> rejectedReviewOperationId
        }

    fun outcomeOperationId(status: NutritionOutcomeStatus): String =
        when (status) {
            NutritionOutcomeStatus.CONSUMED -> consumedOutcomeOperationId
            NutritionOutcomeStatus.NOT_CONSUMED -> notConsumedOutcomeOperationId
            NutritionOutcomeStatus.CANCELLED -> cancelledOutcomeOperationId
        }

    companion object {
        fun create(
            modality: NutritionModality,
            observedAt: String,
            timezone: String,
            sourceText: String?,
            idFactory: () -> String = { UUID.randomUUID().toString() },
        ): NutritionCaptureSession = NutritionCaptureSession(
            modality = modality,
            observedAt = observedAt,
            timezone = timezone,
            sourceText = sourceText,
            analyzeOperationId = idFactory(),
            confirmedReviewOperationId = idFactory(),
            correctedReviewOperationId = idFactory(),
            rejectedReviewOperationId = idFactory(),
            interactionOperationId = idFactory(),
            consumedOutcomeOperationId = idFactory(),
            notConsumedOutcomeOperationId = idFactory(),
            cancelledOutcomeOperationId = idFactory(),
        )
    }
}

data class NutritionEstimateView(
    val kind: String,
    val unit: String,
    val exact: Double?,
    val minimum: Double?,
    val maximum: Double?,
    val estimationBasis: String?,
) {
    fun summary(): String =
        when (kind) {
            "exact" -> exact?.let { "${formatNumber(it)} $unit" } ?: unknownSummary()
            "range" -> if (minimum != null && maximum != null) {
                "${formatNumber(minimum)}-${formatNumber(maximum)} $unit"
            } else {
                unknownSummary()
            }

            else -> unknownSummary()
        }

    fun toReviewJson(): JSONObject = JSONObject()
        .put("kind", kind)
        .put("unit", unit)
        .put("exact", exact ?: JSONObject.NULL)
        .put("minimum", minimum ?: JSONObject.NULL)
        .put("maximum", maximum ?: JSONObject.NULL)
        .put("estimation_basis", estimationBasis ?: JSONObject.NULL)

    private fun unknownSummary(): String =
        if (unit == "unknown") "unknown" else "unknown $unit"

    companion object {
        fun parse(value: JSONObject?): NutritionEstimateView {
            if (value == null) {
                return NutritionEstimateView(
                    kind = "unknown",
                    unit = "unknown",
                    exact = null,
                    minimum = null,
                    maximum = null,
                    estimationBasis = null,
                )
            }
            return NutritionEstimateView(
                kind = value.optString("kind", "unknown"),
                unit = value.optString("unit", "unknown"),
                exact = value.numberOrNull("exact"),
                minimum = value.numberOrNull("minimum"),
                maximum = value.numberOrNull("maximum"),
                estimationBasis = value.stringOrNull("estimation_basis"),
            )
        }

        private fun formatNumber(value: Double): String =
            if (value % 1.0 == 0.0) value.toLong().toString() else {
                "%.1f".format(java.util.Locale.US, value)
            }
    }
}

data class NutritionFactView(
    val nutrient: String,
    val amount: NutritionEstimateView,
    val confidence: String,
) {
    fun summary(): String = "${nutrient.replace('_', ' ')} ${amount.summary()}"

    fun toReviewJson(): JSONObject = JSONObject()
        .put("nutrient", nutrient)
        .put("amount", amount.toReviewJson())
        .put("confidence", confidence)

    companion object {
        fun parse(value: JSONObject): NutritionFactView = NutritionFactView(
            nutrient = value.optString("nutrient", "unknown"),
            amount = NutritionEstimateView.parse(value.optJSONObject("amount")),
            confidence = value.optString("confidence", "low"),
        )
    }
}

data class NutritionItemView(
    val name: String,
    val intakeType: String,
    val serving: NutritionEstimateView,
    val nutrients: List<NutritionFactView>,
    val confidence: String,
    val warnings: List<String>,
) {
    fun nutrientSummary(limit: Int = 2): String =
        nutrients.take(limit).joinToString(" · ") { it.summary() }

    fun toCorrectedReviewJson(itemIndex: Int, correctedName: String): JSONObject =
        JSONObject()
            .put("item_index", itemIndex)
            .put("name", correctedName.trim())
            .put("intake_type", intakeType)
            .put("serving", serving.toReviewJson())
            .put(
                "nutrients",
                JSONArray().apply {
                    nutrients.forEach { put(it.toReviewJson()) }
                },
            )
            .put("confidence", confidence)
            .put(
                "warnings",
                JSONArray().apply {
                    warnings.forEach { put(it) }
                },
            )
}

data class NutritionObservationResult(
    val observationId: String,
    val status: String,
    val confidence: String,
    val warnings: List<String>,
    val items: List<NutritionItemView>,
) {
    val canConfirm: Boolean get() = items.isNotEmpty()
    val canCorrect: Boolean
        get() = items.isNotEmpty() && items.all { it.nutrients.isNotEmpty() }

    /**
     * A corrected review is a complete replacement, not a name-only patch.
     * Serving and nutrient structures from the analysis are preserved while
     * owner-edited names become explicit reviewed values.
     */
    fun correctedItems(names: List<String>): List<JSONObject> {
        require(names.size == items.size) {
            "one corrected name is required for every analyzed item"
        }
        require(names.all { it.isNotBlank() }) {
            "corrected item names must not be blank"
        }
        return items.mapIndexed { index, item ->
            item.toCorrectedReviewJson(index, names[index])
        }
    }

    companion object {
        fun parse(json: String): NutritionObservationResult {
            val root = JSONObject(json)
            val items = root.optJSONArray("items").orEmptyObjects().map { item ->
                val confidence = item.optString("confidence", "low")
                var nutrients = item.optJSONArray("nutrients")
                    .orEmptyObjects()
                    .map(NutritionFactView::parse)
                if (nutrients.isEmpty()) {
                    item.optJSONObject("caffeine")?.let { caffeine ->
                        nutrients = listOf(
                            NutritionFactView(
                                nutrient = "caffeine",
                                amount = NutritionEstimateView.parse(caffeine),
                                confidence = confidence,
                            )
                        )
                    }
                }
                NutritionItemView(
                    name = item.optJSONArray("name_candidates")
                        .orEmptyStrings()
                        .firstOrNull()
                        ?: "Unidentified item",
                    intakeType = item.optString("intake_type", "unknown"),
                    serving = NutritionEstimateView.parse(item.optJSONObject("serving")),
                    nutrients = nutrients,
                    confidence = confidence,
                    warnings = item.optJSONArray("warnings").orEmptyStrings(),
                )
            }
            return NutritionObservationResult(
                observationId = root.getString("observation_id"),
                status = root.optString("status", "insufficient_data"),
                confidence = root.optString("confidence", "low"),
                warnings = root.optJSONArray("warnings").orEmptyStrings(),
                items = items,
            )
        }
    }
}

data class NutritionInteractionResult(
    val interactionId: String,
    val items: List<NutritionItemView>,
    val warnings: List<String>,
    val isConfirmedIntake: Boolean,
    val latestOutcomeStatus: String?,
) {
    companion object {
        fun parse(json: String): NutritionInteractionResult {
            val root = JSONObject(json)
            val sourceItems = if (
                root.has("resolved_items") && !root.isNull("resolved_items")
            ) {
                root.optJSONArray("resolved_items")
            } else {
                root.optJSONArray("items")
            }
            val items = sourceItems.orEmptyObjects().map { item ->
                NutritionItemView(
                    name = item.optString("name", "Unidentified item"),
                    intakeType = item.optString("intake_type", "unknown"),
                    serving = NutritionEstimateView.parse(item.optJSONObject("serving")),
                    nutrients = item.optJSONArray("nutrients")
                        .orEmptyObjects()
                        .map(NutritionFactView::parse),
                    confidence = item.optString("confidence", "low"),
                    warnings = item.optJSONArray("warnings").orEmptyStrings(),
                )
            }
            return NutritionInteractionResult(
                interactionId = root.getString("interaction_id"),
                items = items,
                warnings = root.optJSONArray("warnings").orEmptyStrings(),
                isConfirmedIntake = root.optBoolean("is_confirmed_intake", false),
                latestOutcomeStatus = root.optJSONObject("latest_outcome")
                    ?.stringOrNull("status"),
            )
        }
    }
}

sealed interface NutritionCaptureState {
    data object Editing : NutritionCaptureState

    data class Analyzing(
        val session: NutritionCaptureSession,
    ) : NutritionCaptureState

    data class PhotoReview(
        val session: NutritionCaptureSession,
        val observation: NutritionObservationResult,
    ) : NutritionCaptureState

    data class SubmittingPhotoReview(
        val session: NutritionCaptureSession,
        val observation: NutritionObservationResult,
        val status: NutritionReviewStatus,
        val correctedNames: List<String>,
    ) : NutritionCaptureState

    data class CreatingInteraction(
        val session: NutritionCaptureSession,
        val observation: NutritionObservationResult,
        val status: NutritionReviewStatus,
    ) : NutritionCaptureState

    data class AwaitingOutcome(
        val session: NutritionCaptureSession,
        val interaction: NutritionInteractionResult,
    ) : NutritionCaptureState

    data class SubmittingOutcome(
        val session: NutritionCaptureSession,
        val interaction: NutritionInteractionResult,
        val status: NutritionOutcomeStatus,
        val consumedAt: String?,
    ) : NutritionCaptureState

    data class Completed(
        val status: NutritionOutcomeStatus,
        val interaction: NutritionInteractionResult,
    ) : NutritionCaptureState

    data class Rejected(
        val observation: NutritionObservationResult,
    ) : NutritionCaptureState

    data class Failed(
        val retryState: NutritionCaptureState,
        val detail: String,
    ) : NutritionCaptureState
}

/**
 * Pure transition gate used by Compose and JVM tests. Analysis and photo
 * review can only reach review/interaction states; only a successful,
 * explicit outcome submission can reach [NutritionCaptureState.Completed].
 */
object NutritionCaptureTransitions {
    fun photoAnalyzed(
        state: NutritionCaptureState.Analyzing,
        observation: NutritionObservationResult,
    ): NutritionCaptureState.PhotoReview {
        require(state.session.modality == NutritionModality.PHOTO)
        return NutritionCaptureState.PhotoReview(state.session, observation)
    }

    fun interactionAnalyzed(
        state: NutritionCaptureState.Analyzing,
        interaction: NutritionInteractionResult,
    ): NutritionCaptureState.AwaitingOutcome {
        require(state.session.modality != NutritionModality.PHOTO)
        require(!interaction.isConfirmedIntake) {
            "analysis response must not auto-confirm intake"
        }
        return NutritionCaptureState.AwaitingOutcome(state.session, interaction)
    }

    fun beginPhotoReview(
        state: NutritionCaptureState.PhotoReview,
        status: NutritionReviewStatus,
        correctedNames: List<String> = emptyList(),
    ): NutritionCaptureState.SubmittingPhotoReview {
        if (status == NutritionReviewStatus.CORRECTED) {
            state.observation.correctedItems(correctedNames)
        } else {
            require(correctedNames.isEmpty())
        }
        return NutritionCaptureState.SubmittingPhotoReview(
            session = state.session,
            observation = state.observation,
            status = status,
            correctedNames = correctedNames,
        )
    }

    fun photoReviewStored(
        state: NutritionCaptureState.SubmittingPhotoReview,
    ): NutritionCaptureState =
        if (state.status == NutritionReviewStatus.REJECTED) {
            NutritionCaptureState.Rejected(state.observation)
        } else {
            NutritionCaptureState.CreatingInteraction(
                session = state.session,
                observation = state.observation,
                status = state.status,
            )
        }

    fun interactionCreated(
        state: NutritionCaptureState.CreatingInteraction,
        interaction: NutritionInteractionResult,
    ): NutritionCaptureState.AwaitingOutcome {
        require(!interaction.isConfirmedIntake) {
            "interaction creation must not auto-confirm intake"
        }
        return NutritionCaptureState.AwaitingOutcome(state.session, interaction)
    }

    fun beginOutcome(
        state: NutritionCaptureState.AwaitingOutcome,
        status: NutritionOutcomeStatus,
        consumedAt: String? = null,
    ): NutritionCaptureState.SubmittingOutcome {
        require(
            (status == NutritionOutcomeStatus.CONSUMED && !consumedAt.isNullOrBlank()) ||
                (status != NutritionOutcomeStatus.CONSUMED && consumedAt == null)
        ) {
            "consumed_at is required only for a consumed outcome"
        }
        return NutritionCaptureState.SubmittingOutcome(
            session = state.session,
            interaction = state.interaction,
            status = status,
            consumedAt = consumedAt,
        )
    }

    fun outcomeStored(
        state: NutritionCaptureState.SubmittingOutcome,
        interaction: NutritionInteractionResult,
    ): NutritionCaptureState.Completed {
        require(interaction.latestOutcomeStatus == state.status.wireValue) {
            "outcome response does not match the explicit owner action"
        }
        require(
            interaction.isConfirmedIntake ==
                (state.status == NutritionOutcomeStatus.CONSUMED)
        ) {
            "confirmed-intake state does not match the explicit owner action"
        }
        return NutritionCaptureState.Completed(state.status, interaction)
    }

    fun failed(
        retryState: NutritionCaptureState,
        detail: String,
    ): NutritionCaptureState.Failed {
        require(retryState !is NutritionCaptureState.Editing)
        require(retryState !is NutritionCaptureState.Completed)
        require(retryState !is NutritionCaptureState.Rejected)
        require(retryState !is NutritionCaptureState.Failed)
        return NutritionCaptureState.Failed(retryState, detail)
    }
}

val NutritionCaptureState.isConfirmedConsumption: Boolean
    get() = this is NutritionCaptureState.Completed &&
        status == NutritionOutcomeStatus.CONSUMED

private fun JSONObject.numberOrNull(key: String): Double? =
    if (!has(key) || isNull(key)) null else optDouble(key)

private fun JSONArray?.orEmptyObjects(): List<JSONObject> {
    if (this == null) return emptyList()
    return buildList {
        for (index in 0 until length()) {
            optJSONObject(index)?.let { add(it) }
        }
    }
}

private fun JSONArray?.orEmptyStrings(): List<String> {
    if (this == null) return emptyList()
    return buildList {
        for (index in 0 until length()) {
            optString(index).takeIf { it.isNotBlank() }?.let { add(it) }
        }
    }
}
