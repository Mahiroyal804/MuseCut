"""Round-58 tests (v0.21.0): 8K export.

Round 58 closes the Export "8K" row (1 -> 2): the model already
accepted 8K, but no 8K render had ever run on this VM. A 1 s
colour-clip render at 7680x4320 now runs end to end — ffprobe
reads 7680x4320 h264, and the frame at 0.5 s reads the colour's
luma (YAVG ~31 for #112233), so the file is a real 8K picture,
not a black canvas at 8K dims. The same path was also proven on
real footage first (a 1 s 8K render of the day-street clip:
7680x4320 h264, 1.00 s, 57 s wall — see COMPARISON.md round 58);
the suite test uses the colour clip, the same pattern as the 4K
test in test_pro.py, to keep the suite fast. No engine change was
needed — the row was unrendered, not unimplemented — so the test
would PASS on the old code too; the close is the render proof,
not a fix.
"""

import json
import os
import re
import subprocess
import unittest

from musecut import __version__
from musecut.render import Renderer
from musecut.timeline import validate_timeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round58")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _ffprobe(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _yavg(path, t):
    """Mean luma of the frame at time t (signalstats + metadata)."""
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "info",
                "-ss", str(t), "-i", path,
                "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                "-frames:v", "1", "-f", "null", "-"])
    m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", res.stderr or "")
    assert m, f"no YAVG in signalstats output for {path}@{t}"
    return float(m.group(1))


class TestRound58EightK(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)

    def test_version_pin(self):
        # (round 58) version bumped to 0.21.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())

    def test_8k_render(self):
        proj = {"version": 1, "name": "round58-8k", "width": 7680,
                "height": 4320, "fps": 30, "background": "#000000",
                "clips": [{"src": "color:#112233", "duration": 1}],
                "overlays": [], "texts": [], "audio": [],
                "captions": None, "endcard": None, "export": None,
                "canvas": None, "sequences": {}, "markers": [],
                "adjustment_layers": []}
        validate_timeline(proj)
        out = os.path.join(WORK, "uhd8k.mp4")
        Renderer(proj, workdir=os.path.join(WORK, "render")).render(
            out, quiet=True)
        probe = _ffprobe(out)
        v = next(s for s in probe["streams"]
                 if s["codec_type"] == "video")
        self.assertEqual((v["width"], v["height"]), (7680, 4320))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 1.0,
                               delta=0.15)
        # Frame check: the 8K frame is the colour, not a black
        # canvas at 8K dims (#112233 -> YAVG ~31).
        y = _yavg(out, 0.5)
        self.assertGreater(y, 20.0, f"8K frame is black (YAVG {y})")
        self.assertLess(y, 45.0, f"8K frame is not #112233 (YAVG {y})")


if __name__ == "__main__":
    unittest.main()
