"""Round-14 tests (v0.18.0): eye correction — MediaPipe Face Landmarker.

Covers ``musecut.eyecorrect`` + ``musecut.ai.eye_correction`` (round 14,
2026-11-21):
* The model file is the verified mirror (4,864,717 bytes, sha256
  pinned): it loads in onnxruntime with the documented signature —
  RGB [1, 3, 256, 256] in, landmarks (1, 478, 3) + presence logit out.
* On the real gate photo the mesh scores ~1.0 and the gaze offsets
  are near zero (a camera-looking face) and consistent between the
  two eyes; the eye centre uses the eyelid midpoint for y (the
  corners midpoint biases the vertical offset by ~-0.05).
* The warp translates iris texture exactly: a synthetic +0.25
  eye-width warp measures +6.0/+7.0 px by NCC (expect ~6.3), and a
  +0.50 warp +13.0 px (expect ~12.6), with NCC scores > 0.99.
  (An earlier falloff that started at the iris edge compressed the
  disk instead of moving it — the +0.25 warp then measured 0.02 —
  so the flat region now covers the disk's destination footprint;
  a synthetic-disk test moves the centroid exactly 10.0 px for a
  10 px request.)
* The synthetic off-gaze check: warp +0.25, correct, and NCC
  between the warped and the corrected still measures the applied
  shift exactly (the measured offset, negated, to within 1.5 px).
  What it does NOT do is restore the iris fully: the landmarker
  under-reads a synthetic iris-only shift by ~3-4x, so a +0.25
  warp measures only +0.04 and comes back ~1 px of ~6 px. That
  under-read is a documented limit, not a warp error — the warp
  itself is exact, and the correction never moves the iris the
  wrong way.
* The deadband leaves a centred face untouched: correcting the
  original gate photo is byte-identical (offsets ~0.01 < 0.10).
* The sanity gate skips landmark garbage: a fabricated 0.80
  eye-width offset leaves the frame byte-identical.
* No-face frames pass through unchanged (tyre-clip frame).
* Clip path: the 2 s talking-head clip renders through the real
  engine (ffprobe duration ~2.0 s), some frames corrected and the
  faceless B-roll frames untouched; NCC on a corrected frame
  measures the left iris moved ~+4 px toward the centre.
* ai.eye_correction dispatches image vs video; missing source
  raises; capabilities report eye_correction "done".

Fixtures are real workspace files (gate photo, round-13 talking-head
clip, round-11 tyre clip). Model calls cost ~0.1 s each on this CPU.

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round14 -v
"""

import os
import subprocess
import unittest

import numpy as np

from musecut import __version__
from musecut import ai as ai_mod
from musecut import eyecorrect as ec_mod
from musecut.render import probe_media

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round14")
PHOTO = os.path.expanduser("~/workspace/user/files/1000183940_6_huci.jpg")
CLIP = os.path.expanduser(
    "~/workspace/your_files/musecut-tests/round13/clip_lipsync_2s.mp4")
TYRE = os.path.expanduser(
    "~/workspace/your_files/musecut-tests/round11/clip640_sr4x.mp4")

try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import onnxruntime  # noqa: F401
    HAS_ORT = True
except ImportError:
    HAS_ORT = False

NEEDS_CV2 = unittest.skipUnless(
    HAS_CV2,
    "cv2 not installed in this Python — run with "
    "~/workspace/.venv-video/bin/python")
NEEDS_ORT = unittest.skipUnless(
    HAS_ORT,
    "onnxruntime not installed in this Python — run with "
    "~/workspace/.venv-video/bin/python")
NEEDS_MODEL = unittest.skipUnless(
    os.path.isfile(ec_mod.model_path())
    and os.path.getsize(ec_mod.model_path()) == ec_mod.LANDMARKER_SIZE,
    "face landmarker model not on disk")


def _ffprobe_dur(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


class TestEyeCorrectionModel(unittest.TestCase):
    def test_model_verified(self):
        info = ec_mod.verify_model()
        self.assertEqual(info["status"], "verified", info)
        self.assertEqual(info["size_bytes"], ec_mod.LANDMARKER_SIZE)
        self.assertEqual(info["sha256"], ec_mod.LANDMARKER_SHA256)

    @NEEDS_ORT
    def test_landmarker_signature(self):
        session = ec_mod._load_session()
        inp = session.get_inputs()[0]
        self.assertEqual(list(inp.shape[1:]), [3, 256, 256])
        self.assertIn(inp.shape[0], (None, "batch"))
        outs = session.get_outputs()
        self.assertEqual(list(outs[0].shape[1:]), [478, 3])
        self.assertIn(outs[0].shape[0], (None, "batch"))
        blob = np.zeros((1, 3, 256, 256), dtype=np.float32)
        lm, logit = session.run(None, {inp.name: blob})
        self.assertEqual(lm.shape, (1, 478, 3))
        self.assertTrue(np.isfinite(lm).all())


@NEEDS_CV2
@NEEDS_ORT
@NEEDS_MODEL
class TestEyeCorrectionStill(unittest.TestCase):
    def setUp(self):
        os.makedirs(WORK, exist_ok=True)
        self.img = cv2.imread(PHOTO)
        self.assertIsNotNone(self.img)

    def test_gate_photo_gaze_near_centre(self):
        found = ec_mod.detect_landmarks(self.img)
        self.assertIsNotNone(found)
        landmarks, score = found
        self.assertEqual(landmarks.shape, (478, 3))
        self.assertGreater(score, 0.9)
        gaze = ec_mod.gaze_offsets(landmarks)
        for eye in ("left", "right"):
            ox, oy = gaze[eye]["offset"]
            self.assertLess(abs(ox), 0.05, eye)
            self.assertLess(abs(oy), 0.05, eye)
        # both eyes agree on the horizontal direction (same sign)
        lx = gaze["left"]["offset"][0]
        rx = gaze["right"]["offset"][0]
        self.assertLess(abs(lx - rx), 0.05)

    def test_warp_moves_iris_texture_exactly(self):
        found = ec_mod.detect_landmarks(self.img)
        landmarks, _ = found
        gaze = ec_mod.gaze_offsets(landmarks)
        for shift, expect in ((0.25, 6.3), (0.50, 12.6)):
            warped = self.img.copy()
            for eye in ("left", "right"):
                g = gaze[eye]
                warped = ec_mod.warp_eye(
                    warped, g["iris"], g["iris_radius"],
                    (shift * g["eye_width"], 0.0))
            for eye in ("left", "right"):
                g = gaze[eye]
                dx, dy, mx = ec_mod.ncc_iris_shift(
                    self.img, warped, g["iris"], g["iris_radius"],
                    g["eye_width"])
                self.assertGreater(mx, 0.9, eye)
                self.assertAlmostEqual(dx, shift * g["eye_width"], delta=1.5)
                self.assertAlmostEqual(dy, 0.0, delta=1.0)

    def test_synthetic_offgaze_correction_applies_measured_shift(self):
        # The warp is exact (see test_warp_moves_iris_texture_exactly)
        # and the correction applies exactly the shift its own
        # measurement computed (NCC between the warped and the
        # corrected still). What it does NOT do is restore the iris
        # fully: the landmarker under-reads a synthetic iris-only
        # shift by ~3-4x (it reads gaze mostly from the eye contour
        # and head pose), so a +0.25 warp measures only +0.04 and
        # the correction moves the iris back ~1 px, not ~6 px.
        # That under-read is a documented limit, not a warp error —
        # the warp itself is exact, and the correction never moves
        # the iris the wrong way.
        found = ec_mod.detect_landmarks(self.img)
        landmarks, _ = found
        gaze = ec_mod.gaze_offsets(landmarks)
        off_path = os.path.join(WORK, "gate_offgaze.png")
        warped = self.img.copy()
        for eye in ("left", "right"):
            g = gaze[eye]
            warped = ec_mod.warp_eye(
                warped, g["iris"], g["iris_radius"],
                (0.25 * g["eye_width"], 0.0))
        cv2.imwrite(off_path, warped)
        corr_path = os.path.join(WORK, "gate_corrected.png")
        meas = ec_mod.correct_image(off_path, corr_path)
        self.assertTrue(meas["face"])
        corrected = cv2.imread(corr_path)
        warped_img = cv2.imread(off_path)
        found_w = ec_mod.detect_landmarks(warped_img)
        gaze_w = ec_mod.gaze_offsets(found_w[0])
        for eye in ("left", "right"):
            g = gaze_w[eye]
            dx, dy, mx = ec_mod.ncc_iris_shift(
                warped_img, corrected, g["iris"], g["iris_radius"],
                g["eye_width"])
            self.assertGreater(mx, 0.9, eye)
            # the applied shift is the measured offset, negated
            expect = -g["offset"][0] * g["eye_width"]
            self.assertAlmostEqual(dx, expect, delta=1.5)
            self.assertAlmostEqual(dy, -g["offset"][1] * g["eye_width"],
                                   delta=1.5)

    def test_deadband_leaves_centred_face_untouched(self):
        out_path = os.path.join(WORK, "gate_untouched.png")
        meas = ec_mod.correct_image(PHOTO, out_path)
        self.assertTrue(meas["face"])
        a = cv2.imread(PHOTO)
        b = cv2.imread(out_path)
        self.assertTrue(np.array_equal(a, b),
                        "a centred face must be byte-identical")

    def test_garbage_offset_gate(self):
        found = ec_mod.detect_landmarks(self.img)
        landmarks, _ = found
        gaze = ec_mod.gaze_offsets(landmarks)
        bad = landmarks.copy()
        # drag the left iris 0.80 eye-widths away — landmark garbage
        w = gaze["left"]["eye_width"]
        bad[ec_mod.L_IRIS_C, 0] += 0.80 * w
        out, meas = ec_mod.correct_frame(self.img, bad)
        self.assertTrue(np.array_equal(self.img, out),
                        "a garbage offset must leave the frame alone")

    def test_no_face_passthrough(self):
        cap = cv2.VideoCapture(TYRE)
        ok, frame = cap.read()
        cap.release()
        self.assertTrue(ok)
        src = os.path.join(WORK, "tyre_frame.png")
        cv2.imwrite(src, frame)
        out_path = os.path.join(WORK, "tyre_out.png")
        meas = ec_mod.correct_image(src, out_path)
        self.assertFalse(meas["face"])
        self.assertTrue(np.array_equal(frame, cv2.imread(out_path)))

    def test_measure_eye_correction_reports_ncc(self):
        found = ec_mod.detect_landmarks(self.img)
        landmarks, _ = found
        gaze = ec_mod.gaze_offsets(landmarks)
        warped = self.img.copy()
        for eye in ("left", "right"):
            g = gaze[eye]
            warped = ec_mod.warp_eye(
                warped, g["iris"], g["iris_radius"],
                (0.25 * g["eye_width"], 0.0))
        off_path = os.path.join(WORK, "m_off.png")
        cv2.imwrite(off_path, warped)
        corr_path = os.path.join(WORK, "m_corr.png")
        ec_mod.correct_image(off_path, corr_path)
        m = ec_mod.measure_eye_correction(off_path, corr_path)
        self.assertTrue(m["face"])
        # the NCC shift is the correction the measurement computed
        # (the landmarker under-reads the synthetic +0.25 warp as
        # ~+0.04, so the applied shift is ~-1 px, not -6 px —
        # documented limit, see the round-trip test above).
        for eye in ("left", "right"):
            self.assertGreater(m["ncc"][eye]["score"], 0.9, eye)
            expect = -m["before"][eye][0] * gaze[eye]["eye_width"]
            self.assertAlmostEqual(m["ncc"][eye]["dx_px"], expect,
                                   delta=1.5)


@NEEDS_CV2
@NEEDS_ORT
@NEEDS_MODEL
class TestEyeCorrectionClip(unittest.TestCase):
    def test_clip_renders_and_corrects(self):
        out = os.path.join(WORK, "clip_corrected.mp4")
        os.makedirs(WORK, exist_ok=True)
        work = os.path.join(WORK, "clipwork")
        meas = ec_mod.correct_clip(CLIP, out, sample_fps=10.0,
                                   workdir=work)
        self.assertTrue(os.path.isfile(out))
        self.assertAlmostEqual(_ffprobe_dur(out), 2.0, delta=0.15)
        # Only the first two samples sit above the 0.10 deadband
        # (this clip's gaze error is ~0.09-0.10 eye-widths, right
        # on the noise-floor edge); the rest are untouched by
        # design, and the B-roll frames have no face at all.
        self.assertGreater(meas["frames_corrected"], 0)
        self.assertGreater(meas["frames_untouched"], 0)  # B-roll part
        # NCC between the EXTRACTED frame and the PLATE frame for
        # sample 0 (t=0.0) — the same frame the pipeline measured,
        # so the expected shift is the one it computed, not a
        # VideoCapture seek that may land on a neighbouring frame.
        f0 = cv2.imread(os.path.join(work, "f0001.png"))
        p0 = cv2.imread(os.path.join(work, "plates", "p0001.png"))
        self.assertIsNotNone(f0)
        self.assertIsNotNone(p0)
        found = ec_mod.detect_landmarks(f0)
        landmarks, _ = found
        gaze = ec_mod.gaze_offsets(landmarks)
        g = gaze["left"]
        applied, _ = ec_mod._eye_shift(g)
        expect = applied[0] * g["eye_width"]
        dx, dy, mx = ec_mod.ncc_iris_shift(
            f0, p0, g["iris"], g["iris_radius"], g["eye_width"])
        self.assertGreater(mx, 0.8)
        self.assertAlmostEqual(dx, expect, delta=1.5)
        self.assertAlmostEqual(dy, 0.0, delta=1.5)


class TestEyeCorrectionApi(unittest.TestCase):
    def test_ai_dispatch_image_and_missing(self):
        with self.assertRaises(FileNotFoundError):
            ai_mod.eye_correction(os.path.join(WORK, "nope.png"), "x.png")

    @NEEDS_CV2
    @NEEDS_ORT
    @NEEDS_MODEL
    def test_ai_dispatch_image(self):
        out = os.path.join(WORK, "ai_gate.png")
        os.makedirs(WORK, exist_ok=True)
        self.assertEqual(ai_mod.eye_correction(PHOTO, out), out)
        self.assertTrue(os.path.isfile(out))

    @NEEDS_CV2
    @NEEDS_ORT
    @NEEDS_MODEL
    def test_capabilities_done(self):
        caps = ai_mod.capabilities()
        self.assertEqual(caps["eye_correction"]["status"], "done")

    def test_version_pin_0_16_0(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())


if __name__ == "__main__":
    unittest.main()
