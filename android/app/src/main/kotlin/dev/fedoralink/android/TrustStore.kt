package dev.fedoralink.android

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.util.UUID

// TAG in LinkManager.kt is file-private, so each file carries its own.
private const val TAG = "FedoraLink"

/**
 * Which PC this phone trusts, and the secret shared with it.
 *
 * Kept in EncryptedSharedPreferences rather than the ordinary kind. App
 * storage is already private to us, so this is defence for the case that
 * remains: someone with the unlocked device or an offline copy of its
 * data partition.
 *
 * Falls back to ordinary private preferences if the keystore is
 * unavailable — an old or broken Keystore should degrade the storage, not
 * stop the link working. The fallback is logged, never silent.
 */
object TrustStore {

    private const val PREFS = "fedoralink_trust"
    private const val KEY_DEVICE_ID = "device_id"
    private const val SECRET_PREFIX = "secret_"

    @Volatile
    private var cached: SharedPreferences? = null

    private fun prefs(context: Context): SharedPreferences {
        cached?.let { return it }

        val resolved = try {
            val key = MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            EncryptedSharedPreferences.create(
                context,
                PREFS,
                key,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
        } catch (e: Exception) {
            Log.w(TAG, "encrypted prefs unavailable, falling back to private prefs", e)
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        }

        cached = resolved
        return resolved
    }

    /**
     * This phone's identifier, as the PC knows it.
     *
     * A random UUID minted once, not ANDROID_ID or anything derived from
     * hardware: the PC only needs it to be stable, and a value that
     * follows the user across apps would be a tracking vector for no gain.
     * Reinstalling forces re-enrollment, which is the honest outcome —
     * the secret is gone with it.
     */
    fun deviceId(context: Context): String {
        val store = prefs(context)
        store.getString(KEY_DEVICE_ID, null)?.let { return it }

        val fresh = UUID.randomUUID().toString()
        store.edit().putString(KEY_DEVICE_ID, fresh).apply()
        return fresh
    }

    fun secretFor(context: Context, pcId: String): String? =
        prefs(context).getString(SECRET_PREFIX + pcId, null)

    fun trust(context: Context, pcId: String, secret: String) {
        prefs(context).edit().putString(SECRET_PREFIX + pcId, secret).apply()
        Log.i(TAG, "stored secret for PC $pcId")
    }

    fun revoke(context: Context, pcId: String) {
        prefs(context).edit().remove(SECRET_PREFIX + pcId).apply()
        Log.i(TAG, "revoked PC $pcId")
    }

    /** Forget every PC. Used by "Unlink" in the app. */
    fun revokeAll(context: Context) {
        val store = prefs(context)
        val keys = store.all.keys.filter { it.startsWith(SECRET_PREFIX) }
        store.edit().apply { keys.forEach { remove(it) } }.apply()
        Log.i(TAG, "revoked ${keys.size} PC(s)")
    }
}
