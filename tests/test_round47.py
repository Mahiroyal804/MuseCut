"""Round-47 tests (v0.19.0): blend overlays blend in RGB, not YUV.

Round 47 found the native ``overlays[].blend`` path (documented in
timeline.py, scored 2 in the rubric since round 1) had never been
rendered — and was colour-broken: ffmpeg's ``blend`` on YUV screens
the chroma planes toward max (U and V both -> 255), so the first real
screen-blend render came out pink (YUV screen vs RGB screen 89 MAE
apart on the same two frames; probe47). ``render.py`` now converts
both blend inputs to planar RGB (``gbrp``), blends there, and converts
back to ``yuv420p``; the RGB screen matches the numpy formula to
0.42 MAE.

Tests use solid-colour clips so the check measures the blend itself,
not the footage. Expectations are computed from the MEASURED source
pixels (not the nominal colours), so lavfi/x264 matrix shifts in the
sources do not leak into the assertion.
"""

import json
import os
import subprocess
import unittest

import numpy as np
from PIL import Image

from musecut import __version__
from musecut.render import Renderer
from musecut.timeline import TimelineError, validate_timeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round47")

# A = (100, 150, 80) warm green; B = (200, 60, 40) brick red.
# RGB screen of the pair is a warm tan (~222, 175, 108); a YUV screen
# of the same pair is bright magenta (~255, 100, 255) — the two
# references are ~85 MAE apart, so the test discriminates decisively.
A_HEX = "0x649650"
B_HEX = "0xC83C28"
W, H = 160, 90


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _make_clip(path, hexcolor):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-f", "lavfi", "-i",
                f"color=c={hexcolor}:s={W}x{H}:r=30:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", path])
    assert res.returncode == 0, res.stderr


def _frame(video, t, out_png):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                out_png])
    assert res.returncode == 0, res.stderr
    return np.asarray(Image.open(out_png).convert("RGB")).astype(float)


def _centre(video, t, tag):
    png = os.path.join(WORK, f"c47_{tag}.png")
    fr = _frame(video, t, png)
    return fr[H // 2, W // 2]


def _screen_rgb(a, b):
    af, bf = a / 255.0, b / 255.0
    return 255.0 * (1.0 - (1.0 - af) * (1.0 - bf))


def _rgb_to_yuv_bt601(px):
    r, g, b = px
    return np.array([
        16.0 + 0.257 * r + 0.504 * g + 0.098 * b,
        128.0 - 0.148 * r - 0.291 * g + 0.439 * b,
        128.0 + 0.439 * r - 0.368 * g - 0.071 * b])


def _yuv_to_rgb_bt601(yuv):
    y, u, v = yuv
    return np.array([
        1.164 * (y - 16.0) + 1.596 * (v - 128.0),
        1.164 * (y - 16.0) - 0.392 * (u - 128.0)
        - 0.813 * (v - 128.0),
        1.164 * (y - 16.0) + 2.017 * (u - 128.0)])


def _screen_yuv(a, b):
    """What the OLD code produced: screen per YUV plane, back to RGB."""
    ya, yb = _rgb_to_yuv_bt601(a), _rgb_to_yuv_bt601(b)
    ys = 255.0 * (1.0 - (1.0 - ya / 255.0) * (1.0 - yb / 255.0))
    return np.clip(_yuv_to_rgb_bt601(ys), 0, 255)


def _render(mode, start=0.0, dur=1.0, tag="x"):
    a_path = os.path.join(WORK, "a.mp4")
    b_path = os.path.join(WORK, "b.mp4")
    out = os.path.join(WORK, f"out_{tag}.mp4")
    proj = {
        "version": 1,
        "name": f"round47-{mode}-{tag}",
        "width": W, "height": H, "fps": 30,
        "clips": [{"src": a_path, "start": 0.0, "duration": 1.0,
                   "muted": True}],
        "overlays": [{"src": b_path, "start": start, "duration": dur,
                      "offset": 0.0, "blend": mode}],
    }
    Renderer(proj, workdir=os.path.join(WORK, f"work_{tag}")).render(
        out, quiet=True)
    return out


class TestRound47BlendRGB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        _make_clip(os.path.join(WORK, "a.mp4"), A_HEX)
        _make_clip(os.path.join(WORK, "b.mp4"), B_HEX)
        cls.a_px = _centre(os.path.join(WORK, "a.mp4"), 0.5, "a")
        cls.b_px = _centre(os.path.join(WORK, "b.mp4"), 0.5, "b")

    def test_version_pin(self):
        # (round 47) version bumped to 0.19.0; the pin moves with it.
        # (round 52) version bumped to 0.20.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())

    def test_blend_unknown_mode_rejected(self):
        proj = {"version": 1, "name": "bad-blend", "width": W,
                "height": H, "fps": 30,
                "clips": [{"src": os.path.join(WORK, "a.mp4"),
                           "start": 0.0, "duration": 1.0,
                           "muted": True}],
                "overlays": [{"src": os.path.join(WORK, "b.mp4"),
                              "start": 0.0, "duration": 1.0,
                              "offset": 0.0, "blend": "nope"}]}
        with self.assertRaises(TimelineError):
            validate_timeline(proj)

    def test_screen_blend_matches_rgb_formula(self):
        out = _render("screen", tag="screen")
        got = _centre(out, 0.5, "screen")
        expect = _screen_rgb(self.a_px, self.b_px)
        yuv_expect = _screen_yuv(self.a_px, self.b_px)
        print(f"screen: A={self.a_px}, B={self.b_px}, "
              f"expect RGB {np.round(expect, 1)}, got "
              f"{np.round(got, 1)}, YUV-screen would be "
              f"{np.round(yuv_expect, 1)}")
        self.assertLess(np.abs(got - expect).max(), 12.0,
                        f"screen blend is not the RGB formula: "
                        f"got {got}, expect {expect}")
        # Discrimination: the old YUV blend is ~85 away on this pair.
        self.assertGreater(np.abs(got - yuv_expect).mean(), 25.0,
                           "render matches the YUV screen — the "
                           "RGB fix did not take effect")

    def test_multiply_blend_matches_rgb_formula(self):
        out = _render("multiply", tag="multiply")
        got = _centre(out, 0.5, "multiply")
        expect = self.a_px * self.b_px / 255.0
        print(f"multiply: expect {np.round(expect, 1)}, got "
              f"{np.round(got, 1)}")
        self.assertLess(np.abs(got - expect).max(), 12.0,
                        f"multiply blend is not the RGB formula: "
                        f"got {got}, expect {expect}")

    def test_blend_window_enable(self):
        # Blend only [0.5, 1.0]: t=0.25 must be pure A, t=0.75 the
        # screen — the RGB conversion must not leak outside the
        # window (the post-blend format=yuv420p covers the
        # enable=false passthrough).
        out = _render("screen", start=0.5, dur=0.5, tag="window")
        early = _centre(out, 0.25, "win_early")
        late = _centre(out, 0.75, "win_late")
        expect = _screen_rgb(self.a_px, self.b_px)
        print(f"window: early {np.round(early, 1)} (pure A "
              f"{np.round(self.a_px, 1)}), late {np.round(late, 1)} "
              f"(screen {np.round(expect, 1)})")
        self.assertLess(np.abs(early - self.a_px).max(), 8.0,
                        "blend leaked before its window")
        self.assertLess(np.abs(late - expect).max(), 12.0,
                        "blend missing inside its window")


if __name__ == "__main__":
    unittest.main()
