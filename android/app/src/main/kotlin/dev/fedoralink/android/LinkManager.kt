package dev.fedoralink.android

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothServerSocket
import android.bluetooth.BluetoothSocket
import android.content.Context
import android.os.Build
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.io.OutputStream
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.coroutineContext

private const val TAG = "FedoraLink"

/**
 * Owns the single RFCOMM link to the PC.
 *
 * Both sides can initiate: we run an accept loop so the desktop can call
 * ConnectProfile, and a connect loop so the phone recovers on its own
 * after moving back into range. Whichever lands first wins; the loser is
 * closed by [adopt].
 */
object LinkManager {

    enum class State { STOPPED, WAITING, CONNECTED }

    private val _state = MutableStateFlow(State.STOPPED)
    val state: StateFlow<State> = _state.asStateFlow()

    private val _peerName = MutableStateFlow<String?>(null)
    val peerName: StateFlow<String?> = _peerName.asStateFlow()

    /** True once the PC has proved it holds this phone's secret. */
    private val _authenticated = MutableStateFlow(false)
    val authenticated: StateFlow<Boolean> = _authenticated.asStateFlow()

    /** True while packets are travelling over the LAN rather than Bluetooth. */
    private val _onLan = MutableStateFlow(false)
    val onLan: StateFlow<Boolean> = _onLan.asStateFlow()

    /** Set while enrolling, so the UI can show the code to compare. */
    private val _pairingCode = MutableStateFlow<String?>(null)
    val pairingCode: StateFlow<String?> = _pairingCode.asStateFlow()

    // Per-connection auth state. Cleared by resetAuth() on every close.
    private var peerId: String? = null
    private var ourNonce: String? = null

    private var scope: CoroutineScope? = null
    private var jobs = mutableListOf<Job>()
    private val session = AtomicReference<Session?>(null)
    private val adoptLock = Mutex()

    private lateinit var appContext: Context

    /** Packet handlers keyed by packet type. */
    private val handlers = mutableMapOf<String, MutableList<(JSONObject) -> Unit>>()

    fun on(type: String, handler: (JSONObject) -> Unit) {
        synchronized(handlers) {
            handlers.getOrPut(type) { mutableListOf() }.add(handler)
        }
    }

    fun start(context: Context) {
        if (scope != null) return
        appContext = context.applicationContext

        val newScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = newScope
        _state.value = State.WAITING

        jobs += newScope.launch { acceptLoop() }
        jobs += newScope.launch { connectLoop() }
    }

    fun stop() {
        val current = scope ?: return
        scope = null

        // Close the socket before cancelling: accept() and read() are
        // blocking calls that coroutine cancellation alone won't interrupt.
        session.getAndSet(null)?.close()
        current.cancel()
        jobs.clear()

        _state.value = State.STOPPED
        _peerName.value = null
    }

    /**
     * Whether the link is usable — connected *and* authenticated.
     *
     * Every caller means "can I send something?", and before the handshake
     * completes the answer is no: send() would refuse it. Folding auth in
     * here keeps the tile, the toasts and the relays all honest instead of
     * each having to remember to check twice.
     */
    fun isConnected(): Boolean = session.get() != null && _authenticated.value

    fun send(type: String, body: JSONObject = JSONObject()): Boolean {
        if (!_authenticated.value && type != Protocol.IDENTITY && type != Protocol.AUTH) {
            // A notification or clipboard update must not reach a PC that
            // hasn't proved who it is.
            Log.d(TAG, "refusing to send $type before authentication")
            return false
        }
        val packet = Protocol.packet(type, body)
        // Roughly two orders of magnitude faster when it's up. If the write
        // fails the link is already gone; fall through to Bluetooth rather
        // than dropping the packet.
        if (LanTransport.isUp && LanTransport.send(packet)) return true

        val active = session.get() ?: return false
        return active.send(packet)
    }

    /** Bypasses the gate, for the handshake packets that open it. */
    private fun sendUnauthenticated(type: String, body: JSONObject): Boolean {
        val active = session.get() ?: return false
        return active.send(Protocol.packet(type, body))
    }

    private fun sendAuth(stage: String, body: JSONObject = JSONObject()): Boolean =
        sendUnauthenticated(Protocol.AUTH, body.put("stage", stage))

    // ------------------------------------------------------------ inbound

    private fun dispatch(packet: JSONObject) {
        val type = packet.optString("type")
        val body = packet.optJSONObject("body") ?: JSONObject()

        if (type == Protocol.IDENTITY) {
            onIdentity(body)
            return
        }

        if (type == Protocol.AUTH) {
            onAuth(body)
            return
        }

        if (type == Protocol.UPGRADE) {
            if (_authenticated.value) onUpgradeOffer(body)
            else Log.w(TAG, "ignoring an upgrade offer before authentication")
            return
        }

        if (!_authenticated.value) {
            // The gate. Nothing from an unproven PC reaches a handler.
            Log.w(TAG, "dropping $type from an unauthenticated PC")
            return
        }

        val listeners = synchronized(handlers) { handlers[type]?.toList() }
        if (listeners.isNullOrEmpty()) {
            Log.d(TAG, "no handler for $type")
            return
        }
        listeners.forEach { handler ->
            runCatching { handler(body) }
                .onFailure { Log.e(TAG, "handler for $type threw", it) }
        }
    }

    // --------------------------------------------------------------- auth

    private fun onIdentity(body: JSONObject) {
        _peerName.value = body.optString("deviceName").ifBlank { null }

        val version = body.optInt("protocolVersion", 0)
        if (version < Protocol.MIN_PROTOCOL_VERSION) {
            Log.e(
                TAG,
                "PC speaks protocol $version; ${Protocol.MIN_PROTOCOL_VERSION} or " +
                    "newer is required. Update the daemon on the PC.",
            )
            session.get()?.close()
            return
        }

        val id = body.optString("deviceId").ifBlank { null }
        if (id == null) {
            Log.e(TAG, "PC sent no deviceId; refusing the link")
            session.get()?.close()
            return
        }

        peerId = id
        Log.i(TAG, "handshake with ${_peerName.value} (protocol $version)")

        // Challenge a PC we already know, so it has to prove itself too.
        // A PC we don't know drives enrollment; wait for its offer.
        val secret = TrustStore.secretFor(appContext, id)
        if (secret != null) {
            val nonce = Crypto.newNonce()
            ourNonce = nonce
            sendAuth(Protocol.STAGE_CHALLENGE, JSONObject().put("nonce", nonce))
        }
    }

    private fun onAuth(body: JSONObject) {
        when (body.optString("stage")) {
            Protocol.STAGE_ENROLL -> onEnroll(body)
            Protocol.STAGE_CHALLENGE -> answerChallenge(body.optString("nonce"))
            Protocol.STAGE_RESPONSE -> checkResponse(body.optString("mac", ""))
            Protocol.STAGE_OK -> Log.d(TAG, "PC accepted our response")
            Protocol.STAGE_FAIL -> {
                Log.e(TAG, "PC refused this phone: ${body.optString("reason")}")
                session.get()?.close()
            }
            else -> Log.w(TAG, "unknown auth stage in ${body}")
        }
    }

    /**
     * The PC is offering a secret for a link it doesn't recognise.
     *
     * We store it immediately and show the code: approval happens on the
     * PC, where the human is. A secret for a PC we never approve is inert
     * — it only ever unlocks a link that PC also has to approve.
     */
    private fun onEnroll(body: JSONObject) {
        val id = peerId
        val secret = body.optString("secret").ifBlank { null }
        if (id == null || secret == null) {
            Log.w(TAG, "malformed enrollment offer")
            return
        }

        val code = Crypto.fingerprint(secret)
        if (code == null || code != body.optString("code")) {
            // The digits the PC displayed have to be the digits this
            // secret produces, or one of the two is not what it claims.
            Log.e(TAG, "enrollment code does not match the secret; refusing")
            session.get()?.close()
            return
        }

        TrustStore.trust(appContext, id, secret)
        _pairingCode.value = code
        Log.i(TAG, "enrolled with PC $id, code $code")
    }

    private fun answerChallenge(nonce: String?) {
        val id = peerId
        if (nonce.isNullOrBlank() || id == null) {
            Log.w(TAG, "malformed challenge")
            return
        }

        val secret = TrustStore.secretFor(appContext, id)
        if (secret == null) {
            Log.w(TAG, "challenged by a PC we have no secret for")
            return
        }

        val mac = Crypto.respond(secret, nonce)
        if (mac == null) {
            Log.w(TAG, "could not answer the challenge")
            return
        }
        sendAuth(Protocol.STAGE_RESPONSE, JSONObject().put("mac", mac))
    }

    private fun checkResponse(mac: String) {
        val nonce = ourNonce
        val id = peerId
        // One challenge, one answer.
        ourNonce = null

        if (nonce == null || id == null) {
            Log.w(TAG, "unexpected auth response")
            return
        }

        val secret = TrustStore.secretFor(appContext, id)
        if (secret == null || !Crypto.verify(secret, nonce, mac)) {
            Log.e(TAG, "PC failed our challenge; dropping the link")
            sendAuth(Protocol.STAGE_FAIL, JSONObject().put("reason", "bad response"))
            session.get()?.close()
            return
        }

        Log.i(TAG, "PC authenticated")
        sendAuth(Protocol.STAGE_OK)
        _authenticated.value = true
        _pairingCode.value = null
        BatteryReporter.reportNow(appContext)
        MediaRelay.start(appContext)
        // The desktop cleared its row on disconnect, so re-send even if the
        // phone's own state hasn't moved.
        MediaRelay.forgetLastSent()
        MediaRelay.reportNow()
    }

    /**
     * The PC is offering a LAN link. Take it if we can reach it.
     *
     * Deliberately best-effort: failing to upgrade is not an error, it just
     * means the two aren't on the same network, and Bluetooth carries on.
     */
    private fun onUpgradeOffer(body: JSONObject) {
        val id = peerId ?: return
        val secret = TrustStore.secretFor(appContext, id)
        if (secret == null) {
            Log.w(TAG, "upgrade offer for a PC we have no secret for")
            return
        }

        val nonce = body.optString("nonce").ifBlank { null } ?: return
        val port = body.optInt("port", 0)
        if (port <= 0) return

        val array = body.optJSONArray("hosts") ?: return
        val hosts = (0 until array.length()).mapNotNull { array.optString(it).ifBlank { null } }
        if (hosts.isEmpty()) return

        scope?.launch {
            if (!LanTransport.connect(hosts, port, nonce, secret, id)) return@launch

            _onLan.value = true
            // Blocks until the LAN link ends; Bluetooth is still up
            // underneath, so this is a downgrade rather than a disconnect.
            LanTransport.readLoop { packet -> dispatch(packet) }
            _onLan.value = false
            Log.i(TAG, "LAN link ended; back to Bluetooth")
        }
    }

    private fun resetAuth() {
        // No Bluetooth session means no LAN session: its claim to trust came
        // from that handshake.
        LanTransport.close()
        _onLan.value = false
        _authenticated.value = false
        _pairingCode.value = null
        peerId = null
        ourNonce = null
    }

    // -------------------------------------------------------------- loops

    @SuppressLint("MissingPermission")
    private suspend fun acceptLoop() {
        while (coroutineContext.isActive) {
            val adapter = adapter()
            if (adapter == null || !adapter.isEnabled) {
                delay(5_000)
                continue
            }

            var server: BluetoothServerSocket? = null
            try {
                server = adapter.listenUsingRfcommWithServiceRecord(
                    "FedoraLink", Protocol.SERVICE_UUID,
                )
                while (coroutineContext.isActive) {
                    // Blocking accept; closing the server socket from
                    // elsewhere makes this throw, which exits the loop.
                    val socket = server.accept()
                    Log.i(TAG, "inbound connection from ${socket.remoteDeviceName()}")
                    adopt(socket)
                }
            } catch (e: IOException) {
                Log.d(TAG, "accept loop restarting: ${e.message}")
                delay(3_000)
            } catch (e: SecurityException) {
                Log.e(TAG, "missing Bluetooth permission for accept", e)
                delay(10_000)
            } finally {
                runCatching { server?.close() }
            }
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun connectLoop() {
        var backoff = 3_000L
        while (coroutineContext.isActive) {
            if (session.get() != null) {
                backoff = 3_000L
                delay(5_000)
                continue
            }

            val device = pairedTarget()
            if (device == null) {
                delay(10_000)
                continue
            }

            val socket = tryConnect(device)
            if (socket != null) {
                backoff = 3_000L
                adopt(socket)
            } else {
                delay(backoff)
                // Cap the backoff so a phone that's been away all day still
                // reconnects within a minute of coming back.
                backoff = (backoff * 2).coerceAtMost(60_000L)
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun tryConnect(device: BluetoothDevice): BluetoothSocket? {
        return try {
            val socket = device.createRfcommSocketToServiceRecord(Protocol.SERVICE_UUID)
            // Discovery and connecting contend for the radio; the platform
            // docs are explicit that you cancel discovery first.
            adapter()?.cancelDiscovery()
            socket.connect()
            Log.i(TAG, "outbound connection to ${device.name}")
            socket
        } catch (e: IOException) {
            Log.d(TAG, "connect to ${device.address} failed: ${e.message}")
            null
        } catch (e: SecurityException) {
            Log.e(TAG, "missing Bluetooth permission for connect", e)
            null
        }
    }

    private suspend fun adopt(socket: BluetoothSocket) = adoptLock.withLock {
        if (session.get() != null) {
            // Both sides dialled at once. Keep the established one.
            Log.d(TAG, "already connected; dropping duplicate socket")
            runCatching { socket.close() }
            return@withLock
        }

        val newSession = Session(socket)
        session.set(newSession)
        _state.value = State.CONNECTED
        _peerName.value = socket.remoteDeviceName()

        resetAuth()
        sendIdentity()
        // The first battery report waits for authentication — see
        // checkResponse(). Sending it here would leak to an unproven PC.

        scope?.launch { newSession.readLoop() }
    }

    private fun sendIdentity() {
        val body = JSONObject().apply {
            put("deviceName", Build.MODEL ?: "Android phone")
            put("deviceType", "phone")
            put("deviceId", TrustStore.deviceId(appContext))
            put("protocolVersion", Protocol.PROTOCOL_VERSION)
            put(
                "capabilities",
                JSONArray(listOf("battery", "notification", "clipboard", "ping")),
            )
        }
        sendUnauthenticated(Protocol.IDENTITY, body)
    }

    private fun onSessionClosed(closed: Session) {
        if (session.compareAndSet(closed, null)) {
            _state.value = if (scope != null) State.WAITING else State.STOPPED
            _peerName.value = null
            resetAuth()
            Log.i(TAG, "link closed")
        }
    }

    // ------------------------------------------------------------ helpers

    private fun adapter(): BluetoothAdapter? =
        appContext.getSystemService(android.bluetooth.BluetoothManager::class.java)?.adapter

    @SuppressLint("MissingPermission")
    private fun pairedTarget(): BluetoothDevice? {
        val adapter = adapter() ?: return null
        if (!adapter.isEnabled) return null

        return try {
            val saved = Settings.pairedAddress(appContext)
            val bonded = adapter.bondedDevices ?: return null
            if (saved != null) {
                bonded.firstOrNull { it.address == saved }
            } else {
                // No explicit pick yet: try any bonded computer.
                bonded.firstOrNull {
                    it.bluetoothClass?.majorDeviceClass ==
                        android.bluetooth.BluetoothClass.Device.Major.COMPUTER
                }
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "missing BLUETOOTH_CONNECT for bondedDevices", e)
            null
        }
    }

    @SuppressLint("MissingPermission")
    private fun BluetoothSocket.remoteDeviceName(): String =
        try {
            remoteDevice?.name ?: remoteDevice?.address ?: "PC"
        } catch (_: SecurityException) {
            "PC"
        }

    // ------------------------------------------------------------ session

    private class Session(private val socket: BluetoothSocket) {
        private val reader = Protocol.Reader(socket.inputStream)
        private val output: OutputStream = socket.outputStream
        private val writeLock = Any()
        @Volatile private var closed = false

        fun send(packet: JSONObject): Boolean {
            if (closed) return false
            return try {
                // RFCOMM writes are slow; serialising them keeps two
                // threads from interleaving halves of a line.
                synchronized(writeLock) {
                    output.write(Protocol.serialize(packet))
                    output.flush()
                }
                true
            } catch (e: IOException) {
                Log.d(TAG, "write failed: ${e.message}")
                close()
                false
            }
        }

        suspend fun readLoop() {
            try {
                while (!closed) {
                    val packet = reader.read() ?: break
                    dispatch(packet)
                }
            } catch (e: IOException) {
                Log.d(TAG, "read failed: ${e.message}")
            } finally {
                close()
            }
        }

        fun close() {
            if (closed) return
            closed = true
            runCatching { socket.close() }
            onSessionClosed(this)
        }
    }
}
