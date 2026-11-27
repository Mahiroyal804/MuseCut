"""MuseCut round-2 tests — no-ML gaps from COMPARISON.md.

Features under test (each: unit test + real render proof + frame
check, same discipline as test_v3.py):

1. remove flicker — ffmpeg ``deflicker`` (per-clip effects.deflicker)
2. object lock — timeline lock: ``locked`` pins an element,
   ``lock_to`` binds an overlay/text to an anchor clip's keyframed
   position (NOT motion tracking — it follows authored keyframes)
3. caption animations — native ASS: pop / slide / typewriter
4. keyframe easing — per-keyframe linear / ease-in / ease-out /
   ease-in-out, evaluated per frame by ffmpeg
5. per-word bloom — ``captions.word_bloom``: only the active word
   glows, in its own colour (fixes the round-1 note that
   bloom_color tinted EVERY word's outline gold)

Renders go under ~/workspace/.tmp/musecut-test/round2/ at small
sizes (640x360, 1-3s). Luma is measured on the true Y plane
(format=gray), as in test_v3.py.

Run:  python3 -m unittest discover -s tests -v
(from the repo root; needs ffmpeg on PATH)
"""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

from PIL import Image, ImageStat  # noqa: E402
from musecut import captions as cap_mod  # noqa: E402
from musecut import effects as fx  # noqa: E402
from musecut import keyframes as kf_mod  # noqa: E402
from musecut.render import Renderer  # noqa: E402
from musecut.timeline import TimelineError, validate_timeline  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test/round2")
MEDIA = os.path.join(WORK, "media")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def _make_media():
    os.makedirs(MEDIA, exist_ok=True)
    jobs = [
        (["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          os.path.join(MEDIA, "testsrc.mp4")]),
        # Flickering source: testsrc2 with a sinusoidal luma wobble.
        (["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
          "-vf", "geq=lum_expr='lum(X,Y)+40*sin(N*1.5)':"
                 "cb_expr='cb(X,Y)':cr_expr='cr(X,Y)'",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          os.path.join(MEDIA, "flicker.mp4")]),
    ]
    for argv in jobs:
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error"] + argv)
        assert res.returncode == 0, res.stderr
    # White circle r=60 on black, 640x360 (easing render probe).
    img = Image.new("RGB", (640, 360), (0, 0, 0))
    px = img.load()
    for y in range(360):
        for x in range(640):
            if (x - 320) ** 2 + (y - 180) ** 2 <= 60 * 60:
                px[x, y] = (255, 255, 255)
    img.save(os.path.join(MEDIA, "circle60.png"))
    # White 40x40 square (object-lock overlay).
    Image.new("RGB", (40, 40), (255, 255, 255)).save(
        os.path.join(MEDIA, "square.png"))


def _ffprobe(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height,r_frame_rate",
                "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _proj(width, height, clips, **kw):
    doc = {"version": 1, "name": "round2", "width": width, "height": height,
           "fps": 30, "clips": clips}
    doc.update(kw)
    return doc


def _render(proj, name):
    out = os.path.join(WORK, name + ".mp4")
    renderer = Renderer(proj, workdir=os.path.join(WORK, "render-" + name))
    renderer.render(out, quiet=True)
    assert os.path.isfile(out) and os.path.getsize(out) > 1000, name
    return out


def _frame(video, out, t=None, n=None, gray=True):
    """Extract one frame: by time ``t`` or exact frame number ``n``."""
    vf = "select='eq(n\\,%d)'" % n if n is not None else None
    if gray:
        vf = (vf + ",format=gray") if vf else "format=gray"
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error"]
    if t is not None:
        cmd += ["-ss", str(t)]
    cmd += ["-i", video]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", "1", out]
    res = _run(cmd)
    assert res.returncode == 0, res.stderr
    return Image.open(out)


def _frame_rgb(video, out, n):
    return _frame(video, out, n=n, gray=False).convert("RGB")


def _luma_mean(img, box=None):
    region = img.convert("L").crop(box) if box else img.convert("L")
    return ImageStat.Stat(region).mean[0]


def _bright_count(img, thresh=100):
    return sum(1 for v in img.convert("L").getdata() if v > thresh)


def _blob_bbox(img, thresh=100):
    """Bounding box of the bright blob in a gray frame."""
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    xs = [x for x in range(0, w, 2)
          for y in range(0, h, 2) if px[x, y] > thresh]
    ys = [y for x in range(0, w, 2)
          for y in range(0, h, 2) if px[x, y] > thresh]
    assert xs and ys, "no bright blob found in frame"
    return min(xs), max(xs), min(ys), max(ys)


def _blob_centroid(img, thresh=100):
    x0, x1, y0, y1 = _blob_bbox(img, thresh)
    return (x0 + x1) / 2, (y0 + y1) / 2


class Round2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        _make_media()

    # -- 4. keyframe easing ------------------------------------------
    def test_easing_expr_strings(self):
        kfs = [{"time": 0.0, "value": 0.0, "easing": "ease-in"},
               {"time": 2.0, "value": 10.0}]
        expr = kf_mod.scalar_expr(kfs, 0.0)
        self.assertIn("pow(", expr)  # ease-in: pow(u,2)
        kfs_out = [{"time": 0.0, "value": 0.0, "easing": "ease-out"},
                   {"time": 2.0, "value": 10.0}]
        self.assertIn("1-pow(1-", kf_mod.scalar_expr(kfs_out, 0.0))
        kfs_io = [{"time": 0.0, "value": 0.0, "easing": "ease-in-out"},
                  {"time": 2.0, "value": 10.0}]
        expr_io = kf_mod.scalar_expr(kfs_io, 0.0)
        self.assertIn("pow(", expr_io)
        self.assertIn("if(lt(", expr_io)  # smoothstep branch
        lin = kf_mod.scalar_expr(
            [{"time": 0.0, "value": 0.0}, {"time": 2.0, "value": 10.0}], 0.0)
        self.assertNotIn("pow(", lin)  # linear stays a plain lerp
        # pair_expr carries easing through to the scalar builder
        pair = kf_mod.pair_expr(
            [{"time": 0.0, "value": [0.0, 0.0], "easing": "ease-out"},
             {"time": 2.0, "value": [1.0, 1.0]}], 0)
        self.assertIn("pow(", pair)
        # validation rejects unknown easing names
        with self.assertRaises(ValueError):
            kf_mod.validate_keyframes(
                [{"time": 0.0, "value": 0.0, "easing": "bounce"},
                 {"time": 1.0, "value": 1.0}], "keyframes.scale")
        with self.assertRaises(TimelineError):
            validate_timeline(_proj(
                640, 360, [{"src": "testsrc", "duration": 1,
                            "keyframes": {"scale": [
                                {"time": 0.0, "value": 1.0,
                                 "easing": "bounce"},
                                {"time": 1.0, "value": 2.0}]}}]))

    def test_easing_render(self):
        circle = os.path.join(MEDIA, "circle60.png")

        def width_with(easing):
            kf = {"scale": [{"time": 0.0, "value": 1.0},
                            {"time": 2.0, "value": 6.0}]}
            if easing:
                kf["scale"][0]["easing"] = easing
            out = _render(_proj(640, 360, [{"src": circle, "duration": 2,
                                             "keyframes": kf}]),
                          f"ease-{easing or 'linear'}")
            frame = _frame(out, os.path.join(WORK, f"ease-{easing}.png"),
                           n=30)  # t=1.0s, u=0.5
            x0, x1, _, _ = _blob_bbox(frame)
            return x1 - x0

        w_lin = width_with(None)
        w_in = width_with("ease-in")
        w_out = width_with("ease-out")
        # At u=0.5: linear scale 3.5 (blob ~420px), ease-in 2.25
        # (~270px), ease-out 4.75 (~570px). Base blob is 120px.
        self.assertLess(w_in, w_lin * 0.8,
                        f"ease-in blob {w_in} not smaller than linear {w_lin}")
        self.assertGreater(w_out, w_lin * 1.2,
                           f"ease-out blob {w_out} not larger than linear {w_lin}")

    # -- 1. remove flicker --------------------------------------------
    def test_deflicker_filter_string(self):
        self.assertEqual(fx.deflicker_filter(), "deflicker=size=5:mode=am")
        self.assertEqual(fx.deflicker_filter(11, "median"),
                         "deflicker=size=11:mode=median")
        with self.assertRaises(ValueError):
            fx.deflicker_filter(1)
        with self.assertRaises(ValueError):
            fx.deflicker_filter(5, "weird")
        base = _proj(640, 360, [{"src": "testsrc", "duration": 1}])
        for bad in ({"size": 1}, {"size": 200}, {"mode": "mean"}, "yes"):
            doc = dict(base)
            doc["clips"] = [dict(base["clips"][0],
                                 effects={"deflicker": bad})]
            with self.assertRaises(TimelineError, msg=str(bad)):
                validate_timeline(doc)
        good = dict(base)
        good["clips"] = [dict(base["clips"][0],
                              effects={"deflicker": {"size": 11,
                                                     "mode": "median"}})]
        validate_timeline(good)
        validate_timeline(dict(
            base, clips=[dict(base["clips"][0], effects={"deflicker": True})]))

    def test_deflicker_render(self):
        src = os.path.join(MEDIA, "flicker.mp4")
        frames = list(range(5, 80, 5))

        def luma_series(effects):
            out = _render(_proj(640, 360, [{"src": src, "duration": 3,
                                             "effects": effects}]),
                          "flick-" + ("df" if effects else "raw"))
            return [_luma_mean(_frame(
                out, os.path.join(WORK, f"fl-{n}.png"), n=n))
                for n in frames]

        raw = luma_series({})
        df = luma_series({"deflicker": {"size": 5, "mode": "am"}})
        s_raw, s_df = statistics.pstdev(raw), statistics.pstdev(df)
        self.assertGreater(s_raw, 15, f"flicker source too flat: {s_raw}")
        self.assertLess(s_df, s_raw * 0.5,
                        f"deflicker did not calm the flicker: "
                        f"raw std {s_raw:.1f} -> df std {s_df:.1f}")

    # -- 2. object lock ------------------------------------------------
    def test_object_lock_validation(self):
        base = _proj(640, 360, [{"src": "color:#000000", "duration": 2}])
        # locked clip + transform keyframes -> error
        bad = dict(base)
        bad["clips"] = [dict(base["clips"][0], locked=True, keyframes={
            "position": [{"time": 0.0, "value": [0.5, 0.5]},
                         {"time": 2.0, "value": [0.8, 0.5]}]})]
        with self.assertRaises(TimelineError):
            validate_timeline(bad)
        # lock_to with a clip index that does not exist -> error
        bad2 = _proj(640, 360, [{"src": "color:#000000", "duration": 2}],
                     overlays=[{"src": os.path.join(MEDIA, "square.png"),
                                "start": 0, "duration": 2,
                                "lock_to": {"clip": 3}}])
        with self.assertRaises(TimelineError):
            validate_timeline(bad2)
        # an element cannot be both locked and lock_to-bound
        bad3 = _proj(640, 360, [{"src": "color:#000000", "duration": 2}],
                     overlays=[{"src": os.path.join(MEDIA, "square.png"),
                                "start": 0, "duration": 2, "locked": True,
                                "lock_to": {"clip": 0}}])
        with self.assertRaises(TimelineError):
            validate_timeline(bad3)
        # valid: locked clip (no keyframes), lock_to on clip 0
        good = _proj(640, 360,
                     [{"src": "color:#000000", "duration": 2,
                       "locked": True}],
                     overlays=[{"src": os.path.join(MEDIA, "square.png"),
                                "start": 0, "duration": 2,
                                "lock_to": {"clip": 0, "dx": -20,
                                            "dy": -20}}])
        validate_timeline(good)

    def test_object_lock_overlay_render(self):
        square = os.path.join(MEDIA, "square.png")
        anchor = {"src": "color:#000000", "duration": 2,
                  "keyframes": {"position": [
                      {"time": 0.0, "value": [0.2, 0.5]},
                      {"time": 2.0, "value": [0.8, 0.5]}]}}
        locked = _render(_proj(
            640, 360, [anchor],
            overlays=[{"src": square, "start": 0, "duration": 2,
                       "lock_to": {"clip": 0, "dx": -20, "dy": -20}}]),
            "lock-overlay")
        cx6 = _blob_centroid(_frame(locked, os.path.join(WORK, "lo-6.png"),
                                    n=6))[0]
        cx54 = _blob_centroid(_frame(locked, os.path.join(WORK, "lo-54.png"),
                                     n=54))[0]
        # Anchor x moves 0.2 -> 0.8 over 2s: at n=6 (t=0.2) x~0.26*640,
        # at n=54 (t=1.8) x~0.74*640. The square (40px, offset -20)
        # must travel right with it.
        self.assertGreater(cx54 - cx6, 200,
                           f"locked overlay did not follow anchor: "
                           f"x {cx6:.0f} -> {cx54:.0f}")
        # Control: the same square pinned at a static position stays.
        static = _render(_proj(
            640, 360, [dict(anchor)],
            overlays=[{"src": square, "start": 0, "duration": 2,
                       "x": "center", "y": "center"}]),
            "lock-control")
        sx6 = _blob_centroid(_frame(static, os.path.join(WORK, "lc-6.png"),
                                    n=6))[0]
        sx54 = _blob_centroid(_frame(static, os.path.join(WORK, "lc-54.png"),
                                     n=54))[0]
        self.assertLess(abs(sx54 - sx6), 5,
                        f"unlocked control moved: {sx6:.0f} -> {sx54:.0f}")

    def test_object_lock_text_render(self):
        anchor = {"src": "color:#000000", "duration": 2,
                  "keyframes": {"position": [
                      {"time": 0.0, "value": [0.2, 0.5]},
                      {"time": 2.0, "value": [0.8, 0.5]}]}}
        out = _render(_proj(
            640, 360, [anchor],
            texts=[{"text": "HELLO", "start": 0, "duration": 2,
                    "size": 48, "lock_to": {"clip": 0, "dx": -40,
                                            "dy": -20}}]),
            "lock-text")
        cx6 = _blob_centroid(_frame(out, os.path.join(WORK, "lt-6.png"),
                                    n=6))[0]
        cx54 = _blob_centroid(_frame(out, os.path.join(WORK, "lt-54.png"),
                                     n=54))[0]
        self.assertGreater(cx54 - cx6, 150,
                           f"locked text did not follow anchor: "
                           f"x {cx6:.0f} -> {cx54:.0f}")

    # -- 3. caption animations -----------------------------------------
    def test_caption_animation_ass_strings(self):
        words = [{"word": "HELLO", "start": 0.0, "end": 0.4},
                 {"word": "WORLD", "start": 0.4, "end": 0.9}]
        pop = cap_mod.words_to_ass(words, width=640, height=360,
                                   position="mid", animation="pop")
        pop_line = [l for l in pop.splitlines()
                    if l.startswith("Dialogue")][0]
        self.assertIn("\\fscx140\\fscy140\\t(0,150,\\fscx100\\fscy100)",
                      pop_line)
        self.assertIn("\\fscx100\\fscy100", pop_line)
        slide = cap_mod.words_to_ass(words, width=640, height=360,
                                     position="mid", animation="slide")
        slide_lines = [l for l in slide.splitlines()
                       if l.startswith("Dialogue")]
        # Only the phrase's FIRST event slides in.
        self.assertIn("\\move(320,260,320,180,0,200)", slide_lines[0])
        self.assertNotIn("\\move(", slide_lines[1])
        tw = cap_mod.words_to_ass(
            [{"word": "TYPE", "start": 0.0, "end": 0.8}],
            width=640, height=360, position="mid", animation="typewriter")
        tw_line = [l for l in tw.splitlines()
                   if l.startswith("Dialogue")][0]
        self.assertIn("\\alpha&HFF&\\t(0,200,\\alpha&H00&)}T", tw_line)
        self.assertIn("\\alpha&HFF&\\t(600,800,\\alpha&H00&)}E", tw_line)
        with self.assertRaises(ValueError):
            cap_mod.words_to_ass(words, animation="spin")
        base = _proj(640, 360, [{"src": "testsrc", "duration": 1}],
                     captions={"words": words, "animation": "spin"})
        with self.assertRaises(TimelineError):
            validate_timeline(base)

    def test_pop_render(self):
        words = [{"word": "POP", "start": 0.0, "end": 0.8}]
        out = _render(_proj(640, 360,
                            [{"src": "color:#000000", "duration": 2}],
                            captions={"words": words, "position": "mid",
                                      "animation": "pop"}),
                      "anim-pop")
        x0, x1, _, _ = _blob_bbox(_frame(out, os.path.join(WORK, "pop-1.png"),
                                         n=1))
        y0, y1, _, _ = _blob_bbox(_frame(out, os.path.join(WORK, "pop-15.png"),
                                         n=15))
        w_early, w_late = x1 - x0, y1 - y0
        self.assertGreater(w_early, w_late * 1.15,
                           f"pop did not start larger: {w_early} vs {w_late}")

    def test_slide_render(self):
        words = [{"word": "SLIDE", "start": 0.0, "end": 0.8}]
        out = _render(_proj(640, 360,
                            [{"src": "color:#000000", "duration": 2}],
                            captions={"words": words, "position": "mid",
                                      "animation": "slide"}),
                      "anim-slide")
        none = _render(_proj(640, 360,
                             [{"src": "color:#000000", "duration": 2}],
                             captions={"words": words, "position": "mid"}),
                       "anim-slide-none")
        _, _, y0e, y1e = _blob_bbox(_frame(
            out, os.path.join(WORK, "sl-1.png"), n=1))
        _, _, y0l, y1l = _blob_bbox(_frame(
            out, os.path.join(WORK, "sl-15.png"), n=15))
        _, _, y0n, y1n = _blob_bbox(_frame(
            none, os.path.join(WORK, "sln-15.png"), n=15))
        cy_early, cy_late, cy_none = ((y0e + y1e) / 2, (y0l + y1l) / 2,
                                      (y0n + y1n) / 2)
        # During the slide the line sits ~80px below its final spot...
        self.assertGreater(cy_early - cy_late, 40,
                           f"slide did not start below: {cy_early} vs {cy_late}")
        # ...and after the animation it lands exactly where the
        # un-animated caption sits (the \move end point is right).
        self.assertLess(abs(cy_late - cy_none), 4,
                        f"slide landed off-target: {cy_late} vs {cy_none}")

    def test_typewriter_render(self):
        words = [{"word": "TYPEWRITER", "start": 0.0, "end": 1.5}]
        out = _render(_proj(640, 360,
                            [{"src": "color:#000000", "duration": 2}],
                            captions={"words": words, "position": "mid",
                                      "animation": "typewriter"}),
                      "anim-typewriter")
        counts = [_bright_count(_frame(
            out, os.path.join(WORK, f"tw-{n}.png"), n=n))
            for n in (2, 15, 30, 44)]
        self.assertLess(counts[0], 50, f"first char visible too early: {counts}")
        self.assertGreater(counts[1], counts[0] + 100, f"no growth: {counts}")
        self.assertGreater(counts[2], counts[1] + 100, f"no growth: {counts}")
        self.assertGreater(counts[3], counts[2] + 100, f"no growth: {counts}")

    # -- 5. per-word bloom ---------------------------------------------
    def test_per_word_bloom_ass(self):
        words = [{"word": "HELLO", "start": 0.0, "end": 0.4},
                 {"word": "FEAR", "start": 0.4, "end": 0.9}]
        doc = cap_mod.words_to_ass(
            words, width=640, height=360, bloom_color="#FFD60A",
            bloom_strength=0.6, word_bloom={"FEAR": "#0000FF"})
        style = [l for l in doc.splitlines()
                 if l.startswith("Style:")][0].split(",")
        # Style outline is black now (index 5 = OutlineColour) —
        # the round-1 bug tinted every word gold here.
        self.assertEqual(style[5], "&H00000000")
        events = [l for l in doc.splitlines() if l.startswith("Dialogue")]
        # Dialogue: 0,START,END,... — select by start time.
        fear_ev = [e for e in events
                   if e.split(",")[1] == "0:00:00.40"][0]
        hello_ev = [e for e in events
                    if e.split(",")[1] == "0:00:00.00"][0]
        # FEAR's active word gets the blue per-word bloom outline
        # (#0000FF -> ASS BGR &H00FF0000)...
        self.assertIn("\\3c&H00FF0000", fear_ev)
        # ...while HELLO's active word gets the gold bloom default
        # (#FFD60A -> &H000AD6FF), and FEAR's event carries NO gold
        # outline anywhere (its glow is blue, its reset is black).
        self.assertIn("\\3c&H000AD6FF", hello_ev)
        self.assertNotIn("\\3c&H000AD6FF", fear_ev)
        # Backwards compat: bloom_color alone still glows the active
        # word (gold), inactive words get a black outline.
        doc2 = cap_mod.words_to_ass(words, width=640, height=360,
                                    bloom_color="#FFD60A",
                                    bloom_strength=0.6)
        ev2 = [l for l in doc2.splitlines()
               if l.startswith("Dialogue")][0]
        self.assertIn("\\3c&H000AD6FF", ev2)

    def test_per_word_bloom_render(self):
        words = [{"word": "FEAR", "start": 0.0, "end": 1.0}]

        def outline_blue_minus_red(caps):
            out = _render(_proj(640, 360,
                                [{"src": "color:#000000", "duration": 2}],
                                captions=dict({"words": words,
                                               "position": "mid"},
                                              **caps)),
                          "bloom-" + ("word" if "word_bloom" in caps
                                      else "plain"))
            img = _frame_rgb(out, os.path.join(WORK, "bloom.png"), 15)
            px = img.load()
            diffs = []
            for y in range(0, 360, 2):
                for x in range(0, 640, 2):
                    r, g, b = px[x, y]
                    luma = 0.299 * r + 0.587 * g + 0.114 * b
                    if 20 < luma < 200:  # outline pixels, not fill/bg
                        diffs.append(b - r)
            assert diffs, "no outline pixels found"
            return sum(diffs) / len(diffs)

        blue = outline_blue_minus_red(
            {"bloom_color": "#FFD60A", "bloom_strength": 0.8,
             "word_bloom": {"FEAR": "#0000FF"}})
        gold = outline_blue_minus_red(
            {"bloom_color": "#FFD60A", "bloom_strength": 0.8})
        self.assertGreater(blue, 20, f"blue bloom outline not blue: {blue}")
        self.assertLess(gold, -20, f"gold bloom outline not gold: {gold}")


if __name__ == "__main__":
    unittest.main()
