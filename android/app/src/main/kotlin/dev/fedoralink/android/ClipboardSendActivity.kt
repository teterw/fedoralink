package dev.fedoralink.android

import android.app.Activity
import android.os.Bundle
import android.widget.Toast

/**
 * Invisible activity whose only job is to have focus for one frame.
 *
 * Android 10+ refuses clipboard reads to unfocused apps, so a background
 * service simply cannot do this. Becoming the foreground activity —
 * briefly, from a launcher shortcut or Quick Settings tile — is the
 * supported way to get at it.
 */
class ClipboardSendActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val message = when {
            !LinkManager.isConnected() -> getString(R.string.clipboard_not_connected)
            ClipboardBridge.sendToPc(this) -> getString(R.string.clipboard_sent)
            else -> getString(R.string.clipboard_empty)
        }

        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
        finish()
        // No transition — the user should never see a window appear.
        overridePendingTransition(0, 0)
    }
}
