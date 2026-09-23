package dev.fedoralink.android

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import org.json.JSONObject

/**
 * Find-my-phone in both directions.
 *
 * Inbound (`ring`/`stop`): the PC asks this phone to ring at full volume
 * even when it is silenced. Uses the alarm stream, because that's the one
 * that still plays under Do Not Disturb — which is exactly when you've
 * lost the thing.
 *
 * Outbound (`ringPc`): this phone asks the PC to make noise, for when the
 * laptop is the thing that's missing.
 */
object Ringer {

    private const val RING_DURATION_MS = 15_000L

    private var ringtone: Ringtone? = null
    private var previousAlarmVolume: Int? = null
    private val handler = Handler(Looper.getMainLooper())
    private val stopRunnable = Runnable { stop(null) }

    // The waveform repeats until something cancels it, so the vibrator has
    // to be reachable from stop() — not only from its own delayed callback,
    // or an early stop leaves the phone buzzing with the ringtone silent.
    private var vibrator: Vibrator? = null
    private val cancelVibrationRunnable = Runnable { cancelVibration() }

    /** Find-my-PC: ask the desktop to make noise. */
    fun ringPc(): Boolean =
        LinkManager.send(Protocol.PING, JSONObject().put("ring", true))

    fun ring(context: Context) {
        stop(context)

        val audio = context.getSystemService(AudioManager::class.java) ?: return
        try {
            previousAlarmVolume = audio.getStreamVolume(AudioManager.STREAM_ALARM)
            audio.setStreamVolume(
                AudioManager.STREAM_ALARM,
                audio.getStreamMaxVolume(AudioManager.STREAM_ALARM),
                0,
            )
        } catch (e: SecurityException) {
            // Changing volume under some DND policies needs a grant we
            // don't have. Ring at whatever the current level is.
            Log.d("FedoraLink", "could not raise alarm volume: ${e.message}")
        }

        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)

        ringtone = RingtoneManager.getRingtone(context, uri)?.apply {
            audioAttributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
            play()
        }

        vibrate(context)
        handler.postDelayed(stopRunnable, RING_DURATION_MS)
    }

    fun stop(context: Context?) {
        handler.removeCallbacks(stopRunnable)
        handler.removeCallbacks(cancelVibrationRunnable)
        cancelVibration()

        ringtone?.runCatching { stop() }
        ringtone = null

        val restore = previousAlarmVolume
        previousAlarmVolume = null
        if (context != null && restore != null) {
            runCatching {
                context.getSystemService(AudioManager::class.java)
                    ?.setStreamVolume(AudioManager.STREAM_ALARM, restore, 0)
            }
        }
    }

    private fun cancelVibration() {
        vibrator?.runCatching { cancel() }
        vibrator = null
    }

    private fun vibrate(context: Context) {
        val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.getSystemService(VibratorManager::class.java)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Vibrator::class.java)
        } ?: return

        this.vibrator = vibrator

        val pattern = longArrayOf(0, 400, 300, 400, 300)
        runCatching {
            vibrator.vibrate(VibrationEffect.createWaveform(pattern, 0))
        }
        handler.postDelayed(cancelVibrationRunnable, RING_DURATION_MS)
    }
}
