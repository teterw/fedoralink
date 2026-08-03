package dev.fedoralink.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import org.json.JSONObject

/**
 * Pushes battery level to the PC.
 *
 * ACTION_BATTERY_CHANGED can't be declared in the manifest — it only
 * arrives via a runtime registration, which is why this is tied to the
 * foreground service's lifetime.
 */
object BatteryReporter {

    private var lastLevel = -1
    private var lastCharging = false

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            report(context, force = false)
        }
    }

    fun register(context: Context) {
        context.registerReceiver(receiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
    }

    fun unregister(context: Context) {
        runCatching { context.unregisterReceiver(receiver) }
        lastLevel = -1
    }

    /** Send current state regardless of whether it changed. */
    fun reportNow(context: Context) = report(context, force = true)

    private fun report(context: Context, force: Boolean) {
        if (!LinkManager.isConnected()) return

        val manager = context.getSystemService(BatteryManager::class.java) ?: return
        val level = manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val charging = manager.isCharging

        if (level !in 0..100) return
        // The battery broadcast fires far more often than the level moves;
        // don't spend RFCOMM bandwidth repeating ourselves.
        if (!force && level == lastLevel && charging == lastCharging) return

        lastLevel = level
        lastCharging = charging

        LinkManager.send(
            Protocol.BATTERY,
            JSONObject().put("level", level).put("charging", charging),
        )
    }
}
