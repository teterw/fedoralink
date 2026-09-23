package dev.fedoralink.android

import android.app.Notification
import android.app.RemoteInput
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.text.TextUtils
import android.util.Log
import org.json.JSONObject

private const val TAG = "FedoraLink"

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
                // Tells the PC whether to offer a Reply button. Sent even
                // when false so an app losing its reply action updates the
                // mirror rather than leaving a dead button behind.
                put("canReply", findReplyAction(sbn.notification) != null)
            },
        )
    }

    /**
     * The notification's reply action, if it has one.
     *
     * A messaging app exposes replying as an action carrying a RemoteInput
     * — the same thing the phone's own notification shade uses to show a
     * text box. Anything without one cannot be replied to at all.
     */
    private fun findReplyAction(notification: Notification): Notification.Action? =
        notification.actions?.firstOrNull { action ->
            action.remoteInputs?.isNotEmpty() == true && action.actionIntent != null
        }

    private fun reply(key: String, text: String) {
        val sbn = runCatching { activeNotifications }
            .getOrNull()
            ?.firstOrNull { it.key == key }

        if (sbn == null) {
            Log.w(TAG, "cannot reply: notification $key is gone")
            fail(key, "notification no longer showing")
            return
        }

        val action = findReplyAction(sbn.notification)
        if (action == null) {
            Log.w(TAG, "cannot reply: $key has no reply action")
            fail(key, "this notification cannot be replied to")
            return
        }

        val results = Bundle()
        // Every RemoteInput on the action has to be filled in, or the app
        // sees a null where it expected the user's text.
        action.remoteInputs!!.forEach { input -> results.putCharSequence(input.resultKey, text) }

        val intent = Intent().apply {
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        RemoteInput.addResultsToIntent(action.remoteInputs, intent, results)

        val sent = runCatching { action.actionIntent!!.send(this, 0, intent) }
        if (sent.isFailure) {
            Log.e(TAG, "reply to $key failed", sent.exceptionOrNull())
            fail(key, "the app rejected the reply")
            return
        }

        Log.i(TAG, "replied to $key")
    }

    /** Tell the PC the reply didn't land, so it can say so. */
    private fun fail(key: String, reason: String) {
        LinkManager.send(
            Protocol.NOTIFICATION_ACTION,
            JSONObject().apply {
                put("key", key)
                put("action", "reply-failed")
                put("reason", reason)
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

        /** Send the PC's typed reply through the originating app. */
        fun reply(key: String?, text: String?) {
            if (key.isNullOrEmpty() || text.isNullOrEmpty()) return
            val service = instance
            if (service == null) {
                Log.w(TAG, "cannot reply: notification access is not connected")
                return
            }
            service.reply(key, text)
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
