package dev.fedoralink.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Interoperability with `daemon/fedoralink/session.py`.
 *
 * The vectors and the sealed records below came from the Python side. The
 * last two tests are the ones that matter most: they open ciphertext the
 * desktop actually produced, which is the only way to know the two
 * implementations agree rather than merely look similar.
 */
class LanSessionTest {

    private val secret = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    private val desktopNonce = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8="
    private val phoneNonce = "QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8="

    private val desktopToPhone =
        "19a81cd2420c1d9b7e0d361aaf511396cfed7b54698550c257dc2cad0c544764"
    private val phoneToDesktop =
        "dc369a50aaa308736ddeed644e59bd8741b9a8a3a83509e04e1892f5f1e76ab0"

    private val pingJson = """{"id":1,"type":"fedoralink.ping","body":{}}"""
    private val sealedRecord0 =
        "8d499bbb8152278ae4df5a311f2830e5770426e5a542fccb233f2620b39f522b" +
            "131e265c59c60aa18339bec60c5f12a47226e2814b5be25fc0714a"
    private val sealedRecord1 =
        "db977964e24e4335f2d86c14118554edd91713ee0cd7d55213b6d2c827a8f70f" +
            "50a747e68b07d1b31872813de953f9aa24813b178103a1469306a7"

    private fun hex(s: String) = s.chunked(2).map { it.toInt(16).toByte() }.toByteArray()

    @Test
    fun `hkdf matches rfc5869 test case 1`() {
        val ikm = hex("0b".repeat(22))
        val salt = hex("000102030405060708090a0b0c")
        val info = hex("f0f1f2f3f4f5f6f7f8f9")
        val expected = "3cb25f25faacd57a90434f64d0362f2a" +
            "2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"

        assertEquals(expected, LanSession.hkdf(ikm, salt, info, 42).toHex())
    }

    @Test
    fun `derived keys match the python implementation`() {
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        assertEquals(desktopToPhone, d2p.toHex())
        assertEquals(phoneToDesktop, p2d.toHex())
    }

    @Test
    fun `directions use different keys`() {
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        assertNotEquals(d2p.toHex(), p2d.toHex())
    }

    @Test
    fun `opens a record the desktop sealed`() {
        // The real interop check: ciphertext produced by Python's AESGCM,
        // opened by javax.crypto.
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        val phone = LanSession.RecordCrypto(sendKey = p2d, recvKey = d2p)

        assertEquals(pingJson, String(phone.open(hex(sealedRecord0)), Charsets.UTF_8))
    }

    @Test
    fun `opens consecutive desktop records in order`() {
        // Proves the counter-as-nonce scheme advances identically on both
        // sides — the same plaintext seals differently each time.
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        val phone = LanSession.RecordCrypto(sendKey = p2d, recvKey = d2p)

        assertEquals(pingJson, String(phone.open(hex(sealedRecord0)), Charsets.UTF_8))
        assertEquals(pingJson, String(phone.open(hex(sealedRecord1)), Charsets.UTF_8))
    }

    @Test
    fun `round trips within kotlin`() {
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        val phone = LanSession.RecordCrypto(sendKey = p2d, recvKey = d2p)
        val desktop = LanSession.RecordCrypto(sendKey = d2p, recvKey = p2d)

        val sealed = phone.seal("hello".toByteArray())
        assertEquals("hello", String(desktop.open(sealed.drop(4).toByteArray())))
    }

    @Test
    fun `length prefix matches the payload`() {
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        val sealed = LanSession.RecordCrypto(p2d, d2p).seal("x".toByteArray())
        val length = ((sealed[0].toInt() and 0xff) shl 24) or
            ((sealed[1].toInt() and 0xff) shl 16) or
            ((sealed[2].toInt() and 0xff) shl 8) or
            (sealed[3].toInt() and 0xff)
        assertEquals(sealed.size - 4, length)
    }

    @Test(expected = LanSession.SessionException::class)
    fun `tampered record is rejected`() {
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        val phone = LanSession.RecordCrypto(sendKey = p2d, recvKey = d2p)
        val tampered = hex(sealedRecord0)
        tampered[tampered.size - 1] = (tampered[tampered.size - 1].toInt() xor 1).toByte()
        phone.open(tampered)
    }

    @Test(expected = LanSession.SessionException::class)
    fun `replayed record is rejected`() {
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        val phone = LanSession.RecordCrypto(sendKey = p2d, recvKey = d2p)
        phone.open(hex(sealedRecord0))
        phone.open(hex(sealedRecord0))
    }

    @Test
    fun `reader reassembles records split across reads`() {
        val (d2p, p2d) = LanSession.deriveKeys(secret, desktopNonce, phoneNonce)
        val desktop = LanSession.RecordCrypto(sendKey = d2p, recvKey = p2d)
        val wire = desktop.seal("one".toByteArray()) + desktop.seal("two".toByteArray())

        // One byte at a time, so every boundary is crossed mid-record.
        val stream = object : java.io.InputStream() {
            private val inner = java.io.ByteArrayInputStream(wire)
            override fun read(): Int = inner.read()
            override fun read(b: ByteArray, off: Int, len: Int): Int =
                inner.read(b, off, 1)
        }

        val reader = LanSession.RecordReader(stream)
        val phone = LanSession.RecordCrypto(sendKey = p2d, recvKey = d2p)
        assertEquals("one", String(phone.open(reader.read()!!)))
        assertEquals("two", String(phone.open(reader.read()!!)))
        assertNull(reader.read())
    }

    @Test(expected = LanSession.SessionException::class)
    fun `an absurd length prefix is rejected`() {
        val stream = java.io.ByteArrayInputStream(byteArrayOf(0x7f, -1, -1, -1))
        LanSession.RecordReader(stream).read()
    }

    private fun ByteArray.toHex() = joinToString("") { "%02x".format(it) }
}
