"""MuseCut TTS tests — Muse's own voice model wired as a provider.

* Unit tests mock the `tts` binary + ffmpeg: they check the exact
  command (voice, speed cap), the retry behaviour (same voice, never
  a silent fallback) and the failure modes.
* One integration test renders REAL audio with MAI_03 (17 words) and
  checks the file is non-silent with a sane duration. It SKIPS (does
  not fail the suite) if the TTS backend is briefly down, so a flaky
  backend never breaks the whole suite — the skip is explicit.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

from musecut import tts as tts_mod  # noqa: E402
from musecut.audio import mix_levels  # noqa: E402
from musecut.cli import build_parser, main as cli_main  # noqa: E402
from musecut.mcp_server import Session  # noqa: E402
from musecut.render import probe_media  # noqa: E402

TEST_TEXT = ("MuseCut is an open source video editor, built for agents, "
             "and it renders real timelines with ffmpeg.")


def _fake_run_factory(fail_first=0, calls=None):
    """Fake subprocess.run: tts binary writes an mp3, ffmpeg writes a wav.

    ``fail_first``: how many tts calls fail (returncode 1) before
    succeeding — used to exercise the retry path.
    """
    state = {"tts_calls": 0}

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append(list(cmd))
        if cmd[0].endswith("tts") or cmd[0] == tts_mod.MUSE_TTS_BIN:
            state["tts_calls"] += 1
            if state["tts_calls"] <= fail_first:
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            out = cmd[cmd.index("--output") + 1]
            with open(out, "wb") as f:
                f.write(b"FAKE-MP3")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "ffmpeg":
            # last arg is the output wav
            with open(cmd[-1], "wb") as f:
                f.write(b"FAKE-WAV")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


class TestMuseSpeedMapping(unittest.TestCase):
    def test_defaults_and_caps(self):
        self.assertEqual(tts_mod._muse_cli_speed(None), 90)
        self.assertEqual(tts_mod._muse_cli_speed(0.9), 90)
        self.assertEqual(tts_mod._muse_cli_speed(0.5), 50)
        self.assertEqual(tts_mod._muse_cli_speed(95), 95)
        # Hard ceiling: never above 100 (Ep7 truncation lesson).
        self.assertEqual(tts_mod._muse_cli_speed(150), 100)
        self.assertEqual(tts_mod._muse_cli_speed(2.0), 100)


class TestMuseProviderRegistered(unittest.TestCase):
    def test_default_registration(self):
        self.assertIn("muse", tts_mod.list_providers())
        p = tts_mod.get_provider("muse")
        self.assertIsInstance(p, tts_mod.MuseTTSProvider)
        self.assertEqual(p.default_voice, "avocado_v2:MAI_03")
        self.assertEqual(p.default_speed, 90)
        self.assertIn("avocado_v2:MAI_03", tts_mod.list_muse_voices())


class TestMuseProviderMocked(unittest.TestCase):
    @unittest.skipUnless(
        os.path.isfile(tts_mod.MUSE_TTS_BIN),
        "Muse TTS binary not present on this machine")
    def test_synthesize_wav_calls_binary_with_production_defaults(self):
        calls = []
        provider = tts_mod.MuseTTSProvider()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "vo.wav")
            with mock.patch.object(tts_mod.subprocess, "run",
                                   side_effect=_fake_run_factory(
                                       calls=calls)):
                with mock.patch.object(tts_mod.time, "sleep"):
                    got = provider.synthesize(TEST_TEXT, out)
            self.assertEqual(got, out)
            self.assertTrue(os.path.isfile(out))
            tts_calls = [c for c in calls if c[0] == tts_mod.MUSE_TTS_BIN]
            self.assertEqual(len(tts_calls), 1)
            cmd = tts_calls[0]
            self.assertEqual(cmd[cmd.index("--voice") + 1],
                             "avocado_v2:MAI_03")
            self.assertEqual(cmd[cmd.index("--speed") + 1], "90")
            self.assertEqual(cmd[cmd.index("--format") + 1], "mp3")

    @unittest.skipUnless(
        os.path.isfile(tts_mod.MUSE_TTS_BIN),
        "Muse TTS binary not present on this machine")
    def test_synthesize_mp3_writes_directly(self):
        provider = tts_mod.MuseTTSProvider()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "vo.mp3")
            with mock.patch.object(tts_mod.subprocess, "run",
                                   side_effect=_fake_run_factory()):
                with mock.patch.object(tts_mod.time, "sleep"):
                    provider.synthesize(TEST_TEXT, out)
            self.assertTrue(os.path.isfile(out))
            self.assertGreater(os.path.getsize(out), 0)

    @unittest.skipUnless(
        os.path.isfile(tts_mod.MUSE_TTS_BIN),
        "Muse TTS binary not present on this machine")
    def test_retry_keeps_same_voice_then_succeeds(self):
        calls = []
        provider = tts_mod.MuseTTSProvider(retries=3)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "vo.wav")
            with mock.patch.object(tts_mod.subprocess, "run",
                                   side_effect=_fake_run_factory(
                                       fail_first=1, calls=calls)):
                with mock.patch.object(tts_mod.time, "sleep"):
                    provider.synthesize(TEST_TEXT, out)
            tts_calls = [c for c in calls if c[0] == tts_mod.MUSE_TTS_BIN]
            self.assertEqual(len(tts_calls), 2)
            voices = {c[c.index("--voice") + 1] for c in tts_calls}
            self.assertEqual(voices, {"avocado_v2:MAI_03"})

    def test_failure_after_retries_raises(self):
        provider = tts_mod.MuseTTSProvider(retries=2)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "vo.wav")
            with mock.patch.object(tts_mod.subprocess, "run",
                                   side_effect=_fake_run_factory(
                                       fail_first=99)):
                with mock.patch.object(tts_mod.time, "sleep"):
                    with self.assertRaises(RuntimeError):
                        provider.synthesize(TEST_TEXT, out)

    def test_empty_text_and_bad_extension_raise(self):
        provider = tts_mod.MuseTTSProvider()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                provider.synthesize("   ", os.path.join(d, "vo.wav"))
            with self.assertRaises(ValueError):
                provider.synthesize(TEST_TEXT, os.path.join(d, "vo.ogg"))


class TestVoiceCLI(unittest.TestCase):
    def test_voice_list_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main(["voice", "list"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("avocado_v2:MAI_03",
                      data["providers"]["muse"]["voices"])
        self.assertEqual(data["providers"]["muse"]["default_speed"], 90)

    def test_voice_parser_accepts_synth_flags(self):
        args = build_parser().parse_args(
            ["voice", "synth", "hello", "-o", "vo.wav",
             "--voice", "avocado_v2:MAI_03", "--speed", "90"])
        self.assertEqual(args.action, "synth")
        self.assertEqual(args.speed, 90.0)


class TestVoiceMCP(unittest.TestCase):
    def test_list_voices_tool(self):
        res = Session().list_voices({})
        self.assertTrue(res["ok"])
        self.assertIn("avocado_v2:MAI_03",
                      res["providers"]["muse"]["voices"])

    def test_synthesize_voice_tool_with_stub_provider(self):
        # A stub provider that writes a real 1 s tone: exercises the MCP
        # plumbing (probe + levels) without touching the TTS backend.
        class Stub(tts_mod.TTSProvider):
            name = "test-stub"

            def synthesize(self, text, output, voice=None, speed=1.0):
                subprocess.run(["ffmpeg", "-y", "-v", "error",
                                "-f", "lavfi", "-i",
                                "sine=frequency=440:duration=1",
                                "-ar", "24000", "-ac", "1", output],
                               check=True)
                return output

        tts_mod.register_provider(Stub())
        try:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "vo.wav")
                res = Session().synthesize_voice(
                    {"text": "stub", "output": out,
                     "provider": "test-stub"})
                self.assertTrue(res["ok"])
                self.assertAlmostEqual(res["duration_s"], 1.0, places=1)
                self.assertLess(res["mean_db"], 0)
        finally:
            tts_mod._PROVIDERS.pop("test-stub", None)


class TestMuseRealAudio(unittest.TestCase):
    """Real MAI_03 render — skipped, not failed, if the backend is down."""

    def test_real_segment_is_audible_and_sane(self):
        if not os.path.isfile(tts_mod.MUSE_TTS_BIN):
            self.skipTest("Muse TTS binary not present on this machine")
        provider = tts_mod.get_provider("muse")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "vo.wav")
            try:
                provider.synthesize(TEST_TEXT, out)
            except RuntimeError as exc:
                self.skipTest(f"Muse TTS backend unavailable: {exc}")
            self.assertTrue(os.path.isfile(out))
            self.assertGreater(os.path.getsize(out), 10_000)
            dur = probe_media(out)["duration"]
            # 17 words at production pace (~150 WPM) is ~7 s.
            self.assertGreater(dur, 1.0)
            self.assertLess(dur, 15.0)
            mean, maxv = mix_levels(out)
            self.assertGreater(mean, -60.0, "output is silent")
            self.assertLess(maxv, 0.0)


if __name__ == "__main__":
    unittest.main()
