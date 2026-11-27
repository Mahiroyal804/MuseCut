"""MuseCut hybrid AI backend — agent models + local ONNX runtime.

Owner-approved design (2026-11-20): AI features run on two tiers.

1. **Agent models** — the hosting Muse agent's own models, for
   stills / voice / captions. MuseCut the library cannot call the
   agent's image tool itself, so the host registers a callable; until
   it does, that backend honestly reports ``"stub"``. The voice and
   caption backends in this tier are already wired to real code
   (Muse TTS binary, faster-whisper) and report ``"wired"`` only
   when the underlying binary / package is actually present.
2. **Local ONNX runtime** — per-frame work on this machine
   (object tracking, super resolution). Wired when ``onnxruntime``
   imports AND the model file is on disk; otherwise ``"stub"`` with
   the reason and the exact download URL.

Honesty rule (same as ``ai.py``): a backend reports ``"wired"``
only if its call path was actually exercised or is directly
exercisable in-process. A registered-but-untested callable is
``"registered"``, never ``"wired"``. Nothing here fakes a
capability — an agent asking for a stub gets a clear error, not a
silent no-op.

Model cache: ``~/.cache/musecut-models/`` (shared, durable — NOT
``~/workspace/.tmp``, which a VM replacement can wipe).
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

# ------------------------------------------------------------------
# Model registry (local ONNX tier). `url` is the exact public
# download that produced the file; `path` is where MuseCut looks.
# ------------------------------------------------------------------
MODEL_DIR = os.path.expanduser("~/.cache/musecut-models")
# Durable fallback: ~/.cache is wiped by VM replacements (yolov5n
# was lost that way on 2026-11-20 and had to be re-downloaded).
# A copy kept in musecut/models/ survives, so model_path() prefers
# whichever copy is actually on disk.
WORKSPACE_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models")

KNOWN_MODELS: Dict[str, Dict[str, Any]] = {
    "yolov5n": {
        "file": "yolov5n.onnx",
        "size_bytes": 3981910,
        "url": "https://github.com/ultralytics/yolov5/releases/"
               "download/v7.0/yolov5n.onnx",
        "use": "object tracking (COCO 80 classes) — musecut.track",
        "license": "GPL-3.0 (Ultralytics YOLOv5)",
    },
    "realesrgan-x4plus": {
        "file": "RealESRGAN_x4plus.onnx",
        "size_bytes": 3060406,
        "url": "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
               "qai-hub-models/models/real_esrgan_x4plus/releases/"
               "v0.67.0/real_esrgan_x4plus-onnx-float.zip",
        "use": "super resolution x4 — musecut.ai.super_resolution "
               "(musecut.superres); weights live in the external "
               "real_esrgan_x4plus.data (66,737,664 B) next to the "
               ".onnx; the w8a8 fast variant is in "
               "musecut/models/realesrgan_w8a8/ (same .data name, "
               "different file)",
        "license": "BSD-3-Clause (Real-ESRGAN)",
    },
    "rmbg-1-4": {
        "file": "RMBG-1.4.onnx",
        "size_bytes": 176153355,
        "url": "https://huggingface.co/briaai/RMBG-1.4/resolve/main/"
               "onnx/model.onnx",
        "use": "AI background removal — musecut.ai.remove_background",
        "license": "BRIA source-available, NON-COMMERCIAL "
                   "(bria-rmbg-1.4); commercial use needs a paid "
                   "BRIA licence — see model card",
    },
    "uvr-mdx-net-inst-hq-3": {
        "file": "UVR-MDX-NET-Inst_HQ_3.onnx",
        "size_bytes": 66759214,
        "url": "https://github.com/TRvlvr/model_repo/releases/download/"
               "all_public_uvr_models/UVR-MDX-NET-Inst_HQ_3.onnx",
        "use": "vocal separation — musecut.ai.vocal_separation "
               "(musecut.vocalsep)",
        "license": "no licence stated on the release page (checked "
                   "2026-11-21); the model is distributed by the "
                   "Ultimate Vocal Remover project (TRvlvr) — check "
                   "TRvlvr/model_repo before redistributing it",
    },
    "wav2lip-256": {
        "file": "wav2lip_256.onnx",
        "size_bytes": 214402122,
        "url": "https://huggingface.co/vnalex/wav2lip-256-onnx",
        "use": "lip sync — drive a face's lips from audio "
               "(musecut.ai.lip_sync / musecut.lipsync); Wav2Lip "
               "generator ONNX at 256x256, mel [1,1,80,16] + face "
               "[1,6,256,256] in, face [1,3,256,256] out",
        "license": "no licence stated on the mirror README (checked "
                   "2026-11-21); mirror of archived "
                   "instant-high/wav2lip-onnx-256, adapted from "
                   "Kedreamix/Linly-Talker; original Wav2Lip is "
                   "research code — check the Wav2Lip project before "
                   "redistributing it",
    },
    "face-landmarker-478": {
        "file": "face_landmarker_Nx3x256x256.onnx",
        "size_bytes": 4864717,
        "sha256": "111795f8703cdeb6d0c68a9f3cc966a0f23f8786bb00f4577a11f461fc4276ac",
        "url": "https://github.com/yakhyo/mediapipe-face-mesh-onnx/"
               "releases/download/weights/"
               "face_landmarker_Nx3x256x256.onnx",
        "use": "eye correction — 478-point face mesh with iris "
               "landmarks (musecut.ai.eye_correction / "
               "musecut.eyecorrect); 256x256 RGB in, landmarks "
               "(1, 478, 3) + presence logit out",
        "license": "Apache-2.0 (Google MediaPipe weights via "
                   "PINTO0309's ONNX conversion; yakhyo mirror)",
    },
    "opus-mt-en-es": {
        "file": "translate/en-es/encoder_model_quantized.onnx",
        "size_bytes": 52899742,
        "sha256": "03434edd1147e6c49e22066e9dc5888235e99cc35a6711d1c4ea5b06455d5da9",
        "url": "https://huggingface.co/Xenova/opus-mt-en-es",
        "use": "AI translate en->es — OPUS-MT MarianMT int8 ONNX "
               "(musecut.ai.ai_translate / musecut.translate); "
               "encoder + decoder_model_quantized.onnx (59,842,102 B) "
               "+ tokenizer.json live in musecut/models/translate/en-es/",
        "license": "Apache-2.0 (Helsinki-NLP OPUS-MT; Xenova ONNX "
                   "conversion)",
    },
    "opus-mt-en-mul": {
        "file": "translate/en-mul/encoder_model_quantized.onnx",
        "size_bytes": 52443550,
        "sha256": "f10f49c2691043ba7cfd2a85d86f84dddda428d4209cf9b495f6e114b313737a",
        "url": "https://huggingface.co/Xenova/opus-mt-en-mul",
        "use": "AI translate en->ur (and other targets) — OPUS-MT "
               "multilingual MarianMT int8 ONNX (musecut.translate); "
               "target language is a vocab token prepended by id "
               "('>>urd<<' = 852); decoder_model_quantized.onnx "
               "(59,382,346 B) + tokenizer.json live in "
               "musecut/models/translate/en-mul/",
        "license": "Apache-2.0 (Helsinki-NLP OPUS-MT; Xenova ONNX "
                   "conversion)",
    },
}


def model_path(name: str) -> str:
    """Filesystem path where a known model is expected.

    Prefers whichever copy is actually on disk (cache first, then
    the durable workspace copy); when neither exists, returns the
    cache path — that is where download_model() writes.
    """
    fname = KNOWN_MODELS[name]["file"]
    cache_path = os.path.join(MODEL_DIR, fname)
    if os.path.isfile(cache_path):
        return cache_path
    ws_path = os.path.join(WORKSPACE_MODEL_DIR, fname)
    if os.path.isfile(ws_path):
        return ws_path
    return cache_path


def model_status(name: str) -> Dict[str, Any]:
    """Availability of one known model: present / missing + why."""
    info = KNOWN_MODELS[name]
    path = model_path(name)
    present = os.path.isfile(path) and os.path.getsize(path) > 0
    if present:
        return {"status": "present", "path": path,
                "size_bytes": os.path.getsize(path),
                "use": info["use"]}
    return {"status": "missing", "path": path, "use": info["use"],
            "url": info["url"],
            "reason": "model file not downloaded; "
                      + (f"fetch it from {info['url']}" if info["url"]
                         else "no verified public .onnx mirror found yet "
                              "— do not guess a URL, verify one first")}


def onnxruntime_available() -> bool:
    """True when onnxruntime imports in this interpreter."""
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def onnxruntime_version() -> Optional[str]:
    try:
        import onnxruntime as ort
        return str(ort.__version__)
    except ImportError:
        return None


# ------------------------------------------------------------------
# Agent-model tier: stills. The host agent registers a callable
# with the signature ``fn(prompt, output_path, **kwargs) -> path``.
# ------------------------------------------------------------------
_still_fn: Optional[Callable[..., str]] = None
_still_fn_tested: bool = False


def register_still_fn(fn: Callable[..., str]) -> None:
    """Register the host agent's still-image generator.

    ``fn(prompt, output_path, **kwargs)`` must write a real image
    file and return its path. Until a host registers one, still
    generation is honestly a stub — MuseCut never pretends to
    generate images by itself.
    """
    global _still_fn, _still_fn_tested
    _still_fn = fn
    _still_fn_tested = False


def generate_still(prompt: str, output: str, **kwargs: Any) -> str:
    """Generate a still image via the registered host backend.

    Raises RuntimeError (not a silent placeholder) when no backend
    is registered.
    """
    if _still_fn is None:
        raise RuntimeError(
            "No still-image backend is registered in this process. "
            "The hosting Muse agent must call "
            "musecut.providers.register_still_fn(fn) with its own "
            "image model — see musecut/providers.py. MuseCut does not "
            "generate stills by itself and will not fake one.")
    out = _still_fn(prompt, output, **kwargs)
    global _still_fn_tested
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        _still_fn_tested = True
    return out


# ------------------------------------------------------------------
# Backend registry + status
# ------------------------------------------------------------------
def list_backends() -> List[Dict[str, Any]]:
    """Every AI backend, its tier, and its honest status."""
    from . import tts as tts_mod

    muse_bin = os.path.isfile(tts_mod.MUSE_TTS_BIN)
    try:
        import faster_whisper  # noqa: F401
        whisper = True
    except ImportError:
        whisper = False

    backs: List[Dict[str, Any]] = [
        {
            "name": "muse-voice",
            "tier": "agent-model",
            "capability": "text_to_speech",
            "status": "wired" if muse_bin else "stub",
            "via": "Muse TTS /opt/hatch/bin/tts, voice "
                   "avocado_v2:MAI_03 (speed 90)",
            "reason": None if muse_bin else
                      "Muse TTS binary not found on this machine",
        },
        {
            "name": "whisper-captions",
            "tier": "agent-model",
            "capability": "auto_captions",
            "status": "wired" if whisper else "stub",
            "via": "faster-whisper (word timestamps) -> ASS",
            "reason": None if whisper else
                      "faster-whisper not installed in this Python",
        },
        {
            "name": "agent-stills",
            "tier": "agent-model",
            "capability": "generate_still",
            "status": ("wired" if _still_fn_tested else
                       "registered" if _still_fn is not None else "stub"),
            "via": "host agent's own image model, registered via "
                   "musecut.providers.register_still_fn",
            "reason": (None if _still_fn_tested else
                       "a still backend is registered but has not "
                       "produced a verified image yet" if _still_fn
                       else "no still backend registered in this "
                            "process — the hosting agent must register one"),
        },
        {
            "name": "onnx-runtime",
            "tier": "local-onnx",
            "capability": "per-frame ML (tracking, super resolution)",
            "status": "wired" if onnxruntime_available() else "stub",
            "via": ("onnxruntime "
                    f"{onnxruntime_version() or ''} (CPU)".strip()),
            "reason": None if onnxruntime_available() else
                      "onnxruntime not installed in this Python",
        },
    ]
    for model_name in ("yolov5n", "realesrgan-x4plus", "rmbg-1-4",
                       "uvr-mdx-net-inst-hq-3"):
        st = model_status(model_name)
        backs.append({
            "name": f"onnx-model:{model_name}",
            "tier": "local-onnx",
            "capability": KNOWN_MODELS[model_name]["use"],
            "status": ("wired" if st["status"] == "present"
                       and onnxruntime_available() else "stub"),
            "via": st.get("path", ""),
            "reason": (None if st["status"] == "present"
                       and onnxruntime_available() else st.get("reason")
                       or "onnxruntime not installed"),
        })
    return backs


def backend_status() -> Dict[str, Any]:
    """Full hybrid-backend status report (used by CLI + MCP)."""
    from . import __version__
    backs = list_backends()
    wired = sum(1 for b in backs if b["status"] == "wired")
    return {
        "version": __version__,
        "backends": backs,
        "wired": wired,
        "total": len(backs),
    }
