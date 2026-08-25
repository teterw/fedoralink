package dev.fedoralink.android

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.util.Log
import org.json.JSONObject

/**
 * Clipboard, as far as modern Android allows.
 *
 * Since Android 10, an app can only *read* the clipboard while it holds
 * focus or is the active input method — there is no permission that
 * lifts this. So phone-to-PC has to be user-initiated (see
 * [ClipboardSendActivity], which reads it while genuinely foregrounded),
 * while PC-to-phone works from here.
 */
object ClipboardBridge {

    private const val LABEL = "FedoraLink"

    /** Last value we exchanged, so we don't bounce it back and forth. */
    @Volatile
    var lastSynced: String? = null
        private set

    fun applyFromPc(context: Context, body: JSONObject) {
        val content = body.optString("content")
        if (content.isNullOrEmpty() || content == lastSynced) return

        val manager = context.getSystemService(ClipboardManager::class.java) ?: return
        lastSynced = content
        try {
            manager.setPrimaryClip(ClipData.newPlainText(LABEL, content))
        } catch (e: SecurityException) {
            // Some OEM skins restrict background clipboard writes too.
            Log.w("FedoraLink", "clipboard write refused: ${e.message}")
        }
    }

    /**
     * Reads the clipboard and sends it. Only succeeds from a focused
     * Activity — callers elsewhere will silently read nothing.
     */
    fun sendToPc(context: Context): Boolean {
        val manager = context.getSystemService(ClipboardManager::class.java)
            ?: return false
        val clip = manager.primaryClip ?: return false
        if (clip.itemCount == 0) return false

        val text = clip.getItemAt(0).coerceToText(context)?.toString()
        if (text.isNullOrEmpty()) return false

        return sendText(text)
    }

    /**
     * Sends text we were handed directly, with no clipboard read involved.
     *
     * This is the one path the Android 10 clipboard restriction cannot
     * touch: a share-sheet hand-off puts the text in the Intent, so there
     * is nothing to be refused access to.
     */
    fun sendText(text: String): Boolean {
        if (text.isEmpty()) return false

        lastSynced = text
        return LinkManager.send(Protocol.CLIPBOARD, JSONObject().put("content", text))
    }
}
