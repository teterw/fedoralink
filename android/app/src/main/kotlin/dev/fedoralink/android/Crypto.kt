package dev.fedoralink.android

import java.math.BigInteger
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * The phone's half of the shared-secret handshake.
 *
 * Every function here has to agree byte-for-byte with `daemon/fedoralink/
 * auth.py`, so any change lands on both sides or the link stops
 * authenticating.
 */
object Crypto {

    private const val NONCE_BYTES = 32
    private const val FINGERPRINT_DIGITS = 6

    private val random = SecureRandom()

    fun newNonce(): String {
        val bytes = ByteArray(NONCE_BYTES)
        random.nextBytes(bytes)
        return Base64.getEncoder().encodeToString(bytes)
    }

    /**
     * Six digits for the human to compare against the PC's screen.
     *
     * A hash of the secret, not a slice of it — this gets displayed on a
     * lock screen, and showing key bytes there would hand the secret to
     * anyone glancing at the phone.
     */
    fun fingerprint(secret: String): String? {
        val bytes = decode(secret) ?: return null
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
        // Python reads these 8 bytes as an unsigned integer; Long is
        // signed, so BigInteger with an explicit positive sign is what
        // keeps the two sides agreeing.
        val value = BigInteger(1, digest.copyOfRange(0, 8))
            .mod(BigInteger.TEN.pow(FINGERPRINT_DIGITS))
        return value.toString().padStart(FINGERPRINT_DIGITS, '0')
    }

    /** HMAC-SHA256 of `nonce` under `secret`, lowercase hex. */
    fun respond(secret: String, nonce: String): String? {
        val key = decode(secret) ?: return null
        val challenge = decode(nonce) ?: return null

        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(challenge).joinToString("") { "%02x".format(it) }
    }

    /**
     * Check a response. Never throws: a malformed answer from a peer that
     * hasn't authenticated yet is a failed authentication, not a crash.
     */
    fun verify(secret: String, nonce: String, response: String?): Boolean {
        if (response.isNullOrEmpty()) return false
        val expected = respond(secret, nonce) ?: return false
        // isEqual is the platform's constant-time comparison: it doesn't
        // leak the expected value one byte at a time through how long a
        // mismatch takes to detect.
        return MessageDigest.isEqual(
            expected.toByteArray(Charsets.UTF_8),
            response.toByteArray(Charsets.UTF_8),
        )
    }

    // java.util.Base64 rather than android.util.Base64: available since
    // API 26 (our minSdk), and it means this object has no Android
    // framework dependency at all, so it runs under plain JVM unit tests.
    private fun decode(value: String): ByteArray? = runCatching {
        Base64.getDecoder().decode(value)
    }.getOrNull()?.takeIf { it.isNotEmpty() }
}
