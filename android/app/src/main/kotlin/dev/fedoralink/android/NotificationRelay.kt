package dev.fedoralink.android

import android.app.Notification
import android.content.Context
import android.content.pm.PackageManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.text.TextUtils
import org.json.JSONObject

/**
 * Mirrors phone notifications to the PC.
 *
 * Notification access is a special grant the user makes in system
 * Settings — there's no runtime permission dialog for it, which is why
 * [isEnabled] exists and MainActivity walks the user over there.
 */
class NotificationRelay : NotificationListenerService() {

    override fun onListenerConnected() {
        instance = this
        // The PC may have started mid-session; send what's already showing
        // so its notification list isn't empty until something new arrives.
        runCatching { activeNotifications?.forEach { relay(it) } }
    }

    override fun onListenerDisconnected() {
        instance = null
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) = relay(sbn)

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        if (!LinkManager.isConnected()) return
        LinkManager.send(
            Protocol.NOTIFICATION_DISMISS,
            JSONObject().put("key", sbn.key),
        )
    }

    private fun relay(sbn: StatusBarNotification) {
        if (!LinkManager.isConnected()) return
        if (!shouldRelay(sbn)) return

        val extras = sbn.notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (
            extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
                ?: extras.getCharSequence(Notification.EXTRA_TEXT)
            )?.toString().orEmpty()

        if (title.isBlank() && text.isBlank()) return

        LinkManager.send(
            Protocol.NOTIFICATION,
            JSONObject().apply {
                put("key", sbn.key)
                put("appName", appLabel(sbn.packageName))
                put("packageName", sbn.packageName)
                put("title", title)
                put("text", text)
            },
        )
    }

    private fun shouldRelay(sbn: StatusBarNotification): Boolean {
        // Never mirror our own status notification back to the PC.
        if (sbn.packageName == packageName) return false

        val flags = sbn.notification.flags
        // Group summaries duplicate their children; ongoing notifications
        // are progress bars and media controls, not things to be told about.
        if (flags and Notification.FLAG_GROUP_SUMMARY != 0) return false
        if (flags and Notification.FLAG_ONGOING_EVENT != 0) return false

        return sbn.isClearable
    }

    private fun appLabel(pkg: String): String = try {
        val info = packageManager.getApplicationInfo(pkg, 0)
        packageManager.getApplicationLabel(info).toString()
    } catch (_: PackageManager.NameNotFoundException) {
        pkg
    }

    companion object {
        @Volatile
        private var instance: NotificationRelay? = null

        /** Clear a notification because the PC dismissed its mirror. */
        fun dismiss(key: String?) {
            if (key.isNullOrEmpty()) return
            instance?.runCatching { cancelNotification(key) }
        }

        fun isEnabled(context: Context): Boolean {
            val enabled = android.provider.Settings.Secure.getString(
                context.contentResolver, "enabled_notification_listeners",
            ) ?: return false

            return TextUtils.split(enabled, ":").any {
                android.content.ComponentName.unflattenFromString(it)
                    ?.packageName == context.packageName
            }
        }
    }
}
