package dev.fedoralink.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Cross-language contract with `daemon/fedoralink/auth.py`.
 *
 * The vectors below were produced by the Python implementation. They are
 * the point of this file: the two sides have to agree byte-for-byte, and
 * "looks equivalent" is not the same as "produces the same digits". If a
 * change here turns these red, the link stops authenticating.
 */
class CryptoTest {

    private val nonce = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8="

    private val zeros = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    private val ones = "//////////////////////////////////////////8="
    private val counting = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

    @Test
    fun `fingerprint matches the python implementation`() {
        assertEquals("709175", Crypto.fingerprint(zeros))
        assertEquals("210655", Crypto.fingerprint(ones))
        assertEquals("591654", Crypto.fingerprint(counting))
    }

    @Test
    fun `hmac matches the python implementation`() {
        assertEquals(
            "9724f70a32773a1840904e49bb789f73e0023efea2db9f987f43fc932319b1cb",
            Crypto.respond(zeros, nonce),
        )
        assertEquals(
            "561f147804a6370506db9ed54ec0083540b5ad50f37c06aea2f99b27d6a4325f",
            Crypto.respond(ones, nonce),
        )
        assertEquals(
            "62215de7bddcea7e2c4047ff6bb94f8d18262fc8b3f3648134bb7d44158ff84d",
            Crypto.respond(counting, nonce),
        )
    }

    @Test
    fun `fingerprint is always six digits`() {
        repeat(200) {
            val fp = Crypto.fingerprint(Crypto.newNonce())!!
            assertEquals(6, fp.length)
            assertTrue(fp.all { c -> c.isDigit() })
        }
    }

    @Test
    fun `nonces are unique`() {
        assertEquals(100, (1..100).map { Crypto.newNonce() }.toSet().size)
    }

    @Test
    fun `verify accepts a correct response`() {
        assertTrue(Crypto.verify(counting, nonce, Crypto.respond(counting, nonce)))
    }

    @Test
    fun `verify rejects the wrong secret`() {
        assertFalse(Crypto.verify(zeros, nonce, Crypto.respond(ones, nonce)))
    }

    @Test
    fun `verify rejects a response to another nonce`() {
        val stale = Crypto.respond(counting, Crypto.newNonce())
        assertFalse(Crypto.verify(counting, nonce, stale))
    }

    @Test
    fun `verify never throws on hostile input`() {
        // A peer that hasn't authenticated is untrusted by definition, so
        // each of these is a failed authentication, not a crash.
        assertFalse(Crypto.verify(counting, nonce, null))
        assertFalse(Crypto.verify(counting, nonce, ""))
        assertFalse(Crypto.verify(counting, nonce, "not a mac"))
        assertFalse(Crypto.verify(counting, nonce, "a".repeat(64)))
        assertFalse(Crypto.verify("!!!not base64!!!", nonce, "abcd"))
        assertFalse(Crypto.verify(counting, "!!!not base64!!!", "abcd"))
    }

    @Test
    fun `malformed input yields null rather than throwing`() {
        assertNull(Crypto.fingerprint("!!! not base64 !!!"))
        assertNull(Crypto.fingerprint(""))
        assertNull(Crypto.respond("", nonce))
        assertNull(Crypto.respond(counting, ""))
    }
}
