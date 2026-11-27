"""MuseCut round-3 tests — hybrid backend, non-ML remainders, tracking.

Covers round 3 (v0.5.0):
* version unified to 0.5.0 everywhere (__init__ + pyproject + docs)
* providers: honest wired / registered / stub reporting; YOLOv5n
  model present (workspace fallback survives VM wipes)
* tracking: REAL YOLOv5n ONNX track on a real footage clip
  (mixkit_34562_1080.mp4 — city street, car drives across frame,
  conf 0.86-0.91 verified); track_to_keyframes math; smart_reframe
  end to end (16:9 -> 9:16, car stays centred)
* 3D text, arc/circle text shape, cover templates (validate +
  render), HDR10 export (ffprobe colour metadata), ProRes alpha
  export (ffprobe pix_fmt + alpha channel mean), HEVC-with-alpha
  refused honestly

Small canvases and 1-3 s clips keep the suite fast; the tracking
tests use real footage (the one ML feature must be proven on real
video, not synthetic). Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

from musecut import __version__  # noqa: E402
from musecut import ai as ai_mod  # noqa: E402
from musecut import providers as prov_mod  # noqa: E402
from musecut import track as track_mod  # noqa: E402
from musecut import tts as tts_mod  # noqa: E402
from musecut.render import RenderError, Renderer, probe_media  # noqa: E402
from musecut.render import _expand_cover  # noqa: E402
from musecut.timeline import TimelineError, validate_timeline  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round3")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
# Real footage: city street, a car drives across the frame and the
# tracker follows it at conf 0.86-0.91 (verified 2026-11-20).
CAR_CLIP = os.path.join(POOL, "mixkit_34562_1080.mp4")
# 16:9 minivan clip for the smart-reframe demo (car drives left
# across the frame, conf 0.88-0.90).
VAN_CLIP = os.path.join(POOL, "pexels_1192116.mp4")


def _fully_provisioned():
    """True when this machine has everything test_providers_honest
    pins: the Muse TTS binary, onnxruntime, and the YOLOv5n /
    Real-ESRGAN / RMBG-1.4 models on disk."""
    return (os.path.isfile(tts_mod.MUSE_TTS_BIN)
            and prov_mod.onnxruntime_available()
            and all(prov_mod.model_status(m)["status"] == "present"
                    for m in ("yolov5n", "realesrgan-x4plus", "rmbg-1-4")))


NEEDS_PROVISIONED = unittest.skipUnless(
    _fully_provisioned(),
    "provider wiring test needs the fully provisioned machine "
    "(Muse TTS binary + onnxruntime + YOLOv5n / Real-ESRGAN / "
    "RMBG-1.4 models on disk)")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def _probe(path, entries):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                f"stream={entries}", "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _vstream(probe):
    return next(s for s in probe["streams"]
                if s["codec_type"] == "video")


def _base_proj(clips, **kw):
    proj = {"version": 1, "name": "r3", "width": 320, "height": 180,
            "fps": 30, "background": "#000000", "clips": clips,
            "overlays": [], "texts": [], "audio": [],
            "captions": None, "endcard": None, "export": None,
            "canvas": None, "sequences": {}, "markers": [],
            "adjustment_layers": []}
    proj.update(kw)
    return proj


def _make_media():
    os.makedirs(MEDIA, exist_ok=True)
    clip1 = os.path.join(MEDIA, "clip1.mp4")
    if not os.path.isfile(clip1):
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=30:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", clip1])
        assert res.returncode == 0, res.stderr
    # 2 s slice of the minivan clip, starting where the car is
    # tracked (t=2s in the source) so the tracker's times line up
    # with the clip start.
    van8 = os.path.join(MEDIA, "van2s.mp4")
    if not os.path.isfile(van8) and os.path.isfile(VAN_CLIP):
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "2", "-i", VAN_CLIP, "-t", "2", "-c:v",
                    "libx264", "-pix_fmt", "yuv420p", "-an", van8])
        assert res.returncode == 0, res.stderr


class Round3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        _make_media()

    # ---- version + providers ------------------------------------
    def test_version_unified(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())

    @NEEDS_PROVISIONED
    def test_providers_honest(self):
        st = prov_mod.backend_status()
        self.assertEqual(st["version"], __version__)
        by_name = {b["name"]: b for b in st["backends"]}
        self.assertEqual(by_name["muse-voice"]["status"], "wired")
        self.assertEqual(by_name["onnx-runtime"]["status"], "wired")
        # YOLOv5n is on disk (workspace copy survives VM wipes).
        self.assertEqual(by_name["onnx-model:yolov5n"]["status"], "wired")
        # Super resolution: Real-ESRGAN x4plus ONNX is on disk
        # (round 11 — Qualcomm AI Hub public asset, float + w8a8),
        # so the backend is wired, not a stub.
        sr = by_name["onnx-model:realesrgan-x4plus"]
        self.assertEqual(sr["status"], "wired")
        # faster-whisper captions are wired only in the venv Python;
        # in this interpreter they report stub with the reason.
        wc = by_name["whisper-captions"]
        self.assertIn(wc["status"], ("wired", "stub"))
        caps = ai_mod.capabilities()
        self.assertEqual(caps["track_object"]["status"], "done")
        self.assertEqual(caps["smart_reframe"]["status"], "done")
        self.assertEqual(caps["hdr_export"]["status"], "done")
        self.assertEqual(caps["super_resolution"]["status"], "done")  # round 11
        self.assertEqual(caps["ai_background_removal"]["status"], "done")

    # ---- tracking (real ML, real footage) ------------------------
    def test_track_real_car(self):
        if not os.path.isfile(CAR_CLIP):
            self.skipTest("footage clip missing: " + CAR_CLIP)
        t = track_mod.track_object(CAR_CLIP, class_name="car",
                                   sample_fps=2.0, conf=0.25)
        self.assertGreaterEqual(len(t["frames"]), 3)
        for fr in t["frames"]:
            self.assertGreaterEqual(fr["conf"], 0.5)
            self.assertTrue(0.0 <= fr["x"] <= 1.0)
            self.assertTrue(0.0 <= fr["y"] <= 1.0)
        # The car drives left across the frame: x must fall.
        self.assertGreater(t["frames"][0]["x"] - t["frames"][-1]["x"],
                           0.05)
        out = os.path.join(WORK, "track.json")
        self.assertEqual(track_mod.save_track(t, out), out)
        self.assertEqual(track_mod.load_track(out), t)

    def test_track_missing_video(self):
        with self.assertRaises(track_mod.TrackingError):
            track_mod.track_object("/nonexistent/nope.mp4",
                                   class_name="car")

    def test_track_unknown_class(self):
        with self.assertRaises(track_mod.TrackingError):
            track_mod.track_object(CAR_CLIP, class_name="spaceship")

    def test_track_to_keyframes(self):
        track = {"frames": [
            {"time": 0.0, "x": 0.2, "y": 0.5, "w": 0.3, "h": 0.2,
             "conf": 0.9},
            {"time": 1.0, "x": 0.8, "y": 0.5, "w": 0.3, "h": 0.2,
             "conf": 0.9},
        ]}
        kfs = track_mod.track_to_keyframes(track, 1.78)
        self.assertEqual(len(kfs), 2)
        # Subject left of centre -> frame moves right (px > 0.5);
        # subject right of centre -> frame moves left (px < 0.5).
        self.assertGreater(kfs[0]["value"][0], 0.5)
        self.assertLess(kfs[1]["value"][0], 0.5)
        # Clamped inside the cover limits.
        lim = (1.78 - 1.0) / 2.0
        for kf in kfs:
            self.assertTrue(0.5 - lim <= kf["value"][0] <= 0.5 + lim)
        with self.assertRaises(track_mod.TrackingError):
            track_mod.track_to_keyframes(track, 0.5)
        # Per-axis clamp (round 3 fix): at the 16:9->9:16 cover scale
        # the height ratio is exactly 1.0, so y never moves — a y
        # shift would expose a black bar (frame is canvas-high).
        kfs2 = track_mod.track_to_keyframes(
            {"frames": [{"time": 0.0, "x": 0.3, "y": 0.2,
                         "w": 0.1, "h": 0.1, "conf": 0.9}]},
            3.16, 1.0)
        self.assertAlmostEqual(kfs2[0]["value"][0], 1.132, delta=0.01)
        self.assertEqual(kfs2[0]["value"][1], 0.5)

    def test_ai_track_object_real(self):
        if not os.path.isfile(CAR_CLIP):
            self.skipTest("footage clip missing: " + CAR_CLIP)
        out = os.path.join(WORK, "ai_track.json")
        self.assertEqual(ai_mod.track_object(CAR_CLIP, out,
                                             class_name="car",
                                             sample_fps=2.0), out)
        t = json.load(open(out))
        self.assertGreaterEqual(len(t["frames"]), 3)

    def test_ai_smart_reframe_renders(self):
        van = os.path.join(MEDIA, "van2s.mp4")
        if not os.path.isfile(van):
            self.skipTest("minivan slice missing")
        tpath = os.path.join(WORK, "reframe.json")
        ai_mod.smart_reframe(van, tpath, class_name="car",
                             sample_fps=2.0, width=720, height=1280)
        with open(tpath) as f:
            proj = json.load(f)
        self.assertEqual((proj["width"], proj["height"]), (720, 1280))
        clip = proj["clips"][0]
        # (round 3 fix) fit "none" + cover-scale keyframes — NOT
        # fit "cover", which double-scaled (grid diagnostic).
        self.assertEqual(clip["fit"], "none")
        self.assertAlmostEqual(
            clip["keyframes"]["scale"][0]["value"], 1280 / 720,
            delta=0.01)
        kfs = clip["keyframes"]["position"]
        self.assertGreaterEqual(len(kfs), 3)
        # 16:9->9:16 at the cover scale: zero vertical slack, so
        # every py keyframe is exactly 0.5 (no black bars).
        for kf in kfs:
            self.assertEqual(kf["value"][1], 0.5)
        # The crop actually moves (distinct keyframe values).
        vals = {tuple(kf["value"]) for kf in kfs}
        self.assertGreater(len(vals), 1)
        out = os.path.join(WORK, "reframe.mp4")
        Renderer(proj).render(out)
        probe = _probe(out, "codec_type,codec_name,width,height")
        v = _vstream(probe)
        self.assertEqual((v["width"], v["height"]), (720, 1280))
        # Round 4 trims the reframe to the tracked span (1.5s here),
        # not the full 2.0s clip.
        self.assertAlmostEqual(float(probe["format"]["duration"]), 1.5,
                               delta=0.3)
        # No black bars: every row of a mid-frame has content.
        from PIL import Image
        import numpy as np
        frame = os.path.join(WORK, "reframe_frame.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "1.0", "-i", out, "-frames:v", "1", frame])
        self.assertEqual(res.returncode, 0)
        img = np.asarray(Image.open(frame).convert("L"), dtype=float)
        rows = np.where(img.mean(axis=1) > 12)[0]
        self.assertEqual((rows.min(), rows.max()), (0, 1279))

    def test_reframe_geometry_grid(self):
        """Grid diagnostic (regression test for the two reframe bugs).

        A 1280x720 grid clip (vertical lines every 128px) reframed
        to 720x1280 with a fake track (subject 0.3 -> 0.7). The
        subject must land on the canvas centre at every instant —
        measured from the grid lines in the rendered frame, not from
        the keyframe numbers. Catches (1) the fit="cover" double
        scale (grid spacing would be 404px instead of 228px) and
        (2) a wrong y clamp (black bars top/bottom).
        """
        grid = os.path.join(MEDIA, "grid.mp4")
        if not os.path.isfile(grid):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-f", "lavfi", "-i",
                        "color=c=0x202020:size=1280x720:rate=30:duration=2",
                        "-vf", "drawgrid=w=128:h=72:c=0x808080:t=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", grid])
            self.assertEqual(res.returncode, 0, res.stderr)
        trk = {"frames": [
            {"time": 0.0, "x": 0.3, "y": 0.5, "w": 0.1, "h": 0.1,
             "conf": 0.9},
            {"time": 1.0, "x": 0.7, "y": 0.5, "w": 0.1, "h": 0.1,
             "conf": 0.9},
        ]}
        e_x = (1280 / 720) * (1280 / 720)   # 3.16
        e_y = (720 / 1280) * (1280 / 720)   # 1.0
        kfs = track_mod.track_to_keyframes(trk, e_x, e_y)
        proj = _base_proj([], width=720, height=1280)
        proj["clips"] = [{
            "src": grid, "duration": 2.0, "fit": "none",
            "keyframes": {"position": kfs,
                          "scale": [{"time": 0.0,
                                     "value": 1280 / 720}]},
        }]
        out = os.path.join(WORK, "grid_ref.mp4")
        Renderer(proj).render(out)
        from PIL import Image
        import numpy as np
        frame = os.path.join(WORK, "grid_frame.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.3", "-i", out, "-frames:v", "1", frame])
        self.assertEqual(res.returncode, 0)
        img = np.asarray(Image.open(frame).convert("L"), dtype=float)
        # No black bars (y clamp correct).
        rows = np.where(img.mean(axis=1) > 12)[0]
        self.assertEqual((rows.min(), rows.max()), (0, 1279))
        # Vertical grid lines: spacing must be 128 * 1.7778 = 228px
        # (single cover zoom), NOT 404px (double scale).
        row = img[100, :]
        above = row > 80
        runs, in_run = [], False
        for i, v in enumerate(above):
            if v and not in_run:
                start = i
                in_run = True
            elif not v and in_run:
                runs.append((start, i - 1))
                in_run = False
        lines = [round((a + b) / 2) for a, b in runs]
        self.assertGreaterEqual(len(lines), 2)
        spacings = [b - a for a, b in zip(lines, lines[1:])]
        for sp in spacings:
            self.assertAlmostEqual(sp, 228, delta=15)
        # At t=0.3 the subject is at source x=0.42 (0.3 -> 0.7 over
        # 1s); the grid line at source x=512 (0.4) must sit ~46px
        # left of centre (360 - 0.02*2278 = 314).
        self.assertTrue(any(abs(x - 314) <= 20 for x in lines),
                        f"grid lines {lines} — subject not centred")

    # ---- validation: 3D text / shape / cover / export ------------
    def test_3d_text_validation(self):
        bad_depth = _base_proj([{"src": "color:000000", "duration": 1}],
                               texts=[{"text": "X", "start": 0,
                                       "duration": 1, "3d": {"depth": 0}}])
        with self.assertRaises(TimelineError):
            validate_timeline(bad_depth)
        bad_depth2 = _base_proj([{"src": "color:000000", "duration": 1}],
                                texts=[{"text": "X", "start": 0,
                                        "duration": 1,
                                        "3d": {"depth": 21}}])
        with self.assertRaises(TimelineError):
            validate_timeline(bad_depth2)
        for d3 in (True, {"depth": 6}, {"depth": 12,
                                        "depth_color": "#FF0000"}):
            ok = _base_proj([{"src": "color:000000", "duration": 1}],
                            texts=[{"text": "X", "start": 0, "duration": 1,
                                    "3d": d3}])
            validate_timeline(ok)

    def test_shape_validation(self):
        for shape in ({"type": "spiral"}, {"type": "arc", "angle": 10},
                      {"type": "arc", "radius": 10},
                      {"type": "circle", "angle": 400}):
            bad = _base_proj([{"src": "color:000000", "duration": 1}],
                             texts=[{"text": "X", "start": 0, "duration": 1,
                                     "shape": shape}])
            with self.assertRaises(TimelineError, msg=str(shape)):
                validate_timeline(bad)
        for shape in ({"type": "arc"}, {"type": "arc", "radius": 100,
                                        "angle": 120},
                      {"type": "circle", "radius": 80}):
            ok = _base_proj([{"src": "color:000000", "duration": 1}],
                            texts=[{"text": "X", "start": 0, "duration": 1,
                                    "shape": shape}])
            validate_timeline(ok)

    def test_cover_validation(self):
        bad = _base_proj([], cover={"template": "fancy", "title": "T"})
        with self.assertRaises(TimelineError):
            validate_timeline(bad)
        no_title = _base_proj([], cover={"template": "bold"})
        with self.assertRaises(TimelineError):
            validate_timeline(no_title)
        for template in ("bold", "split", "minimal"):
            ok = _base_proj([], cover={"template": template, "title": "T",
                                       "subtitle": "s", "duration": 1})
            validate_timeline(ok)

    def test_fit_none_needs_keyframes(self):
        bad = _base_proj([{"src": "color:000000", "duration": 1,
                           "fit": "none"}])
        with self.assertRaises(TimelineError):
            validate_timeline(bad)
        ok = _base_proj([{"src": "color:000000", "duration": 1,
                          "fit": "none",
                          "keyframes": {"position": [
                              {"time": 0.0, "value": [0.5, 0.5]}],
                                        "scale": [
                              {"time": 0.0, "value": 1.5}]}}])
        validate_timeline(ok)

    def test_export_validation(self):
        # HDR needs h265; alpha needs ProRes 4444.
        for exp in ({"codec": "h264", "hdr": True},
                    {"codec": "h264", "alpha": True},
                    {"codec": "h265", "alpha": True}):
            bad = _base_proj([{"src": "color:000000", "duration": 1}],
                             export=exp)
            with self.assertRaises(TimelineError, msg=str(exp)):
                validate_timeline(bad)
        for exp in ({"codec": "h265", "hdr": True},
                    {"codec": "prores4444", "alpha": True}):
            ok = _base_proj([{"src": "color:000000", "duration": 1}],
                            export=exp)
            validate_timeline(ok)

    def test_cover_expansion(self):
        proj = _base_proj([{"src": "color:000000", "duration": 1}],
                          cover={"template": "bold", "title": "HELLO",
                                 "subtitle": "world", "duration": 3})
        expanded = _expand_cover(proj)
        self.assertEqual(expanded["clips"][0]["src"], "color:#0B0B0B")
        self.assertEqual(expanded["clips"][0]["duration"], 3)
        self.assertEqual(expanded["clips"][1]["src"], "color:000000")
        self.assertEqual(expanded["texts"][0]["text"], "HELLO")
        self.assertEqual(expanded["texts"][1]["text"], "world")
        # The input project is not mutated.
        self.assertEqual(len(proj["clips"]), 1)

    # ---- renders ---------------------------------------------------
    def test_3d_text_renders(self):
        proj = _base_proj([{"src": "color:101010", "duration": 2}],
                          texts=[{"text": "DEEP", "start": 0, "duration": 2,
                                  "font": "Anton", "size": 48,
                                  "color": "#FFD60A", "x": "center",
                                  "y": "center",
                                  "3d": {"depth": 8,
                                         "depth_color": "#333333"}}])
        out = os.path.join(WORK, "text3d.mp4")
        Renderer(proj).render(out)
        probe = _probe(out, "codec_type,codec_name,width,height")
        self.assertEqual(_vstream(probe)["codec_name"], "h264")
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.3)

    def test_arc_text_renders(self):
        proj = _base_proj([{"src": "color:101010", "duration": 2}],
                          texts=[{"text": "ARC", "start": 0, "duration": 2,
                                  "font": "Anton", "size": 48,
                                  "color": "#FFFFFF", "x": "center",
                                  "y": "center",
                                  "shape": {"type": "arc", "radius": 60,
                                            "angle": 180}}])
        out = os.path.join(WORK, "arc.mp4")
        Renderer(proj).render(out)
        probe = _probe(out, "codec_type,codec_name,width,height")
        self.assertEqual(_vstream(probe)["codec_name"], "h264")

    def test_cover_renders(self):
        proj = _base_proj([], cover={"template": "split",
                                     "title": "COVER", "subtitle": "test",
                                     "duration": 2})
        out = os.path.join(WORK, "cover.mp4")
        Renderer(proj).render(out)
        probe = _probe(out, "codec_type,codec_name,width,height")
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.3)

    def test_hdr_renders(self):
        proj = _base_proj([{"src": "color:303030", "duration": 2}],
                          export={"codec": "h265", "hdr": True})
        out = os.path.join(WORK, "hdr.mp4")
        Renderer(proj).render(out)
        probe = _probe(
            out, "codec_type,codec_name,width,height,pix_fmt,profile,"
                 "color_transfer,color_primaries,color_space")
        v = _vstream(probe)
        self.assertEqual(v["codec_name"], "hevc")
        self.assertEqual(v["pix_fmt"], "yuv420p10le")
        self.assertEqual(v["color_transfer"], "smpte2084")
        self.assertEqual(v["color_primaries"], "bt2020")
        self.assertEqual(v["color_space"], "bt2020nc")

    def test_alpha_renders(self):
        # Semi-transparent red (alpha 0x80 = 128) on ProRes 4444.
        proj = _base_proj([{"src": "color:FF000080", "duration": 2}],
                          export={"codec": "prores4444", "alpha": True})
        out = os.path.join(WORK, "alpha.mov")
        Renderer(proj).render(out)
        probe = _probe(out, "codec_type,codec_name,width,height,pix_fmt")
        v = _vstream(probe)
        self.assertEqual(v["codec_name"], "prores")
        self.assertEqual(v["pix_fmt"], "yuva444p12le")  # ProRes 4444 is 12-bit
        # The alpha plane must actually carry 0.5, not 0 or 255.
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-v", "error", "-i", out,
             "-vf", "alphaextract", "-frames:v", "1", "-f", "rawvideo",
             "-"], capture_output=True)
        self.assertEqual(res.returncode, 0)
        data = res.stdout
        self.assertGreater(len(data), 1000)
        mean = sum(data) / len(data)
        self.assertAlmostEqual(mean, 128, delta=10)

    def test_ai_hdr_export_real(self):
        clip1 = os.path.join(MEDIA, "clip1.mp4")
        out = os.path.join(WORK, "ai_hdr.mp4")
        self.assertEqual(ai_mod.hdr_export(clip1, out), out)
        probe = _probe(out, "codec_type,codec_name,color_transfer,"
                            "color_primaries")
        v = _vstream(probe)
        self.assertEqual(v["codec_name"], "hevc")
        self.assertEqual(v["color_transfer"], "smpte2084")
        self.assertEqual(v["color_primaries"], "bt2020")


if __name__ == "__main__":
    unittest.main()
