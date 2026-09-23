package dev.fedoralink.android

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.util.Base64
import android.util.Log
import java.io.OutputStream
import java.security.MessageDigest
import java.util.UUID
import org.json.JSONObject

private const val TAG = "FedoraLink"

/**
 * File transfer, phone side.
 *
 * Mirrors `daemon/fedoralink/plugins/files.py`, including the rules that
 * keep a failed transfer from leaving a corrupt file: chunks must arrive in
 * order, the declared size is a hard limit, and nothing is published to
 * MediaStore until the hash matches.
 *
 * Incoming files are accepted without a prompt — the PC is already an
 * authenticated, user-approved device, and the phone has no good moment to
 * interrupt for a dialog. Outgoing ones start from the share sheet, so the
 * user chose them by definition.
 */
object FileTransfer {

    private const val CHUNK_BYTES = 32 * 1024
    private const val MAX_FILE_BYTES = 8L * 1024 * 1024 * 1024

    private class Incoming(
        val id: String,
        val name: String,
        val size: Long,
        val uri: Uri?,
        val stream: OutputStream,
    ) {
        val digest: MessageDigest = MessageDigest.getInstance("SHA-256")
        var received: Long = 0
        var nextSeq: Int = 0
    }

    private var incoming: Incoming? = null
    private var outgoingId: String? = null
    private var cancelled = false

    // ------------------------------------------------------------- inbound

    fun onOffer(context: Context, body: JSONObject) {
        if (incoming != null) {
            cancel(body.optString("id"), "already receiving a file")
            return
        }

        val id = body.optString("id").ifBlank { null } ?: return
        val size = body.optLong("size", -1)
        if (size < 0 || size > MAX_FILE_BYTES) {
            cancel(id, "unusable size")
            return
        }

        val name = safeName(body.optString("name"))

        val opened = runCatching { openForWriting(context, name) }.getOrNull()
        if (opened == null) {
            Log.e(TAG, "cannot open a destination for $name")
            cancel(id, "cannot write the file on the phone")
            return
        }

        incoming = Incoming(id, name, size, opened.first, opened.second)
        LinkManager.send(Protocol.FILE_ACCEPT, JSONObject().put("id", id))
        Log.i(TAG, "receiving $name ($size bytes)")
    }

    fun onChunk(context: Context, body: JSONObject) {
        val transfer = incoming ?: return
        if (body.optString("id") != transfer.id) return

        val seq = body.optInt("seq", -1)
        if (seq != transfer.nextSeq) {
            // Accepting this silently is how you get a file of the right
            // length and the wrong contents.
            fail(context, "chunk out of order: got $seq, want ${transfer.nextSeq}")
            return
        }

        val data = runCatching {
            Base64.decode(body.optString("data"), Base64.DEFAULT)
        }.getOrNull()
        if (data == null) {
            fail(context, "chunk was not valid base64")
            return
        }

        if (transfer.received + data.size > transfer.size) {
            fail(context, "PC sent more data than it declared")
            return
        }

        val written = runCatching {
            transfer.stream.write(data)
            transfer.digest.update(data)
        }
        if (written.isFailure) {
            fail(context, "could not write to storage")
            return
        }

        transfer.received += data.size
        transfer.nextSeq++
    }

    fun onDone(context: Context, body: JSONObject) {
        val transfer = incoming ?: return
        if (body.optString("id") != transfer.id) return
        incoming = null

        runCatching { transfer.stream.close() }

        if (transfer.received != transfer.size) {
            Log.e(TAG, "transfer ended early: ${transfer.received}/${transfer.size}")
            discard(context, transfer)
            return
        }

        val expected = body.optString("sha256").lowercase()
        val actual = transfer.digest.digest()
            .joinToString("") { "%02x".format(it) }

        if (expected.isNotEmpty() && expected != actual) {
            Log.e(TAG, "hash mismatch on ${transfer.name}; discarding")
            discard(context, transfer)
            return
        }

        // Only now is it a real file: publishing before the hash matched
        // would leave a corrupt download in the user's gallery.
        publish(context, transfer)
        Log.i(TAG, "received ${transfer.name}")
    }

    fun onCancel(context: Context, body: JSONObject) {
        val transfer = incoming
        if (transfer != null && body.optString("id") == transfer.id) {
            incoming = null
            runCatching { transfer.stream.close() }
            discard(context, transfer)
            Log.i(TAG, "PC cancelled the transfer: ${body.optString("reason")}")
            return
        }

        if (body.optString("id") == outgoingId) {
            cancelled = true
            Log.i(TAG, "PC cancelled our upload: ${body.optString("reason")}")
        }
    }

    private fun fail(context: Context, reason: String) {
        Log.e(TAG, "incoming transfer failed: $reason")
        val transfer = incoming ?: return
        incoming = null
        runCatching { transfer.stream.close() }
        cancel(transfer.id, reason)
        discard(context, transfer)
    }

    private fun cancel(id: String?, reason: String) {
        if (id.isNullOrEmpty()) return
        LinkManager.send(
            Protocol.FILE_CANCEL,
            JSONObject().put("id", id).put("reason", reason),
        )
    }

    // ------------------------------------------------------------ outbound

    /**
     * Send a file the user picked from the share sheet. Blocking — call it
     * off the main thread.
     */
    fun send(context: Context, uri: Uri): Boolean {
        if (!LinkManager.isConnected()) return false

        val name = safeName(displayName(context, uri))
        val size = fileSize(context, uri)
        if (size < 0) {
            Log.w(TAG, "cannot determine the size of $uri")
            return false
        }

        val id = UUID.randomUUID().toString()
        outgoingId = id
        cancelled = false

        LinkManager.send(
            Protocol.FILE_OFFER,
            JSONObject().put("id", id).put("name", name).put("size", size),
        )

        // The desktop asks its user before accepting, so this waits for the
        // FILE_ACCEPT that LinkService routes back into acceptOutgoing().
        if (!awaitAccept(id)) {
            Log.i(TAG, "PC did not accept $name")
            outgoingId = null
            return false
        }

        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(CHUNK_BYTES)
        var seq = 0

        val streamed = runCatching {
            context.contentResolver.openInputStream(uri)!!.use { input ->
                while (true) {
                    if (cancelled) return@use
                    val count = input.read(buffer)
                    if (count <= 0) break

                    val slice = buffer.copyOfRange(0, count)
                    digest.update(slice)

                    val sent = LinkManager.send(
                        Protocol.FILE_CHUNK,
                        JSONObject()
                            .put("id", id)
                            .put("seq", seq++)
                            .put(
                                "data",
                                Base64.encodeToString(slice, Base64.NO_WRAP),
                            ),
                    )
                    if (!sent) {
                        Log.w(TAG, "link went away mid-transfer")
                        return@use
                    }
                }
            }
        }

        if (streamed.isFailure || cancelled) {
            cancel(id, if (cancelled) "cancelled" else "could not read the file")
            outgoingId = null
            return false
        }

        LinkManager.send(
            Protocol.FILE_DONE,
            JSONObject()
                .put("id", id)
                .put("sha256", digest.digest().joinToString("") { "%02x".format(it) }),
        )
        outgoingId = null
        Log.i(TAG, "sent $name")
        return true
    }

    @Volatile
    private var accepted: String? = null

    fun acceptOutgoing(id: String) {
        accepted = id
    }

    private fun awaitAccept(id: String, timeoutMs: Long = 120_000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (accepted == id) {
                accepted = null
                return true
            }
            if (cancelled) return false
            Thread.sleep(100)
        }
        return false
    }

    // ------------------------------------------------------------- storage

    /** Returns (uri or null, stream). Uri is null on the legacy path. */
    private fun openForWriting(context: Context, name: String): Pair<Uri?, OutputStream> {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                // Hidden from the gallery until the transfer completes.
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = context.contentResolver.insert(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI, values,
            ) ?: throw IllegalStateException("MediaStore refused the insert")
            val stream = context.contentResolver.openOutputStream(uri)
                ?: throw IllegalStateException("could not open $uri")
            return Pair(uri, stream)
        }

        // Below Q, app-specific external storage needs no permission. Not
        // the Downloads folder, but it is somewhere the file can go without
        // asking for WRITE_EXTERNAL_STORAGE.
        val dir = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
            ?: throw IllegalStateException("no external files dir")
        dir.mkdirs()
        val file = java.io.File(dir, name)
        return Pair(null, file.outputStream())
    }

    private fun publish(context: Context, transfer: Incoming) {
        val uri = transfer.uri ?: return
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return

        val values = ContentValues().apply {
            put(MediaStore.Downloads.IS_PENDING, 0)
        }
        runCatching { context.contentResolver.update(uri, values, null, null) }
    }

    private fun discard(context: Context, transfer: Incoming) {
        val uri = transfer.uri
        if (uri != null) {
            runCatching { context.contentResolver.delete(uri, null, null) }
        }
    }

    // -------------------------------------------------------------- helpers

    private fun displayName(context: Context, uri: Uri): String? =
        runCatching {
            context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (index >= 0 && cursor.moveToFirst()) cursor.getString(index) else null
            }
        }.getOrNull() ?: uri.lastPathSegment

    private fun fileSize(context: Context, uri: Uri): Long =
        runCatching {
            context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                val index = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (index >= 0 && cursor.moveToFirst()) cursor.getLong(index) else -1L
            } ?: -1L
        }.getOrDefault(-1L)

    /** Never trust a peer-supplied name as a path. */
    private fun safeName(name: String?, fallback: String = "received-file"): String {
        if (name.isNullOrBlank()) return fallback
        val base = name.replace('\\', '/').substringAfterLast('/')
            .replace(Regex("[^A-Za-z0-9._-]"), "_")
            .trimStart('.')
            .take(200)
            .trim('_')
        return base.ifBlank { fallback }
    }
}
