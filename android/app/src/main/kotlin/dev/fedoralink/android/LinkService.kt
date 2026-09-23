package dev.fedoralink.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * Keeps the Bluetooth link alive.
 *
 * Android will kill a plain background process holding a socket, so the
 * connection has to live inside a foreground service with an ongoing
 * notification. That notification is the price of the feature, not an
 * oversight.
 */
class LinkService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var handlersRegistered = false

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startInForeground(getString(R.string.status_waiting))

        registerHandlers()
        LinkManager.start(this)
        BatteryReporter.register(this)

        scope.launch {
            combine(LinkManager.state, LinkManager.peerName) { state, peer ->
                when (state) {
                    LinkManager.State.CONNECTED ->
                        getString(R.string.status_connected_to, peer ?: "PC")
                    LinkManager.State.WAITING -> getString(R.string.status_waiting)
                    LinkManager.State.STOPPED -> getString(R.string.status_stopped)
                }
            }.collect { text ->
                notificationManager().notify(NOTIFICATION_ID, buildNotification(text))
                // A fresh link should report battery immediately rather
                // than waiting for the next system broadcast.
                if (LinkManager.isConnected()) BatteryReporter.reportNow(this@LinkService)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            Settings.setEnabled(this, false)
            stopSelf()
            return START_NOT_STICKY
        }
        Settings.setEnabled(this, true)
        // Restart if the system reclaims us — the whole point is to stay up.
        return START_STICKY
    }

    override fun onDestroy() {
        MediaRelay.stop()
        BatteryReporter.unregister(this)
        LinkManager.stop()
        Ringer.stop(this)
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun registerHandlers() {
        if (handlersRegistered) return
        handlersRegistered = true

        LinkManager.on(Protocol.PING) { body ->
            if (body.optBoolean("ring", false)) Ringer.ring(applicationContext)
            else Ringer.stop(applicationContext)
        }
        LinkManager.on(Protocol.FILE_OFFER) { body ->
            FileTransfer.onOffer(applicationContext, body)
        }
        LinkManager.on(Protocol.FILE_CHUNK) { body ->
            FileTransfer.onChunk(applicationContext, body)
        }
        LinkManager.on(Protocol.FILE_DONE) { body ->
            FileTransfer.onDone(applicationContext, body)
        }
        LinkManager.on(Protocol.FILE_CANCEL) { body ->
            FileTransfer.onCancel(applicationContext, body)
        }
        LinkManager.on(Protocol.FILE_ACCEPT) { body ->
            FileTransfer.acceptOutgoing(body.optString("id"))
        }
        LinkManager.on(Protocol.MEDIA) { body ->
            MediaRelay.command(body.optString("action"))
        }
        LinkManager.on(Protocol.CLIPBOARD) { body ->
            ClipboardBridge.applyFromPc(applicationContext, body)
        }
        LinkManager.on(Protocol.NOTIFICATION_DISMISS) { body ->
            NotificationRelay.dismiss(body.optString("key"))
        }
        LinkManager.on(Protocol.NOTIFICATION_ACTION) { body ->
            when (body.optString("action")) {
                "dismiss" -> NotificationRelay.dismiss(body.optString("key"))
                "reply" -> NotificationRelay.reply(
                    body.optString("key"),
                    body.optString("text"),
                )
            }
        }
    }

    /**
     * Android 14 rejects a foreground service that doesn't declare why it
     * exists, and the type has to be passed here as well as in the manifest.
     */
    private fun startInForeground(text: String) {
        val notification = buildNotification(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun notificationManager() =
        getSystemService(NotificationManager::class.java)

    private fun createChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_link_status),
            // MIN keeps it collapsed at the bottom of the shade rather
            // than nagging — it's a status line, not an alert.
            NotificationManager.IMPORTANCE_MIN,
        ).apply {
            description = getString(R.string.channel_link_status_desc)
            setShowBadge(false)
        }
        notificationManager().createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, LinkService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_link)
            .setContentIntent(open)
            .addAction(
                Notification.Action.Builder(null, getString(R.string.action_stop), stop).build()
            )
            .setOngoing(true)
            .setShowWhen(false)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "link_status"
        private const val NOTIFICATION_ID = 1
        const val ACTION_STOP = "dev.fedoralink.android.STOP"

        fun start(context: Context) {
            val intent = Intent(context, LinkService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, LinkService::class.java).setAction(ACTION_STOP)
            )
        }
    }
}
