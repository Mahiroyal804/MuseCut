"""Round-31 tests (v0.18.0): the radial (clock wipe) transition.

Covers the engine change this round — ``transition_in`` type
``"radial"`` mapping to ffmpeg xfade ``radial``:
* The type validates in the timeline (and an unknown type still
  fails validation).
* ``render.XFADE_MAP["radial"]`` is ``"radial"`` (not the "fade"
  fallback an unmapped type would get).
* A real render through the engine: two tiny synthetic colour clips
  (red -> blue) joined by a 0.5 s radial transition — ffprobe
  duration is the overlapped total, the pre-transition frame is
  pure red, the post-transition frame is pure blue, and the
  mid-transition frame carries BOTH colours (the sweep is in
  progress, not a hard cut).

The transition's measured shape (soft ~30-40 deg angular ramp,
50% angle theta_50(p) ~= 455*p - 76 deg) is probed in
``~/workspace/your_files/musecut-tests/round31/probe_radial.py``
and verified on the real demo in ``mastery31_verify.py`` — this
file pins the wiring, the probe pins the geometry.

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round31 -v
"""

import json
import os
import subprocess
import unittest

from musecut import __version__
from musecut.render import XFADE_MAP, Renderer
from musecut.timeline import TimelineError, validate_timeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round31")
MEDIA = os.path.join(WORK, "media")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _make_media():
    os.makedirs(MEDIA, exist_ok=True)
    jobs = [
        (["-f", "lavfi", "-i", "color=c=red:s=320x180:d=2:r=30",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          os.path.join(MEDIA, "red.mp4")]),
        (["-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=30",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          os.path.join(MEDIA, "blue.mp4")]),
    ]
    for argv in jobs:
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error"] + argv)
        assert res.returncode == 0, res.stderr


def _ffprobe(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _frame_rgb(video, out, n):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-i", video, "-vf", "select='eq(n\\,%d)'" % n,
                "-frames:v", "1", out])
    assert res.returncode == 0, res.stderr
    from PIL import Image
    return Image.open(out).convert("RGB")


class TestRadialTransition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _make_media()

    def test_version_is_0_18_0(self):
        self.assertEqual(__version__, "0.21.0")

    def test_radial_validates(self):
        proj = {"version": 1, "name": "r31", "width": 320, "height": 180,
                "fps": 30,
                "clips": [
                    {"src": os.path.join(MEDIA, "red.mp4"),
                     "start": 0, "duration": 2.0, "muted": True},
                    {"src": os.path.join(MEDIA, "blue.mp4"),
                     "start": 0, "duration": 2.0, "muted": True,
                     "transition_in": {"type": "radial",
                                       "duration": 0.5}},
                ]}
        validate_timeline(proj)  # must not raise
        proj["clips"][1]["transition_in"]["type"] = "nonsense"
        with self.assertRaises(TimelineError):
            validate_timeline(proj)

    def test_radial_xfade_mapping(self):
        self.assertEqual(XFADE_MAP["radial"], "radial")

    def test_radial_render(self):
        proj = {"version": 1, "name": "r31-render", "width": 320,
                "height": 180, "fps": 30,
                "clips": [
                    {"src": os.path.join(MEDIA, "red.mp4"),
                     "start": 0, "duration": 2.0, "muted": True,
                     "fit": "cover"},
                    {"src": os.path.join(MEDIA, "blue.mp4"),
                     "start": 0, "duration": 2.0, "muted": True,
                     "fit": "cover",
                     "transition_in": {"type": "radial",
                                       "duration": 0.5}},
                ],
                "audio": []}
        out = os.path.join(WORK, "radial-render.mp4")
        Renderer(proj,
                 workdir=os.path.join(WORK, "render-radial")).render(
                     out, quiet=True)
        self.assertTrue(os.path.isfile(out))
        dur = float(_ffprobe(out)["format"]["duration"])
        # 2.0 + 2.0 - 0.5 overlap = 3.5 s.
        self.assertAlmostEqual(dur, 3.5, delta=0.15)

        def _mean_rgb(n):
            im = _frame_rgb(out, os.path.join(WORK, f"f{n}.png"), n)
            import numpy as np
            arr = np.asarray(im).reshape(-1, 3).astype(float)
            return tuple(arr.mean(axis=0))

        # Pre-transition (n=30, t=1.0): pure red.
        r0, g0, b0 = _mean_rgb(30)
        self.assertGreater(r0, 200)
        self.assertLess(b0, 60)
        # Post-transition (n=100, t=3.33): pure blue.
        r2, g2, b2 = _mean_rgb(100)
        self.assertGreater(b2, 200)
        self.assertLess(r2, 60)
        # Mid-transition (n=52, t=1.73; window [1.5, 2.0], p~=0.47):
        # the sweep is in progress — both colours present.
        r1, g1, b1 = _mean_rgb(52)
        self.assertGreater(r1, 30, f"mid frame red {r1:.0f}")
        self.assertGreater(b1, 30, f"mid frame blue {b1:.0f}")


if __name__ == "__main__":
    unittest.main()
