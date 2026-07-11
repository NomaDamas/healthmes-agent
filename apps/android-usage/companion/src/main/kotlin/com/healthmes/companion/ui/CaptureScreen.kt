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
import com.healthmes.companion.R
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Capture shortcuts (issue #10): photo (camera or picker) / voice memo →
 * `POST /v1/media` → food-log or medical-record create with an EDITABLE
 * description — the same contracts the Telegram capture skill uses. The
 * health-context snapshot on medical records is attached server-side; the
 * app sends capture metadata only (local-first, no health data uploaded from
 * the client beyond the capture itself).
 *
 * No CameraX dependency on purpose: ACTION_IMAGE_CAPTURE + the photo picker
 * cover the "camera/photo" capture path without a camera permission, keeping
 * the app lean. Voice memos record on-device via MediaRecorder into
 * audio/mp4 (.m4a on the server's allowlist).
 */
private enum class CaptureKind { FOOD, MEDICATION, SYMPTOM }

/** A staged attachment (not yet uploaded). */
private data class Staged(
    val uri: Uri?,
    val file: File?,
    val contentType: String,
    val label: String,
    val sizeBytes: Long,
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
    val recorderHolder = remember { RecorderHolder() }

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

    // Stop a dangling recorder when leaving the screen.
    DisposableEffect(Unit) {
        onDispose { recorderHolder.cancel() }
    }

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

        // Kind selector
        Text(
            stringResource(R.string.capture_kind_label),
            style = MaterialTheme.typography.titleSmall,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            KindChip(CaptureKind.FOOD, kind, R.string.capture_kind_food) { kind = it }
            KindChip(CaptureKind.MEDICATION, kind, R.string.capture_kind_medication) { kind = it }
            KindChip(CaptureKind.SYMPTOM, kind, R.string.capture_kind_symptom) { kind = it }
        }

        // Capture sources
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(
                enabled = !busy && !isRecording,
                onClick = {
                    val result = runCatching {
                        val dir = File(context.cacheDir, "captures").apply { mkdirs() }
                        val file = File(dir, "photo-${System.currentTimeMillis()}.jpg")
                        val uri = FileProvider.getUriForFile(
                            context, "${context.packageName}.fileprovider", file
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
                                R.string.capture_photo_failed, it.message ?: "?"
                            )
                        },
                    )
                },
            ) { Text(stringResource(R.string.capture_take_photo)) }
            OutlinedButton(
                enabled = !busy && !isRecording,
                onClick = {
                    pickMedia.launch(
                        PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                    )
                },
            ) { Text(stringResource(R.string.capture_pick_photo)) }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
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
                            context, Manifest.permission.RECORD_AUDIO
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
        }
        if (isRecording) {
            Text(
                stringResource(R.string.capture_recording),
                color = MaterialTheme.colorScheme.primary,
                style = MaterialTheme.typography.bodyMedium,
            )
        }

        // Staged attachment
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
                            contentDescription = stringResource(R.string.capture_remove_attachment),
                        )
                    }
                }
            }
        }

        // Editable description (+ transcript for medical voice notes)
        OutlinedTextField(
            value = description,
            onValueChange = { description = it },
            label = { Text(stringResource(R.string.capture_description_hint)) },
            modifier = Modifier.fillMaxWidth(),
            minLines = 2,
        )
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
                    api == null -> message = context.getString(R.string.capture_error_not_paired)
                    description.isBlank() ->
                        message = context.getString(R.string.capture_error_description_required)

                    else -> {
                        busy = true
                        message = context.getString(R.string.capture_saving)
                        val request = SaveRequest(
                            kind = kind,
                            description = description.trim(),
                            transcript = transcript.trim(),
                            staged = staged,
                        )
                        scope.launch {
                            val outcome = withContext(Dispatchers.IO) {
                                save(context, api, request)
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
            Text(stringResource(if (busy) R.string.capture_saving else R.string.capture_save))
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
    onSelect: (String) -> Unit,
) {
    FilterChip(
        selected = selected == value.name,
        onClick = { onSelect(value.name) },
        label = { Text(stringResource(labelRes)) },
    )
}

/** MediaRecorder lifecycle kept out of composition. */
private class RecorderHolder {
    private var recorder: MediaRecorder? = null
    private var output: File? = null

    /** True when recording started. */
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

    /** The recorded file, or null when stop failed / nothing was recording. */
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
            // already released
        }
        recorder = null
        output?.delete()
        output = null
    }
}

private data class SaveRequest(
    val kind: String,
    val description: String,
    val transcript: String,
    val staged: Staged?,
)

private data class SaveOutcome(val success: Boolean, val message: String)

/** Upload (optional) then create — blocking; call on Dispatchers.IO. */
private fun save(context: Context, api: HealthmesApi, request: SaveRequest): SaveOutcome {
    // 1) media upload (skipped for text-only captures)
    var mediaPath: String? = null
    val attachment = request.staged
    if (attachment != null) {
        val bytes = try {
            when {
                attachment.file != null -> attachment.file.readBytes()
                attachment.uri != null ->
                    context.contentResolver.openInputStream(attachment.uri)?.use { it.readBytes() }
                else -> null
            }
        } catch (e: Exception) {
            return SaveOutcome(
                false, context.getString(R.string.capture_upload_failed, e.message ?: "?")
            )
        } ?: return SaveOutcome(
            false, context.getString(R.string.capture_upload_failed, attachment.label)
        )

        when (val response = api.postMultipart("/v1/media", attachment.contentType, bytes)) {
            is HealthmesApi.Response.NetworkError -> return SaveOutcome(
                false, context.getString(R.string.capture_upload_failed, response.reason)
            )

            is HealthmesApi.Response.Http -> {
                if (!response.isSuccess) {
                    val detail = ApiError.parseOrNull(response.body)?.message
                        ?: "HTTP ${response.code}"
                    return SaveOutcome(
                        false, context.getString(R.string.capture_upload_failed, detail)
                    )
                }
                mediaPath = runCatching { MediaUploadResult.parse(response.body).mediaPath }
                    .getOrNull()
                    ?: return SaveOutcome(
                        false,
                        context.getString(R.string.capture_upload_failed, "unparseable response"),
                    )
            }
        }
    }

    // 2) create the row (food vs medical — the Telegram-skill contracts)
    val isFood = request.kind == CaptureKind.FOOD.name
    val path = if (isFood) CaptureRequests.FOOD_LOGS_PATH else CaptureRequests.MEDICAL_RECORDS_PATH
    val body = if (isFood) {
        CaptureRequests.foodLogBody(request.description, mediaPath, source = CAPTURE_SOURCE)
    } else {
        CaptureRequests.medicalRecordBody(
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
    }
    return when (val response = api.postJson(path, body)) {
        is HealthmesApi.Response.NetworkError ->
            SaveOutcome(false, context.getString(R.string.capture_create_failed, response.reason))

        is HealthmesApi.Response.Http ->
            if (response.isSuccess) {
                SaveOutcome(
                    true,
                    context.getString(
                        if (isFood) R.string.capture_saved_food else R.string.capture_saved_medical
                    ),
                )
            } else {
                val detail = ApiError.parseOrNull(response.body)?.message
                    ?: "HTTP ${response.code}"
                SaveOutcome(false, context.getString(R.string.capture_create_failed, detail))
            }
    }
}

/** context.capture.source value — identifies this surface in stored records. */
private const val CAPTURE_SOURCE = "android-companion"
