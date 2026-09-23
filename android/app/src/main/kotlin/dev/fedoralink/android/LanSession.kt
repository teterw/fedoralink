package dev.fedoralink.android

import java.io.InputStream
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Key derivation and record encryption for the LAN link.
 *
 * Mirrors `daemon/fedoralink/session.py` exactly — same HKDF, same info
 * strings, same counter-as-nonce scheme. The two are pinned against shared
 * vectors in LanSessionTest; if they drift, the LAN link stops working.
 *
 * No Android framework types, so it runs under plain JVM unit tests.
 */
object LanSession {

    private const val KEY_BYTES = 32
    private const val NONCE_BYTES = 12
    private const val TAG_BITS = 128

    const val LENGTH_PREFIX = 4
    const val MAX_RECORD_BYTES = 8 * 1024 * 1024

    private val INFO_DESKTOP = "fedoralink desktop->phone".toByteArray(Charsets.UTF_8)
    private val INFO_PHONE = "fedoralink phone->desktop".toByteArray(Charsets.UTF_8)

    class SessionException(message: String) : Exception(message)

    /** HKDF-SHA256, RFC 5869. */
    fun hkdf(secret: ByteArray, salt: ByteArray, info: ByteArray, length: Int = KEY_BYTES): ByteArray {
        val extract = Mac.getInstance("HmacSHA256")
        extract.init(SecretKeySpec(salt, "HmacSHA256"))
        val prk = extract.doFinal(secret)

        val out = ByteArray(length)
        var block = ByteArray(0)
        var written = 0
        var counter = 1

        while (written < length) {
            val expand = Mac.getInstance("HmacSHA256")
            expand.init(SecretKeySpec(prk, "HmacSHA256"))
            expand.update(block)
            expand.update(info)
            expand.update(counter.toByte())
            block = expand.doFinal()

            val take = minOf(block.size, length - written)
            block.copyInto(out, written, 0, take)
            written += take
            counter++
        }
        return out
    }

    /**
     * Returns (desktopToPhone, phoneToDesktop).
     *
     * Both nonces go into the salt, so neither side alone decides the keys
     * and a recorded session can't be decrypted against a later one.
     */
    fun deriveKeys(
        secretB64: String,
        desktopNonceB64: String,
        phoneNonceB64: String,
    ): Pair<ByteArray, ByteArray> {
        val secret = decode(secretB64) ?: throw SessionException("bad secret")
        val desktopNonce = decode(desktopNonceB64) ?: throw SessionException("bad nonce")
        val phoneNonce = decode(phoneNonceB64) ?: throw SessionException("bad nonce")

        val salt = desktopNonce + phoneNonce
        return Pair(
            hkdf(secret, salt, INFO_DESKTOP),
            hkdf(secret, salt, INFO_PHONE),
        )
    }

    private fun decode(value: String): ByteArray? = runCatching {
        Base64.getDecoder().decode(value)
    }.getOrNull()?.takeIf { it.isNotEmpty() }

    /**
     * Seals and opens AES-256-GCM records.
     *
     * The nonce is the record counter, never random: GCM fails
     * catastrophically on nonce reuse under one key, and a counter cannot
     * collide the way random bits eventually can.
     */
    class RecordCrypto(private val sendKey: ByteArray, private val recvKey: ByteArray) {
        private var sendCounter = 0L
        private var recvCounter = 0L

        private fun nonce(counter: Long): ByteArray {
            val out = ByteArray(NONCE_BYTES)
            for (i in 0 until 8) {
                out[NONCE_BYTES - 1 - i] = ((counter shr (8 * i)) and 0xff).toByte()
            }
            return out
        }

        /** Encrypt one record, length-prefixed and ready for the wire. */
        fun seal(plaintext: ByteArray): ByteArray {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(
                Cipher.ENCRYPT_MODE,
                SecretKeySpec(sendKey, "AES"),
                GCMParameterSpec(TAG_BITS, nonce(sendCounter)),
            )
            sendCounter++
            val sealed = cipher.doFinal(plaintext)

            val out = ByteArray(LENGTH_PREFIX + sealed.size)
            val length = sealed.size
            out[0] = (length ushr 24).toByte()
            out[1] = (length ushr 16).toByte()
            out[2] = (length ushr 8).toByte()
            out[3] = length.toByte()
            sealed.copyInto(out, LENGTH_PREFIX)
            return out
        }

        /**
         * Decrypt one record. The counter advances only on success, so a
         * bad record can't desynchronise the stream — but it does mean the
         * caller must treat a failure as fatal to the session.
         */
        fun open(sealed: ByteArray): ByteArray {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(
                Cipher.DECRYPT_MODE,
                SecretKeySpec(recvKey, "AES"),
                GCMParameterSpec(TAG_BITS, nonce(recvCounter)),
            )
            val plaintext = try {
                cipher.doFinal(sealed)
            } catch (e: Exception) {
                // Wrong key, tampered, or replayed at the wrong position.
                throw SessionException("record failed authentication")
            }
            recvCounter++
            return plaintext
        }
    }

    /**
     * Reads length-prefixed records off a stream.
     *
     * Not newline-delimited like Protocol.Reader: ciphertext contains
     * arbitrary bytes, and a newline inside a record would split it.
     */
    class RecordReader(private val stream: InputStream) {
        private val chunk = ByteArray(8192)
        private var buffer = ByteArray(0)

        /** Blocks until a record arrives. Null when the peer hangs up. */
        fun read(): ByteArray? {
            while (true) {
                takeBufferedRecord()?.let { return it }

                val count = stream.read(chunk)
                if (count == -1) return null
                buffer += chunk.copyOfRange(0, count)
            }
        }

        private fun takeBufferedRecord(): ByteArray? {
            if (buffer.size < LENGTH_PREFIX) return null

            val length = ((buffer[0].toInt() and 0xff) shl 24) or
                ((buffer[1].toInt() and 0xff) shl 16) or
                ((buffer[2].toInt() and 0xff) shl 8) or
                (buffer[3].toInt() and 0xff)

            if (length <= 0 || length > MAX_RECORD_BYTES) {
                buffer = ByteArray(0)
                throw SessionException("record claims $length bytes")
            }

            val end = LENGTH_PREFIX + length
            if (buffer.size < end) return null

            val record = buffer.copyOfRange(LENGTH_PREFIX, end)
            buffer = buffer.copyOfRange(end, buffer.size)
            return record
        }
    }
}
