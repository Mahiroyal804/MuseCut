"""Round-8 tests (v0.10.0).

Covers round 8 (2026-11-21) — the keyframe/easing bug-hunt sweep and
the text-animation fixes. Every render test asserts an EXACT value
(a blob width, a centroid, a luma, an RMS ratio), not just an
ordering — round 2's ease-out test only asserted ease-out > linear,
which is why the round-7 precedence bug survived since round 2.

Bugs found by this sweep (all verified by render before fixing):
* speed_curve + transform keyframes: _expand_speed_curve copied the
  keyframes verbatim into every segment, so the whole curve replayed
  per segment (a scale 1->3 over 4 s restarted at 1.0 in segment 2).
  Fixed by window-shifting keyframes into each segment's output
  window (keyframes.shift_to_window); legacy zoom/pan re-anchored
  the same way.
* clip_durs for a speed-curve clip used the source duration instead
  of the segments' summed output durations, so main_dur (audio
  fade-out timing, endcard total, reported duration) was wrong — a
  1s@1x + 1s@2x clip reported 2.0 s while the file is 1.5 s.
* "slide-down" was validated and documented (timeline.py) but had no
  render branch — it rendered static text. "slide-up" only moved for
  y="lower", and in the wrong direction (it started 40 px ABOVE and
  sank instead of rising). Both fixed; slides now work for any x/y
  (including object-lock) and move in the named direction.
* New text animations: slide-left / slide-right (in), pulse (loop),
  fade-out (out) — the text row previously had in-animations only.
* ``geq`` chroma bug (round 8, found by the opacity render test):
  ``opacity_keyframe_filter`` specified only lum_expr + alpha_expr,
  and geq fills UNSPECIFIED planes with the luma value — every
  opacity-keyframed clip came out pink (Cb/Cr = 235 on a white
  frame; RGB (255, 190, 190) instead of white). The luma itself
  was always correct (Y = 235 at alpha 1.0, verified in the Y
  domain), which is why the old luma-only assertion passed and
  the bug survived. Fixed by pinning cb_expr/cr_expr to identity
  in ``effects.opacity_keyframe_filter``.
* Input seek (round 8 speed): clips now seek with ``-ss`` before
  ``-i`` instead of decoding from 0 and trimming in the graph
  (10x decode on a 50 s trim, pixel-identical); audio-only export
  builds the audio graph directly instead of rendering a video
  intermediate first (104 s -> 1.2 s for an 8 s wav, mix RMS
  ratio 1.005). ``-filter_complex_threads`` was measured and gives
  NO speedup on the bench project (medians 51.8 s vs 51.9 s) —
  the early 1.16x claim was suite contention, retracted.

Measurements use numpy + PIL only (no cv2) so the suite still runs
under the system python.
"""

import json
import math
import os
import subprocess
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

import numpy as np
from PIL import Image

from musecut import keyframes as kf_mod
from musecut.render import Renderer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round8")
MEDIA = os.path.join(WORK, "media")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _frame(video, n, out_png):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-i", video, "-vf", f"select='eq(n\\,{n})'",
                "-frames:v", "1", out_png])
    assert res.returncode == 0, res.stderr
    return out_png


def _gray(png_path):
    return np.asarray(Image.open(png_path).convert("L")).astype(np.float32)


def _blob_width(png_path, thresh=128):
    """Width of the bright blob (columns with any bright pixel)."""
    a = _gray(png_path)
    cols = np.where((a > thresh).any(axis=0))[0]
    return int(cols[-1] - cols[0] + 1) if len(cols) else 0


def _bright_centroid(png_path, thresh=128):
    """Centroid (x, y) of bright pixels, or None."""
    a = _gray(png_path)
    ys, xs = np.where(a > thresh)
    if len(xs) == 0:
        return None
    return float(xs.mean()), float(ys.mean())


def _text_edges(png_path, thresh=128):
    """(top row, left col) of the first bright pixel."""
    a = _gray(png_path)
    ys, xs = np.where(a > thresh)
    if len(xs) == 0:
        return None
    return int(ys.min()), int(xs.min())


def _bbox_mean(png_path, box):
    """Mean luma inside box (top, left, bottom, right)."""
    a = _gray(png_path)
    top, left, bottom, right = box
    return float(a[top:bottom, left:right].mean())


def _rms_wav(path, t0, t1, sr=48000):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    import struct
    samples = np.asarray(struct.unpack(f"<{n}h", raw), dtype=np.float64)
    i0, i1 = int(t0 * sr), int(t1 * sr)
    seg = samples[i0:i1]
    return float(np.sqrt((seg ** 2).mean())) if len(seg) else 0.0


class Round8Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(MEDIA, exist_ok=True)
        # White circle r=60 on black, 640x360 (scale render probe).
        circle = os.path.join(MEDIA, "circle60.png")
        if not os.path.isfile(circle):
            img = Image.new("RGB", (640, 360), (0, 0, 0))
            px = img.load()
            for y in range(360):
                for x in range(640):
                    if (x - 320) ** 2 + (y - 180) ** 2 <= 60 * 60:
                        px[x, y] = (255, 255, 255)
            img.save(circle)
        cls.circle = circle
        # White 40x40 square at (70,70)-(110,110) on black, 200x200
        # (rotation / position probe; centroid (90, 90)).
        square = os.path.join(MEDIA, "square200.png")
        if not os.path.isfile(square):
            img = Image.new("RGB", (200, 200), (0, 0, 0))
            px = img.load()
            for y in range(70, 110):
                for x in range(70, 110):
                    px[x, y] = (255, 255, 255)
            img.save(square)
        cls.square = square
        # 4 s 440 Hz tone (volume-keyframe probe).
        tone = os.path.join(MEDIA, "tone440.wav")
        if not os.path.isfile(tone):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-f", "lavfi",
                        "-i", "aevalsrc='0.35*sin(2*PI*440*t)':d=4:"
                              "sample_rate=48000",
                        "-c:a", "pcm_s16le", tone])
            assert res.returncode == 0, res.stderr
        cls.tone = tone

    def _scale_proj(self, kfs, duration=2.0, **clip_extra):
        clip = {"src": self.circle, "duration": duration,
                "keyframes": {"scale": kfs}}
        clip.update(clip_extra)
        return {"version": 1, "name": "r8-scale", "width": 640,
                "height": 360, "fps": 30, "clips": [clip]}

    def _render(self, proj, name):
        out = os.path.join(WORK, name)
        Renderer(proj, workdir=os.path.join(WORK, "render-" + name)
                 ).render(out, quiet=True)
        return out

    # ---- Python-side keyframe evaluation ---------------------------
    def test_evaluate_exact_values(self):
        """evaluate() mirrors scalar_expr: easing, holds, segments."""
        kfs = [{"time": 0.0, "value": 0.0, "easing": "ease-in-out"},
               {"time": 2.0, "value": 10.0}]
        self.assertAlmostEqual(kf_mod.evaluate(kfs, 0.5), 1.25)
        self.assertAlmostEqual(kf_mod.evaluate(kfs, 1.0), 5.0)
        self.assertAlmostEqual(kf_mod.evaluate(kfs, 1.5), 8.75)
        self.assertAlmostEqual(kf_mod.evaluate(kfs, -1.0), 0.0)
        self.assertAlmostEqual(kf_mod.evaluate(kfs, 99.0), 10.0)
        pair = [{"time": 0.0, "value": [0.0, 1.0]},
                {"time": 2.0, "value": [1.0, 0.0]}]
        self.assertEqual(kf_mod.evaluate(pair, 1.0, pair=True),
                         [0.5, 0.5])

    def test_shift_to_window_exact_values(self):
        """Window-shift keeps the curve continuous across segments."""
        kfs = [{"time": 0.0, "value": 1.0},
               {"time": 3.0, "value": 4.0}]
        shifted = kf_mod.shift_to_window(kfs, 2.0, 3.0)
        self.assertEqual([k["time"] for k in shifted], [0.0, 1.0])
        self.assertAlmostEqual(shifted[0]["value"], 3.0)
        self.assertAlmostEqual(shifted[1]["value"], 4.0)
        # Interior keyframes shift; boundary easing comes from the
        # segment the window starts in.
        kfs2 = [{"time": 0.0, "value": 0.0, "easing": "ease-out"},
                {"time": 1.0, "value": 1.0},
                {"time": 2.0, "value": 2.0}]
        sh2 = kf_mod.shift_to_window(kfs2, 0.5, 1.5)
        self.assertEqual([k["time"] for k in sh2], [0.0, 0.5, 1.0])
        self.assertAlmostEqual(sh2[0]["value"], 0.75)
        self.assertEqual(sh2[0]["easing"], "ease-out")
        self.assertAlmostEqual(sh2[1]["value"], 1.0)
        self.assertAlmostEqual(sh2[2]["value"], 1.5)
        # Constant (single keyframe) passes through unchanged.
        self.assertEqual(kf_mod.shift_to_window(
            [{"time": 0.0, "value": 2.5}], 1.0, 2.0),
            [{"time": 0.0, "value": 2.5}])

    # ---- easing exact values (render) ------------------------------
    def test_ease_in_out_exact_values_render(self):
        """Scale 1->5 over 2 s, ease-in-out.

        At u=0.25 the factor is 2*0.25^2 = 0.125 -> scale 1.5, blob
        180 px; at u=0.75 it is 1-(2-2*0.75)^2/2 = 0.875 -> scale
        4.5, blob 540 px. (Round 2 only pinned u=0.5, where
        ease-in-out == linear == 0.5, so a wrong smoothstep would
        have passed.)
        """
        proj = self._scale_proj(
            [{"time": 0.0, "value": 1.0, "easing": "ease-in-out"},
             {"time": 2.0, "value": 5.0}])
        out = self._render(proj, "ease-in-out.mp4")
        w1 = _blob_width(_frame(out, 15, os.path.join(WORK, "eio-15.png")))
        w2 = _blob_width(_frame(out, 45, os.path.join(WORK, "eio-45.png")))
        self.assertGreaterEqual(w1, 160, f"u=0.25 blob {w1}px, want ~180")
        self.assertLessEqual(w1, 200, f"u=0.25 blob {w1}px, want ~180")
        self.assertGreaterEqual(w2, 510, f"u=0.75 blob {w2}px, want ~540")
        self.assertLessEqual(w2, 570, f"u=0.75 blob {w2}px, want ~540")

    def test_ease_in_exact_value_render(self):
        """Scale 1->5 over 2 s, ease-in: u=0.5 -> 0.25 -> scale 2.0,
        blob 240 px."""
        proj = self._scale_proj(
            [{"time": 0.0, "value": 1.0, "easing": "ease-in"},
             {"time": 2.0, "value": 5.0}])
        out = self._render(proj, "ease-in.mp4")
        w = _blob_width(_frame(out, 30, os.path.join(WORK, "ei-30.png")))
        self.assertGreaterEqual(w, 215, f"blob {w}px, want ~240")
        self.assertLessEqual(w, 265, f"blob {w}px, want ~240")

    def test_multi_segment_easing_render(self):
        """Two segments, easing only on the first: scale 1->2
        (ease-out) -> 4 (linear). At t=0.5 (seg 1, u=0.5, ease-out
        0.75) scale 1.75, blob 210 px; at t=1.5 (seg 2, linear u=0.5)
        scale 3.0, blob 360 px."""
        proj = self._scale_proj(
            [{"time": 0.0, "value": 1.0, "easing": "ease-out"},
             {"time": 1.0, "value": 2.0},
             {"time": 2.0, "value": 4.0}])
        out = self._render(proj, "multi-seg.mp4")
        w1 = _blob_width(_frame(out, 15, os.path.join(WORK, "ms-15.png")))
        w2 = _blob_width(_frame(out, 45, os.path.join(WORK, "ms-45.png")))
        self.assertGreaterEqual(w1, 190, f"seg1 blob {w1}px, want ~210")
        self.assertLessEqual(w1, 230, f"seg1 blob {w1}px, want ~210")
        self.assertGreaterEqual(w2, 335, f"seg2 blob {w2}px, want ~360")
        self.assertLessEqual(w2, 385, f"seg2 blob {w2}px, want ~360")

    def test_hold_before_first_and_after_last_render(self):
        """Scale keyframes start at t=1.0 (2.0) and end at t=2.0
        (3.0): at t=0.5 the value holds the first (blob 240 px);
        at t=2.5 it holds the last (blob 360 px)."""
        proj = self._scale_proj(
            [{"time": 1.0, "value": 2.0},
             {"time": 2.0, "value": 3.0}], duration=3.0)
        out = self._render(proj, "hold.mp4")
        w1 = _blob_width(_frame(out, 15, os.path.join(WORK, "h-15.png")))
        w2 = _blob_width(_frame(out, 75, os.path.join(WORK, "h-75.png")))
        self.assertGreaterEqual(w1, 220, f"hold-first {w1}px, want ~240")
        self.assertLessEqual(w1, 260, f"hold-first {w1}px, want ~240")
        self.assertGreaterEqual(w2, 335, f"hold-last {w2}px, want ~360")
        self.assertLessEqual(w2, 385, f"hold-last {w2}px, want ~360")

    # ---- rotation / opacity / position / volume (render) ------------
    def test_rotation_exact_value_render(self):
        """Rotate 0->90 deg linear over 2 s. The square's centroid
        starts at (90, 90) (radius 14.1 from the centre); at 45 deg
        it sits on the same circle with one axis at ~100 (either
        (100, 86) or (86, 100) depending on rotation sense)."""
        proj = {"version": 1, "name": "r8-rot", "width": 200,
                "height": 200, "fps": 30,
                "clips": [{"src": self.square, "duration": 2,
                           "keyframes": {"rotation": [
                               {"time": 0.0, "value": 0.0},
                               {"time": 2.0, "value": 90.0}]}}]}
        out = self._render(proj, "rotate.mp4")
        c0 = _bright_centroid(_frame(out, 3, os.path.join(WORK, "r-3.png")))
        c1 = _bright_centroid(_frame(out, 30, os.path.join(WORK, "r-30.png")))
        self.assertIsNotNone(c0)
        self.assertIsNotNone(c1)
        self.assertAlmostEqual(c0[0], 90, delta=3)
        self.assertAlmostEqual(c0[1], 90, delta=3)
        r = math.hypot(c1[0] - 100, c1[1] - 100)
        self.assertAlmostEqual(r, 14.1, delta=4,
                               msg=f"45deg centroid {c1}, radius {r:.1f}")
        self.assertLess(min(abs(c1[0] - 100), abs(c1[1] - 100)), 5,
                        f"45deg centroid {c1}: one axis must stay ~100")

    def test_opacity_exact_value_render(self):
        """White clip, opacity 1->0 linear over 2 s: at t=1.0 the
        frame is 50% white over black -> mean luma ~127."""
        proj = {"version": 1, "name": "r8-op", "width": 320,
                "height": 180, "fps": 30,
                "clips": [{"src": "color:#FFFFFF", "duration": 2,
                           "keyframes": {"opacity": [
                               {"time": 0.0, "value": 1.0},
                               {"time": 2.0, "value": 0.0}]}}]}
        out = self._render(proj, "opacity.mp4")
        a1 = _gray(_frame(out, 30, os.path.join(WORK, "o-30.png")))
        a0 = _gray(_frame(out, 3, os.path.join(WORK, "o-3.png")))
        self.assertGreaterEqual(float(a1.mean()), 110,
                                f"t=1.0 mean {a1.mean():.1f}, want ~127")
        self.assertLessEqual(float(a1.mean()), 145,
                             f"t=1.0 mean {a1.mean():.1f}, want ~127")
        self.assertGreater(float(a0.mean()), 220,
                           f"t=0.1 mean {a0.mean():.1f}, want ~242")

    def test_pair_position_exact_value_render(self):
        """Position [0.2, 0.8]->[0.8, 0.2] linear, fixed scale 0.5:
        the 200x200 source is first fit-scaled to 360x360 on the
        640x360 canvas (factor 1.8), so the white square (centre
        (90, 90) in the source) sits at (302, 162) pre-transform;
        the 0.5 transform scale halves that to (151, 81), and the
        overlay anchors the 320x180 scaled frame at the position
        point. At t=1.0 (u=0.5, pos (0.5, 0.5)) the centroid is
        (160+151, 90+81) = (311, 171); at t=0.2 (pos (0.26, 0.74))
        it is (6.4+151, 176.4+81) = (157, 257). (The first version
        of this test expected (320, 180) — it forgot the leg's
        fit-scale and the square's off-centre source position.)"""
        proj = {"version": 1, "name": "r8-pos", "width": 640,
                "height": 360, "fps": 30,
                "clips": [{"src": self.square, "duration": 2,
                           "keyframes": {
                               "scale": [{"time": 0.0, "value": 0.5}],
                               "position": [
                                   {"time": 0.0, "value": [0.2, 0.8]},
                                   {"time": 2.0, "value": [0.8, 0.2]}]}}]}
        out = self._render(proj, "position.mp4")
        c1 = _bright_centroid(_frame(out, 30, os.path.join(WORK, "p-30.png")))
        c0 = _bright_centroid(_frame(out, 6, os.path.join(WORK, "p-6.png")))
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c0)
        self.assertAlmostEqual(c1[0], 311, delta=8, msg=f"centre {c1}")
        self.assertAlmostEqual(c1[1], 171, delta=8, msg=f"centre {c1}")
        self.assertAlmostEqual(c0[0], 157, delta=12, msg=f"early {c0}")
        self.assertAlmostEqual(c0[1], 257, delta=12, msg=f"early {c0}")

    def test_volume_keyframes_exact_value_render(self):
        """Volume 1.0->0.0 linear over 4 s: RMS[1.8, 2.0] / RMS[0.3,
        0.5] should be ~0.525/0.9 = 0.58 (both windows sit before
        the final 1 s fade-out)."""
        proj = {"version": 1, "name": "r8-vol", "width": 320,
                "height": 180, "fps": 30,
                "clips": [{"src": "color:#000000", "duration": 4,
                           "muted": True}],
                "audio": [{"src": self.tone, "role": "voice",
                           "start": 0, "duration": 4,
                           "volume_keyframes": [
                               {"time": 0.0, "value": 1.0},
                               {"time": 4.0, "value": 0.0}]}]}
        out = self._render(proj, "volume.mp4")
        wav = os.path.join(WORK, "volume.wav")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-i", out, "-vn", "-ac", "1", "-ar", "48000",
                    "-c:a", "pcm_s16le", wav])
        assert res.returncode == 0, res.stderr
        early = _rms_wav(wav, 0.3, 0.5)
        late = _rms_wav(wav, 1.8, 2.0)
        ratio = late / early if early else 0.0
        self.assertGreater(ratio, 0.45, f"RMS ratio {ratio:.2f}, want ~0.58")
        self.assertLess(ratio, 0.72, f"RMS ratio {ratio:.2f}, want ~0.58")

    # ---- speed_curve + keyframes (the round-8 find) -----------------
    def test_speed_curve_keyframes_window_render(self):
        """Circle probe, 4 s image, speed_curve 1x then 2x, scale
        1->3 linear over the 3 s OUTPUT timeline. Segment 1 (0-2 s
        out) is correct either way; at t=2.5 (segment 2, local 0.5)
        the correct scale is 1+2*(2.5/3) = 2.667 -> blob 320 px.
        The pre-fix bug replayed the curve per segment -> 1.333 ->
        blob 160 px."""
        proj = self._scale_proj(
            [{"time": 0.0, "value": 1.0},
             {"time": 3.0, "value": 3.0}],
            duration=4.0,
            speed_curve=[{"time": 0, "speed": 1.0},
                         {"time": 2, "speed": 2.0}])
        out = self._render(proj, "curve-kf.mp4")
        w1 = _blob_width(_frame(out, 30, os.path.join(WORK, "ck-30.png")))
        w2 = _blob_width(_frame(out, 75, os.path.join(WORK, "ck-75.png")))
        self.assertGreaterEqual(w1, 180, f"seg1 blob {w1}px, want ~200")
        self.assertLessEqual(w1, 220, f"seg1 blob {w1}px, want ~200")
        self.assertGreaterEqual(w2, 290, f"seg2 blob {w2}px, want ~320")
        self.assertLessEqual(w2, 350, f"seg2 blob {w2}px, want ~320")

    def test_clip_durs_speed_curve(self):
        """build_command total for a 2 s clip at 1s@1x + 1s@2x is
        1.5 s, not the 2.0 s source duration (pre-fix main_dur)."""
        proj = self._scale_proj(
            [{"time": 0.0, "value": 1.0}],
            duration=2.0,
            speed_curve=[{"time": 0, "speed": 1.0},
                         {"time": 1, "speed": 2.0}])
        r = Renderer(proj, workdir=os.path.join(WORK, "render-durs"))
        _, total, _ = r.build_command(os.path.join(WORK, "durs.mp4"))
        self.assertAlmostEqual(total, 1.5, delta=0.01)

    # ---- text animations (round-8 fixes) ----------------------------
    def _text_proj(self, animation, y="center", x="center"):
        return {"version": 1, "name": "r8-text", "width": 640,
                "height": 360, "fps": 30,
                "clips": [{"src": "color:#000000", "duration": 2,
                           "muted": True}],
                "texts": [{"text": "TEST", "start": 0, "duration": 2,
                           "font": "Anton", "size": 100,
                           "color": "#FFFFFF", "x": x, "y": y,
                           "animation": animation}]}

    def test_slide_up_direction_and_position_render(self):
        """slide-up rises into place from 40 px below, for ANY y —
        including centre (pre-fix it only moved for y="lower", and
        in the wrong direction). At t=0.1 the top edge sits ~27 px
        below the settled top edge."""
        for y in ("center", "lower"):
            out = self._render(self._text_proj("slide-up", y=y),
                               f"slide-up-{y}.mp4")
            e1 = _text_edges(_frame(out, 3, os.path.join(
                WORK, f"su-{y}-3.png")))
            e2 = _text_edges(_frame(out, 15, os.path.join(
                WORK, f"su-{y}-15.png")))
            self.assertIsNotNone(e1)
            self.assertIsNotNone(e2)
            d = e1[0] - e2[0]
            self.assertGreaterEqual(d, 18, f"y={y}: top moved {d}px, want ~+27")
            self.assertLessEqual(d, 36, f"y={y}: top moved {d}px, want ~+27")

    def test_slide_down_renders(self):
        """slide-down settles from 40 px above: at t=0.1 the top
        edge sits ~27 px ABOVE the settled top edge. Pre-fix this
        animation was validated + documented but had no render
        branch at all (static text)."""
        out = self._render(self._text_proj("slide-down"), "slide-down.mp4")
        e1 = _text_edges(_frame(out, 3, os.path.join(WORK, "sd-3.png")))
        e2 = _text_edges(_frame(out, 15, os.path.join(WORK, "sd-15.png")))
        self.assertIsNotNone(e1)
        self.assertIsNotNone(e2)
        d = e1[0] - e2[0]
        self.assertGreaterEqual(d, -36, f"top moved {d}px, want ~-27")
        self.assertLessEqual(d, -18, f"top moved {d}px, want ~-27")

    def test_slide_left_renders(self):
        """slide-left comes in from 60 px right: at t=0.1 the left
        edge sits ~40 px right of the settled left edge."""
        out = self._render(self._text_proj("slide-left"), "slide-left.mp4")
        e1 = _text_edges(_frame(out, 3, os.path.join(WORK, "sl-3.png")))
        e2 = _text_edges(_frame(out, 15, os.path.join(WORK, "sl-15.png")))
        self.assertIsNotNone(e1)
        self.assertIsNotNone(e2)
        d = e1[1] - e2[1]
        self.assertGreaterEqual(d, 30, f"left moved {d}px, want ~+40")
        self.assertLessEqual(d, 50, f"left moved {d}px, want ~+40")

    def test_pulse_and_fade_out_render(self):
        """pulse: alpha breathes 0.5..1.0 at 1.5 Hz — the text-zone
        mean at t=0.5 (alpha 0.5) is ~half the mean at t=0.833
        (alpha 1.0). fade-out: at t=1.9 the text is at alpha ~0.33
        of its t=1.5 level."""
        out = self._render(self._text_proj("pulse"), "pulse.mp4")
        a = _gray(_frame(out, 25, os.path.join(WORK, "pu-25.png")))
        ys, xs = np.where(a > 128)
        box = (int(ys.min()), int(xs.min()), int(ys.max()) + 1,
               int(xs.max()) + 1)
        m_hi = _bbox_mean(os.path.join(WORK, "pu-25.png"), box)
        _frame(out, 15, os.path.join(WORK, "pu-15.png"))
        m_lo = _bbox_mean(os.path.join(WORK, "pu-15.png"), box)
        ratio = m_lo / m_hi if m_hi else 0.0
        self.assertGreater(ratio, 0.38, f"pulse ratio {ratio:.2f}, want ~0.5")
        self.assertLess(ratio, 0.68, f"pulse ratio {ratio:.2f}, want ~0.5")

        out2 = self._render(self._text_proj("fade-out"), "fade-out.mp4")
        b = _gray(_frame(out2, 45, os.path.join(WORK, "fo-45.png")))
        ys, xs = np.where(b > 128)
        box2 = (int(ys.min()), int(xs.min()), int(ys.max()) + 1,
                int(xs.max()) + 1)
        m_full = _bbox_mean(os.path.join(WORK, "fo-45.png"), box2)
        _frame(out2, 57, os.path.join(WORK, "fo-57.png"))
        m_end = _bbox_mean(os.path.join(WORK, "fo-57.png"), box2)
        ratio2 = m_end / m_full if m_full else 0.0
        self.assertGreater(ratio2, 0.15, f"fade-out ratio {ratio2:.2f}, want ~0.33")
        self.assertLess(ratio2, 0.55, f"fade-out ratio {ratio2:.2f}, want ~0.33")
    # ---- input seek + audio-only + filter threads (round 8 speed) --
    def test_input_seek_exact_frames(self):
        """A clip trimmed at 4.0 s renders EXACTLY the source frames
        from 4.0 s on — the input -ss seek must not shift content.
        The rendered frame at t=0.5 must match the source frame at
        t=4.5 (MAE small, encode noise only) and must NOT match the
        source frame at t=0.5 (that would mean the seek never
        happened)."""
        src = os.path.join(MEDIA, "seek-src.mp4")
        if not os.path.isfile(src):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-f", "lavfi",
                        "-i", "testsrc2=size=640x360:rate=30:"
                              "duration=6",
                        "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "18", "-pix_fmt", "yuv420p", src])
            assert res.returncode == 0, res.stderr
        proj = {"version": 1, "name": "r8-seek", "width": 640,
                "height": 360, "fps": 30,
                "clips": [{"src": src, "start": 4.0, "duration": 1.0,
                           "muted": True}]}
        out = self._render(proj, "seek.mp4")
        got = _frame(out, 15, os.path.join(WORK, "seek-got.png"))
        want = os.path.join(WORK, "seek-want.png")
        wrong = os.path.join(WORK, "seek-wrong.png")
        _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
              "-ss", "4.5", "-i", src, "-frames:v", "1", want])
        _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
              "-ss", "0.5", "-i", src, "-frames:v", "1", wrong])
        a = np.asarray(Image.open(got).convert("RGB"), dtype=np.float64)
        b = np.asarray(Image.open(want).convert("RGB"), dtype=np.float64)
        c = np.asarray(Image.open(wrong).convert("RGB"), dtype=np.float64)
        mae_ok = float(np.abs(a - b).mean())
        mae_wrong = float(np.abs(a - c).mean())
        self.assertLess(mae_ok, 5.0,
                        f"seek frame MAE {mae_ok:.2f} vs source t=4.5")
        # testsrc2 at 0.5 s vs 4.5 s differs by only MAE ~9 (most
        # of the pattern is static), so the bar is a clear
        # separation from the right frame, not an absolute 10.
        self.assertGreater(mae_wrong, 6.0,
                           f"frame matches t=0.5 (MAE {mae_wrong:.2f}) — "
                           "seek did not happen")
        self.assertLess(mae_ok, mae_wrong * 0.5,
                        f"seek frame not clearly the t=4.5 frame: "
                        f"{mae_ok:.2f} vs {mae_wrong:.2f}")

    def test_audio_only_direct_graph(self):
        """A wav export renders the audio graph directly — no video
        intermediate file, and the mix is identical to the video
        path (same volume-keyframe RMS ratio ~0.58)."""
        proj = {"version": 1, "name": "r8-wav", "width": 320,
                "height": 180, "fps": 30,
                "clips": [{"src": "color:#000000", "duration": 4,
                           "muted": True}],
                "audio": [{"src": self.tone, "role": "voice",
                           "start": 0, "duration": 4,
                           "volume_keyframes": [
                               {"time": 0.0, "value": 1.0},
                               {"time": 4.0, "value": 0.0}]}]}
        workdir = os.path.join(WORK, "render-wav")
        out = os.path.join(WORK, "direct.wav")
        Renderer(proj, workdir=workdir).render(out, quiet=True)
        self.assertFalse(
            os.path.exists(os.path.join(workdir, "audio_export_tmp.mp4")),
            "direct audio graph must not create a video intermediate")
        self.assertTrue(os.path.isfile(out) and os.path.getsize(out) > 0)
        with wave.open(out, "rb") as w:
            dur = w.getnframes() / w.getframerate()
        self.assertAlmostEqual(dur, 4.0, delta=0.05)
        # Stereo-aware RMS (the graph outputs stereo).
        with wave.open(out, "rb") as w:
            n = w.getnframes()
            ch = w.getnchannels()
            raw = w.readframes(n)
        import struct
        s = np.asarray(struct.unpack(f"<{n * ch}h", raw),
                       dtype=np.float64)
        sr = 48000
        early = s[int(0.3 * sr) * ch:int(0.5 * sr) * ch]
        late = s[int(1.8 * sr) * ch:int(2.0 * sr) * ch]
        ratio = (float(np.sqrt((late ** 2).mean())) /
                 float(np.sqrt((early ** 2).mean())))
        self.assertGreater(ratio, 0.45, f"wav RMS ratio {ratio:.2f}, want ~0.58")
        self.assertLess(ratio, 0.72, f"wav RMS ratio {ratio:.2f}, want ~0.58")

    def test_filter_threads_flag_in_command(self):
        """Renderer(filter_threads=4) inserts -filter_complex_threads
        4 before -filter_complex; 0 omits it; default is auto
        (min(cpu_count, 8))."""
        proj = self._scale_proj([{"time": 0.0, "value": 1.0}])
        r4 = Renderer(proj, workdir=os.path.join(WORK, "render-ft4"),
                      filter_threads=4)
        cmd4, _, _ = r4.build_command(os.path.join(WORK, "ft4.mp4"))
        self.assertIn("-filter_complex_threads", cmd4)
        i = cmd4.index("-filter_complex_threads")
        self.assertEqual(cmd4[i + 1], "4")
        self.assertLess(i, cmd4.index("-filter_complex"))
        r0 = Renderer(proj, workdir=os.path.join(WORK, "render-ft0"),
                      filter_threads=0)
        cmd0, _, _ = r0.build_command(os.path.join(WORK, "ft0.mp4"))
        self.assertNotIn("-filter_complex_threads", cmd0)
        ra = Renderer(proj, workdir=os.path.join(WORK, "render-fta"))
        cmda, _, _ = ra.build_command(os.path.join(WORK, "fta.mp4"))
        self.assertIn("-filter_complex_threads", cmda)
        ia = cmda.index("-filter_complex_threads")
        self.assertEqual(cmda[ia + 1], str(min(os.cpu_count() or 4, 8)))


if __name__ == "__main__":
    unittest.main()
