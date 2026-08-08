package com.healthmes.companion

import com.healthmes.api.NutritionCaptureSession
import com.healthmes.api.NutritionCaptureState
import com.healthmes.api.NutritionCaptureTransitions
import com.healthmes.api.NutritionInteractionResult
import com.healthmes.api.NutritionModality
import com.healthmes.api.NutritionObservationResult
import com.healthmes.api.NutritionOutcomeStatus
import com.healthmes.api.NutritionReviewStatus
import com.healthmes.api.isConfirmedConsumption
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class NutritionCaptureStateTest {

    @Test
    fun `observation parser keeps complete estimates for corrected review`() {
        val observation = NutritionObservationResult.parse(OBSERVATION_JSON)

        assertEquals("observation-123", observation.observationId)
        assertEquals("bottled coffee", observation.items.single().name)
        assertEquals("355 ml", observation.items.single().serving.summary())
        assertEquals("caffeine 180 mg", observation.items.single().nutrientSummary())

        val corrected = observation.correctedItems(listOf("small bottled latte")).single()
        assertEquals("small bottled latte", corrected.getString("name"))
        assertEquals(
            "visible_label",
            corrected.getJSONObject("serving").getString("estimation_basis"),
        )
        assertEquals(
            180.0,
            corrected.getJSONArray("nutrients")
                .getJSONObject(0)
                .getJSONObject("amount")
                .getDouble("exact"),
            0.0,
        )
    }

    @Test
    fun `legacy observation caffeine estimate becomes reviewable nutrient`() {
        val root = JSONObject(OBSERVATION_JSON)
        val item = root.getJSONArray("items").getJSONObject(0)
        val caffeine = item.getJSONArray("nutrients")
            .getJSONObject(0)
            .getJSONObject("amount")
        item.remove("nutrients")
        item.put("caffeine", caffeine)
        val observation = NutritionObservationResult.parse(root.toString())

        assertEquals("caffeine", observation.items.single().nutrients.single().nutrient)
        assertEquals("180 mg", observation.items.single().nutrients.single().amount.summary())
        assertEquals(
            1,
            observation.correctedItems(listOf("coffee"))
                .single()
                .getJSONArray("nutrients")
                .length(),
        )
    }

    @Test
    fun `interaction parser prefers resolved items and remains unconfirmed`() {
        val interaction = NutritionInteractionResult.parse(INTERACTION_JSON)

        assertEquals("interaction-123", interaction.interactionId)
        assertEquals("owner corrected latte", interaction.items.single().name)
        assertFalse(interaction.isConfirmedIntake)
        assertEquals(null, interaction.latestOutcomeStatus)
    }

    @Test
    fun `photo analysis and confirmed review never imply consumed`() {
        val analyzing = NutritionCaptureState.Analyzing(session(NutritionModality.PHOTO))
        val review = NutritionCaptureTransitions.photoAnalyzed(
            analyzing,
            NutritionObservationResult.parse(OBSERVATION_JSON),
        )
        val submitting = NutritionCaptureTransitions.beginPhotoReview(
            review,
            NutritionReviewStatus.CONFIRMED,
        )
        val creating = NutritionCaptureTransitions.photoReviewStored(submitting)

        assertTrue(creating is NutritionCaptureState.CreatingInteraction)
        assertFalse(review.isConfirmedConsumption)
        assertFalse(submitting.isConfirmedConsumption)
        assertFalse(creating.isConfirmedConsumption)
    }

    @Test
    fun `rejected photo review never advances to interaction`() {
        val review = NutritionCaptureTransitions.photoAnalyzed(
            NutritionCaptureState.Analyzing(session(NutritionModality.PHOTO)),
            NutritionObservationResult.parse(OBSERVATION_JSON),
        )
        val rejected = NutritionCaptureTransitions.photoReviewStored(
            NutritionCaptureTransitions.beginPhotoReview(
                review,
                NutritionReviewStatus.REJECTED,
            )
        )

        assertTrue(rejected is NutritionCaptureState.Rejected)
        assertFalse(rejected.isConfirmedConsumption)
    }

    @Test
    fun `text analysis waits for explicit owner outcome`() {
        val analyzed = NutritionCaptureTransitions.interactionAnalyzed(
            NutritionCaptureState.Analyzing(session(NutritionModality.TEXT)),
            NutritionInteractionResult.parse(INTERACTION_JSON),
        )

        assertTrue(analyzed is NutritionCaptureState.AwaitingOutcome)
        assertFalse(analyzed.isConfirmedConsumption)
    }

    @Test
    fun `analysis rejects a response that claims confirmed intake`() {
        val invalid = INTERACTION_JSON.replace(
            """"is_confirmed_intake": false""",
            """"is_confirmed_intake": true""",
        )

        assertThrows(IllegalArgumentException::class.java) {
            NutritionCaptureTransitions.interactionAnalyzed(
                NutritionCaptureState.Analyzing(session(NutritionModality.TEXT)),
                NutritionInteractionResult.parse(invalid),
            )
        }
    }

    @Test
    fun `only stored consumed outcome reaches confirmed consumption`() {
        val awaiting = NutritionCaptureTransitions.interactionAnalyzed(
            NutritionCaptureState.Analyzing(session(NutritionModality.TEXT)),
            NutritionInteractionResult.parse(INTERACTION_JSON),
        )
        val submitting = NutritionCaptureTransitions.beginOutcome(
            awaiting,
            NutritionOutcomeStatus.CONSUMED,
            consumedAt = "2026-08-08T12:45:00+09:00",
        )
        val completed = NutritionCaptureTransitions.outcomeStored(
            submitting,
            NutritionInteractionResult.parse(CONSUMED_INTERACTION_JSON),
        )

        assertFalse(awaiting.isConfirmedConsumption)
        assertFalse(submitting.isConfirmedConsumption)
        assertEquals(
            "2026-08-08T12:45:00+09:00",
            submitting.consumedAt,
        )
        assertTrue(completed.isConfirmedConsumption)
    }

    @Test
    fun `not consumed and cancelled outcomes never become confirmed intake`() {
        NutritionOutcomeStatus.entries
            .filterNot { it == NutritionOutcomeStatus.CONSUMED }
            .forEach { status ->
                val awaiting = NutritionCaptureTransitions.interactionAnalyzed(
                    NutritionCaptureState.Analyzing(session(NutritionModality.VOICE)),
                    NutritionInteractionResult.parse(INTERACTION_JSON),
                )
                val submitting = NutritionCaptureTransitions.beginOutcome(awaiting, status)
                val response = INTERACTION_JSON
                    .replace(
                        """"latest_outcome": null""",
                        """"latest_outcome": {"status": "${status.wireValue}"}""",
                    )
                val completed = NutritionCaptureTransitions.outcomeStored(
                    submitting,
                    NutritionInteractionResult.parse(response),
                )

                assertFalse(completed.isConfirmedConsumption)
            }
    }

    @Test
    fun `operation ids remain stable for retries and distinct between actions`() {
        val ids = ArrayDeque(
            listOf(
                "analyze",
                "review-confirm",
                "review-correct",
                "review-reject",
                "interaction",
                "outcome-consumed",
                "outcome-not-consumed",
                "outcome-cancelled",
            )
        )
        val session = NutritionCaptureSession.create(
            modality = NutritionModality.PHOTO,
            observedAt = "2026-08-08T12:40:00+09:00",
            timezone = "Asia/Seoul",
            sourceText = null,
            idFactory = { ids.removeFirst() },
        )

        assertEquals(
            session.reviewOperationId(NutritionReviewStatus.CONFIRMED),
            session.reviewOperationId(NutritionReviewStatus.CONFIRMED),
        )
        assertEquals(
            session.outcomeOperationId(NutritionOutcomeStatus.CONSUMED),
            session.outcomeOperationId(NutritionOutcomeStatus.CONSUMED),
        )
        assertTrue(
            session.reviewOperationId(NutritionReviewStatus.CONFIRMED) !=
                session.reviewOperationId(NutritionReviewStatus.REJECTED)
        )
        assertTrue(
            session.outcomeOperationId(NutritionOutcomeStatus.CONSUMED) !=
                session.outcomeOperationId(NutritionOutcomeStatus.NOT_CONSUMED)
        )
    }

    private fun session(modality: NutritionModality): NutritionCaptureSession =
        NutritionCaptureSession.create(
            modality = modality,
            observedAt = "2026-08-08T12:40:00+09:00",
            timezone = "Asia/Seoul",
            sourceText = if (modality == NutritionModality.TEXT) "Bibimbap" else null,
        )

    private companion object {
        val OBSERVATION_JSON = """
            {
              "observation_id": "observation-123",
              "status": "usable",
              "confidence": "high",
              "warnings": [],
              "items": [{
                "intake_type": "beverage",
                "name_candidates": ["bottled coffee"],
                "serving": {
                  "kind": "exact",
                  "unit": "ml",
                  "exact": 355,
                  "minimum": null,
                  "maximum": null,
                  "estimation_basis": "visible_label"
                },
                "nutrients": [{
                  "nutrient": "caffeine",
                  "amount": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 180,
                    "minimum": null,
                    "maximum": null,
                    "estimation_basis": "visible_label"
                  },
                  "confidence": "high"
                }],
                "confidence": "high",
                "warnings": []
              }]
            }
        """.trimIndent()

        val INTERACTION_JSON = """
            {
              "interaction_id": "interaction-123",
              "items": [{
                "name": "raw latte",
                "intake_type": "beverage",
                "serving": {"kind": "unknown", "unit": "ml"},
                "nutrients": [],
                "confidence": "low",
                "warnings": []
              }],
              "resolved_items": [{
                "name": "owner corrected latte",
                "intake_type": "beverage",
                "serving": {
                  "kind": "exact",
                  "unit": "ml",
                  "exact": 250,
                  "minimum": null,
                  "maximum": null
                },
                "nutrients": [{
                  "nutrient": "energy",
                  "amount": {
                    "kind": "exact",
                    "unit": "kcal",
                    "exact": 80,
                    "minimum": null,
                    "maximum": null
                  },
                  "confidence": "high",
                  "origin": "user"
                }],
                "confidence": "high",
                "warnings": []
              }],
              "warnings": [],
              "is_confirmed_intake": false,
              "latest_outcome": null
            }
        """.trimIndent()

        val CONSUMED_INTERACTION_JSON = INTERACTION_JSON
            .replace(
                """"is_confirmed_intake": false""",
                """"is_confirmed_intake": true""",
            )
            .replace(
                """"latest_outcome": null""",
                """"latest_outcome": {"status": "consumed"}""",
            )
    }
}
