package dev.fedoralink.android

import java.io.ByteArrayInputStream
import java.io.InputStream
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Framing tests for the phone's reader.
 *
 * This is the half of the protocol that keeps the multibyte guarantee
 * through explicit newline-boundary logic rather than structurally — the
 * Python reader buffers bytes and decodes whole lines, so it cannot get
 * this wrong, and this one can. Hence the tests.
 */
class ProtocolReaderTest {

    /** Hands out `count` bytes at a time, like a real socket read. */
    private class ChunkedStream(data: ByteArray, private val count: Int) : InputStream() {
        private val inner = ByteArrayInputStream(data)

        override fun read(): Int = inner.read()

        override fun read(b: ByteArray, off: Int, len: Int): Int =
            inner.read(b, off, minOf(len, count))
    }

    private fun line(type: String, body: String = "{}"): ByteArray =
        """{"id":1,"type":"$type","body":$body}""".plus("\n").toByteArray(Charsets.UTF_8)

    private fun reader(data: ByteArray, chunk: Int = 4096) =
        Protocol.Reader(ChunkedStream(data, chunk))

    @Test
    fun `reads a whole packet`() {
        val packet = reader(line("fedoralink.ping")).read()
        assertEquals("fedoralink.ping", packet?.optString("type"))
    }

    @Test
    fun `reads several packets in order`() {
        val data = line("fedoralink.battery", """{"level":1}""") +
            line("fedoralink.battery", """{"level":2}""")
        val r = reader(data)
        assertEquals(1, r.read()?.getJSONObject("body")?.getInt("level"))
        assertEquals(2, r.read()?.getJSONObject("body")?.getInt("level"))
    }

    @Test
    fun `reassembles a packet delivered one byte at a time`() {
        val r = reader(line("fedoralink.ping", """{"ring":true}"""), chunk = 1)
        assertEquals(true, r.read()?.getJSONObject("body")?.getBoolean("ring"))
    }

    @Test
    fun `a multibyte character split across reads does not mojibake`() {
        val thai = "ทดสอบการแจ้งเตือน"
        val data = line("fedoralink.notification", """{"text":"$thai"}""")

        // One byte at a time guarantees a cut inside a 3-byte sequence.
        val r = reader(data, chunk = 1)
        val text = r.read()?.getJSONObject("body")?.getString("text")

        assertEquals(thai, text)
        assertEquals(false, text?.contains('�'))
    }

    @Test
    fun `an emoji split across reads survives`() {
        val withEmoji = "ระบบ 🔔 พร้อม"
        val data = line("fedoralink.notification", """{"text":"$withEmoji"}""")
        val r = reader(data, chunk = 1)
        assertEquals(withEmoji, r.read()?.getJSONObject("body")?.getString("text"))
    }

    @Test
    fun `null is returned when the peer hangs up`() {
        assertNull(reader(ByteArray(0)).read())
    }

    @Test
    fun `blank lines are skipped`() {
        val data = "\n\n".toByteArray() + line("fedoralink.ping")
        assertEquals("fedoralink.ping", reader(data).read()?.optString("type"))
    }

    @Test
    fun `a malformed packet is skipped and the stream survives`() {
        // Framing is intact, so the next packet must still arrive — this is
        // why one bad packet doesn't drop the socket.
        val data = "{not json}\n".toByteArray() + line("fedoralink.ping")
        assertEquals("fedoralink.ping", reader(data).read()?.optString("type"))
    }

    @Test
    fun `a packet without a type is skipped`() {
        val data = """{"body":{}}""".plus("\n").toByteArray() + line("fedoralink.ping")
        assertEquals("fedoralink.ping", reader(data).read()?.optString("type"))
    }

    @Test
    fun `a missing body is filled in`() {
        val data = """{"type":"fedoralink.ping"}""".plus("\n").toByteArray()
        assertEquals(0, reader(data).read()?.getJSONObject("body")?.length())
    }

    @Test
    fun `an oversize line does not wedge the reader`() {
        // Over the 512 KiB cap with no newline: the reader must drop it and
        // keep going rather than grow without bound or stop reading.
        val flood = ByteArray(600 * 1024) { 'x'.code.toByte() }
        val data = flood + "\n".toByteArray() + line("fedoralink.ping")
        assertEquals("fedoralink.ping", reader(data, chunk = 8192).read()?.optString("type"))
    }
}
