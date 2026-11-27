"""MuseCut v3 round-1 tests — gaps 1-9 from COMPARISON.md, closed natively.

Each feature gets a unit test AND a real render proof with a frame
check (PIL). Renders go under ~/workspace/.tmp/musecut-test/v3/ at
small sizes (640x360 / 720x1280 / 1280x720, 1-2s).

Luma is measured on the true Y plane (frames extracted with
``format=gray``): measuring via RGB->L would mix chroma back in and
a truly black luma frame (e.g. a glitch strobe frame, whose chroma
survives) would read ~79 instead of ~0 — verified while writing
these tests.

Run:  python3 -m unittest discover -s tests -v
(from the repo root; needs ffmpeg on PATH)
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

from PIL import Image, ImageChops, ImageStat  # noqa: E402
from musecut import captions as cap_mod  # noqa: E402
from musecut import effects as fx  # noqa: E402
from musecut.render import Renderer  # noqa: E402
from musecut.timeline import TimelineError, validate_timeline  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test/v3")
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
        (["-f", "lavfi", "-i", "smptebars=size=1280x720:rate=30:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          os.path.join(MEDIA, "smptebars.mp4")]),
    ]
    for argv in jobs:
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error"] + argv)
        assert res.returncode == 0, res.stderr
    # 720x960 solid-orange portrait (for the blur-canvas fill test)
    Image.new("RGB", (720, 960), (255, 136, 0)).save(
        os.path.join(MEDIA, "portrait.png"))
    # 1280x720 black frame + white circle, centred (zoompan aspect test)
    for name, radius in (("circle200.png", 200), ("circle120.png", 120)):
        img = Image.new("RGB", (1280, 720), (0, 0, 0))
        px = img.load()
        cx, cy = 640, 360
        for y in range(720):
            for x in range(1280):
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius:
                    px[x, y] = (255, 255, 255)
        img.save(os.path.join(MEDIA, name))


def _ffprobe(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height,r_frame_rate",
                "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _proj(width, height, clips, **kw):
    doc = {"version": 1, "name": "v3", "width": width, "height": height,
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


def _luma_mean(img, box=None):
    """Mean luma of a (gray) frame, optionally inside ``box``."""
    region = img.convert("L").crop(box) if box else img.convert("L")
    return ImageStat.Stat(region).mean[0]


def _luma_max(img, box=None):
    region = img.convert("L").crop(box) if box else img.convert("L")
    return region.getextrema()[1]


def _mean_abs_diff(a, b):
    return ImageStat.Stat(
        ImageChops.difference(a.convert("L"), b.convert("L"))).mean[0]


def _white_blob_bbox(img, thresh=128):
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


def _words():
    return [
        {"word": "ONE", "start": 0.0, "end": 0.4},
        {"word": "TWO", "start": 0.4, "end": 0.8},
        {"word": "THREE", "start": 0.8, "end": 1.2},
        {"word": "FOUR", "start": 1.2, "end": 1.6},
        {"word": "FIVE", "start": 1.6, "end": 2.0},
    ]


class V3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        _make_media()

    # -- 1. native letterbox -----------------------------------------
    def test_letterbox_validation(self):
        base = _proj(1280, 720, [{"src": "testsrc", "duration": 1}])
        for bad in ({"top": 0.5}, {"bottom": 0.45}, {"top": -0.1},
                    {"side": 0.1}, "wide", 0.125):
            doc = dict(base, letterbox=bad)
            with self.assertRaises(TimelineError, msg=str(bad)):
                validate_timeline(doc)
        for good in (True, {"top": 0.125, "bottom": 0.125},
                     {"top": 0.0, "bottom": 0.4}, {"top": 0.2}):
            validate_timeline(dict(base, letterbox=good))

    def test_letterbox_render(self):
        proj = _proj(1280, 720, [{"src": "testsrc", "duration": 2}],
                     letterbox=True,
                     endcard={"duration": 1, "title": "NEXT",
                              "background": "#FF0000"})
        out = _render(proj, "letterbox")
        frame = _frame(out, os.path.join(WORK, "lb.png"), t=1.0)
        top = _luma_mean(frame, (0, 0, 1280, 57))       # top 8% of rows
        centre = _luma_mean(frame, (0, 288, 1280, 432))  # centre band
        self.assertLess(top, 8, f"top bar not black: {top}")
        self.assertGreater(centre, 30, f"centre too dark: {centre}")
        # end card is concatenated AFTER the letterbox stage: its top
        # rows stay red, not black-barred.
        ec = _frame(out, os.path.join(WORK, "lb-ec.png"), t=2.5, gray=False)
        red = ImageStat.Stat(ec.convert("RGB").crop((0, 0, 1280, 36))).mean[0]
        self.assertGreater(red, 150, f"end card top rows not red: {red}")

    # -- 2. mid-frame multi-colour karaoke ----------------------------
    def test_karaoke_ass_mid_multicolor(self):
        words = [{"word": "HELLO", "start": 0.0, "end": 0.4},
                 {"word": "world", "start": 0.4, "end": 0.9}]
        doc = cap_mod.words_to_ass(
            words, width=1280, height=720, position="mid",
            word_colors={"hello": "#FF0000"})
        style = [l for l in doc.splitlines() if l.startswith("Style:")][0]
        # Alignment 5 (middle-centre), MarginV 0 for mid
        self.assertIn(",5,40,40,0,1", style)
        # HELLO active -> red #FF0000 as ASS BGR &H000000FF
        self.assertIn("{\\1c&H000000FF}HELLO", doc)
        # world active -> default gold #FFD60A as &H000AD6FF
        self.assertIn("{\\1c&H000AD6FF}world", doc)
        top = cap_mod.words_to_ass(words, width=1280, height=720,
                                   position="top")
        style_top = [l for l in top.splitlines()
                     if l.startswith("Style:")][0]
        self.assertIn(",8,40,40,36,1", style_top)  # Alignment 8, MV 36
        low = cap_mod.words_to_ass(words, width=1280, height=720)
        style_low = [l for l in low.splitlines()
                     if l.startswith("Style:")][0]
        self.assertIn(",2,40,40,54,1", style_low)  # Alignment 2, MV 54
        with self.assertRaises(ValueError):
            cap_mod.words_to_ass(words, position="sideways")

    def test_karaoke_mid_render(self):
        proj = _proj(1280, 720,
                     [{"src": "color:#000000", "duration": 2}],
                     captions={"position": "mid",
                               "word_colors": {"THREE": "#FF0000"},
                               "words": _words()})
        out = _render(proj, "karaoke-mid")
        frame = _frame(out, os.path.join(WORK, "km.png"), t=1.0)
        mid = _luma_mean(frame, (0, 288, 1280, 432))
        mid_max = _luma_max(frame, (0, 288, 1280, 432))
        low = _luma_mean(frame, (0, 612, 1280, 720))
        # The active word at 1.0s is THREE (red, luma ~76), so the
        # band mean is modest; the bright-white inactive words are
        # what push the max up. Together with a black lower third
        # this proves mid-frame placement.
        self.assertGreater(mid, 5, f"mid band has no caption: {mid}")
        self.assertGreater(mid_max, 150, "no bright caption text mid-frame")
        self.assertLess(low, 5, f"caption leaked to lower third: {low}")

    # -- 3. diagonal mask seam ----------------------------------------
    def test_diagonal_mask_filter_string(self):
        f = fx.mask_filter("diagonal", 0.5, 0.5, 0.4, 0.05, 45)
        self.assertIn("geq", f)
        f90 = fx.mask_filter("diagonal", 0.5, 0.5, 0.4, 0.05, 90)
        # angle=90: sin=1, cos=0 -> d is the aspect-corrected dx only,
        # i.e. the same vertical seam as "linear" (white left of x)
        self.assertIn("*1.000000", f90)
        self.assertIn("*0.000000", f90)
        with self.assertRaises(ValueError):
            fx.mask_filter("triangle")
        # validation: angle range
        base = _proj(640, 360, [{"src": "testsrc", "duration": 1,
                                 "mask": {"shape": "diagonal",
                                          "angle": 200}}])
        with self.assertRaises(TimelineError):
            validate_timeline(base)
        validate_timeline(_proj(
            640, 360, [{"src": "testsrc", "duration": 1,
                        "mask": {"shape": "diagonal", "angle": -180,
                                 "effect": "desaturate"}}]))

    def test_diagonal_mask_render(self):
        src = os.path.join(MEDIA, "testsrc.mp4")
        mask = {"shape": "diagonal", "x": 0.5, "y": 0.5, "size": 0.4,
                "feather": 0.05, "angle": 45, "effect": "darken"}
        masked = _render(_proj(1280, 720,
                               [{"src": src, "duration": 2, "mask": mask}]),
                         "mask-diagonal")
        clean = _render(_proj(1280, 720, [{"src": src, "duration": 2}]),
                        "mask-clean")
        fm = _frame(masked, os.path.join(WORK, "md.png"), t=1.0)
        fc = _frame(clean, os.path.join(WORK, "mc.png"), t=1.0)
        a = math.radians(45)
        sa, ca = math.sin(a), math.cos(a)
        W, H = 1280, 720
        darkened = total = 0
        dpos_diffs, dneg_diffs = [], []
        for gy in range(0, H, 64):
            for gx in range(0, W, 64):
                box = (gx, gy, min(gx + 16, W), min(gy + 16, H))
                diff = _luma_mean(fm, box) - _luma_mean(fc, box)
                total += 1
                if diff < -20:
                    darkened += 1
                px, py = gx + 8, gy + 8
                d = ((px / W - 0.5) * (W / H)) * sa - (py / H - 0.5) * ca
                (dpos_diffs if d > 0.05 else
                 dneg_diffs if d < -0.05 else []).append(diff)
        frac = darkened / total
        self.assertGreater(frac, 0.15, f"too little darkened: {frac}")
        self.assertLess(frac, 0.85, f"too much darkened: {frac}")
        self.assertLess(sum(dpos_diffs) / len(dpos_diffs), -20,
                        "d>0 side should be darkened")
        self.assertGreater(sum(dneg_diffs) / len(dneg_diffs), -10,
                           "d<0 side should stay original")

    def test_diagonal_90_like_linear_render(self):
        src = os.path.join(MEDIA, "testsrc.mp4")
        mask = {"shape": "diagonal", "x": 0.5, "y": 0.5, "size": 0.4,
                "feather": 0.05, "angle": 90, "effect": "darken"}
        masked = _render(_proj(1280, 720,
                               [{"src": src, "duration": 2, "mask": mask}]),
                         "mask-diag90")
        clean = _render(_proj(1280, 720, [{"src": src, "duration": 2}]),
                        "mask-clean")
        fm = _frame(masked, os.path.join(WORK, "m90.png"), t=1.0)
        fc = _frame(clean, os.path.join(WORK, "m90c.png"), t=1.0)
        left = _luma_mean(fm, (0, 0, 640, 720)) - \
            _luma_mean(fc, (0, 0, 640, 720))
        right = _luma_mean(fm, (640, 0, 1280, 720)) - \
            _luma_mean(fc, (640, 0, 1280, 720))
        self.assertGreater(left, -8, f"left half darkened: {left}")
        self.assertLess(right, -20, f"right half not darkened: {right}")

    # -- 4. desaturate mask effect ------------------------------------
    def test_desaturate_mask_render(self):
        src = os.path.join(MEDIA, "smptebars.mp4")
        mask = {"shape": "circle", "x": 0.5, "y": 0.5, "size": 0.35,
                "feather": 0.05, "effect": "desaturate"}
        out = _render(_proj(1280, 720,
                            [{"src": src, "duration": 2, "mask": mask}]),
                      "mask-desaturate")
        frame = _frame(out, os.path.join(WORK, "desat.png"), t=1.0,
                       gray=False).convert("RGB")
        W, H = 1280, 720
        px = frame.load()
        inside, outside = [], []
        for y in range(0, H, 16):
            for x in range(0, W, 16):
                r = math.hypot((x / W - 0.5) * (W / H), y / H - 0.5)
                rr, gg, bb = px[x, y]
                spread = max(rr, gg, bb) - min(rr, gg, bb)
                if r < 0.2:
                    inside.append(spread)
                elif r > 0.5:
                    outside.append(spread)
        self.assertGreater(sum(inside) / len(inside), 40,
                           "inside the circle should stay colourful")
        self.assertLess(sum(outside) / len(outside), 12,
                        "outside the circle should be B&W")

    # -- 5+6. per-clip fit: blur canvas + cover -----------------------
    def test_fit_blur_render(self):
        portrait = os.path.join(MEDIA, "portrait.png")
        blur = _render(_proj(1080, 1920,
                             [{"src": portrait, "duration": 2,
                               "fit": "blur"}]),
                       "fit-blur")
        plain = _render(_proj(1080, 1920,
                              [{"src": portrait, "duration": 2}]),
                        "fit-contain")
        for path, label in ((blur, "blur"), (plain, "contain")):
            frame = _frame(path, os.path.join(WORK, f"fb-{label}.png"),
                           t=1.0)
            corners = [_luma_mean(frame, box) for box in
                       ((0, 0, 120, 120), (960, 0, 1080, 120),
                        (0, 1800, 120, 1920), (960, 1800, 1080, 1920))]
            mean_corner = sum(corners) / len(corners)
            if label == "blur":
                self.assertGreater(mean_corner, 30,
                                   f"blur corners black: {corners}")
            else:
                self.assertLess(mean_corner, 8,
                                f"contain corners not black: {corners}")

    def test_fit_cover_render(self):
        src = os.path.join(MEDIA, "smptebars.mp4")
        out = _render(_proj(720, 1280,
                            [{"src": src, "duration": 2, "fit": "cover"}]),
                      "fit-cover")
        frame = _frame(out, os.path.join(WORK, "fc.png"), t=1.0)
        top_rows = _luma_mean(frame, (0, 0, 720, 64))
        self.assertGreater(top_rows, 30,
                           f"cover has pad bars at top: {top_rows}")
        for box in ((0, 0, 80, 80), (640, 0, 720, 80)):
            self.assertGreater(_luma_mean(frame, box), 15,
                               f"cover corner black: {box}")

    def test_fit_and_glitch_validation(self):
        base = _proj(640, 360, [{"src": "testsrc", "duration": 1}])
        bad = dict(base)
        bad["clips"] = [dict(base["clips"][0], fit="stretch")]
        with self.assertRaises(TimelineError):
            validate_timeline(bad)
        for gl in ({"strength": 1.5}, {"strobe": 61}, {"strobe": True},
                   "yes"):
            bad = dict(base)
            bad["clips"] = [dict(base["clips"][0],
                                 effects={"glitch": gl})]
            with self.assertRaises(TimelineError, msg=str(gl)):
                validate_timeline(bad)
        good = dict(base)
        good["clips"] = [dict(base["clips"][0], fit="cover",
                              effects={"glitch": {"strength": 0.5,
                                                  "strobe": 4}})]
        validate_timeline(good)
        validate_timeline(dict(
            base, clips=[dict(base["clips"][0],
                              effects={"glitch": True})]))

    # -- 7. scale keyframe cap 0..8 ------------------------------------
    def test_scale_cap_validation(self):
        base = _proj(640, 360, [{"src": "testsrc", "duration": 1}])
        ok = dict(base)
        ok["clips"] = [dict(base["clips"][0], keyframes={
            "scale": [{"time": 0.0, "value": 1.0},
                      {"time": 1.0, "value": 6.0}]})]
        validate_timeline(ok)
        bad = dict(base)
        bad["clips"] = [dict(base["clips"][0], keyframes={
            "scale": [{"time": 0.0, "value": 1.0},
                      {"time": 1.0, "value": 9.0}]})]
        with self.assertRaises(TimelineError):
            validate_timeline(bad)
        bad_op = dict(base)
        bad_op["clips"] = [dict(base["clips"][0], keyframes={
            "opacity": [{"time": 0.0, "value": 1.0},
                        {"time": 1.0, "value": 1.5}]})]
        with self.assertRaises(TimelineError):
            validate_timeline(bad_op)

    def test_scale_keyframes_render(self):
        proj = _proj(640, 360, [{"src": "testsrc", "duration": 2,
                                 "keyframes": {
                                     "scale": [
                                         {"time": 0.0, "value": 1.0},
                                         {"time": 2.0, "value": 6.0}]}}])
        out = _render(proj, "scale-6x")
        probe = _ffprobe(out)
        video = next(s for s in probe["streams"]
                     if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (640, 360))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.4)

    # -- 8. legacy zoompan aspect fix ----------------------------------
    def test_zoompan_aspect_1_to_1(self):
        src = os.path.join(MEDIA, "circle200.png")
        proj = _proj(720, 1280, [{"src": src, "duration": 2,
                                  "keyframes": {
                                      "zoom": {"from": 1.0, "to": 1.0}}}])
        out = _render(proj, "zoom-1to1")
        frame = _frame(out, os.path.join(WORK, "z11.png"), t=1.0)
        x0, x1, y0, y1 = _white_blob_bbox(frame)
        w, h = x1 - x0, y1 - y0
        self.assertLess(abs(w - h) / max(w, h), 0.10,
                        f"circle stretched to {w}x{h}")
        self.assertGreater(w, 100, "blob implausibly small")

    def test_zoompan_aspect_1_to_1p5(self):
        src = os.path.join(MEDIA, "circle120.png")
        proj = _proj(720, 1280, [{"src": src, "duration": 2,
                                  "keyframes": {
                                      "zoom": {"from": 1.0, "to": 1.5}}}])
        out = _render(proj, "zoom-1to1p5")
        frame = _frame(out, os.path.join(WORK, "z115.png"), t=1.8)
        x0, x1, y0, y1 = _white_blob_bbox(frame)
        w, h = x1 - x0, y1 - y0
        self.assertLess(abs(w - h) / max(w, h), 0.10,
                        f"circle stretched to {w}x{h} at zoom 1.5")
        self.assertGreater(w, 100, "blob implausibly small")

    # -- 9. glitch / strobe --------------------------------------------
    def test_glitch_filter_string(self):
        f = fx.glitch_filter(0.8, 4)
        self.assertIn("rgbashift", f)
        self.assertIn("geq", f)
        self.assertIn("rh=9", f)          # max(1, int(12*0.8)) = 9
        self.assertIn("if(eq(mod(N,4),0),0,lum(", f)
        self.assertIn("noise=alls=16", f)  # int(4+16*0.8) = 16
        plain = fx.glitch_filter()
        self.assertIn("rgbashift", plain)
        self.assertNotIn("mod(N,", plain)  # strobe off: no black term

    def test_glitch_render(self):
        glitched = _render(_proj(
            640, 360, [{"src": "testsrc", "duration": 2,
                        "effects": {"glitch": {"strength": 0.8,
                                               "strobe": 4}}}]),
            "glitch")
        clean = _render(_proj(640, 360,
                              [{"src": "testsrc", "duration": 2}]),
                        "glitch-clean")
        for n in (0, 4):
            frame = _frame(glitched,
                           os.path.join(WORK, f"gl-n{n}.png"), n=n)
            self.assertLess(_luma_mean(frame), 8,
                            f"strobe frame N={n} not black")
        g1 = _frame(glitched, os.path.join(WORK, "gl-n1.png"), n=1,
                    gray=False)
        c1 = _frame(clean, os.path.join(WORK, "gl-c1.png"), n=1, gray=False)
        self.assertGreater(_mean_abs_diff(g1, c1), 3,
                           "glitched frame identical to clean render")


if __name__ == "__main__":
    unittest.main()
