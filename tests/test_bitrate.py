import os, sys, unittest
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_playback_state  # noqa: F401  (stubs env like other tests)
from musicbot import audio
from musicbot.config import ydl_opts, ydl_opts_single


def vc(bitrate, limit):
    return NS(channel=NS(bitrate=bitrate, guild=NS(bitrate_limit=limit)))


class BitrateTest(unittest.TestCase):
    def test_uses_channel_bitrate(self):
        self.assertEqual(audio.target_bitrate(vc(256000, 384000)), 256)

    def test_capped_by_boost_tier(self):
        self.assertEqual(audio.target_bitrate(vc(384000, 96000)), 96)

    def test_capped_at_510(self):
        self.assertEqual(audio.target_bitrate(vc(800000, 0)), 510)

    def test_fallback_when_unknown(self):
        self.assertEqual(audio.target_bitrate(NS(channel=None)), audio.DEFAULT_BITRATE_KBPS)
        self.assertEqual(audio.target_bitrate(vc(0, 0)), audio.DEFAULT_BITRATE_KBPS)

    def test_format_prefers_opus(self):
        for o in (ydl_opts, ydl_opts_single):
            self.assertTrue(o["format"].startswith("bestaudio[acodec=opus]"))
            self.assertTrue(o["format"].endswith("/best"))


if __name__ == "__main__":
    unittest.main()