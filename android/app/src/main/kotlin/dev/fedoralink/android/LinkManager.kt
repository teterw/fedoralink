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

    fun isConnected(): Boolean = session.get() != null

    fun send(type: String, body: JSONObject = JSONObject()): Boolean {
        val active = session.get() ?: return false
        return active.send(Protocol.packet(type, body))
    }

    // ------------------------------------------------------------ inbound

    private fun dispatch(packet: JSONObject) {
        val type = packet.optString("type")
        val body = packet.optJSONObject("body") ?: JSONObject()

        if (type == Protocol.IDENTITY) {
            _peerName.value = body.optString("deviceName").ifBlank { null }
            Log.i(TAG, "handshake with ${_peerName.value}")
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

        sendIdentity()
        BatteryReporter.reportNow(appContext)

        scope?.launch { newSession.readLoop() }
    }

    private fun sendIdentity() {
        val body = JSONObject().apply {
            put("deviceName", Build.MODEL ?: "Android phone")
            put("deviceType", "phone")
            put("protocolVersion", Protocol.PROTOCOL_VERSION)
            put(
                "capabilities",
                JSONArray(listOf("battery", "notification", "clipboard", "ping")),
            )
        }
        send(Protocol.IDENTITY, body)
    }

    private fun onSessionClosed(closed: Session) {
        if (session.compareAndSet(closed, null)) {
            _state.value = if (scope != null) State.WAITING else State.STOPPED
            _peerName.value = null
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
