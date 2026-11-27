"""Round-13 tests (v0.15.0): lip sync — genuine Wav2Lip ONNX.

Covers ``musecut.lipsync`` + ``musecut.ai.lip_sync`` (round 13,
2026-11-21):
* The model file is the verified mirror (214,402,122 bytes, sha256
  pinned): it loads in onnxruntime with the documented signature —
  mel [1, 1, 80, 16] + face [1, 6, 256, 256] in, face [1, 3, 256, 256]
  out — and one inference on synthetic inputs returns a sane range.
* The mel path is Wav2Lip's preprocessing (numpy STFT, reflect
  padding, Slaney melbank, 80 mels, fmin 55 / fmax 7600, symmetric
  normalise to [-4, 4]): shape (80, T), range inside [-4, 4], and
  mel chunks are 16 frames each, one per video frame.
* Face detection finds the real face in the gate photo; face_input
  is (1, 6, 256, 256) with the masked half exactly zeroed.
* paste_face is byte-identical outside the box and keeps the upper
  face identical (only the mouth/jaw region is replaced).
* One model call is deterministic (same mel twice -> mouth diff
  0.0) and the mouth follows the audio (speech vs silence mouth
  diff > 1.0 on the 0-255 scale; different audio times differ).
* Image path: a 1 s photo animation renders through the real engine
  (ffprobe-verified duration + audio stream present). Clip path: a
  1 s talking-head slice renders the same way. Missing source /
  missing audio raise; ai.lip_sync without audio raises ValueError;
  capabilities report lip_sync "done" (model on disk + onnxruntime).

Fixtures are real workspace files (gate photo, car08 voiceover,
reel talking head) — the same pattern as earlier rounds' real-media
fixtures. The model calls cost ~1 s each on this CPU; the suite
keeps them to a handful (1 s renders at 10 fps).

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round13 -v
"""

import os
import subprocess
import unittest

import numpy as np

from musecut import __version__
from musecut import ai as ai_mod
from musecut import lipsync as ls_mod
from musecut.render import probe_media

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round13")
MEDIA = os.path.join(WORK, "media")
PHOTO = os.path.expanduser("~/workspace/user/files/1000183940_6_huci.jpg")
VOICE = os.path.expanduser(
    "~/workspace/youtube-finance/car08/voiceover/seg01.wav")
REEL = os.path.expanduser("~/workspace/.tmp/reel-swap2/reel.mp4")

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
    os.path.isfile(ls_mod.model_path())
    and os.path.getsize(ls_mod.model_path()) == ls_mod.MODEL_SIZE,
    "wav2lip model not on disk")
NEEDS_VOICE = unittest.skipUnless(
    os.path.isfile(VOICE),
    "car08 voiceover missing: " + VOICE)
NEEDS_PHOTO = unittest.skipUnless(
    os.path.isfile(PHOTO),
    "gate photo missing: " + PHOTO)


def _mouth_diff(a, b):
    return float(np.abs(a[170:235, 90:170].astype(np.float32)
                        - b[170:235, 90:170].astype(np.float32)).mean())


class TestLipSyncMel(unittest.TestCase):
    @NEEDS_VOICE
    def test_mel_shape_and_range(self):
        wav = ls_mod.load_wav_16k(VOICE)
        mel = ls_mod.mel_spectrogram(wav[:16000])  # first second only
        self.assertEqual(mel.shape[0], 80)
        self.assertGreater(mel.shape[1], 70)  # ~80 frames per second
        self.assertGreaterEqual(float(mel.min()), -4.0)
        self.assertLessEqual(float(mel.max()), 4.0)

    @NEEDS_VOICE
    def test_mel_chunks(self):
        wav = ls_mod.load_wav_16k(VOICE)
        mel = ls_mod.mel_spectrogram(wav[:16000])
        chunks = ls_mod.mel_chunks(mel, 25.0, 25)
        self.assertEqual(len(chunks), 25)
        for c in chunks:
            self.assertEqual(c.shape, (80, 16))

    def test_load_wav_errors(self):
        with self.assertRaises(ls_mod.LipSyncError):
            ls_mod.load_wav_16k(os.path.join(WORK, "nope.wav"))


@NEEDS_CV2
@NEEDS_PHOTO
class TestLipSyncFace(unittest.TestCase):
    def test_detect_face_real_photo(self):
        import cv2
        img = cv2.imread(PHOTO)
        self.assertIsNotNone(img)
        box = ls_mod.detect_face(img)
        x0, y0, x1, y1 = box
        self.assertGreater(x1 - x0, 60)
        self.assertGreater(y1 - y0, 60)
        self.assertGreaterEqual(x0, 0)
        self.assertGreaterEqual(y0, 0)

    def test_face_input_layout(self):
        import cv2
        img = cv2.imread(PHOTO)
        box = ls_mod.detect_face(img)
        face256 = cv2.resize(img[box[1]:box[3], box[0]:box[2]], (256, 256))
        six = ls_mod.face_input(face256)
        self.assertEqual(six.shape, (1, 6, 256, 256))
        # masked half (channels 0-2, rows 128+) is exactly zero
        self.assertEqual(float(np.abs(six[0, :3, 128:, :]).max()), 0.0)
        # reference half (channels 3-5) carries the face
        self.assertGreater(float(np.abs(six[0, 3:, :, :]).mean()), 0.01)

    def test_paste_face_identity_outside_box(self):
        import cv2
        img = cv2.imread(PHOTO)
        box = ls_mod.detect_face(img)
        rng = np.random.default_rng(0)
        fake = rng.integers(0, 255, (256, 256, 3), dtype=np.uint8)
        out = ls_mod.paste_face(img, box, fake)
        self.assertEqual(out.shape, img.shape)
        # outside the box: byte-identical
        mask = np.ones(img.shape[:2], dtype=bool)
        mask[box[1]:box[3], box[0]:box[2]] = False
        self.assertEqual(float(np.abs(
            out[mask].astype(np.float32)
            - img[mask].astype(np.float32)).max()), 0.0)
        # upper face (rows 0..100 of the crop): identical to original
        x0, y0, x1, y1 = box
        h = y1 - y0
        top = int(100 / 256 * h)
        self.assertEqual(float(np.abs(
            out[y0:y0 + top, x0:x1].astype(np.float32)
            - img[y0:y0 + top, x0:x1].astype(np.float32)).max()), 0.0)


@NEEDS_ORT
class TestLipSyncModel(unittest.TestCase):
    def test_model_signature_and_size(self):
        import onnxruntime as ort
        self.assertEqual(os.path.getsize(ls_mod.model_path()),
                         ls_mod.MODEL_SIZE)
        sess = ort.InferenceSession(ls_mod.model_path(),
                                    providers=["CPUExecutionProvider"])
        ins = {i.name: list(i.shape) for i in sess.get_inputs()}
        outs = {o.name: list(o.shape) for o in sess.get_outputs()}
        self.assertEqual(ins["mel_spectrogram"], [1, 1, 80, 16])
        self.assertEqual(ins["video_frames"], [1, 6, 256, 256])
        self.assertEqual(outs["predicted_frames"], [1, 3, 256, 256])

    @NEEDS_CV2
    def test_one_call_deterministic_and_audio_driven(self):
        import cv2
        img = cv2.imread(PHOTO)
        box = ls_mod.detect_face(img)
        face256 = cv2.resize(img[box[1]:box[3], box[0]:box[2]], (256, 256))
        wav = ls_mod.load_wav_16k(VOICE)
        mel = ls_mod.mel_spectrogram(wav)
        chunks = ls_mod.mel_chunks(mel, 25.0, 30)
        sess = ls_mod._load_session()
        o1 = ls_mod.lipsync_face(face256, chunks[5], sess)
        o1b = ls_mod.lipsync_face(face256, chunks[5], sess)
        o2 = ls_mod.lipsync_face(face256, chunks[15], sess)
        silent = np.full((80, 16), -4.0, dtype=np.float32)
        o_sil = ls_mod.lipsync_face(face256, silent, sess)
        self.assertEqual(o1.shape, (256, 256, 3))
        self.assertEqual(_mouth_diff(o1, o1b), 0.0)
        self.assertGreater(_mouth_diff(o1, o2), 1.0)
        self.assertGreater(_mouth_diff(o2, o_sil), 1.0)

    def test_measure_lipsync(self):
        m = ls_mod.measure_lipsync(PHOTO, VOICE)
        self.assertEqual(m["determinism_mouth_diff"], 0.0)
        self.assertGreater(m["audio_variance_mouth_diff"], 1.0)
        self.assertGreater(m["speech_vs_silence_mouth_diff"], 1.0)
        self.assertEqual(m["mel_shape"][0], 80)


@NEEDS_CV2
@NEEDS_ORT
@NEEDS_MODEL
class TestLipSyncRender(unittest.TestCase):
    def test_image_path_renders_with_audio(self):
        out = os.path.join(WORK, "photo_lipsync.mp4")
        res = ai_mod.lip_sync(PHOTO, out, audio=VOICE, fps=10.0, seconds=1.0)
        self.assertEqual(res, out)
        info = probe_media(out)
        self.assertGreater(info["duration"], 0.8)
        self.assertLess(info["duration"], 1.5)
        self.assertTrue(info.get("has_audio"),
                        f"no audio stream in {out}: {info}")
        self.assertEqual(info["width"], 720)
        self.assertEqual(info["height"], 1600)

    def test_clip_path_renders_with_audio(self):
        os.makedirs(MEDIA, exist_ok=True)
        clip = os.path.join(MEDIA, "talking_2s.mp4")
        # -ss 10: a clean talking-head segment. The reel at 5 s is
        # heavily composited (translucent overlays) — Haar finds only
        # an 87 px false positive there and misses the speaker, so
        # the 5 s slice is the wrong fixture (caught in round 13).
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-v", "error",
             "-ss", "10", "-i", REEL, "-t", "2", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", clip],
            check=True)
        # Regression pin: the largest face on the slice's first frame
        # must be the speaker (>=150 px), not an overlay false positive.
        import cv2
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-v", "error",
             "-i", clip, "-frames:v", "1",
             os.path.join(MEDIA, "talking_f0.png")],
            check=True)
        frame0 = cv2.imread(os.path.join(MEDIA, "talking_f0.png"))
        box = ls_mod.detect_face(frame0)
        self.assertGreaterEqual(box[2] - box[0], 150)
        out = os.path.join(WORK, "clip_lipsync.mp4")
        res = ai_mod.lip_sync(clip, out, audio=VOICE, fps=10.0, seconds=1.0)
        self.assertEqual(res, out)
        info = probe_media(out)
        self.assertGreater(info["duration"], 0.8)
        self.assertTrue(info.get("has_audio"),
                        f"no audio stream in {out}: {info}")

    def test_missing_audio_raises(self):
        with self.assertRaises(ValueError):
            ai_mod.lip_sync(PHOTO, os.path.join(WORK, "x.mp4"))
        with self.assertRaises(FileNotFoundError):
            ai_mod.lip_sync(os.path.join(WORK, "nope.png"),
                            os.path.join(WORK, "x.mp4"), audio=VOICE)

    def test_broll_frames_pass_through_untouched(self):
        """A cut to B-roll mid-clip must not get a stale face paste.

        The 10 s slice cuts to a cityscape at ~1.3 s. Frames after
        the first failed re-detection (plate p0016..p0020 at 10 fps,
        detection runs every 5 frames) must be byte-identical
        to the extracted source frames — the round-13 clip proof
        first pasted a softened stale box onto the cityscape
        (box diff 1.83) before the pass-through fix.
        """
        import cv2
        clip = os.path.join(MEDIA, "talking_2s.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-v", "error",
             "-ss", "10", "-i", REEL, "-t", "2", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", clip],
            check=True)
        workdir = os.path.join(WORK, "lipsync-broll")
        out = os.path.join(WORK, "clip_lipsync_broll.mp4")
        res = ai_mod.lip_sync(clip, out, audio=VOICE, fps=10.0,
                              seconds=2.0, workdir=workdir)
        self.assertEqual(res, out)
        for n in range(16, 21):
            src = cv2.imread(os.path.join(workdir, f"f{n:04d}.png"))
            plate = cv2.imread(
                os.path.join(workdir, "frames", f"p{n:04d}.png"))
            self.assertIsNotNone(src, f"f{n:04d} missing")
            self.assertIsNotNone(plate, f"p{n:04d} missing")
            self.assertEqual(
                float(np.abs(src.astype(np.float32)
                            - plate.astype(np.float32)).max()), 0.0,
                f"plate frame {n} differs from source (B-roll paste?)")

    def test_capabilities_done(self):
        caps = ai_mod.capabilities()
        self.assertEqual(caps["lip_sync"]["status"], "done")


@NEEDS_CV2
class TestOverlayVideoSync(unittest.TestCase):
    """A video overlay with start>0 must play in sync, not freeze.

    Regression for the round-13 foreground-pass mastery: the
    engine trimmed a video overlay to 0..dur and reset its
    timestamps to zero, so the overlay filter — evaluated at main
    time start..start+dur — held the LAST overlay frame for the
    whole window. The pass window went black on the plate's
    opaque last frame. The fix shifts the overlay's timestamps
    to its timeline start (setpts +start/TB).
    """

    def test_video_overlay_with_start_plays_in_sync(self):
        import cv2
        from musecut.render import Renderer
        os.makedirs(MEDIA, exist_ok=True)
        rb = os.path.join(MEDIA, "redblue.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=red:s=100x100:d=0.5",
             "-f", "lavfi", "-i", "color=c=blue:s=100x100:d=0.5",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             rb], check=True)
        proj = {"version": 1, "name": "r13-ovsync", "width": 640,
                "height": 360, "fps": 30,
                "clips": [{"src": "color:#000000", "duration": 3.0}],
                "overlays": [{"src": rb, "start": 1.0, "duration": 1.0,
                              "width": 100, "x": "center",
                              "y": "center"}]}
        out = os.path.join(WORK, "overlay_sync.mp4")
        Renderer(proj, workdir=os.path.join(WORK, "render-ovsync")
                 ).render(out, quiet=True)

        def centre(t, tag):
            png = os.path.join(WORK, f"ovsync-{tag}.png")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-y", "-v", "error",
                 "-ss", str(t), "-i", out, "-frames:v", "1", png],
                check=True)
            img = cv2.imread(png)
            # Overlay is 100x100 centred at (320, 180).
            return img[160:200, 300:340].mean(axis=(0, 1))

        bgr_before = centre(0.5, "before")
        self.assertLess(bgr_before.max(), 40,
                        f"overlay visible before its start: {bgr_before}")
        bgr_red = centre(1.25, "red")
        self.assertGreater(bgr_red[2], 150,
                           f"overlay first half not red: {bgr_red}")
        self.assertLess(bgr_red[0], 80, f"overlay first half: {bgr_red}")
        bgr_blue = centre(1.75, "blue")
        self.assertGreater(bgr_blue[0], 150,
                           f"overlay second half not blue: {bgr_blue}")
        self.assertLess(bgr_blue[2], 80, f"overlay second half: {bgr_blue}")


class TestVersionPin(unittest.TestCase):
    def test_version_pin_0_15_0(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())


if __name__ == "__main__":
    unittest.main()
