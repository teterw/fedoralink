package dev.fedoralink.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Bring the link back after a reboot, but only if the user had it on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (!Settings.isEnabled(context)) return
        LinkService.start(context)
    }
}
