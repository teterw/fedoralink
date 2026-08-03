package dev.fedoralink.android

import org.json.JSONObject
import java.io.InputStream
import java.util.UUID

/**
 * Wire protocol: newline-delimited JSON, mirroring daemon/fedoralink/protocol.py.
 *
 * Any change here has to land on both sides.
 */
object Protocol {
    /** Must match SERVICE_UUID in the Python daemon. */
    val SERVICE_UUID: UUID = UUID.fromString("3a94ef31-dc98-495b-bf8b-e4796714e90c")

    const val PROTOCOL_VERSION = 1

    const val IDENTITY = "fedoralink.identity"
    const val BATTERY = "fedoralink.battery"
    const val NOTIFICATION = "fedoralink.notification"
    const val NOTIFICATION_DISMISS = "fedoralink.notification.dismiss"
    const val NOTIFICATION_ACTION = "fedoralink.notification.action"
    const val CLIPBOARD = "fedoralink.clipboard"
    const val PING = "fedoralink.ping"

    private const val MAX_LINE_BYTES = 512 * 1024

    fun packet(type: String, body: JSONObject = JSONObject()): JSONObject =
        JSONObject().apply {
            put("id", System.currentTimeMillis())
            put("type", type)
            put("body", body)
        }

    fun serialize(packet: JSONObject): ByteArray =
        (packet.toString() + "\n").toByteArray(Charsets.UTF_8)

    /**
     * Reads one packet at a time off a blocking stream.
     *
     * Deliberately not a BufferedReader: we need a hard cap on line length
     * so a desynced peer can't make us allocate without bound.
     */
    class Reader(private val stream: InputStream) {
        private val buffer = StringBuilder()
        private val chunk = ByteArray(4096)
        private val pending = java.io.ByteArrayOutputStream()

        /** Blocks until a packet arrives. Returns null when the peer hangs up. */
        fun read(): JSONObject? {
            while (true) {
                val line = takeBufferedLine()
                if (line != null) {
                    val parsed = parse(line)
                    if (parsed != null) return parsed
                    // Malformed packet: framing is intact, so skip it and
                    // keep the link up rather than dropping the socket.
                    continue
                }

                val count = stream.read(chunk)
                if (count == -1) return null

                pending.write(chunk, 0, count)
                if (pending.size() > MAX_LINE_BYTES) {
                    pending.reset()
                    buffer.setLength(0)
                    continue
                }

                // Decode only at newline boundaries so a packet split
                // mid-multibyte-character doesn't produce mojibake.
                val bytes = pending.toByteArray()
                val lastNewline = bytes.lastIndexOf('\n'.code.toByte())
                if (lastNewline >= 0) {
                    buffer.append(String(bytes, 0, lastNewline + 1, Charsets.UTF_8))
                    pending.reset()
                    pending.write(bytes, lastNewline + 1, bytes.size - lastNewline - 1)
                }
            }
        }

        private fun takeBufferedLine(): String? {
            val newline = buffer.indexOf("\n")
            if (newline == -1) return null
            val line = buffer.substring(0, newline)
            buffer.delete(0, newline + 1)
            return line.ifBlank { null } ?: takeBufferedLine()
        }

        private fun parse(line: String): JSONObject? = try {
            val obj = JSONObject(line)
            if (obj.has("type")) {
                if (!obj.has("body")) obj.put("body", JSONObject())
                obj
            } else {
                null
            }
        } catch (_: Exception) {
            null
        }
    }
}
