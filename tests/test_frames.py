#!/usr/bin/env python3
"""Protocol-level tests: framing, CRC and request coercion.

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import powerstation_control as pc
from powerstation_bridge import as_bool


def frame(cmd, data, serial="0001", encoding="base2"):
    return pc.send_data_conversion(serial, cmd, data, encoding).encode()


class NextFrameTests(unittest.TestCase):
    def test_single_frame(self):
        f = frame("93", "5A0000")
        text, rest = pc._next_frame(f)
        self.assertEqual(text, f.decode())
        self.assertEqual(rest, b"")

    def test_two_frames_in_one_read(self):
        f1, f2 = frame("61", "55"), frame("93", "5A0000", serial="0002")
        text, rest = pc._next_frame(f1 + f2)
        self.assertEqual(text, f1.decode())
        text2, rest2 = pc._next_frame(rest)
        self.assertEqual(text2, f2.decode())
        self.assertEqual(rest2, b"")

    def test_partial_frame_then_remainder(self):
        f = frame("93", "5A" * 200)
        text, rest = pc._next_frame(f[:40])
        self.assertIsNone(text)
        self.assertEqual(rest, f[:40])
        text, rest = pc._next_frame(rest + f[40:])
        self.assertEqual(text, f.decode())
        self.assertEqual(rest, b"")

    def test_garbage_prefix_is_skipped(self):
        f = frame("93", "5A0000")
        text, rest = pc._next_frame(b"\x00\xff junk " + f)
        self.assertEqual(text, f.decode())
        self.assertEqual(rest, b"")

    def test_bytes_without_a_frame_start_are_trimmed(self):
        # Everything except a possible split frame start is dropped, so a
        # header straddling two reads is not lost.
        text, rest = pc._next_frame(b"no frame here at all")
        self.assertIsNone(text)
        self.assertLessEqual(len(rest), 3)
        text, rest = pc._next_frame(rest + frame("93", "5A0000"))
        self.assertIsNotNone(text)

    def test_payload_containing_the_terminator_survives(self):
        # 0xCC,0x33 inside the data field renders as the literal terminator
        payload = "AA" + "CC33" + "BB" * 20
        f = frame("93", payload)
        self.assertIn(b"CC33", f[16:-4])  # the trap really is present
        text, rest = pc._next_frame(f)
        self.assertIsNotNone(text)
        self.assertEqual(text, f.decode())
        self.assertTrue(pc._frame_crc_ok(text))
        self.assertEqual(pc.parse_response_frame(text)[1], payload)
        self.assertEqual(rest, b"")

    def test_length_and_terminator_disagreement_falls_back(self):
        # A frame whose length field is understated (as the vendor does for
        # ASCII payloads) must still be recovered by the terminator scan.
        f = frame("30", "MyNet", encoding="ascii")  # vendor writes 11 + hexchars
        self.assertNotEqual(int(f[4:8], 16), len(f) // 2)  # length is overstated
        text, rest = pc._next_frame(f)
        self.assertIsNotNone(text)
        self.assertEqual(text, f.decode())
        self.assertTrue(pc._frame_crc_ok(text))


class FrameBuilderTests(unittest.TestCase):
    def test_crc_matches_reference_implementation(self):
        # Independent CRC over the same polynomial, not the module's table.
        def crc(hex_str):
            c = 0
            for i in range(0, len(hex_str), 2):
                b = int(hex_str[i:i + 2], 16)
                c ^= b
                for _ in range(8):
                    c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
            return format(c, "04X")

        f = pc.send_data_conversion("0001", "12", "80")
        self.assertEqual(f[-8:-4], crc(f[:-8]))
        self.assertTrue(pc._frame_crc_ok(f))

    def test_base2_length_is_the_whole_frame(self):
        f = pc.send_data_conversion("0001", "12", "80")
        self.assertEqual(int(f[4:8], 16), len(f) // 2)

    def test_serial_wraps(self):
        self.assertEqual(pc.auto_increment_serial("0001"), "0002")
        self.assertEqual(pc.auto_increment_serial("FFFF"), "0000")


class AsBoolTests(unittest.TestCase):
    def test_true_spellings(self):
        for v in (True, 1, 2, -1, "true", "TRUE", "on", "1", "yes", "enabled", " On "):
            self.assertTrue(as_bool(v), v)

    def test_false_spellings(self):
        for v in (False, 0, "false", "FALSE", "off", "0", "no", "", "disabled", None):
            self.assertFalse(as_bool(v), v)

    def test_missing_key_defaults_to_off(self):
        self.assertFalse(as_bool({}.get("on")))

    def test_unparsable_strings_do_not_enable(self):
        for v in ("maybe", "off ", "2x"):
            self.assertFalse(as_bool(v), v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
