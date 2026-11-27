"""MuseCut smoke test — renders a REAL video and verifies it.

Covers the v1 feature set end to end:
* 3 clips (testsrc + colour), zoom keyframes, cinematic grade,
  vignette + grain, speed change, whoosh + crossfade + dip-to-black
  transitions
* word-highlight captions (ASS) burned in
* text overlay with animation
* voice + music tracks with ducking under voice
* end card
* ffprobe verification (codec, size, fps, duration, audio)
* frame verification (signalstats: frames are not black)
* timeline validation, ASS generation, atempo factoring, MCP tools/list

Run:  python3 -m unittest discover -s tests -v
(from the repo root; needs ffmpeg on PATH)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

from musecut import captions as cap_mod  # noqa: E402
from musecut.render import Renderer, _atempo_chain  # noqa: E402
from musecut.timeline import TimelineError, validate_timeline  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test")
MEDIA = os.path.join(WORK, "media")
OUT = os.path.join(WORK, "smoke.mp4")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def _make_media():
    os.makedirs(MEDIA, exist_ok=True)
    jobs = [
        (["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", os.path.join(MEDIA, "clip1.mp4")]),
        (["-f", "lavfi", "-i", "smptebars=size=1280x720:rate=30:duration=3",
          "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", os.path.join(MEDIA, "clip2.mp4")]),
        (["-f", "lavfi", "-i", "sine=frequency=880:duration=5",
          "-c:a", "libmp3lame", "-q:a", "4",
          os.path.join(MEDIA, "voice.mp3")]),
        (["-f", "lavfi", "-i", "sine=frequency=220:duration=8",
          "-c:a", "libmp3lame", "-q:a", "4",
          os.path.join(MEDIA, "music.mp3")]),
    ]
    for argv in jobs:
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error"] + argv)
        assert res.returncode == 0, res.stderr


def _ffprobe(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height,r_frame_rate",
                "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _black_runs(path):
    """(start, end) of black runs via blackdetect (pix threshold 10%)."""
    res = _run(["ffmpeg", "-hide_banner", "-i", path,
                "-vf", "blackdetect=d=0.5:pix_th=0.10", "-f", "null", "-"])
    runs = []
    for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)",
                         res.stderr or ""):
        runs.append((float(m.group(1)), float(m.group(2))))
    return runs


def smoke_timeline():
    return {
        "version": 1,
        "name": "smoke",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "background": "#000000",
        "clips": [
            {
                "src": os.path.join(MEDIA, "clip1.mp4"),
                "duration": 3,
                "effects": {"filter": "cinematic", "vignette": 0.5,
                            "grain": 0.4},
                "keyframes": {"zoom": {"from": 1.0, "to": 1.2}},
            },
            {
                "src": os.path.join(MEDIA, "clip2.mp4"),
                "duration": 3,
                "speed": 1.5,
                "transition_in": {"type": "whoosh", "duration": 0.5},
            },
            {
                "src": "color:#7A1F2B",
                "duration": 3,
                "transition_in": {"type": "dip-to-black", "duration": 0.5},
            },
        ],
        "texts": [
            {"text": "MuseCut smoke test", "start": 0.5, "duration": 2.5,
             "font": "Anton", "size": 72, "animation": "slide-up",
             "y": 0.12},
        ],
        "audio": [
            {"src": os.path.join(MEDIA, "voice.mp3"), "role": "voice",
             "start": 0, "duration": 5, "volume": 1.0},
            {"src": os.path.join(MEDIA, "music.mp3"), "role": "music",
             "start": 0, "duration": 8, "volume": 0.5, "loop": True,
             "ducking": {"sidechain": "voice", "threshold": 0.02,
                         "ratio": 8}},
        ],
        "captions": {
            "style": "viral-gold",
            "words": [
                {"word": "This", "start": 0.2, "end": 0.5},
                {"word": "is", "start": 0.5, "end": 0.7},
                {"word": "MuseCut", "start": 0.7, "end": 1.2},
                {"word": "smoke", "start": 1.2, "end": 1.6},
                {"word": "test", "start": 1.6, "end": 2.0},
                {"word": "rendering", "start": 3.1, "end": 3.6},
                {"word": "real", "start": 3.6, "end": 3.9},
                {"word": "video", "start": 3.9, "end": 4.4},
            ],
        },
        "endcard": {"duration": 3, "title": "Watch next",
                    "subtitle": "MuseCut — built for agents",
                    "background": "#0B0B0B"},
    }


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(WORK, exist_ok=True)
        _make_media()

    def test_full_render(self):
        proj = smoke_timeline()
        validate_timeline(proj)
        renderer = Renderer(proj, workdir=os.path.join(WORK, "render"))
        result = renderer.render(OUT, quiet=True)
        self.assertTrue(os.path.isfile(OUT))
        self.assertGreater(os.path.getsize(OUT), 10_000)

        probe = _ffprobe(OUT)
        video = next(s for s in probe["streams"]
                     if s["codec_type"] == "video")
        audio = next(s for s in probe["streams"]
                     if s["codec_type"] == "audio")
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(video["width"], 1280)
        self.assertEqual(video["height"], 720)
        self.assertEqual(video["r_frame_rate"], "30/1")
        self.assertEqual(audio["codec_name"], "aac")
        # main: 3 + 3/1.5 + 3 - 0.5 - 0.5 = 7s, + 3s end card = 10s
        dur = float(probe["format"]["duration"])
        self.assertAlmostEqual(dur, 10.0, delta=0.6)

        # frames are real content, not black: no black run may start
        # inside the main video (0-7s). The end card (7-10s) is a
        # near-black card by design, so black there is expected.
        runs = _black_runs(OUT)
        early_black = [r for r in runs if r[0] < 6.9]
        self.assertEqual(early_black, [], f"black frames in main video: {runs}")

        # captions ASS was generated with the words in it
        ass_path = os.path.join(WORK, "render", "captions.ass")
        self.assertTrue(os.path.isfile(ass_path))
        ass = open(ass_path, encoding="utf-8").read()
        self.assertIn("MuseCut", ass)
        # active word carries the gold colour override (&H00BGR: FFD60A)
        self.assertIn("&H000AD6FF", ass)

        # spot-check a frame from the end card region is dark but present
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "8.5", "-i", OUT, "-frames:v", "1",
                    os.path.join(WORK, "frame_endcard.png")])
        self.assertEqual(res.returncode, 0)
        self.assertGreater(
            os.path.getsize(os.path.join(WORK, "frame_endcard.png")), 2000)

    def test_timeline_validation(self):
        bad = smoke_timeline()
        bad["clips"][0]["speed"] = 99
        with self.assertRaises(TimelineError):
            validate_timeline(bad)
        bad2 = smoke_timeline()
        bad2["clips"][1]["transition_in"] = {"type": "explode",
                                             "duration": 0.5}
        with self.assertRaises(TimelineError):
            validate_timeline(bad2)

    def test_ass_generation(self):
        words = [{"word": "hello", "start": 0.0, "end": 0.4},
                 {"word": "world", "start": 0.4, "end": 0.9}]
        doc = cap_mod.words_to_ass(words, width=1280, height=720)
        self.assertIn("Dialogue", doc)
        self.assertIn("hello", doc)
        self.assertIn("world", doc)
        # active word gets the gold colour override
        self.assertIn("1c&H000AD6FF", doc)

    def test_atempo_chain(self):
        self.assertEqual(_atempo_chain(1.0), "")
        self.assertEqual(_atempo_chain(4.0), "atempo=2.0000,atempo=2.0000")
        chain = _atempo_chain(0.1)
        prod = 1.0
        for part in chain.split(","):
            prod *= float(part.split("=")[1])
        self.assertAlmostEqual(prod, 0.1, places=3)

    def test_mcp_tools_list(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "musecut.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, cwd=ROOT,
            env={**os.environ, "PYTHONPATH": ROOT})
        try:
            def send(obj):
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            def notify(obj):
                # Notifications get NO response — write only, never read.
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

            init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2024-11-05",
                                    "capabilities": {},
                                    "clientInfo": {"name": "test",
                                                   "version": "0"}}})
            self.assertEqual(init["result"]["serverInfo"]["name"], "musecut")
            notify({"jsonrpc": "2.0",
                    "method": "notifications/initialized"})
            tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {t["name"] for t in tools["result"]["tools"]}
            for needed in ("create_project", "add_clip", "add_captions",
                           "render", "get_status"):
                self.assertIn(needed, names)
            call = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "create_project",
                                    "arguments": {"name": "t"}}})
            self.assertFalse(call["result"]["isError"])
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
