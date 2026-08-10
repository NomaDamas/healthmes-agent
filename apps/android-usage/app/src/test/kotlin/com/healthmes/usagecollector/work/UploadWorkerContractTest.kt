package com.healthmes.usagecollector.work

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class UploadWorkerContractTest {

    @Test
    fun `worker KDoc documents ordered snapshot semantics`() {
        val source = sequenceOf(
            File(
                "src/main/kotlin/com/healthmes/usagecollector/work/UploadWorker.kt"
            ),
            File(
                "app/src/main/kotlin/com/healthmes/usagecollector/work/UploadWorker.kt"
            ),
        ).firstOrNull(File::isFile)
        val text = checkNotNull(source) {
            "UploadWorker.kt was not found from ${File(".").absolutePath}"
        }.readText()
        val kdoc = text.substringBefore("class UploadWorker")

        assertTrue(kdoc.contains("snapshot_sequence"))
        assertTrue(kdoc.contains("strictly increasing"))
        assertTrue(kdoc.contains("rejects stale conflicts"))
        assertTrue(kdoc.contains("pairing revision"))
        assertTrue(kdoc.contains("incomplete source set"))
        assertFalse(kdoc.contains("last-write-wins"))
    }
}
