package com.healthmes.usagecollector.net

internal sealed interface ChunkUploadResult {
    data object Success : ChunkUploadResult
    data object Cancelled : ChunkUploadResult
    data class TransientFailure(val reason: String) : ChunkUploadResult
    data class PermanentFailure(val reason: String) : ChunkUploadResult
    data class IsolatableFailure(val reason: String) : ChunkUploadResult
}

internal data class UploadTerminalFailure(
    val reason: String,
    val transient: Boolean,
)

internal data class UploadIsolationResult(
    val sent: Int,
    val discarded: List<UploadSample>,
    val failure: UploadTerminalFailure?,
    val cancelled: Boolean,
)

/**
 * Bisects deterministic sample-level failures until only the rejected sample
 * is discarded. A transient or batch-level permanent failure stops the pass;
 * callers keep their watermark unchanged so all source data remains retryable.
 */
internal fun uploadWithIsolation(
    samples: List<UploadSample>,
    maxChunkSize: Int,
    shouldContinue: () -> Boolean = { true },
    uploadChunk: (List<UploadSample>) -> ChunkUploadResult,
): UploadIsolationResult {
    require(maxChunkSize > 0)
    var sent = 0
    val discarded = mutableListOf<UploadSample>()
    var failure: UploadTerminalFailure? = null
    var cancelled = false

    fun upload(chunk: List<UploadSample>): Boolean {
        if (!shouldContinue()) {
            cancelled = true
            return false
        }
        return when (val result = uploadChunk(chunk)) {
            ChunkUploadResult.Success -> {
                sent += chunk.size
                true
            }

            ChunkUploadResult.Cancelled -> {
                cancelled = true
                false
            }

            is ChunkUploadResult.IsolatableFailure -> {
                if (chunk.size == 1) {
                    discarded += chunk.single()
                    true
                } else {
                    val midpoint = chunk.size / 2
                    upload(chunk.subList(0, midpoint)) &&
                        upload(chunk.subList(midpoint, chunk.size))
                }
            }

            is ChunkUploadResult.TransientFailure -> {
                failure = UploadTerminalFailure(
                    reason = result.reason,
                    transient = true,
                )
                false
            }

            is ChunkUploadResult.PermanentFailure -> {
                failure = UploadTerminalFailure(
                    reason = result.reason,
                    transient = false,
                )
                false
            }
        }
    }

    for (chunk in samples.chunked(maxChunkSize)) {
        if (!upload(chunk)) break
    }
    return UploadIsolationResult(
        sent = sent,
        discarded = discarded.toList(),
        failure = failure,
        cancelled = cancelled,
    )
}
