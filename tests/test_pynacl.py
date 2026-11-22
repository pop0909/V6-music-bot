"""PyNaCl >= 1.6.2（1.5.0 有已知漏洞）：requirements 要求新版，且 discord.py 的語音加密照樣能用
執行：python -m unittest discover -s tests
"""
import os
import re
import struct
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


class PyNaClTest(unittest.TestCase):
    def test_requirements_pin(self):
        with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
            req = f.read()
        m = re.search(r"^PyNaCl>=([\d.]+)", req, re.M | re.I)
        self.assertIsNotNone(m, "requirements.txt 要寫 PyNaCl>=1.6.2")
        self.assertGreaterEqual(_ver(m.group(1)), (1, 6, 2))

    def test_installed_version(self):
        import nacl
        self.assertGreaterEqual(_ver(nacl.__version__), (1, 6, 2),
                                "請 pip install -r requirements.txt 升級 PyNaCl")

    def test_discord_voice_encrypt_roundtrip(self):
        """用 discord.py 自己的語音封包加密（Discord 現行模式 aead_xchacha20_poly1305_rtpsize），再用 PyNaCl 解回來"""
        import discord
        import nacl.secret
        from discord import voice_client
        self.assertTrue(voice_client.has_nacl)
        self.assertIn("aead_xchacha20_poly1305_rtpsize", discord.VoiceClient.supported_modes)
        key = bytes(range(32))

        class VC(discord.VoiceClient):          # 不連線：只把連線後才有的值換成固定值
            secret_key = list(key)
            mode = "aead_xchacha20_poly1305_rtpsize"
            ssrc = 12345
        vc = object.__new__(VC)
        vc._connection = type("C", (), {"dave_session": None, "can_encrypt": False})()   # 沒有 DAVE 端對端加密
        vc._incr_nonce = 7
        vc.sequence, vc.timestamp = 1, 960
        opus = b"\xfc\xff\xfe" * 20
        pkt = vc._get_voice_packet(opus)
        pkt = bytes(pkt)
        header, body, n4 = pkt[:12], pkt[12:-4], pkt[-4:]
        self.assertEqual(struct.unpack(">I", n4)[0], 7)
        self.assertEqual(struct.unpack_from(">I", header, 8)[0], 12345)
        nonce = n4 + bytes(20)
        self.assertEqual(nacl.secret.Aead(key).decrypt(body, header, nonce), opus)
        self.assertEqual(vc._incr_nonce, 8)


if __name__ == "__main__":
    unittest.main()
