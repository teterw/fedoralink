package dev.fedoralink.android

import android.content.Context

/** Which PC we link to, remembered across restarts. */
object Settings {
    private const val PREFS = "fedoralink"
    private const val KEY_PAIRED_ADDRESS = "paired_address"
    private const val KEY_ENABLED = "enabled"

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun pairedAddress(context: Context): String? =
        prefs(context).getString(KEY_PAIRED_ADDRESS, null)

    fun setPairedAddress(context: Context, address: String?) {
        prefs(context).edit().putString(KEY_PAIRED_ADDRESS, address).apply()
    }

    /** Whether the user wants the link running at all (survives reboot). */
    fun isEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_ENABLED, false)

    fun setEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_ENABLED, enabled).apply()
    }
}
