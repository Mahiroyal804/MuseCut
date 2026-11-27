"""Round-52 tests (v0.20.0): native mirror/flip effect.

Round 52 adds ``effects.flip`` — ``true`` / ``"horizontal"`` /
``"vertical"`` / ``"both"`` — mapping to ffmpeg ``hflip`` /
``vflip``. A geometric mirror only: no colour or timing change,
so a flipped clip is frame-identical to its source under the
mirror map. The effect exists so the round-52 mastery demo (the
180-degree rule) can render its flipped control through the
engine — the mirror a camera on the other side of the axis of
action would see — instead of hand-rolling ffmpeg outside it.

Tests use solid two-tone clips so the check measures the mirror
itself, not the footage. Expectations are computed from the
MEASURED source pixels (not the nominal colours), so lavfi/x264
matrix shifts in the sources do not leak into the assertion.
On the OLD code (no flip wiring) the render tests FAIL: the
effect key is ignored silently and the frame comes out
unflipped, so the swapped-half assertion reads the source's own
half (~180 away on the red/blue pair).
"""

import os
import subprocess
import unittest

import numpy as np
from PIL import Image

from musecut import __version__
from musecut.effects import flip_filter
from musecut.render import Renderer
from musecut.timeline import TimelineError, validate_timeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round52")

# Left red / right blue (horizontal clip); top red / bottom blue
# (vertical clip). Red vs blue are ~180 apart on the R/B channels,
# so a swapped half reads decisively, not marginally.
W, H = 160, 90


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _make_clip(path, layout):
    if layout == "h":
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-f", "lavfi", "-i",
                    "color=c=0xFF0000:s=80x90:r=30:d=1",
                    "-f", "lavfi", "-i",
                    "color=c=0x0000FF:s=80x90:r=30:d=1",
                    "-filter_complex", "hstack",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path])
    else:
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-f", "lavfi", "-i",
                    "color=c=0xFF0000:s=160x45:r=30:d=1",
                    "-f", "lavfi", "-i",
                    "color=c=0x0000FF:s=160x45:r=30:d=1",
                    "-filter_complex", "vstack",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path])
    assert res.returncode == 0, res.stderr


def _frame(video, t, out_png):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                out_png])
    assert res.returncode == 0, res.stderr
    return np.asarray(Image.open(out_png).convert("RGB")).astype(float)


def _patch(fr, cx, cy):
    """Mean RGB of a 5x5 patch around (cx, cy) — clear of the seam."""
    return fr[cy - 2:cy + 3, cx - 2:cx + 3].reshape(-1, 3).mean(axis=0)


def _halves(video, t, tag):
    png = os.path.join(WORK, f"c52_{tag}.png")
    fr = _frame(video, t, png)
    left = _patch(fr, W // 4, H // 2)
    right = _patch(fr, 3 * W // 4, H // 2)
    top = _patch(fr, W // 2, H // 4)
    bottom = _patch(fr, W // 2, 3 * H // 4)
    return left, right, top, bottom


def _render(src, flip, tag):
    out = os.path.join(WORK, f"out_{tag}.mp4")
    clip = {"src": src, "start": 0.0, "duration": 1.0, "muted": True}
    if flip is not None:
        clip["effects"] = {"flip": flip}
    proj = {"version": 1, "name": f"round52-{tag}", "width": W,
            "height": H, "fps": 30, "clips": [clip]}
    Renderer(proj, workdir=os.path.join(WORK, f"work_{tag}")).render(
        out, quiet=True)
    return out


class TestRound52Flip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        cls.h_src = os.path.join(WORK, "h.mp4")
        cls.v_src = os.path.join(WORK, "v.mp4")
        _make_clip(cls.h_src, "h")
        _make_clip(cls.v_src, "v")
        cls.h_left, cls.h_right, _, _ = _halves(cls.h_src, 0.5, "h_src")
        _, _, cls.v_top, cls.v_bottom = _halves(cls.v_src, 0.5, "v_src")

    def test_version_pin(self):
        # (round 52) version bumped to 0.20.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())

    def test_flip_filter_strings(self):
        self.assertEqual(flip_filter("horizontal"), "hflip")
        self.assertEqual(flip_filter("vertical"), "vflip")
        self.assertEqual(flip_filter("both"), "hflip,vflip")
        self.assertEqual(flip_filter(), "hflip")
        with self.assertRaises(ValueError):
            flip_filter("diagonal")

    def test_flip_validates(self):
        base = {"version": 1, "name": "r52", "width": W, "height": H,
                "fps": 30,
                "clips": [{"src": self.h_src, "start": 0.0,
                           "duration": 1.0, "muted": True}]}
        import copy
        for ok in (True, "horizontal", "vertical", "both"):
            proj = copy.deepcopy(base)
            proj["clips"][0]["effects"] = {"flip": ok}
            validate_timeline(proj)
        for bad in ("diagonal", 123, ["horizontal"]):
            proj = copy.deepcopy(base)
            proj["clips"][0]["effects"] = {"flip": bad}
            with self.assertRaises(TimelineError):
                validate_timeline(proj)

    def test_flip_horizontal_render_swaps_halves(self):
        out = _render(self.h_src, "horizontal", "hflip")
        left, right, _, _ = _halves(out, 0.5, "hflip")
        print(f"hflip: src L={np.round(self.h_left, 1)} "
              f"R={np.round(self.h_right, 1)}; got "
              f"L={np.round(left, 1)} R={np.round(right, 1)}")
        self.assertLess(np.abs(left - self.h_right).max(), 12.0,
                        "flipped left half is not the source right")
        self.assertLess(np.abs(right - self.h_left).max(), 12.0,
                        "flipped right half is not the source left")
        # Discrimination: on the old code the flip is ignored, so
        # the left half would read the source left (~180 away).
        self.assertGreater(np.abs(left - self.h_left).mean(), 50.0,
                           "render matches the unflipped source — "
                           "the flip did not take effect")

    def test_flip_true_means_horizontal(self):
        out = _render(self.h_src, True, "true")
        left, right, _, _ = _halves(out, 0.5, "true")
        self.assertLess(np.abs(left - self.h_right).max(), 12.0,
                        "flip:true left half is not the source right")
        self.assertLess(np.abs(right - self.h_left).max(), 12.0,
                        "flip:true right half is not the source left")

    def test_flip_vertical_render_swaps_halves(self):
        out = _render(self.v_src, "vertical", "vflip")
        _, _, top, bottom = _halves(out, 0.5, "vflip")
        print(f"vflip: src T={np.round(self.v_top, 1)} "
              f"B={np.round(self.v_bottom, 1)}; got "
              f"T={np.round(top, 1)} B={np.round(bottom, 1)}")
        self.assertLess(np.abs(top - self.v_bottom).max(), 12.0,
                        "flipped top half is not the source bottom")
        self.assertLess(np.abs(bottom - self.v_top).max(), 12.0,
                        "flipped bottom half is not the source top")
        self.assertGreater(np.abs(top - self.v_top).mean(), 50.0,
                           "render matches the unflipped source — "
                           "the flip did not take effect")

    def test_flip_both_swaps_both_axes(self):
        out = _render(self.h_src, "both", "both")
        left, right, _, _ = _halves(out, 0.5, "both")
        self.assertLess(np.abs(left - self.h_right).max(), 12.0,
                        "flip:both left half is not the source right")
        self.assertLess(np.abs(right - self.h_left).max(), 12.0,
                        "flip:both right half is not the source left")


if __name__ == "__main__":
    unittest.main()
