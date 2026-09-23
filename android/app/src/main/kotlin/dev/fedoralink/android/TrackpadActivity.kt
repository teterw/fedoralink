package dev.fedoralink.android

import android.os.Bundle
import android.os.SystemClock
import android.view.MotionEvent
import android.view.WindowManager
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import dev.fedoralink.android.databinding.ActivityTrackpadBinding
import kotlin.math.abs
import org.json.JSONObject

/**
 * The phone as a trackpad for the PC.
 *
 * Gestures, chosen to match what a laptop touchpad does so nobody has to
 * learn anything: one finger moves the pointer, a tap is a left click, a
 * two-finger tap is a right click, two fingers dragging scrolls, and a long
 * press followed by movement drags.
 *
 * Motion is coalesced to roughly 60 Hz. Touch events arrive at over twice
 * that, and each one is a JSON packet — over Bluetooth's ~200 KB/s,
 * forwarding every single one would flood the link and make the pointer
 * *less* responsive, not more.
 */
class TrackpadActivity : AppCompatActivity() {

    private lateinit var binding: ActivityTrackpadBinding

    // Coalescing state: deltas accumulate here between flushes.
    private var pendingDx = 0f
    private var pendingDy = 0f
    private var lastFlush = 0L

    private var lastX = 0f
    private var lastY = 0f
    private var downAt = 0L
    private var travelled = 0f
    private var pointerCount = 0
    private var dragging = false
    private var scrolling = false

    /** Screen density, so a swipe feels the same on any phone. */
    private var density = 1f

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityTrackpadBinding.inflate(layoutInflater)
        setContentView(binding.root)

        density = resources.displayMetrics.density
        // A trackpad you have to keep waking is not a trackpad.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        binding.surface.setOnTouchListener { _, event -> onTouch(event) }

        if (!LinkManager.isConnected()) {
            Toast.makeText(this, R.string.clipboard_not_connected, Toast.LENGTH_SHORT)
                .show()
        }
    }

    override fun onPause() {
        super.onPause()
        // Never leave a button held down because the user switched away.
        if (dragging) {
            sendButton("left", false)
            dragging = false
        }
        flush(force = true)
    }

    private fun onTouch(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                lastX = event.x
                lastY = event.y
                downAt = SystemClock.uptimeMillis()
                travelled = 0f
                pointerCount = 1
                scrolling = false
            }

            MotionEvent.ACTION_POINTER_DOWN -> {
                pointerCount = event.pointerCount
                // Two fingers: stop moving the pointer, start scrolling.
                if (pointerCount >= 2) {
                    flush(force = true)
                    scrolling = true
                    lastX = event.x
                    lastY = event.y
                }
            }

            MotionEvent.ACTION_MOVE -> {
                val dx = event.x - lastX
                val dy = event.y - lastY
                lastX = event.x
                lastY = event.y
                travelled += abs(dx) + abs(dy)

                if (scrolling) {
                    // Natural direction: dragging the content down scrolls up.
                    sendScroll(-dx / density, -dy / density)
                } else {
                    if (!dragging && isLongPress()) {
                        dragging = true
                        sendButton("left", true)
                    }
                    pendingDx += dx / density
                    pendingDy += dy / density
                    flush(force = false)
                }
            }

            MotionEvent.ACTION_POINTER_UP -> {
                // Two-finger tap, if neither finger really moved.
                if (event.pointerCount == 2 && travelled < TAP_SLOP && isTap()) {
                    click("right")
                }
                pointerCount = event.pointerCount - 1
            }

            MotionEvent.ACTION_UP -> {
                flush(force = true)
                if (dragging) {
                    sendButton("left", false)
                    dragging = false
                } else if (!scrolling && travelled < TAP_SLOP && isTap()) {
                    click("left")
                }
                scrolling = false
                pointerCount = 0
            }

            MotionEvent.ACTION_CANCEL -> {
                if (dragging) {
                    sendButton("left", false)
                    dragging = false
                }
                flush(force = true)
                scrolling = false
            }
        }
        return true
    }

    private fun isTap(): Boolean =
        SystemClock.uptimeMillis() - downAt < TAP_MAX_MS

    private fun isLongPress(): Boolean =
        SystemClock.uptimeMillis() - downAt > LONG_PRESS_MS && travelled < TAP_SLOP * 2

    private fun flush(force: Boolean) {
        val now = SystemClock.uptimeMillis()
        if (!force && now - lastFlush < FLUSH_INTERVAL_MS) return
        if (pendingDx == 0f && pendingDy == 0f) return

        send("motion", JSONObject().put("dx", round(pendingDx)).put("dy", round(pendingDy)))
        pendingDx = 0f
        pendingDy = 0f
        lastFlush = now
    }

    private fun click(button: String) {
        sendButton(button, true)
        sendButton(button, false)
    }

    private fun sendButton(button: String, pressed: Boolean) {
        send("button", JSONObject().put("button", button).put("pressed", pressed))
    }

    private fun sendScroll(dx: Float, dy: Float) {
        send("scroll", JSONObject().put("dx", round(dx)).put("dy", round(dy)))
    }

    private fun send(kind: String, body: JSONObject) {
        LinkManager.send(Protocol.INPUT, body.put("kind", kind))
    }

    /** Two decimals is plenty, and keeps the packets small. */
    private fun round(value: Float): Double =
        (Math.round(value * 100.0) / 100.0)

    private companion object {
        /** ~60 Hz. Touch events arrive faster; each one is a packet. */
        const val FLUSH_INTERVAL_MS = 16L
        const val TAP_MAX_MS = 250L
        const val LONG_PRESS_MS = 400L

        /** Movement below this still counts as a tap, in raw pixels. */
        const val TAP_SLOP = 24f
    }
}
