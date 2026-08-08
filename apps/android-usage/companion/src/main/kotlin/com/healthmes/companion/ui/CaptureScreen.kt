package com.healthmes.companion.ui

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Clear
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import com.healthmes.api.ApiError
import com.healthmes.api.CaptureRequests
import com.healthmes.api.HealthmesApi
import com.healthmes.api.MediaUploadResult
import com.healthmes.api.NutritionCaptureSession
import com.healthmes.api.NutritionCaptureState
import com.healthmes.api.NutritionCaptureTransitions
import com.healthmes.api.NutritionInteractionResult
import com.healthmes.api.NutritionModality
import com.healthmes.api.NutritionObservationResult
import com.healthmes.api.NutritionOutcomeStatus
import com.healthmes.api.NutritionReviewStatus
import com.healthmes.companion.R
import java.io.File
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Owner-reviewed capture surface.
 *
 * Nutrition follows observation/interaction/outcome contracts and cannot
 * become consumed from analysis alone. Medication and symptom capture keep
 * the existing `/v1/medical-records` contract, including server-attached
 * health context.
 */
private enum class CaptureKind { FOOD, MEDICATION, SYMPTOM }

/** A local attachment that has not necessarily been uploaded yet. */
private data class Staged(
    val uri: Uri?,
    val file: File?,
    val contentType: String,
    val label: String,
    val sizeBytes: Long,
)

private data class NutritionDraft(
    val description: String,
    val staged: Staged?,
    val uploadedMediaPath: String? = null,
)

@Composable
fun CaptureScreen(services: AppServices, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var kind by rememberSaveable { mutableStateOf(CaptureKind.FOOD.name) }
    var description by rememberSaveable { mutableStateOf("") }
    var transcript by rememberSaveable { mutableStateOf("") }
    var staged by remember { mutableStateOf<Staged?>(null) }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    var isRecording by remember { mutableStateOf(false) }
    var nutritionState by remember {
        mutableStateOf<NutritionCaptureState>(NutritionCaptureState.Editing)
    }
    var nutritionDraft by remember { mutableStateOf<NutritionDraft?>(null) }
    var correctionMode by remember { mutableStateOf(false) }
    var correctedNames by remember { mutableStateOf(emptyList<String>()) }
    val recorderHolder = remember { RecorderHolder() }

    fun resetNutrition() {
        nutritionState = NutritionCaptureState.Editing
        nutritionDraft = null
        correctionMode = false
        correctedNames = emptyList()
        description = ""
        staged = null
        message = null
    }

    // -- capture launchers ---------------------------------------------------
    var cameraTarget by remember { mutableStateOf<Pair<Uri, File>?>(null) }
    val takePicture = rememberLauncherForActivityResult(
        ActivityResultContracts.TakePicture()
    ) { success ->
        val target = cameraTarget
        if (success && target != null) {
            staged = Staged(
                uri = target.first,
                file = target.second,
                contentType = "image/jpeg",
                label = target.second.name,
                sizeBytes = target.second.length(),
            )
        }
    }
    val pickMedia = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia()
    ) { uri ->
        if (uri != null) {
            val type = context.contentResolver.getType(uri) ?: "image/jpeg"
            val size = context.contentResolver.openInputStream(uri)?.use {
                it.available().toLong()
            } ?: 0L
            staged = Staged(uri, null, type, uri.lastPathSegment ?: "photo", size)
        }
    }
    val requestMic = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            isRecording = recorderHolder.start(context)
        } else {
            message = context.getString(R.string.capture_mic_permission)
        }
    }

    DisposableEffect(Unit) {
        onDispose { recorderHolder.cancel() }
    }

    val foodFlowActive =
        kind == CaptureKind.FOOD.name && nutritionState !is NutritionCaptureState.Editing
    val voiceFoodDraft =
        kind == CaptureKind.FOOD.name && staged?.contentType?.startsWith("audio/") == true

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            stringResource(R.string.capture_title),
            style = MaterialTheme.typography.titleLarge,
            modifier = Modifier.semantics { heading() },
        )
        Text(
            stringResource(R.string.capture_note),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Text(
            stringResource(R.string.capture_kind_label),
            style = MaterialTheme.typography.titleSmall,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            KindChip(
                CaptureKind.FOOD,
                kind,
                R.string.capture_kind_food,
                enabled = !busy && !foodFlowActive,
            ) { kind = it }
            KindChip(
                CaptureKind.MEDICATION,
                kind,
                R.string.capture_kind_medication,
                enabled = !busy && !foodFlowActive,
            ) { kind = it }
            KindChip(
                CaptureKind.SYMPTOM,
                kind,
                R.string.capture_kind_symptom,
                enabled = !busy && !foodFlowActive,
            ) { kind = it }
        }

        if (!foodFlowActive) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(
                    enabled = !busy && !isRecording,
                    onClick = {
                        val result = runCatching {
                            val dir = File(context.cacheDir, "captures").apply { mkdirs() }
                            val file = File(dir, "photo-${System.currentTimeMillis()}.jpg")
                            val uri = FileProvider.getUriForFile(
                                context,
                                "${context.packageName}.fileprovider",
                                file,
                            )
                            Pair(uri, file)
                        }
                        result.fold(
                            onSuccess = { target ->
                                cameraTarget = target
                                takePicture.launch(target.first)
                            },
                            onFailure = {
                                message = context.getString(
                                    R.string.capture_photo_failed,
                                    it.message ?: "?",
                                )
                            },
                        )
                    },
                ) { Text(stringResource(R.string.capture_take_photo)) }
                OutlinedButton(
                    enabled = !busy && !isRecording,
                    onClick = {
                        pickMedia.launch(
                            PickVisualMediaRequest(
                                ActivityResultContracts.PickVisualMedia.ImageOnly
                            )
                        )
                    },
                ) { Text(stringResource(R.string.capture_pick_photo)) }
            }
            OutlinedButton(
                enabled = !busy,
                onClick = {
                    if (isRecording) {
                        val recorded = recorderHolder.stop()
                        isRecording = false
                        if (recorded != null) {
                            staged = Staged(
                                uri = null,
                                file = recorded,
                                contentType = "audio/mp4",
                                label = recorded.name,
                                sizeBytes = recorded.length(),
                            )
                        }
                    } else if (
                        ContextCompat.checkSelfPermission(
                            context,
                            Manifest.permission.RECORD_AUDIO,
                        ) == PackageManager.PERMISSION_GRANTED
                    ) {
                        isRecording = recorderHolder.start(context)
                    } else {
                        requestMic.launch(Manifest.permission.RECORD_AUDIO)
                    }
                },
            ) {
                Text(
                    stringResource(
                        if (isRecording) R.string.capture_stop_recording
                        else R.string.capture_record_voice
                    )
                )
            }
            if (isRecording) {
                Text(
                    stringResource(R.string.capture_recording),
                    color = MaterialTheme.colorScheme.primary,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }

            staged?.let { attachment ->
                Card {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 8.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                    ) {
                        Text(
                            stringResource(
                                R.string.capture_attached,
                                "${attachment.label} (${attachment.contentType})",
                                attachment.sizeBytes / 1024,
                            ),
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.weight(1f),
                        )
                        IconButton(onClick = { staged = null }) {
                            Icon(
                                Icons.Filled.Clear,
                                contentDescription = stringResource(
                                    R.string.capture_remove_attachment
                                ),
                            )
                        }
                    }
                }
            }

            if (voiceFoodDraft) {
                Text(
                    stringResource(R.string.capture_voice_analysis_note),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                OutlinedTextField(
                    value = description,
                    onValueChange = { description = it },
                    label = {
                        Text(
                            stringResource(
                                if (kind == CaptureKind.FOOD.name) {
                                    R.string.capture_food_text_hint
                                } else {
                                    R.string.capture_description_hint
                                }
                            )
                        )
                    },
                    modifier = Modifier.fillMaxWidth(),
                    minLines = 2,
                )
            }
            if (kind != CaptureKind.FOOD.name) {
                OutlinedTextField(
                    value = transcript,
                    onValueChange = { transcript = it },
                    label = { Text(stringResource(R.string.capture_transcript_hint)) },
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            Button(
                enabled = !busy && !isRecording,
                onClick = {
                    val api = services.api()
                    when {
                        api == null -> {
                            message = context.getString(R.string.capture_error_not_paired)
                        }

                        kind == CaptureKind.FOOD.name -> {
                            val modality = nutritionModality(staged)
                            if (
                                modality == NutritionModality.TEXT &&
                                description.isBlank()
                            ) {
                                message = context.getString(
                                    R.string.capture_error_description_required
                                )
                            } else {
                                val session = NutritionCaptureSession.create(
                                    modality = modality,
                                    observedAt = currentTimestamp(),
                                    timezone = ZoneId.systemDefault().id,
                                    sourceText = when (modality) {
                                        NutritionModality.TEXT -> description.trim()
                                        NutritionModality.PHOTO ->
                                            description.trim().takeIf { it.isNotBlank() }

                                        NutritionModality.VOICE -> null
                                    },
                                )
                                val analyzing = NutritionCaptureState.Analyzing(session)
                                val draft = NutritionDraft(
                                    description = description.trim(),
                                    staged = staged,
                                )
                                nutritionDraft = draft
                                correctionMode = false
                                correctedNames = emptyList()
                                launchNutritionAnalysis(
                                    scope = scope,
                                    context = context,
                                    api = api,
                                    state = analyzing,
                                    draft = draft,
                                    onDraft = { nutritionDraft = it },
                                    onState = { nutritionState = it },
                                    onBusy = { busy = it },
                                    onMessage = { message = it },
                                )
                            }
                        }

                        description.isBlank() -> {
                            message = context.getString(
                                R.string.capture_error_description_required
                            )
                        }

                        else -> {
                            val request = MedicalSaveRequest(
                                kind = kind,
                                description = description.trim(),
                                transcript = transcript.trim(),
                                staged = staged,
                            )
                            busy = true
                            message = context.getString(R.string.capture_saving)
                            scope.launch {
                                val outcome = withContext(Dispatchers.IO) {
                                    saveMedical(context, api, request)
                                }
                                message = outcome.message
                                if (outcome.success) {
                                    description = ""
                                    transcript = ""
                                    staged = null
                                }
                                busy = false
                            }
                        }
                    }
                },
            ) {
                Text(
                    stringResource(
                        when {
                            busy && kind == CaptureKind.FOOD.name ->
                                R.string.capture_analyzing

                            busy -> R.string.capture_saving
                            kind == CaptureKind.FOOD.name -> R.string.capture_analyze
                            else -> R.string.capture_save
                        }
                    )
                )
            }
        }

        if (kind == CaptureKind.FOOD.name) {
            when (val state = nutritionState) {
                NutritionCaptureState.Editing -> Unit

                is NutritionCaptureState.Analyzing -> {
                    CaptureProgressCard(R.string.capture_analyzing)
                }

                is NutritionCaptureState.PhotoReview -> {
                    NutritionResultCard(
                        title = stringResource(R.string.capture_review_title),
                        status = state.observation.status,
                        confidence = state.observation.confidence,
                        warnings = state.observation.warnings,
                        items = state.observation.items,
                    )
                    if (correctionMode) {
                        correctedNames.forEachIndexed { index, value ->
                            OutlinedTextField(
                                value = value,
                                onValueChange = { updated ->
                                    correctedNames = correctedNames.mapIndexed {
                                            itemIndex, current ->
                                            if (itemIndex == index) updated else current
                                        }
                                },
                                label = {
                                    Text(
                                        stringResource(
                                            R.string.capture_correct_item,
                                            index + 1,
                                        )
                                    )
                                },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                            )
                        }
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Button(
                                enabled = correctedNames.isNotEmpty() &&
                                    correctedNames.all { it.isNotBlank() },
                                modifier = Modifier.weight(1f),
                                onClick = {
                                    val submitting =
                                        NutritionCaptureTransitions.beginPhotoReview(
                                            state,
                                            NutritionReviewStatus.CORRECTED,
                                            correctedNames,
                                        )
                                    correctionMode = false
                                    services.api()?.let { api ->
                                        launchPhotoReview(
                                            scope = scope,
                                            context = context,
                                            api = api,
                                            state = submitting,
                                            onState = { nutritionState = it },
                                            onBusy = { busy = it },
                                            onMessage = { message = it },
                                        )
                                    }
                                },
                            ) {
                                Text(stringResource(R.string.capture_save_correction))
                            }
                            OutlinedButton(
                                modifier = Modifier.weight(1f),
                                onClick = { correctionMode = false },
                            ) {
                                Text(stringResource(R.string.capture_back))
                            }
                        }
                    } else {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Button(
                                enabled = state.observation.canConfirm,
                                modifier = Modifier.weight(1f),
                                onClick = {
                                    val submitting =
                                        NutritionCaptureTransitions.beginPhotoReview(
                                            state,
                                            NutritionReviewStatus.CONFIRMED,
                                        )
                                    services.api()?.let { api ->
                                        launchPhotoReview(
                                            scope = scope,
                                            context = context,
                                            api = api,
                                            state = submitting,
                                            onState = { nutritionState = it },
                                            onBusy = { busy = it },
                                            onMessage = { message = it },
                                        )
                                    }
                                },
                            ) {
                                Text(stringResource(R.string.capture_looks_right))
                            }
                            OutlinedButton(
                                enabled = state.observation.canCorrect,
                                modifier = Modifier.weight(1f),
                                onClick = {
                                    correctedNames =
                                        state.observation.items.map { it.name }
                                    correctionMode = true
                                },
                            ) {
                                Text(stringResource(R.string.capture_correct))
                            }
                        }
                        OutlinedButton(
                            modifier = Modifier.fillMaxWidth(),
                            onClick = {
                                val submitting =
                                    NutritionCaptureTransitions.beginPhotoReview(
                                        state,
                                        NutritionReviewStatus.REJECTED,
                                    )
                                services.api()?.let { api ->
                                    launchPhotoReview(
                                        scope = scope,
                                        context = context,
                                        api = api,
                                        state = submitting,
                                        onState = { nutritionState = it },
                                        onBusy = { busy = it },
                                        onMessage = { message = it },
                                    )
                                }
                            },
                        ) {
                            Text(stringResource(R.string.capture_reject))
                        }
                    }
                }

                is NutritionCaptureState.SubmittingPhotoReview -> {
                    CaptureProgressCard(R.string.capture_review_saving)
                }

                is NutritionCaptureState.CreatingInteraction -> {
                    CaptureProgressCard(R.string.capture_creating_interaction)
                }

                is NutritionCaptureState.AwaitingOutcome -> {
                    NutritionResultCard(
                        title = stringResource(R.string.capture_outcome_title),
                        status = null,
                        confidence = null,
                        warnings = state.interaction.warnings,
                        items = state.interaction.items,
                    )
                    Text(
                        stringResource(R.string.capture_outcome_note),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        Button(
                            enabled = state.interaction.items.isNotEmpty(),
                            modifier = Modifier.weight(1f),
                            onClick = {
                                val submitting = NutritionCaptureTransitions.beginOutcome(
                                    state,
                                    NutritionOutcomeStatus.CONSUMED,
                                    consumedAt = currentTimestamp(),
                                )
                                services.api()?.let { api ->
                                    launchNutritionOutcome(
                                        scope = scope,
                                        context = context,
                                        api = api,
                                        state = submitting,
                                        onState = { nutritionState = it },
                                        onBusy = { busy = it },
                                        onMessage = { message = it },
                                    )
                                }
                            },
                        ) {
                            Text(stringResource(R.string.capture_consumed))
                        }
                        OutlinedButton(
                            modifier = Modifier.weight(1f),
                            onClick = {
                                val submitting = NutritionCaptureTransitions.beginOutcome(
                                    state,
                                    NutritionOutcomeStatus.NOT_CONSUMED,
                                )
                                services.api()?.let { api ->
                                    launchNutritionOutcome(
                                        scope = scope,
                                        context = context,
                                        api = api,
                                        state = submitting,
                                        onState = { nutritionState = it },
                                        onBusy = { busy = it },
                                        onMessage = { message = it },
                                    )
                                }
                            },
                        ) {
                            Text(stringResource(R.string.capture_not_consumed))
                        }
                    }
                    OutlinedButton(
                        modifier = Modifier.fillMaxWidth(),
                        onClick = {
                            val submitting = NutritionCaptureTransitions.beginOutcome(
                                state,
                                NutritionOutcomeStatus.CANCELLED,
                            )
                            services.api()?.let { api ->
                                launchNutritionOutcome(
                                    scope = scope,
                                    context = context,
                                    api = api,
                                    state = submitting,
                                    onState = { nutritionState = it },
                                    onBusy = { busy = it },
                                    onMessage = { message = it },
                                )
                            }
                        },
                    ) {
                        Text(stringResource(R.string.capture_cancel))
                    }
                }

                is NutritionCaptureState.SubmittingOutcome -> {
                    CaptureProgressCard(R.string.capture_submitting_outcome)
                }

                is NutritionCaptureState.Completed -> {
                    CaptureTerminalCard(
                        messageRes = when (state.status) {
                            NutritionOutcomeStatus.CONSUMED ->
                                R.string.capture_completed_consumed

                            NutritionOutcomeStatus.NOT_CONSUMED ->
                                R.string.capture_completed_not_consumed

                            NutritionOutcomeStatus.CANCELLED ->
                                R.string.capture_completed_cancelled
                        },
                        onReset = ::resetNutrition,
                    )
                }

                is NutritionCaptureState.Rejected -> {
                    CaptureTerminalCard(
                        messageRes = R.string.capture_photo_rejected,
                        onReset = ::resetNutrition,
                    )
                }

                is NutritionCaptureState.Failed -> {
                    Card {
                        Column(
                            modifier = Modifier.padding(16.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Text(
                                stringResource(
                                    R.string.capture_nutrition_failed,
                                    state.detail,
                                ),
                                color = MaterialTheme.colorScheme.error,
                            )
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(8.dp),
                            ) {
                                Button(
                                    modifier = Modifier.weight(1f),
                                    onClick = {
                                        val api = services.api() ?: return@Button
                                        when (val retry = state.retryState) {
                                            is NutritionCaptureState.Analyzing -> {
                                                nutritionDraft?.let { draft ->
                                                    launchNutritionAnalysis(
                                                        scope = scope,
                                                        context = context,
                                                        api = api,
                                                        state = retry,
                                                        draft = draft,
                                                        onDraft = {
                                                            nutritionDraft = it
                                                        },
                                                        onState = {
                                                            nutritionState = it
                                                        },
                                                        onBusy = { busy = it },
                                                        onMessage = {
                                                            message = it
                                                        },
                                                    )
                                                }
                                            }

                                            is NutritionCaptureState.SubmittingPhotoReview -> {
                                                launchPhotoReview(
                                                    scope = scope,
                                                    context = context,
                                                    api = api,
                                                    state = retry,
                                                    onState = {
                                                        nutritionState = it
                                                    },
                                                    onBusy = { busy = it },
                                                    onMessage = { message = it },
                                                )
                                            }

                                            is NutritionCaptureState.CreatingInteraction -> {
                                                launchPhotoInteraction(
                                                    scope = scope,
                                                    context = context,
                                                    api = api,
                                                    state = retry,
                                                    onState = {
                                                        nutritionState = it
                                                    },
                                                    onBusy = { busy = it },
                                                    onMessage = { message = it },
                                                )
                                            }

                                            is NutritionCaptureState.SubmittingOutcome -> {
                                                launchNutritionOutcome(
                                                    scope = scope,
                                                    context = context,
                                                    api = api,
                                                    state = retry,
                                                    onState = {
                                                        nutritionState = it
                                                    },
                                                    onBusy = { busy = it },
                                                    onMessage = { message = it },
                                                )
                                            }

                                            else -> resetNutrition()
                                        }
                                    },
                                ) {
                                    Text(stringResource(R.string.capture_retry))
                                }
                                OutlinedButton(
                                    modifier = Modifier.weight(1f),
                                    onClick = ::resetNutrition,
                                ) {
                                    Text(stringResource(R.string.capture_start_over))
                                }
                            }
                        }
                    }
                }
            }
        }

        message?.let {
            Text(it, style = MaterialTheme.typography.bodyMedium)
        }
        Spacer(modifier = Modifier.width(1.dp))
    }
}

@Composable
private fun KindChip(
    value: CaptureKind,
    selected: String,
    labelRes: Int,
    enabled: Boolean,
    onSelect: (String) -> Unit,
) {
    FilterChip(
        selected = selected == value.name,
        enabled = enabled,
        onClick = { onSelect(value.name) },
        label = { Text(stringResource(labelRes)) },
    )
}

@Composable
private fun CaptureProgressCard(messageRes: Int) {
    Card {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            CircularProgressIndicator()
            Text(
                stringResource(messageRes),
                style = MaterialTheme.typography.bodyMedium,
            )
        }
    }
}

@Composable
private fun NutritionResultCard(
    title: String,
    status: String?,
    confidence: String?,
    warnings: List<String>,
    items: List<com.healthmes.api.NutritionItemView>,
) {
    Card {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            if (status != null && confidence != null) {
                Text(
                    stringResource(
                        R.string.capture_review_meta,
                        status.replace('_', ' '),
                        confidence,
                    ),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (items.isEmpty()) {
                Text(
                    stringResource(R.string.capture_no_items),
                    color = MaterialTheme.colorScheme.error,
                )
            }
            items.forEach { item ->
                Text(item.name, style = MaterialTheme.typography.titleSmall)
                Text(
                    stringResource(
                        R.string.capture_serving_line,
                        item.serving.summary(),
                    ),
                    style = MaterialTheme.typography.bodySmall,
                )
                item.nutrientSummary().takeIf { it.isNotBlank() }?.let {
                    Text(
                        stringResource(R.string.capture_nutrients_line, it),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                item.warnings.forEach {
                    Text(
                        stringResource(R.string.capture_warning_line, it),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
            warnings.forEach {
                Text(
                    stringResource(R.string.capture_warning_line, it),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }
    }
}

@Composable
private fun CaptureTerminalCard(messageRes: Int, onReset: () -> Unit) {
    Card {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(stringResource(messageRes), style = MaterialTheme.typography.bodyMedium)
            Button(onClick = onReset) {
                Text(stringResource(R.string.capture_another))
            }
        }
    }
}

private fun nutritionModality(staged: Staged?): NutritionModality =
    when {
        staged?.contentType?.startsWith("image/") == true -> NutritionModality.PHOTO
        staged?.contentType?.startsWith("audio/") == true -> NutritionModality.VOICE
        else -> NutritionModality.TEXT
    }

private fun currentTimestamp(): String =
    OffsetDateTime.now(ZoneId.systemDefault()).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)

private fun launchNutritionAnalysis(
    scope: CoroutineScope,
    context: Context,
    api: HealthmesApi,
    state: NutritionCaptureState.Analyzing,
    draft: NutritionDraft,
    onDraft: (NutritionDraft) -> Unit,
    onState: (NutritionCaptureState) -> Unit,
    onBusy: (Boolean) -> Unit,
    onMessage: (String?) -> Unit,
) {
    onState(state)
    onBusy(true)
    onMessage(null)
    scope.launch {
        when (
            val attempt = withContext(Dispatchers.IO) {
                analyzeNutrition(context, api, state, draft)
            }
        ) {
            is NutritionAnalysisAttempt.Success -> {
                onDraft(attempt.draft)
                onState(attempt.state)
            }

            is NutritionAnalysisAttempt.Failure -> {
                onDraft(attempt.draft)
                onState(NutritionCaptureTransitions.failed(state, attempt.detail))
            }
        }
        onBusy(false)
    }
}

private fun launchPhotoReview(
    scope: CoroutineScope,
    context: Context,
    api: HealthmesApi,
    state: NutritionCaptureState.SubmittingPhotoReview,
    onState: (NutritionCaptureState) -> Unit,
    onBusy: (Boolean) -> Unit,
    onMessage: (String?) -> Unit,
) {
    onState(state)
    onBusy(true)
    onMessage(null)
    scope.launch {
        when (
            val review = withContext(Dispatchers.IO) {
                submitPhotoReview(api, state)
            }
        ) {
            is CaptureCall.Failure -> {
                onState(NutritionCaptureTransitions.failed(state, review.detail))
                onBusy(false)
            }

            is CaptureCall.Success -> {
                when (val next = NutritionCaptureTransitions.photoReviewStored(state)) {
                    is NutritionCaptureState.Rejected -> {
                        onState(next)
                        onBusy(false)
                    }

                    is NutritionCaptureState.CreatingInteraction -> {
                        onState(next)
                        when (
                            val created = withContext(Dispatchers.IO) {
                                createPhotoInteraction(api, next)
                            }
                        ) {
                            is CaptureCall.Success -> onState(created.value)
                            is CaptureCall.Failure -> onState(
                                NutritionCaptureTransitions.failed(next, created.detail)
                            )
                        }
                        onBusy(false)
                    }

                    else -> {
                        onState(
                            NutritionCaptureTransitions.failed(
                                state,
                                context.getString(R.string.capture_invalid_state),
                            )
                        )
                        onBusy(false)
                    }
                }
            }
        }
    }
}

private fun launchPhotoInteraction(
    scope: CoroutineScope,
    context: Context,
    api: HealthmesApi,
    state: NutritionCaptureState.CreatingInteraction,
    onState: (NutritionCaptureState) -> Unit,
    onBusy: (Boolean) -> Unit,
    onMessage: (String?) -> Unit,
) {
    onState(state)
    onBusy(true)
    onMessage(null)
    scope.launch {
        when (
            val created = withContext(Dispatchers.IO) {
                createPhotoInteraction(api, state)
            }
        ) {
            is CaptureCall.Success -> onState(created.value)
            is CaptureCall.Failure -> onState(
                NutritionCaptureTransitions.failed(state, created.detail)
            )
        }
        onBusy(false)
    }
}

private fun launchNutritionOutcome(
    scope: CoroutineScope,
    context: Context,
    api: HealthmesApi,
    state: NutritionCaptureState.SubmittingOutcome,
    onState: (NutritionCaptureState) -> Unit,
    onBusy: (Boolean) -> Unit,
    onMessage: (String?) -> Unit,
) {
    onState(state)
    onBusy(true)
    onMessage(null)
    scope.launch {
        when (
            val outcome = withContext(Dispatchers.IO) {
                submitNutritionOutcome(api, state)
            }
        ) {
            is CaptureCall.Success -> onState(outcome.value)
            is CaptureCall.Failure -> onState(
                NutritionCaptureTransitions.failed(state, outcome.detail)
            )
        }
        onBusy(false)
    }
}

private sealed interface NutritionAnalysisAttempt {
    data class Success(
        val draft: NutritionDraft,
        val state: NutritionCaptureState,
    ) : NutritionAnalysisAttempt

    data class Failure(
        val draft: NutritionDraft,
        val detail: String,
    ) : NutritionAnalysisAttempt
}

private fun analyzeNutrition(
    context: Context,
    api: HealthmesApi,
    state: NutritionCaptureState.Analyzing,
    draft: NutritionDraft,
): NutritionAnalysisAttempt {
    var updatedDraft = draft
    val mediaPath = if (state.session.modality == NutritionModality.TEXT) {
        null
    } else {
        draft.uploadedMediaPath ?: when (
            val uploaded = draft.staged?.let { uploadAttachment(context, api, it) }
                ?: CaptureCall.Failure("missing photo or voice attachment")
        ) {
            is CaptureCall.Success -> {
                updatedDraft = draft.copy(uploadedMediaPath = uploaded.value)
                uploaded.value
            }

            is CaptureCall.Failure -> {
                return NutritionAnalysisAttempt.Failure(updatedDraft, uploaded.detail)
            }
        }
    }
    val body = when (state.session.modality) {
        NutritionModality.PHOTO -> CaptureRequests.photoAnalyzeBody(
            mediaPath = requireNotNull(mediaPath),
            capturedAt = state.session.observedAt,
            timezone = state.session.timezone,
            source = CAPTURE_SOURCE,
        )

        NutritionModality.TEXT -> CaptureRequests.textAnalyzeBody(
            operationId = state.session.analyzeOperationId,
            intent = CaptureRequests.INTENT_LOG_CONSUMED,
            observedAt = state.session.observedAt,
            timezone = state.session.timezone,
            source = CAPTURE_SOURCE,
            sourceText = requireNotNull(state.session.sourceText),
        )

        NutritionModality.VOICE -> CaptureRequests.voiceAnalyzeBody(
            operationId = state.session.analyzeOperationId,
            intent = CaptureRequests.INTENT_LOG_CONSUMED,
            observedAt = state.session.observedAt,
            timezone = state.session.timezone,
            source = CAPTURE_SOURCE,
            mediaPath = requireNotNull(mediaPath),
        )
    }
    val path = if (state.session.modality == NutritionModality.PHOTO) {
        CaptureRequests.NUTRITION_OBSERVATIONS_ANALYZE_PATH
    } else {
        CaptureRequests.INTAKE_INTERACTIONS_ANALYZE_PATH
    }
    return when (val response = postJson(api, path, body)) {
        is CaptureCall.Failure ->
            NutritionAnalysisAttempt.Failure(updatedDraft, response.detail)

        is CaptureCall.Success -> {
            val parsed = runCatching {
                if (state.session.modality == NutritionModality.PHOTO) {
                    NutritionCaptureTransitions.photoAnalyzed(
                        state,
                        NutritionObservationResult.parse(response.value),
                    )
                } else {
                    NutritionCaptureTransitions.interactionAnalyzed(
                        state,
                        NutritionInteractionResult.parse(response.value),
                    )
                }
            }
            parsed.fold(
                onSuccess = { NutritionAnalysisAttempt.Success(updatedDraft, it) },
                onFailure = {
                    NutritionAnalysisAttempt.Failure(
                        updatedDraft,
                        "invalid analysis response: ${it.message ?: it.javaClass.simpleName}",
                    )
                },
            )
        }
    }
}

private fun submitPhotoReview(
    api: HealthmesApi,
    state: NutritionCaptureState.SubmittingPhotoReview,
): CaptureCall<Unit> {
    val correctedItems =
        if (state.status == NutritionReviewStatus.CORRECTED) {
            state.observation.correctedItems(state.correctedNames)
        } else {
            emptyList()
        }
    val body = CaptureRequests.photoReviewBody(
        operationId = state.session.reviewOperationId(state.status),
        status = state.status.wireValue,
        source = CAPTURE_SOURCE,
        correctedItems = correctedItems,
    )
    return when (
        val response = postJson(
            api,
            CaptureRequests.nutritionObservationReviewPath(
                state.observation.observationId
            ),
            body,
        )
    ) {
        is CaptureCall.Success -> CaptureCall.Success(Unit)
        is CaptureCall.Failure -> response
    }
}

private fun createPhotoInteraction(
    api: HealthmesApi,
    state: NutritionCaptureState.CreatingInteraction,
): CaptureCall<NutritionCaptureState.AwaitingOutcome> {
    val body = CaptureRequests.photoInteractionBody(
        operationId = state.session.interactionOperationId,
        intent = CaptureRequests.INTENT_LOG_CONSUMED,
        nutritionObservationId = state.observation.observationId,
        source = CAPTURE_SOURCE,
        sourceText = state.session.sourceText,
    )
    return when (
        val response = postJson(api, CaptureRequests.INTAKE_INTERACTIONS_PATH, body)
    ) {
        is CaptureCall.Failure -> response
        is CaptureCall.Success -> runCatching {
            NutritionCaptureTransitions.interactionCreated(
                state,
                NutritionInteractionResult.parse(response.value),
            )
        }.fold(
            onSuccess = { CaptureCall.Success(it) },
            onFailure = {
                CaptureCall.Failure(
                    "invalid interaction response: ${it.message ?: it.javaClass.simpleName}"
                )
            },
        )
    }
}

private fun submitNutritionOutcome(
    api: HealthmesApi,
    state: NutritionCaptureState.SubmittingOutcome,
): CaptureCall<NutritionCaptureState.Completed> {
    val body = CaptureRequests.intakeOutcomeBody(
        operationId = state.session.outcomeOperationId(state.status),
        status = state.status.wireValue,
        source = CAPTURE_SOURCE,
        consumedAt = state.consumedAt,
    )
    return when (
        val response = postJson(
            api,
            CaptureRequests.intakeOutcomePath(state.interaction.interactionId),
            body,
        )
    ) {
        is CaptureCall.Failure -> response
        is CaptureCall.Success -> runCatching {
            NutritionCaptureTransitions.outcomeStored(
                state,
                NutritionInteractionResult.parse(response.value),
            )
        }.fold(
            onSuccess = { CaptureCall.Success(it) },
            onFailure = {
                CaptureCall.Failure(
                    "invalid outcome response: ${it.message ?: it.javaClass.simpleName}"
                )
            },
        )
    }
}

private sealed interface CaptureCall<out T> {
    data class Success<T>(val value: T) : CaptureCall<T>
    data class Failure(val detail: String) : CaptureCall<Nothing>
}

private fun postJson(api: HealthmesApi, path: String, body: String): CaptureCall<String> =
    when (val response = api.postJson(path, body)) {
        is HealthmesApi.Response.NetworkError -> CaptureCall.Failure(response.reason)
        is HealthmesApi.Response.Http -> if (response.isSuccess) {
            CaptureCall.Success(response.body)
        } else {
            val detail = ApiError.parseOrNull(response.body)?.message
                ?: "HTTP ${response.code}"
            CaptureCall.Failure(detail)
        }
    }

private fun uploadAttachment(
    context: Context,
    api: HealthmesApi,
    attachment: Staged,
): CaptureCall<String> {
    val bytes = try {
        when {
            attachment.file != null -> attachment.file.readBytes()
            attachment.uri != null ->
                context.contentResolver.openInputStream(attachment.uri)?.use { it.readBytes() }

            else -> null
        }
    } catch (e: Exception) {
        return CaptureCall.Failure(e.message ?: e.javaClass.simpleName)
    } ?: return CaptureCall.Failure(attachment.label)

    return when (
        val response = api.postMultipart("/v1/media", attachment.contentType, bytes)
    ) {
        is HealthmesApi.Response.NetworkError -> CaptureCall.Failure(response.reason)
        is HealthmesApi.Response.Http -> {
            if (!response.isSuccess) {
                val detail = ApiError.parseOrNull(response.body)?.message
                    ?: "HTTP ${response.code}"
                CaptureCall.Failure(detail)
            } else {
                runCatching { MediaUploadResult.parse(response.body).mediaPath }
                    .fold(
                        onSuccess = { CaptureCall.Success(it) },
                        onFailure = { CaptureCall.Failure("unparseable upload response") },
                    )
            }
        }
    }
}

/** MediaRecorder lifecycle kept out of composition. */
private class RecorderHolder {
    private var recorder: MediaRecorder? = null
    private var output: File? = null

    fun start(context: Context): Boolean {
        cancel()
        return try {
            val dir = File(context.cacheDir, "captures").apply { mkdirs() }
            val file = File(dir, "voice-${System.currentTimeMillis()}.m4a")
            @Suppress("DEPRECATION")
            val mediaRecorder =
                if (Build.VERSION.SDK_INT >= 31) MediaRecorder(context) else MediaRecorder()
            mediaRecorder.setAudioSource(MediaRecorder.AudioSource.MIC)
            mediaRecorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            mediaRecorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            mediaRecorder.setOutputFile(file.absolutePath)
            mediaRecorder.prepare()
            mediaRecorder.start()
            recorder = mediaRecorder
            output = file
            true
        } catch (_: Exception) {
            cancel()
            false
        }
    }

    fun stop(): File? {
        val mediaRecorder = recorder ?: return null
        val file = output
        recorder = null
        output = null
        return try {
            mediaRecorder.stop()
            mediaRecorder.release()
            file
        } catch (_: Exception) {
            mediaRecorder.release()
            file?.delete()
            null
        }
    }

    fun cancel() {
        try {
            recorder?.release()
        } catch (_: Exception) {
            // Already released.
        }
        recorder = null
        output?.delete()
        output = null
    }
}

private data class MedicalSaveRequest(
    val kind: String,
    val description: String,
    val transcript: String,
    val staged: Staged?,
)

private data class SaveOutcome(val success: Boolean, val message: String)

/** Existing medical upload/create behavior, kept separate from nutrition. */
private fun saveMedical(
    context: Context,
    api: HealthmesApi,
    request: MedicalSaveRequest,
): SaveOutcome {
    val mediaPath = request.staged?.let { attachment ->
        when (val uploaded = uploadAttachment(context, api, attachment)) {
            is CaptureCall.Success -> uploaded.value
            is CaptureCall.Failure -> return SaveOutcome(
                false,
                context.getString(R.string.capture_upload_failed, uploaded.detail),
            )
        }
    }
    val body = CaptureRequests.medicalRecordBody(
        kind = if (request.kind == CaptureKind.MEDICATION.name) {
            CaptureRequests.KIND_MEDICATION
        } else {
            CaptureRequests.KIND_SYMPTOM
        },
        description = request.description,
        mediaPath = mediaPath,
        transcript = request.transcript.takeIf { it.isNotBlank() },
        captureSource = CAPTURE_SOURCE,
    )
    return when (
        val response = postJson(api, CaptureRequests.MEDICAL_RECORDS_PATH, body)
    ) {
        is CaptureCall.Success -> SaveOutcome(
            true,
            context.getString(R.string.capture_saved_medical),
        )

        is CaptureCall.Failure -> SaveOutcome(
            false,
            context.getString(R.string.capture_create_failed, response.detail),
        )
    }
}

private const val CAPTURE_SOURCE = "android-companion"
