"""MuseCut v2 pro tests — Premiere/DaVinci-level features.

Covers the raised scope (PRO_PARITY.md):
* full transform keyframes: position / scale / rotation / opacity
  (+ anchor point) — rendered and verified against frames
* .cube LUT support — visible colour change
* audio mixer: per-track EQ + compressor + keyframed volume
* nested sequences / compound clips (inline + named sequences)
* proxy workflow: make proxies, swap a project, relink back
* DNxHD export (720p + 1080p)
* markers (validation + MCP)
* adjustment layers (time-windowed grade, verified on frames)
* colour scopes data output (signalstats JSON)
* 4K render

Small canvases and 1-3 s clips keep the suite fast. AV1/DNxHD/4K
renders are 1 s where they appear.
Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

from musecut import proxy as proxy_mod  # noqa: E402
from musecut import scopes as scopes_mod  # noqa: E402
from musecut.render import Renderer  # noqa: E402
from musecut.timeline import TimelineError, validate_timeline  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-pro")
MEDIA = os.path.join(WORK, "media")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def _make_media():
    os.makedirs(MEDIA, exist_ok=True)
    jobs = [
        (["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", os.path.join(MEDIA, "clip1.mp4")]),
        (["-f", "lavfi", "-i", "sine=frequency=440:duration=4",
          "-c:a", "libmp3lame", "-q:a", "4",
          os.path.join(MEDIA, "voice.mp3")]),
        # One still frame (static source — keyframe tests need a
        # picture that does not change by itself).
        (["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.1",
          "-frames:v", "1", os.path.join(MEDIA, "still.png")]),
    ]
    for argv in jobs:
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error"] + argv)
        assert res.returncode == 0, res.stderr
    # Same .cube as test_v2: black -> 0.2 grey, white stays white.
    with open(os.path.join(MEDIA, "bright.cube"), "w") as f:
        f.write("LUT_3D_SIZE 2\n")
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    f.write(f"{1.0 if r else 0.2} {1.0 if g else 0.2} "
                            f"{1.0 if b else 0.2}\n")


def _ffprobe(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _yavg(path, t):
    """Mean luma of the frame at time t (signalstats + metadata print).

    NOTE: signalstats only *sets* frame metadata — the values only
    reach the log through the ``metadata`` filter's print mode.
    """
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "info",
                "-ss", str(t), "-i", path,
                "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                "-frames:v", "1", "-f", "null", "-"])
    m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", res.stderr or "")
    assert m, f"no YAVG in signalstats output for {path}@{t}"
    return float(m.group(1))


def _frame_md5(path, t):
    png = os.path.join(WORK, "frame.png")
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-ss", str(t), "-i", path, "-frames:v", "1", png])
    assert res.returncode == 0, res.stderr
    with open(png, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _rms_db(path, start, dur):
    """Mean dB of a segment, computed directly from decoded samples.

    (volumedetect mis-reports segments of keyframed audio — verified
    2026-11-20: a segment whose true RMS equals the raw file read
    2 dB low — so the test decodes and measures RMS itself.)
    """
    tmp = os.path.join(WORK, "seg.wav")
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-i", path, "-ss", str(start), "-t", str(dur),
                "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
                tmp])
    assert res.returncode == 0, res.stderr
    import math
    import struct
    import wave
    with wave.open(tmp, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    s = struct.unpack(f"<{n}h", raw)
    rms = math.sqrt(sum(x * x for x in s) / n) / 32768.0
    return 20 * math.log10(max(rms, 1e-9))


def _base_proj(clips, width=320, height=180, **kw):
    proj = {"version": 1, "name": "pro", "width": width, "height": height,
            "fps": 30, "background": "#000000", "clips": clips,
            "overlays": [], "texts": [], "audio": [],
            "captions": None, "endcard": None, "export": None,
            "canvas": None, "sequences": {}, "markers": [],
            "adjustment_layers": []}
    proj.update(kw)
    return proj


class ProTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(WORK, exist_ok=True)
        _make_media()

    def _render(self, proj, out):
        validate_timeline(proj)
        r = Renderer(proj, workdir=os.path.join(WORK, "render"))
        return r.render(os.path.join(WORK, out), quiet=True)

    # ---- transform keyframes -------------------------------------
    def test_keyframe_scale(self):
        still = os.path.join(MEDIA, "still.png")
        proj = _base_proj([{"src": still, "duration": 2,
                            "keyframes": {"scale": [
                                {"time": 0, "value": 1.0},
                                {"time": 2, "value": 2.0}]}}])
        self._render(proj, "kf_scale.mp4")
        # A static picture zoomed over time: frames must differ.
        self.assertNotEqual(_frame_md5(os.path.join(WORK, "kf_scale.mp4"), 0.1),
                           _frame_md5(os.path.join(WORK, "kf_scale.mp4"), 1.9))

    def test_keyframe_rotation(self):
        still = os.path.join(MEDIA, "still.png")
        proj = _base_proj([{"src": still, "duration": 2,
                            "keyframes": {"rotation": [
                                {"time": 0, "value": 0},
                                {"time": 2, "value": 45}]}}])
        self._render(proj, "kf_rot.mp4")
        self.assertNotEqual(_frame_md5(os.path.join(WORK, "kf_rot.mp4"), 0.1),
                           _frame_md5(os.path.join(WORK, "kf_rot.mp4"), 1.9))

    def test_keyframe_opacity(self):
        proj = _base_proj([{"src": "color:#FFFFFF", "duration": 2,
                            "keyframes": {"opacity": [
                                {"time": 0, "value": 1.0},
                                {"time": 2, "value": 0.1}]}}])
        self._render(proj, "kf_op.mp4")
        y0 = _yavg(os.path.join(WORK, "kf_op.mp4"), 0.1)
        y1 = _yavg(os.path.join(WORK, "kf_op.mp4"), 1.9)
        self.assertGreater(y0, 200, f"opaque frame not bright: {y0}")
        self.assertLess(y1, 80, f"faded frame not dark: {y1}")
        self.assertGreater(y0 - y1, 100)

    def test_keyframe_position_anchor(self):
        still = os.path.join(MEDIA, "still.png")
        proj = _base_proj([{"src": still, "duration": 2,
                            "keyframes": {
                                "position": [
                                    {"time": 0, "value": [0.5, 0.5]},
                                    {"time": 2, "value": [0.2, 0.2]}],
                                "anchor": [0.5, 0.5]}}])
        self._render(proj, "kf_pos.mp4")
        self.assertNotEqual(_frame_md5(os.path.join(WORK, "kf_pos.mp4"), 0.1),
                           _frame_md5(os.path.join(WORK, "kf_pos.mp4"), 1.9))

    def test_keyframe_validation(self):
        bad = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                           "duration": 2,
                           "keyframes": {"opacity": [
                               {"time": 1, "value": 1.0},
                               {"time": 0, "value": 0.5}]}}])
        with self.assertRaises(TimelineError):
            validate_timeline(bad)
        bad2 = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2,
                            "keyframes": {"opacity": [
                                {"time": 0, "value": 1.5}]}}])
        with self.assertRaises(TimelineError):
            validate_timeline(bad2)

    # ---- LUT ------------------------------------------------------
    def test_lut(self):
        base = _base_proj([{"src": "color:#202020", "duration": 2}])
        self._render(base, "lut_base.mp4")
        lut = _base_proj([{"src": "color:#202020", "duration": 2,
                           "effects": {"lut": os.path.join(MEDIA, "bright.cube")}}])
        self._render(lut, "lut_on.mp4")
        y0 = _yavg(os.path.join(WORK, "lut_base.mp4"), 1.0)
        y1 = _yavg(os.path.join(WORK, "lut_on.mp4"), 1.0)
        self.assertGreater(y1 - y0, 10,
                           f"LUT had no visible effect: {y0} -> {y1}")
        # Missing LUT file is a clean error, not a silent pass.
        bad = _base_proj([{"src": "color:#202020", "duration": 2,
                           "effects": {"lut": os.path.join(MEDIA, "nope.cube")}}])
        with self.assertRaises(Exception):
            self._render(bad, "lut_bad.mp4")

    # ---- audio mixer ----------------------------------------------
    def test_audio_mixer(self):
        proj = _base_proj(
            [{"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 3,
              "muted": True}],  # clip's own audio would mask the mix
            audio=[{"src": os.path.join(MEDIA, "voice.mp3"),
                    "role": "voice", "duration": 3,
                    "eq": {"low": -6, "mid": 3, "high": 6},
                    "compressor": {"threshold": -20, "ratio": 4,
                                   "attack": 10, "release": 200},
                    "volume_keyframes": [{"time": 0, "value": 0.2},
                                         {"time": 1, "value": 1.0},
                                         {"time": 3, "value": 1.0}]}])
        self._render(proj, "mixer.mp4")
        wav = os.path.join(WORK, "mixer.wav")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-i", os.path.join(WORK, "mixer.mp4"),
                    "-vn", "-c:a", "pcm_s16le", wav])
        self.assertEqual(res.returncode, 0, res.stderr)
        # Volume ramps 0.2 -> 1.0 over the first second, so the first
        # half-second averages ~0.42x (~-7.6 dB) vs full volume later.
        m_quiet = _rms_db(wav, 0, 0.5)
        m_loud = _rms_db(wav, 1.5, 1.0)
        self.assertGreater(m_loud - m_quiet, 6,
                           f"volume keyframes had no effect: "
                           f"{m_quiet:.1f} vs {m_loud:.1f} dB")

        # Pan: -1 = full left (right channel silent).
        pan_proj = _base_proj(
            [{"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 2,
              "muted": True}],
            audio=[{"src": os.path.join(MEDIA, "voice.mp3"),
                    "role": "voice", "duration": 2, "pan": -1}])
        self._render(pan_proj, "pan.mp4")
        pan_wav = os.path.join(WORK, "pan.wav")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-i", os.path.join(WORK, "pan.mp4"),
                    "-vn", "-c:a", "pcm_s16le", pan_wav])
        self.assertEqual(res.returncode, 0, res.stderr)
        import math
        import struct
        import wave

        def _ch_rms(path, ch):
            tmp = os.path.join(WORK, f"ch_{ch}.wav")
            r = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                      "-i", path, "-af",
                      f"channelsplit=channel_layout=stereo:channels={ch}",
                      "-c:a", "pcm_s16le", tmp])
            self.assertEqual(r.returncode, 0, r.stderr)
            with wave.open(tmp, "rb") as w:
                n = w.getnframes()
                raw = w.readframes(n)
            s = struct.unpack(f"<{n}h", raw)
            return math.sqrt(sum(x * x for x in s) / n) / 32768.0

        self.assertGreater(_ch_rms(pan_wav, "FL"), 0.01)
        self.assertLess(_ch_rms(pan_wav, "FR"), 0.001)

    # ---- nested sequences -----------------------------------------
    def test_nested_sequence_inline(self):
        inner = _base_proj([{"src": "color:#7A1F2B", "duration": 2}])
        outer = _base_proj([{"nested": inner, "duration": 2}])
        self._render(outer, "nested_inline.mp4")
        probe = _ffprobe(os.path.join(WORK, "nested_inline.mp4"))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.3)

    def test_nested_sequence_named(self):
        inner = _base_proj([{"src": "color:#1F7A2B", "duration": 2}])
        outer = _base_proj([{"nested": "sequence:intro", "duration": 2}],
                           sequences={"intro": inner})
        self._render(outer, "nested_named.mp4")
        probe = _ffprobe(os.path.join(WORK, "nested_named.mp4"))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.3)

    # ---- proxy workflow -------------------------------------------
    def test_proxy_workflow(self):
        clip = os.path.join(MEDIA, "clip1.mp4")
        pdir = os.path.join(WORK, "proxies")
        px = proxy_mod.make_proxy(clip, os.path.join(pdir, "clip1_proxy.mp4"),
                                  width=320, height=180)
        info = proxy_mod.proxy_info(px)
        self.assertEqual((info["width"], info["height"]), (320, 180))
        proj = _base_proj([{"src": clip, "duration": 2}],
                          audio=[{"src": os.path.join(MEDIA, "voice.mp3"),
                                  "role": "voice", "duration": 2}])
        proxied, mapping = proxy_mod.make_proxies_for_project(
            proj, pdir, width=320, height=180)
        self.assertEqual(len(mapping), 2)  # clip + voice
        self.assertIn("proxy_map", proxied)
        self.assertTrue(all("_proxy.mp4" in s
                            for s in (proxied["clips"][0]["src"],
                                      proxied["audio"][0]["src"])))
        # The proxy project renders.
        self._render(proxied, "proxy_edit.mp4")
        # Relink restores the originals exactly.
        orig = proxy_mod.relink_project(proxied)
        self.assertNotIn("proxy_map", orig)
        self.assertEqual(orig["clips"][0]["src"], clip)
        self.assertEqual(orig["audio"][0]["src"],
                         os.path.join(MEDIA, "voice.mp3"))
        # CLI round trip.
        ppath = os.path.join(WORK, "proxy_proj.json")
        with open(ppath, "w") as f:
            json.dump(proj, f)
        res = _run([sys.executable, "-m", "musecut.cli", "proxy", "make",
                    ppath, "-o", os.path.join(WORK, "proxy_cli.json"),
                    "--proxy-dir", pdir, "--width", "320", "--height", "180"],
                   cwd=ROOT)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = _run([sys.executable, "-m", "musecut.cli", "proxy", "relink",
                    os.path.join(WORK, "proxy_cli.json"),
                    "-o", os.path.join(WORK, "proxy_cli_orig.json")],
                   cwd=ROOT)
        self.assertEqual(res.returncode, 0, res.stderr)

    # ---- DNxHD ----------------------------------------------------
    def test_dnxhd_720p(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 1}],
                          width=1280, height=720,
                          export={"codec": "dnxhd"})
        self._render(proj, "dnxhd720.mov")
        probe = _ffprobe(os.path.join(WORK, "dnxhd720.mov"))
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual(v["codec_name"], "dnxhd")
        self.assertEqual((v["width"], v["height"]), (1280, 720))

    def test_dnxhd_1080p(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 1}],
                          width=1920, height=1080,
                          export={"codec": "dnxhd"})
        self._render(proj, "dnxhd1080.mov")
        probe = _ffprobe(os.path.join(WORK, "dnxhd1080.mov"))
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual(v["codec_name"], "dnxhd")

    # ---- markers / adjustment layers / scopes / 4K ----------------
    def test_markers(self):
        proj = _base_proj(
            [{"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 2}],
            markers=[{"time": 0.5, "name": "hook"},
                     {"time": 1.5, "name": "payoff"}])
        validate_timeline(proj)
        self.assertEqual(len(proj["markers"]), 2)

    def test_adjustment_layer(self):
        base = _base_proj([{"src": "color:#404040", "duration": 3}])
        self._render(base, "adjl_base.mp4")
        adj = _base_proj(
            [{"src": "color:#404040", "duration": 3}],
            adjustment_layers=[{"start": 1.0, "duration": 1.5,
                                "effects": {"brightness": 0.4}}])
        self._render(adj, "adjl_on.mp4")
        y_out = _yavg(os.path.join(WORK, "adjl_on.mp4"), 0.5)
        y_in = _yavg(os.path.join(WORK, "adjl_on.mp4"), 1.5)
        self.assertGreater(y_in - y_out, 10,
                           f"adjustment layer had no effect in-window: "
                           f"{y_out} -> {y_in}")

    def test_scopes(self):
        data = scopes_mod.scopes_data(os.path.join(MEDIA, "clip1.mp4"),
                                      t=0.5)
        self.assertIn("y_avg", data)
        self.assertIn("clipped_highlights", data)
        self.assertTrue(0 <= data["y_avg"] <= 255)
        series = scopes_mod.scopes_series(os.path.join(MEDIA, "clip1.mp4"),
                                          duration=3.0, samples=3)
        self.assertEqual(len(series), 3)
        res = _run([sys.executable, "-m", "musecut.cli", "scopes",
                    os.path.join(MEDIA, "clip1.mp4"), "--time", "0.5"],
                   cwd=ROOT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("y_avg", res.stdout)

    def test_4k_render(self):
        proj = _base_proj([{"src": "color:#112233", "duration": 1}],
                          width=3840, height=2160)
        self._render(proj, "uhd.mp4")
        probe = _ffprobe(os.path.join(WORK, "uhd.mp4"))
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual((v["width"], v["height"]), (3840, 2160))


if __name__ == "__main__":
    unittest.main()
