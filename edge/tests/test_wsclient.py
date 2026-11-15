# edge/tests/test_wsclient.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   The agent's tiny WebSocket client (edge/wsclient.py): reading messages that the server split into several
#   frames, answering pings, and closing, using a scripted stand-in for the network (no sockets needed).
# ---------------------------------------------------------------------------
import struct
import unittest

import helpers  # noqa: F401  (makes edge/ importable)
from wsclient import WS, WSClosed


# Builds a server frame (unmasked): fin bit, opcode, payload.
def frame(op, payload, fin=True):
    head = bytes([(0x80 if fin else 0) | op])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


# A WS object whose network is a byte string we control; anything it "sends" is recorded.
def scripted(*frames):
    ws = WS.__new__(WS)
    ws._incoming = bytearray(b"".join(frames))
    ws.sent = []

    def read(n):
        if len(ws._incoming) < n:
            raise WSClosed("closed")
        out = bytes(ws._incoming[:n])
        del ws._incoming[:n]
        return out
    ws._read = read
    ws._send = lambda op, payload: ws.sent.append((op, payload))
    return ws


class Receiving(unittest.TestCase):
    def test_a_single_frame_message(self):
        self.assertEqual("hello", scripted(frame(1, b"hello")).recv_text())

    def test_a_message_split_into_several_frames_is_joined(self):
        ws = scripted(frame(1, b"abc", fin=False), frame(0, b"def", fin=False), frame(0, b"ghi", fin=True))
        self.assertEqual("abcdefghi", ws.recv_text())

    def test_a_large_message_in_4096_byte_pieces_arrives_whole(self):
        text = '{"t":"content","url":"/apps/launcher.html?' + "x" * 900_000 + '"}'
        raw = text.encode()
        pieces = [raw[i:i + 4096] for i in range(0, len(raw), 4096)]
        frames = [frame(1 if i == 0 else 0, p, fin=(i == len(pieces) - 1)) for i, p in enumerate(pieces)]
        self.assertEqual(text, scripted(*frames).recv_text())

    def test_two_messages_in_a_row(self):
        ws = scripted(frame(1, b"one"), frame(1, b"two"))
        self.assertEqual(["one", "two"], [ws.recv_text(), ws.recv_text()])

    def test_a_ping_between_fragments_is_answered_and_does_not_break_the_message(self):
        ws = scripted(frame(1, b"ab", fin=False), frame(9, b"are-you-there"), frame(0, b"cd", fin=True))
        self.assertEqual("abcd", ws.recv_text())
        self.assertEqual([(0xA, b"are-you-there")], ws.sent)

    def test_lengths_that_need_two_and_eight_byte_fields(self):
        for size in (125, 126, 65535, 65536):
            with self.subTest(size=size):
                self.assertEqual("y" * size, scripted(frame(1, b"y" * size)).recv_text())

    def test_utf8_text_split_in_the_middle_of_a_character_still_decodes(self):
        raw = "안녕하세요 hand".encode("utf-8")
        ws = scripted(frame(1, raw[:4], fin=False), frame(0, raw[4:], fin=True))
        self.assertEqual("안녕하세요 hand", ws.recv_text())

    def test_pong_and_binary_frames_are_skipped(self):
        ws = scripted(frame(10, b"x"), frame(2, b"\x00\x01"), frame(1, b"after"))
        self.assertEqual("after", ws.recv_text())

    def test_server_close_raises(self):
        with self.assertRaises(WSClosed):
            scripted(frame(8, b"")).recv_text()

    def test_a_connection_that_ends_mid_frame_raises(self):
        with self.assertRaises(WSClosed):
            scripted(frame(1, b"abcdef")[:4]).recv_text()


if __name__ == "__main__":
    unittest.main()
