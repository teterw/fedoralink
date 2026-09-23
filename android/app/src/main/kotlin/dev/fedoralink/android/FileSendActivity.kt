package dev.fedoralink.android

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * Share-sheet entry point for sending a file to the PC.
 *
 * Invisible and immediate: it hands the Uri to [FileTransfer] on a
 * background thread and finishes, because the transfer can take minutes
 * over Bluetooth and holding an activity open for it would be wrong.
 *
 * The scope deliberately outlives the activity. LinkService is a foreground
 * service holding the socket, so the process stays alive; tying the
 * transfer to the activity's lifecycle would cancel it the moment the
 * window closed.
 */
class FileSendActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val uris = incomingUris()

        val message = when {
            uris.isEmpty() -> getString(R.string.file_nothing_shared)
            !LinkManager.isConnected() -> getString(R.string.clipboard_not_connected)
            else -> {
                start(uris)
                resources.getQuantityString(R.plurals.file_sending, uris.size, uris.size)
            }
        }

        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
        finish()
        overridePendingTransition(0, 0)
    }

    // The typed-Class overloads only exist from API 33; minSdk is 26, so
    // these are the forms that work across the whole range.
    @Suppress("DEPRECATION")
    private fun incomingUris(): List<Uri> {
        val intent = intent ?: return emptyList()
        return when (intent.action) {
            Intent.ACTION_SEND ->
                listOfNotNull(intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE ->
                intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM).orEmpty()
            else -> emptyList()
        }
    }

    private fun start(uris: List<Uri>) {
        // Read permission is granted to this activity's task, so the Uris
        // have to be opened before it goes away — FileTransfer.send does
        // that first thing.
        val context = applicationContext
        scope.launch {
            // Sequential on purpose: the protocol carries one transfer at a
            // time, and queueing them is the honest way to send several.
            for (uri in uris) {
                FileTransfer.send(context, uri)
            }
        }
    }

    companion object {
        private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    }
}
