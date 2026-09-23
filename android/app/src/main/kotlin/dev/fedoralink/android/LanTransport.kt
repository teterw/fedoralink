package dev.fedoralink.android

import android.util.Log
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.Socket
import org.json.JSONObject

private const val TAG = "FedoraLink"

/**
 * The phone's end of the LAN link.
 *
 * The desktop hands over an address, a port and a nonce inside a
 * `fedoralink.upgrade` packet on the already-authenticated Bluetooth link,
 * so there is no discovery step and nothing to spoof. This connects,
 * proves it holds the device secret, checks that the PC can prove the same,
 * and then carries the ordinary packet stream inside AES-GCM records.
 *
 * Bluetooth stays connected underneath as the fallback: if this drops, the
 * link degrades rather than disappearing.
 */
object LanTransport {

    private const val CONNECT_TIMEOUT_MS = 3_000
    private const val HANDSHAKE_TIMEOUT_MS = 10_000
    private const val MAX_HANDSHAKE_BYTES = 8192

    private var socket: Socket? = null
    private var crypto: LanSession.RecordCrypto? = null
    private val writeLock = Any()

    @Volatile
    var isUp: Boolean = false
        private set

    /**
     * Take up an upgrade offer. Blocking — call it off the main thread.
     *
     * Returns true once the encrypted session is established; the caller
     * then drives [readLoop].
     */
    fun connect(
        hosts: List<String>,
        port: Int,
        desktopNonce: String,
        secret: String,
        deviceId: String,
    ): Boolean {
        close()

        val phoneNonce = Crypto.newNonce()
        val mac = Crypto.respond(secret, desktopNonce)
        if (mac == null) {
            Log.w(TAG, "cannot answer the LAN challenge")
            return false
        }

        // Several addresses when the PC has more than one interface; the
        // first reachable one wins.
        for (host in hosts) {
            val attempt = runCatching {
                openAndHandshake(host, port, desktopNonce, phoneNonce, mac, secret, deviceId)
            }
            if (attempt.getOrDefault(false)) return true
            Log.d(TAG, "LAN upgrade via $host failed: ${attempt.exceptionOrNull()?.message}")
        }

        Log.i(TAG, "no LAN route to the PC; staying on Bluetooth")
        return false
    }

    private fun openAndHandshake(
        host: String,
        port: Int,
        desktopNonce: String,
        phoneNonce: String,
        mac: String,
        secret: String,
        deviceId: String,
    ): Boolean {
        val sock = Socket()
        sock.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
        sock.soTimeout = HANDSHAKE_TIMEOUT_MS
        // Small packets that matter immediately — coalescing them would add
        // latency to every keystroke-sized notification.
        sock.tcpNoDelay = true

        val hello = JSONObject().apply {
            put("deviceId", deviceId)
            put("nonce", phoneNonce)
            put("mac", mac)
        }
        sock.getOutputStream().write(Protocol.serialize(hello))
        sock.getOutputStream().flush()

        val answer = readLine(sock.getInputStream())
        if (answer == null) {
            sock.close()
            return false
        }

        // The PC has to prove itself too — otherwise the phone would be
        // trusting an address it was simply handed.
        if (!Crypto.verify(secret, phoneNonce, answer.optString("mac", ""))) {
            Log.e(TAG, "PC failed the LAN challenge; refusing the upgrade")
            sock.close()
            return false
        }

        val (desktopToPhone, phoneToDesktop) = try {
            LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        } catch (e: Exception) {
            Log.e(TAG, "could not derive LAN keys", e)
            sock.close()
            return false
        }

        sock.soTimeout = 0
        synchronized(writeLock) {
            socket = sock
            crypto = LanSession.RecordCrypto(
                sendKey = phoneToDesktop,
                recvKey = desktopToPhone,
            )
            isUp = true
        }

        Log.i(TAG, "LAN link up with $host:$port")
        return true
    }

    /** Reads one newline-terminated JSON object, with a hard cap. */
    private fun readLine(stream: InputStream): JSONObject? {
        val buffer = StringBuilder()
        while (true) {
            val byte = stream.read()
            if (byte == -1) return null
            if (byte == '\n'.code) break
            buffer.append(byte.toChar())
            if (buffer.length > MAX_HANDSHAKE_BYTES) {
                Log.w(TAG, "LAN handshake line too long")
                return null
            }
        }
        return runCatching { JSONObject(buffer.toString()) }.getOrNull()
    }

    /** Blocking read loop. Returns when the link closes. */
    fun readLoop(onPacket: (JSONObject) -> Unit) {
        val sock = socket ?: return
        val session = crypto ?: return
        val records = LanSession.RecordReader(sock.getInputStream())

        try {
            while (true) {
                val record = records.read() ?: break
                for (packet in parsePackets(session.open(record))) {
                    onPacket(packet)
                }
            }
        } catch (e: Exception) {
            Log.i(TAG, "LAN link ended: ${e.message}")
        } finally {
            close()
        }
    }

    fun send(packet: JSONObject): Boolean {
        synchronized(writeLock) {
            val sock = socket ?: return false
            val session = crypto ?: return false
            return try {
                sock.getOutputStream().write(session.seal(Protocol.serialize(packet)))
                sock.getOutputStream().flush()
                true
            } catch (e: Exception) {
                Log.i(TAG, "LAN write failed: ${e.message}")
                false
            }
        }
    }

    fun close() {
        synchronized(writeLock) {
            isUp = false
            crypto = null
            socket?.let { runCatching { it.close() } }
            socket = null
        }
    }

    /**
     * Packets inside one decrypted record.
     *
     * Records arrive whole, so unlike the Bluetooth path there is no
     * reassembly to do — but a record may still hold more than one line, so
     * this splits rather than assuming exactly one.
     */
    private fun parsePackets(plaintext: ByteArray): List<JSONObject> =
        String(plaintext, Charsets.UTF_8)
            .split('\n')
            .filter { it.isNotBlank() }
            .mapNotNull { line ->
                runCatching { JSONObject(line) }
                    .getOrNull()
                    ?.takeIf { it.has("type") }
                    ?.also { if (!it.has("body")) it.put("body", JSONObject()) }
            }
}
