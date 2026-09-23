package dev.fedoralink.android

import android.content.ComponentName
import android.content.Context
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.util.Log
import org.json.JSONObject

private const val TAG = "FedoraLink"

/**
 * Reports what the phone is playing, and takes commands from the PC.
 *
 * Bluetooth already does this over AVRCP — but only while A2DP is
 * connected, and FedoraLink disconnects audio by default so music doesn't
 * come out of the laptop. Dropping A2DP takes AVRCP with it, so this is
 * what puts the media keys back.
 *
 * [MediaSessionManager.getActiveSessions] needs an enabled notification
 * listener, which the app already has for notification mirroring, so this
 * costs no new permission. It throws SecurityException if the grant is
 * revoked, hence the runCatching around every call into it.
 */
object MediaRelay {

    private var manager: MediaSessionManager? = null
    private var component: ComponentName? = null
    private var listener: MediaSessionManager.OnActiveSessionsChangedListener? = null

    /** Last payload sent, so an unchanged session doesn't spam the link. */
    private var lastSent: String? = null

    fun start(context: Context) {
        if (manager != null) return

        val service = context.getSystemService(MediaSessionManager::class.java)
        if (service == null) {
            Log.w(TAG, "no MediaSessionManager; media control unavailable")
            return
        }

        manager = service
        component = ComponentName(context, NotificationRelay::class.java)

        val callback = MediaSessionManager.OnActiveSessionsChangedListener {
            reportNow()
        }
        listener = callback

        val added = runCatching {
            service.addOnActiveSessionsChangedListener(callback, component)
        }
        if (added.isFailure) {
            // Notification access not granted yet. MainActivity walks the
            // user there; this starts working when it does.
            Log.i(TAG, "media sessions unavailable until notification access is granted")
            manager = null
            listener = null
            return
        }

        reportNow()
    }

    fun stop() {
        val service = manager
        val callback = listener
        if (service != null && callback != null) {
            runCatching { service.removeOnActiveSessionsChangedListener(callback) }
        }
        manager = null
        listener = null
        component = null
        lastSent = null
    }

    /** The session worth showing: the first one actually playing, else the first. */
    private fun activeController(): MediaController? {
        val service = manager ?: return null
        val sessions = runCatching { service.getActiveSessions(component) }
            .getOrElse {
                Log.w(TAG, "cannot read media sessions", it)
                return null
            }

        return sessions.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING }
            ?: sessions.firstOrNull()
    }

    fun reportNow() {
        if (!LinkManager.isConnected()) return

        val controller = activeController()
        val body = if (controller == null) {
            // Explicitly empty rather than silence, so the desktop clears
            // its row instead of showing a stale track forever.
            JSONObject().put("playing", false).put("hasSession", false)
        } else {
            val metadata = controller.metadata
            val state = controller.playbackState?.state
            JSONObject().apply {
                put("hasSession", true)
                put("playing", state == PlaybackState.STATE_PLAYING)
                put(
                    "title",
                    metadata?.getString(android.media.MediaMetadata.METADATA_KEY_TITLE).orEmpty(),
                )
                put(
                    "artist",
                    metadata?.getString(android.media.MediaMetadata.METADATA_KEY_ARTIST).orEmpty(),
                )
                put("app", controller.packageName.orEmpty())
            }
        }

        val encoded = body.toString()
        if (encoded == lastSent) return
        lastSent = encoded

        LinkManager.send(Protocol.MEDIA, body)
    }

    /** Apply a command from the PC. */
    fun command(action: String?) {
        val controls = activeController()?.transportControls
        if (controls == null) {
            Log.d(TAG, "media command '$action' with no active session")
            return
        }

        runCatching {
            when (action) {
                "play" -> controls.play()
                "pause" -> controls.pause()
                "playpause" ->
                    if (activeController()?.playbackState?.state == PlaybackState.STATE_PLAYING) {
                        controls.pause()
                    } else {
                        controls.play()
                    }
                "next" -> controls.skipToNext()
                "previous" -> controls.skipToPrevious()
                else -> Log.w(TAG, "unknown media action '$action'")
            }
        }.onFailure { Log.w(TAG, "media command '$action' failed", it) }

        // The state change isn't instant; let the session settle before
        // telling the PC what happened.
        reportNow()
    }
}
