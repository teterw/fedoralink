package dev.fedoralink.android

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/**
 * Quick Settings tile that pushes the clipboard to the PC.
 *
 * The tile can't read the clipboard itself — it launches
 * [ClipboardSendActivity], which can, because it briefly holds focus.
 */
class ClipboardTileService : TileService() {

    override fun onStartListening() {
        super.onStartListening()
        qsTile?.apply {
            state = if (LinkManager.isConnected()) Tile.STATE_INACTIVE else Tile.STATE_UNAVAILABLE
            label = getString(R.string.tile_clipboard)
            updateTile()
        }
    }

    override fun onClick() {
        super.onClick()

        val intent = Intent(this, ClipboardSendActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14 removed the Intent overload in favour of a
            // PendingIntent, so the system controls the launch.
            startActivityAndCollapse(
                PendingIntent.getActivity(
                    this, 0, intent,
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                )
            )
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(intent)
        }
    }
}
