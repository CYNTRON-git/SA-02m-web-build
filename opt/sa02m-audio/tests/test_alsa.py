"""Card resolution from /proc/asound (contract §Устройство захвата)."""

from __future__ import annotations

import os
import tempfile
import unittest

from sa02m_audio import alsa

from .fakes import BENCH_CARDS


class AlsaTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = self.td.name

    def cards(self, text, capture=()):
        with open(os.path.join(self.dir, "cards"), "w", encoding="utf-8") as fh:
            fh.write(text)
        for index in capture:
            os.makedirs(os.path.join(self.dir, "card%d" % index, "pcm0c"), exist_ok=True)

    def test_bench_fifine_line(self):
        parsed = alsa.parse_cards(BENCH_CARDS)
        self.assertEqual([(c.index, c.id, c.driver) for c in parsed],
                         [(0, "Codec", "H3_Audio_Codec"), (1, "Microphone", "USB-Audio")])
        self.cards(BENCH_CARDS, capture=(0, 1))
        device, card = alsa.resolve("auto", self.dir)
        self.assertEqual(device, "plughw:CARD=Microphone,DEV=0")
        self.assertEqual(card.index, 1)

    def test_no_usb_card_is_no_mic(self):
        self.cards(" 0 [Codec          ]: H3_Audio_Codec - H3 Audio Codec\n"
                   "                      H3 Audio Codec\n", capture=(0,))
        self.assertEqual(alsa.resolve("auto", self.dir), (None, None))

    def test_usb_card_without_capture_pcm_is_skipped(self):
        self.cards(" 0 [Codec          ]: H3_Audio_Codec - H3 Audio Codec\n"
                   " 1 [Speaker        ]: USB-Audio - USB Speaker\n"
                   " 2 [Microphone     ]: USB-Audio - Fifine Microphone\n", capture=(0, 2))
        os.makedirs(os.path.join(self.dir, "card1", "pcm0p"))
        device, card = alsa.resolve("auto", self.dir)
        self.assertEqual((device, card.index), ("plughw:CARD=Microphone,DEV=0", 2))

    def test_two_usb_mics_first_wins(self):
        self.cards(" 1 [Microphone     ]: USB-Audio - Fifine Microphone\n"
                   " 2 [Microphone_1   ]: USB-Audio - Fifine Microphone\n", capture=(1, 2))
        self.assertEqual(alsa.resolve("auto", self.dir)[0], "plughw:CARD=Microphone,DEV=0")

    def test_index_other_than_one_after_a_replug(self):
        self.cards(" 0 [Codec          ]: H3_Audio_Codec - H3 Audio Codec\n"
                   " 3 [Microphone     ]: USB-Audio - Fifine Microphone\n", capture=(3,))
        device, card = alsa.resolve("auto", self.dir)
        self.assertEqual((device, card.index), ("plughw:CARD=Microphone,DEV=0", 3))

    def test_id_outside_the_alsa_grammar_falls_back_to_the_index(self):
        self.cards(" 4 [Mic-Pro        ]: USB-Audio - Something\n", capture=(4,))
        self.assertEqual(alsa.resolve("auto", self.dir)[0], "plughw:4,0")

    def test_explicit_device_needs_some_capture_card(self):
        self.cards(BENCH_CARDS, capture=(1,))
        self.assertEqual(alsa.resolve("hw:1,0", self.dir)[0], "hw:1,0")
        self.cards(BENCH_CARDS)
        os.rmdir(os.path.join(self.dir, "card1", "pcm0c"))
        self.assertEqual(alsa.resolve("hw:1,0", self.dir), (None, None))

    def test_missing_proc_file_is_no_mic(self):
        self.assertEqual(alsa.resolve("auto", os.path.join(self.dir, "nope")), (None, None))


if __name__ == "__main__":
    unittest.main()
