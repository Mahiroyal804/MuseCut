"""MuseCut render engine.

Builds ONE ffmpeg ``filter_complex`` graph for the whole timeline and
runs it. No intermediate renders, no temp video files (except the
vidstab pass-1 ``.trf`` and the generated ASS captions file).

Pipeline per main clip::

    trim -> [reverse] -> [speed setpts] -> [zoompan keyframes | scale+pad]
        -> [vidstab] -> [chroma key over colour bg] -> [mask]
        -> [colour/effects] -> [grade/vignette/grain] -> fps + format

Clips are joined with ``xfade`` (crossfade / dip-to-black / wipe) or a
hard ``concat`` cut. Clip audio is joined with ``acrossfade``/``concat``,
then mixed with the voice/music/sfx tracks; music ducks under voice
via ``sidechaincompress``. Word-highlight captions are burned in with
libass. An optional end card is concatenated at the tail.

Export (v2): the project's ``export`` block picks the codec —
H.264/H.265 (default), ProRes 422/LT/HQ/4444/Proxy (``prores_ks``),
AV1 (``libsvtav1``), DNxHD/DNxHR (``dnxhd`` — broadcast HD/UHD,
valid frame sizes only), animated GIF (single-pass palettegen), or
audio-only WAV/MP3 (rendered to a video intermediate, then the audio
is extracted — ffmpeg errors on unmapped filter outputs, so a direct
audio-only graph is not possible). A per-resolution bitrate ladder
(``export.bitrate_ladder``) mirrors CapCut's server-driven ``Hw``
model locally. Canvas backgrounds (colour / image / blurred copy of
the clip) sit behind the main track, and per-clip motion blur
(``tmix``) is available.

Pro (v2): full transform keyframes (position / scale / rotation /
opacity, with anchor point), .cube LUTs, per-track EQ + compressor +
keyframed volume, nested sequences / compound clips (a project as a
clip, rendered recursively), adjustment layers (effects over a time
range), and markers (editorial metadata).

Environment notes (this VM): /tmp is a 512 MB tmpfs, so all scratch
goes under ``workdir`` (default ``~/workspace/.tmp/musecut``). Network
is irrelevant here — everything is local ffmpeg.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from . import audio as audio_mod
from . import captions as cap_mod
from . import effects as fx
from . import timeline as tl_mod
from . import keyframes as kf_mod
from .timeline import TimelineError, default_font_path, validate_timeline

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# xfade transition name mapping: timeline name -> ffmpeg xfade name
XFADE_MAP = {
    "crossfade": "fade",
    "dip-to-black": "fadeblack",
    "wipe": "wipeleft",
    "fade": "fade",
    "none": "fade",
    # (round 5, v0.7.0) — all verified against this ffmpeg build's
    # `xfade` list (ffmpeg 8.1: fade 0, slideleft 5, circleopen 19,
    # dissolve 25, pixelize 26, hblur 35, fadewhite 13, zoomin 43).
    # "whoosh" is intentionally absent: it falls back to "fade" and
    # adds a synthesised whoosh SFX at the transition midpoint.
    "slide": "slideleft",
    "zoom": "zoomin",
    "dissolve": "dissolve",
    "circle": "circleopen",
    "radial": "radial",  # (round 31, v0.18.0) clock wipe — measured
    # in round31/probe_radial.py: a soft ~30-40 deg angular ramp
    # sweeping clockwise from 12 o'clock, 50% angle
    # theta_50(p) ~= 455*p - 76 deg (NOT 360*p); see EDITING_MASTERY.md
    "pixelize": "pixelize",
    "dip-to-white": "fadewhite",
}

# Export codecs (v2). Values verified against CapCut 20.3.0's
# ExportConfig$CodecType enum (see the RE findings §4.4) and mapped
# to real ffmpeg encoders. "has_audio": False means the output carries
# no audio stream at all (GIF); audio-only codecs carry no video.
EXPORT_CODECS: Dict[str, Dict[str, Any]] = {
    "h264": {"encoder": "libx264", "container": "mp4",
             "pix_fmt": "yuv420p", "audio_codec": "aac",
             "has_video": True, "has_audio": True},
    "h265": {"encoder": "libx265", "container": "mp4",
             "pix_fmt": "yuv420p", "audio_codec": "aac",
             "has_video": True, "has_audio": True},
    "prores422": {"encoder": "prores_ks", "profile": 2,
                  "container": "mov", "pix_fmt": "yuv422p10le",
                  "audio_codec": "aac", "has_video": True,
                  "has_audio": True},
    "prores422lt": {"encoder": "prores_ks", "profile": 1,
                    "container": "mov", "pix_fmt": "yuv422p10le",
                    "audio_codec": "aac", "has_video": True,
                    "has_audio": True},
    "prores422hq": {"encoder": "prores_ks", "profile": 3,
                    "container": "mov", "pix_fmt": "yuv422p10le",
                    "audio_codec": "aac", "has_video": True,
                    "has_audio": True},
    "prores4444": {"encoder": "prores_ks", "profile": 4,
                   "container": "mov", "pix_fmt": "yuv444p10le",
                   "audio_codec": "aac", "has_video": True,
                   "has_audio": True},
    "prores422proxy": {"encoder": "prores_ks", "profile": 0,
                       "container": "mov", "pix_fmt": "yuv422p10le",
                       "audio_codec": "aac", "has_video": True,
                       "has_audio": True},
    "av1": {"encoder": "libsvtav1", "container": "mp4",
            "pix_fmt": "yuv420p", "audio_codec": "aac",
            "has_video": True, "has_audio": True,
            "extra": ["-preset", "8", "-crf", "32"]},
    "gif": {"encoder": "gif", "container": "gif", "pix_fmt": "rgb24",
            "has_video": True, "has_audio": False},
    "wav": {"encoder": "pcm_s16le", "container": "wav",
            "has_video": False, "has_audio": True},
    "mp3": {"encoder": "libmp3lame", "container": "mp3",
            "has_video": False, "has_audio": True,
            "extra": ["-q:a", "4"]},
    # DNxHD/DNxHR (broadcast mezzanine). ffmpeg's dnxhd encoder only
    # accepts specific frame-size/bitrate combinations, so the bitrate
    # and profile come from _dnxhd_settings() (not the ladder), and
    # _resolve_codec() rejects non-HD frame sizes outright.
    "dnxhd": {"encoder": "dnxhd", "container": "mov",
              "pix_fmt": "yuv422p", "audio_codec": "aac",
              "has_video": True, "has_audio": True},
}

# DNxHD/DNxHR valid (frame size -> (bitrate, profile)). Verified
# 2026-11-20: 1280x720 @145M yuv422p, 1920x1080 @175M yuv422p,
# 3840x2160 @880M dnxhr_hq. Other sizes are rejected by the encoder.
_DNXHD_SETTINGS = {
    (1280, 720): ("145M", "dnxhd"),
    (960, 720): ("145M", "dnxhd"),
    (1440, 1080): ("175M", "dnxhd"),
    (1920, 1080): ("175M", "dnxhd"),
    (3840, 2160): ("880M", "dnxhr_hq"),
}


def _split_filter_chain(chain: str) -> List[str]:
    """Split a comma-joined filter chain on commas outside quotes.

    Needed to attach ``:enable=...`` to each filter of an adjustment
    layer: ``curves`` arguments carry quoted strings that themselves
    contain commas.
    """
    parts: List[str] = []
    cur = ""
    quoted = False
    for ch in chain:
        if ch == "'":
            quoted = not quoted
            cur += ch
        elif ch == "," and not quoted:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts
# Keys: sd (<=480p), hd (<=720p), fullhd (<=1080p), 2k (<=1440p),
# 4k (>1440p). Values are ffmpeg bitrate strings.
DEFAULT_LADDER: Dict[str, Any] = {
    "sd": "2M", "hd": "4M", "fullhd": "8M", "2k": "16M", "4k": "32M",
    "gop": 0,  # 0 = 2x fps
    "profile": "high",
}

_ENCODER_TO_CODEC = {
    "libx264": "h264", "libx265": "h265", "prores_ks": "prores422",
    "libsvtav1": "av1", "libaom-av1": "av1", "gif": "gif",
    "pcm_s16le": "wav", "libmp3lame": "mp3", "dnxhd": "dnxhd",
}


def _parse_bitrate(v: Any) -> int:
    """'8M' / '500k' / 8000000 -> bits per second (int)."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower()
    mult = 1
    if s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("k"):
        mult, s = 1_000, s[:-1]
    return int(float(s) * mult)


def _offset_expr(expr: str, delta: int) -> str:
    """Add a pixel offset to a drawtext/overlay position expression.

    Position expressions are either bare (``(w-text_w)/2``, ``40``)
    or single-quoted (the slide-up ``y`` expression). A bare
    expression gets ``(expr)+d``; a quoted one gets the offset
    inside the quotes so ffmpeg still sees one expression.
    """
    if delta == 0:
        return expr
    if expr.startswith("'") and expr.endswith("'"):
        return expr[:-1] + f"+{delta}'"
    return f"({expr})+{delta}"


def _codec_from_extension(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    return {".wav": "wav", ".mp3": "mp3", ".gif": "gif"}.get(ext)


def _expand_cover(project: Dict[str, Any]) -> Dict[str, Any]:
    """Expand a top-level ``cover`` block (round 3) into a head clip
    + composed title/subtitle texts. Returns a NEW project dict;
    the input is not mutated. The cover IS the video's first frame:
    a colour clip of ``cover.duration`` with the template's layout.
    """
    import copy
    cover = project["cover"]
    H = int(project["height"])
    d = float(cover.get("duration", 3.0) or 3.0)
    bg = str(cover.get("background", "#0B0B0B"))
    accent = str(cover.get("accent", "#FFD60A"))
    title = str(cover["title"])
    subtitle = str(cover.get("subtitle") or "")
    template = str(cover.get("template", "bold"))

    def _t(text: str, size: int, color: str, x: Any, y: Any,
           font: str = "Anton") -> Dict[str, Any]:
        return {"text": text, "start": 0.0, "duration": d, "font": font,
                "size": size, "color": color, "x": x, "y": y,
                "animation": "fade"}

    texts: List[Dict[str, Any]] = []
    if template == "bold":
        # Giant centred title + accent subtitle below.
        texts.append(_t(title, int(H * 0.13), "#FFFFFF", "center", 0.36))
        if subtitle:
            texts.append(_t(subtitle, int(H * 0.045), accent, "center",
                            0.58, font="Inter"))
    elif template == "split":
        # Big title top-left, ASCII divider, subtitle bottom-left.
        texts.append(_t(title, int(H * 0.11), "#FFFFFF", "left", 0.10))
        texts.append(_t("-" * 12, int(H * 0.05), accent, "left", 0.30))
        if subtitle:
            texts.append(_t(subtitle, int(H * 0.05), accent, "left",
                            0.78, font="Inter"))
    else:  # minimal — small title + subtitle lower-left, empty frame
        texts.append(_t(title, int(H * 0.06), "#FFFFFF", "left", 0.72))
        if subtitle:
            texts.append(_t(subtitle, int(H * 0.035), accent, "left",
                            0.82, font="Inter"))
    proj = copy.deepcopy(project)
    proj["clips"] = ([{"src": f"color:{bg}", "duration": d}]
                     + list(proj.get("clips") or []))
    proj["texts"] = texts + list(proj.get("texts") or [])
    return proj


class RenderError(RuntimeError):
    """Raised when ffmpeg fails or a render input is unusable."""


class RenderResult:
    def __init__(self, output: str, duration: float, width: int,
                 height: int, fps: int, elapsed: float,
                 command: List[str]):
        self.output = output
        self.duration = duration
        self.width = width
        self.height = height
        self.fps = fps
        self.elapsed = elapsed
        self.command = command

    def as_dict(self) -> Dict[str, Any]:
        return {
            "output": self.output,
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "elapsed_seconds": round(self.elapsed, 1),
            "size_bytes": os.path.getsize(self.output),
        }


# ------------------------------------------------------------------ probing
def probe_media(path: str) -> Dict[str, Any]:
    """ffprobe a file: duration, streams, dimensions, fps, audio presence."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=index,codec_type,codec_name,width,height,r_frame_rate,"
           "nb_frames,duration",
           "-show_entries", "format=duration", "-of", "json", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             env=audio_mod.ffmpeg_env(), check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise RenderError(f"cannot probe {path!r}: {exc.stderr.strip()[:200]}")
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    aud = next((s for s in streams if s["codec_type"] == "audio"), None)
    dur = 0.0
    if video and video.get("duration"):
        dur = float(video["duration"])
    elif data.get("format", {}).get("duration"):
        dur = float(data["format"]["duration"])
    fps = 30.0
    if video and video.get("r_frame_rate"):
        num, den = video["r_frame_rate"].split("/")
        if int(den):
            fps = int(num) / int(den)
    return {
        "duration": dur,
        "has_video": video is not None,
        "has_audio": aud is not None,
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "fps": fps,
        "video_codec": video["codec_name"] if video else None,
        "audio_codec": aud["codec_name"] if aud else None,
    }


def _is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def _atempo_chain(speed: float) -> str:
    """Decompose a speed factor into atempo stages (each 0.5..2.0)."""
    if abs(speed - 1.0) < 1e-6:
        return ""
    factors: List[float] = []
    s = speed
    while s > 2.0:
        factors.append(2.0)
        s /= 2.0
    while s < 0.5:
        factors.append(0.5)
        s /= 0.5
    factors.append(s)
    return ",".join(f"atempo={f:.4f}" for f in factors)


def _escape_drawtext(text: str) -> str:
    """Escape text for a drawtext filter argument.

    drawtext uses ``:expansion=none`` so ``%`` is literal, but ``:``,
    ``'`` and ``\\`` still need escaping.
    """
    return (text.replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace(":", "\\:")
                .replace("\n", " "))


def _escape_filter_path(path: str) -> str:
    return (path.replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace(":", "\\:"))


# ------------------------------------------------------------------ renderer
class Renderer:
    def __init__(self, project: Dict[str, Any],
                 workdir: Optional[str] = None,
                 ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                 preset: str = "veryfast", crf: int = 20,
                 codec: Optional[str] = None,
                 export: Optional[Dict[str, Any]] = None,
                 filter_threads: Optional[int] = None):
        self.project = validate_timeline(dict(project))
        self.workdir = workdir or os.path.expanduser("~/workspace/.tmp/musecut")
        os.makedirs(self.workdir, exist_ok=True)
        self.ffmpeg = ffmpeg
        self.preset = preset
        self.crf = crf
        # (round 8) filter-graph threading: -filter_complex_threads
        # lets independent legs (clip chains, overlays) run on
        # separate threads. Measured on the round-8 benchmark:
        # 126.8 s -> 109.6 s (1.16x) with a BYTE-IDENTICAL output
        # (same md5, frame MAE 0.0) — threading changes scheduling,
        # not pixels. None = auto (CPU count, capped at 8); 0 = off.
        if filter_threads is None:
            self._filter_threads = min(os.cpu_count() or 4, 8)
        else:
            self._filter_threads = int(filter_threads)
        # Codec resolution order: explicit `codec` arg > project
        # export.codec > output-file extension > "h264". Resolved
        # lazily in _resolve_codec(output) because the extension is
        # only known at render time.
        self._codec_arg = codec
        self._export_arg = dict(export) if export else None
        self.codec_name: Optional[str] = None
        self.spec: Dict[str, Any] = EXPORT_CODECS["h264"]
        self.pix_fmt = "yuv420p"
        self.has_video_out = True
        self.has_audio_out = True
        self._hdr = False
        self._alpha = False
        self.W = int(self.project["width"])
        self.H = int(self.project["height"])
        self.fps = int(self.project["fps"])
        self._probe_cache: Dict[str, Dict[str, Any]] = {}
        self._n = 0  # label counter
        self._nest_depth = 0  # nested-sequence recursion depth (max 3)
        # (round 3) cover templates: expand the top-level `cover`
        # block into a colour clip at the head of the timeline +
        # composed title/subtitle texts, so the rest of the engine
        # sees plain clips and texts.
        if self.project.get("cover"):
            self.project = _expand_cover(self.project)

    def _resolve_codec(self, output: str) -> Dict[str, Any]:
        """Pick the export codec; sets codec_name/spec/pix_fmt and the
        has_video_out / has_audio_out flags. Idempotent."""
        if self.codec_name is not None:
            return self.spec
        name = self._codec_arg
        if name is not None:
            # Accept both timeline names ("h265") and raw encoder
            # names ("libx265") for backward compatibility.
            name = _ENCODER_TO_CODEC.get(name, name)
            if name not in EXPORT_CODECS:
                raise RenderError(f"unknown export codec: {name!r}")
        else:
            exp = self._export_arg if self._export_arg is not None \
                else (self.project.get("export") or {})
            name = exp.get("codec") or _codec_from_extension(output) \
                or "h264"
            if name not in EXPORT_CODECS:
                raise RenderError(f"unknown export codec: {name!r}")
        self.codec_name = name
        self.spec = EXPORT_CODECS[name]
        self.pix_fmt = self.spec.get("pix_fmt", "yuv420p")
        self.has_video_out = bool(self.spec.get("has_video", True))
        self.has_audio_out = bool(self.spec.get("has_audio", True))
        # (round 3) HDR10: 10-bit PQ/BT.2020 via zscale, libx265 with
        # mastering metadata. Alpha: keep the alpha plane through
        # the graph (ProRes 4444 only — HEVC-with-alpha is blocked
        # on this build, see timeline.py).
        exp = self._export_arg if self._export_arg is not None \
            else (self.project.get("export") or {})
        self._hdr = bool(exp.get("hdr"))
        self._alpha = bool(exp.get("alpha"))
        if self._hdr and name != "h265":
            raise RenderError(
                "HDR export needs codec h265 — "
                f"got {name!r}")
        if self._alpha and name != "prores4444":
            raise RenderError(
                "alpha export needs codec prores4444 — HEVC-with-alpha "
                "is blocked on this build (libx265 lists no alpha "
                "pixel formats; verified 2026-11-20)")
        if self._hdr:
            self.pix_fmt = "yuv420p10le"
        if self._alpha:
            # ProRes 4444 is a 12-bit codec: prores_ks negotiates
            # yuva444p12le even when 10-bit is requested.
            self.pix_fmt = "yuva444p12le"
        if name == "dnxhd" and (self.W, self.H) not in _DNXHD_SETTINGS:
            raise RenderError(
                f"DNxHD export needs a standard HD/UHD frame size "
                f"({', '.join(f'{w}x{h}' for w, h in _DNXHD_SETTINGS)}), "
                f"got {self.W}x{self.H}")
        return self.spec

    def _ladder_entry(self) -> Optional[Dict[str, Any]]:
        """Bitrate-ladder entry for this canvas, or None if no ladder
        is configured. Returns {bitrate, gop, profile}."""
        exp = self._export_arg if self._export_arg is not None \
            else (self.project.get("export") or {})
        lad = exp.get("bitrate_ladder")
        if not lad:
            return None
        merged = dict(DEFAULT_LADDER)
        merged.update(lad)
        key = ("sd" if self.H <= 480 else "hd" if self.H <= 720
               else "fullhd" if self.H <= 1080 else "2k"
               if self.H <= 1440 else "4k")
        gop = int(merged.get("gop") or 0) or (2 * self.fps)
        return {"bitrate": _parse_bitrate(merged[key]), "gop": gop,
                "profile": str(merged.get("profile") or "high")}

    # -- helpers ------------------------------------------------------
    def _lbl(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def _probe(self, path: str) -> Dict[str, Any]:
        if path not in self._probe_cache:
            self._probe_cache[path] = probe_media(path)
        return self._probe_cache[path]

    def _resolve_nested(self, nested: Any) -> Dict[str, Any]:
        """Resolve a clip's ``nested`` value to a project dict.

        Accepts an inline project dict, ``"sequence:<name>"`` (a
        top-level ``sequences`` entry), or a path to a .json project.
        """
        if isinstance(nested, dict):
            return validate_timeline(dict(nested))
        if isinstance(nested, str) and nested.startswith("sequence:"):
            name = nested.split(":", 1)[1]
            seqs = self.project.get("sequences") or {}
            if name not in seqs:
                raise RenderError(f"unknown nested sequence: {name!r}")
            return validate_timeline(dict(seqs[name]))
        if isinstance(nested, str):
            from .timeline import load_project
            try:
                return load_project(nested)
            except (OSError, ValueError) as exc:
                raise RenderError(f"cannot load nested project {nested!r}: {exc}")
        raise RenderError(f"invalid nested value: {nested!r}")

    def _resolve_clip(self, clip: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in a clip's effective duration from probing."""
        c = dict(clip)
        if c.get("nested") is not None:
            if self._nest_depth >= 3:
                raise RenderError(
                    "nested sequence depth exceeds 3 — check for a cycle")
            doc = self._resolve_nested(c.pop("nested"))
            import hashlib
            blob = json.dumps(doc, sort_keys=True).encode()
            digest = hashlib.sha1(blob).hexdigest()[:12]
            tmp = os.path.join(self.workdir, f"nested_{digest}.mp4")
            if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                sub = Renderer(doc, workdir=self.workdir,
                               preset=self.preset, crf=self.crf)
                sub._nest_depth = self._nest_depth + 1
                sub.render(tmp, quiet=True)
            c["src"] = tmp
            c["_nested"] = True
        src = c["src"]
        lut = (c.get("effects") or {}).get("lut")
        if lut and not os.path.isfile(str(lut)):
            raise RenderError(f"LUT file not found: {lut!r}")
        if src.startswith("color:") or src == "testsrc":
            c["_duration"] = float(c.get("duration") or 3.0)
            c["_has_audio"] = False
            return c
        if not os.path.isfile(src):
            raise RenderError(f"clip file not found: {src!r}")
        info = self._probe(src)
        c["_has_audio"] = info["has_audio"]
        if _is_image(src):
            c["_duration"] = float(c.get("duration") or 3.0)
            c["_is_image"] = True
            return c
        src_dur = info["duration"]
        start = float(c.get("start", 0.0))
        dur = c.get("duration")
        if dur is None:
            dur = max(src_dur - start, 0.1)
        else:
            dur = min(float(dur), max(src_dur - start, 0.1))
        c["_duration"] = float(dur)
        return c

    def _expand_speed_curve(self, clip: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Split a speed-curve clip into constant-speed sub-clips."""
        curve = clip.get("speed_curve")
        if not curve:
            return [clip]
        start = float(clip.get("start", 0.0))
        total = clip["_duration"]
        points = sorted(curve, key=lambda p: p["time"])
        segments: List[Dict[str, Any]] = []
        for i, p in enumerate(points):
            seg_start = start + float(p["time"])
            seg_end = start + (float(points[i + 1]["time"])
                               if i + 1 < len(points) else total)
            if seg_end <= seg_start:
                continue
            sub = dict(clip)
            sub.pop("speed_curve", None)
            sub["start"] = seg_start
            sub["duration"] = seg_end - seg_start
            sub["speed"] = float(p["speed"])
            sub["_duration"] = seg_end - seg_start
            segments.append(sub)
        if not segments:
            return [clip]
        # (round 8) Keyframes are authored in clip OUTPUT time, but
        # each segment leg evaluates them against its own local t
        # (0 at the segment start). Window-shift them into each
        # segment's output window — before this fix the whole curve
        # replayed in every segment (a scale 1->3 over 4 s restarted
        # at 1.0 in segment 2). Legacy zoom/pan interpolate linearly
        # over clip output time, so re-anchor from/to per segment.
        kf = clip.get("keyframes") or {}
        total_out = sum(s["_duration"] / float(s["speed"])
                        for s in segments)
        out_t = 0.0
        for seg in segments:
            seg_out = seg["_duration"] / float(seg["speed"])
            w0, w1 = out_t, out_t + seg_out
            if kf:
                new_kf = dict(kf)
                for prop in ("position", "scale", "rotation", "opacity"):
                    if prop in kf:
                        new_kf[prop] = kf_mod.shift_to_window(
                            kf[prop], w0, w1, pair=(prop == "position"))
                if "zoom" in kf:
                    z = kf["zoom"]
                    z0, z1 = float(z.get("from", 1.0)), float(z.get("to", 1.25))
                    new_kf["zoom"] = {
                        "from": z0 + (z1 - z0) * (w0 / total_out),
                        "to": z0 + (z1 - z0) * (w1 / total_out),
                    }
                if "pan" in kf:
                    pan = kf["pan"]
                    pf = tuple(pan.get("from", (0.5, 0.5)))
                    pt = tuple(pan.get("to", (0.5, 0.5)))
                    new_kf["pan"] = {
                        "from": [pf[a] + (pt[a] - pf[a]) * (w0 / total_out)
                                 for a in (0, 1)],
                        "to": [pf[a] + (pt[a] - pf[a]) * (w1 / total_out)
                               for a in (0, 1)],
                    }
                seg["keyframes"] = new_kf
            out_t = w1
        return segments

    # -- video legs ---------------------------------------------------
    def _video_leg(self, idx: int, seg: Dict[str, Any],
                   filters: List[str], add_input=None,
                   seek: float = 0.0) -> str:
        """Build one segment's video chain. Returns its output label.

        ``add_input`` (from ``build_command``) registers extra
        inputs — used by ``background_remove`` for the mask video
        and the replacement-background source.

        ``seek`` is the input-level ``-ss`` offset already applied
        to this input (round 8): the trim window shifts down by it,
        so the leg still cuts exactly the same source frames.
        """
        src = seg["src"]
        start = float(seg.get("start", 0.0)) - seek
        dur = seg["_duration"]
        speed = float(seg.get("speed") or 1.0)
        label = f"[{idx}:v]"
        chain: List[str] = []

        if seg.get("freeze_at") is not None:
            f = float(seg["freeze_at"]) - seek
            chain.append(f"trim=start={f}:end={f + 0.04}")
            chain.append(f"setpts=N/({self.fps}*TB)")
            chain.append("loop=loop=-1:size=1:start=0")
            chain.append(f"trim=duration={dur:.3f}")
            chain.append("setpts=PTS-STARTPTS")
        else:
            chain.append(f"trim=start={start:.3f}:end={start + dur:.3f}")
            chain.append("setpts=PTS-STARTPTS")
            if seg.get("reverse"):
                chain.append("reverse")
                chain.append("setpts=PTS-STARTPTS")
            if abs(speed - 1.0) > 1e-6:
                chain.append(f"setpts=PTS/{speed:.4f}")

        chain.append(f"fps=fps={self.fps}")

        br = seg.get("background_remove")
        if br:
            # (round 4, v0.6.0) AI background removal: the mask
            # video (grayscale, source resolution/fps, built by
            # musecut.bgremove) is merged as the clip's alpha,
            # then composited onto the replacement background —
            # all BEFORE fit/grade, at native resolution, so the
            # mask aligns 1:1 with the clip. The mask input loops
            # (-stream_loop) so it covers any clip duration.
            # Limits (validated in timeline.py): no speed change,
            # no reverse, no freeze, and "transparent" needs no
            # position/scale/opacity keyframes (the transform
            # block composites onto a black canvas, which would
            # destroy the alpha).
            if add_input is None:
                raise RenderError(
                    "background_remove needs the renderer's input "
                    "registry — render through build_command, not "
                    "_video_leg directly")
            mask = str(br["mask"])
            bg = br.get("background", "#00FF00")
            out0 = self._lbl("v")
            filters.append(f"{label}{','.join(chain)}[{out0}]")
            midx = add_input(["-stream_loop", "-1"], mask)
            m = self._lbl("mk")
            filters.append(
                f"[{midx}:v]trim=start={start:.3f}:end={start + dur:.3f},"
                f"setpts=PTS-STARTPTS,fps=fps={self.fps},"
                f"format=gray[{m}]")
            am = self._lbl("am")
            filters.append(f"[{out0}][{m}]alphamerge[{am}]")
            if bg == "transparent":
                label = f"[{am}]"
                chain = []
            elif isinstance(bg, str) and bg.startswith("#"):
                # Solid colour: paint it with geq (same size as
                # the frame itself, so no size bookkeeping), then
                # overlay the alpha frame on top.
                hexcol = bg.lstrip("#")
                r = int(hexcol[0:2], 16)
                g = int(hexcol[2:4], 16)
                b = int(hexcol[4:6], 16)
                a1, a2 = self._lbl("bg"), self._lbl("bg")
                filters.append(f"[{am}]split[{a1}][{a2}]")
                sol = self._lbl("bg")
                filters.append(
                    f"[{a1}]geq=r='{r}':g='{g}':b='{b}'[{sol}]")
                comp = self._lbl("bg")
                filters.append(
                    f"[{sol}][{a2}]overlay=0:0,format=yuv420p[{comp}]")
                label = f"[{comp}]"
                chain = []
            else:
                # Image background: scale it to the frame with
                # scale2ref (second output is the reference frame
                # unchanged), then overlay the alpha frame on top.
                bidx = add_input([], str(bg))
                scaled = self._lbl("bg")
                ref = self._lbl("bg")
                filters.append(
                    f"[{bidx}:v][{am}]scale2ref[{scaled}][{ref}]")
                comp = self._lbl("bg")
                filters.append(
                    f"[{scaled}][{ref}]overlay=0:0,"
                    f"format=yuv420p[{comp}]")
                label = f"[{comp}]"
                chain = []

        canvas = self.project.get("canvas") or {}
        canvas_type = canvas.get("type") if canvas else None
        kf = seg.get("keyframes")
        has_zoom = bool(kf and kf.get("zoom"))
        # Per-clip fit mode (v3). "contain" = the old scale+pad;
        # "cover" fills the canvas (centre-crop); "blur" centres the
        # clip on a blurred copy of itself. fit is IGNORED when legacy
        # zoom keyframes are present — zoompan does its own cover
        # after the aspect fix below.
        fit = seg.get("fit", "contain")
        if has_zoom:
            # zoompan already outputs a full canvas frame; a canvas
            # background would be invisible behind it.
            z = kf["zoom"]
            pan_from = tuple(kf.get("pan", {}).get("from", (0.5, 0.5)))
            pan_to = tuple(kf.get("pan", {}).get("to", (0.5, 0.5)))
            # (v3 fix) cover-crop to canvas aspect BEFORE zoompan, so
            # its crop->scale to s=WxH is aspect-preserving. Without
            # this a 16:9 source into a 9:16 canvas came out stretched.
            chain.append(
                f"scale={self.W}:{self.H}:force_original_aspect_ratio=increase,"
                f"crop={self.W}:{self.H},setsar=1")
            chain.append(fx.zoompan_filter(
                self.W, self.H, dur / speed, self.fps,
                float(z.get("from", 1.0)), float(z.get("to", 1.25)),
                pan_from, pan_to))
        elif fit == "cover":
            chain.append(
                f"scale={self.W}:{self.H}:force_original_aspect_ratio=increase,"
                f"crop={self.W}:{self.H},setsar=1")
        elif fit == "none":
            # (round 3) no sizing at all: the clip stays at native
            # resolution and the transform keyframes (position/scale)
            # below do the sizing. Required by smart_reframe — with
            # "cover" the keyframe scale would apply ON TOP of the
            # cover scale (double zoom) and the position math would
            # be wrong (verified by grid diagnostic 2026-11-20).
            # Validation guarantees transform keyframes exist.
            pass
        elif fit == "blur":
            # Foreground: scale to fit (no pad). Emit the partial
            # chain now, then the split/blur/overlay label stage,
            # then reset label/chain so the remaining steps
            # (stabilize/colour/grade/...) continue on the composited
            # full-canvas frame.
            chain.append(
                f"scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,"
                "setsar=1")
            out = self._lbl("v")
            filters.append(f"{label}{','.join(chain)}[{out}]")
            cur = out
            s1, s2, bg = self._lbl("fb"), self._lbl("fb"), self._lbl("fbg")
            filters.append(f"[{cur}]split[{s1}][{s2}]")
            filters.append(
                f"[{s2}]{fx.blur_canvas_filter(self.W, self.H, 25.0)}"
                f"[{bg}]")
            comp = self._lbl("fb")
            filters.append(
                f"[{bg}][{s1}]overlay=(W-w)/2:(H-h)/2[{comp}]")
            cur = comp
            label = f"[{cur}]"
            chain = []
        elif canvas_type in ("image", "blur"):
            # No pad here — the canvas composite (below) supplies the
            # full-size background and centres the clip on it.
            chain.append(
                f"scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,"
                "setsar=1")
        elif canvas_type == "color":
            color = (canvas.get("color", "#000000")).lstrip("#")
            chain.append(
                f"scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,"
                f"pad={self.W}:{self.H}:(ow-iw)/2:(oh-ih)/2:color=0x{color},"
                "setsar=1")
        else:
            chain.append(
                f"scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,"
                f"pad={self.W}:{self.H}:(ow-iw)/2:(oh-ih)/2,"
                "setsar=1")

        if seg.get("stabilize"):
            trf = os.path.join(self.workdir,
                               f"stab_{idx}_{abs(hash(src)) % 99999}.trf")
            if not os.path.exists(trf):
                cmd = fx.stabilize_detect_cmd(src, trf, start, dur)
                subprocess.run(cmd, check=True, env=audio_mod.ffmpeg_env())
            chain.append(fx.stabilize_filter(trf))

        effects = seg.get("effects") or {}
        colour = fx.color_filter(effects)
        if colour:
            chain.append(colour)
        # Colour preset (e.g. cinematic) is already in the colour chain
        # above; here only add vignette + grain on top of it.
        extra = fx.grade_filter(vignette=float(effects.get("vignette", 0) or 0),
                                grain=float(effects.get("grain", 0) or 0))
        if extra:
            chain.append(extra)
        gl = effects.get("glitch")
        if gl:
            # (v3) RGB-split + slice displacement + noise, optional
            # strobe. Runs after the grade, before motion blur.
            if isinstance(gl, dict):
                chain.append(fx.glitch_filter(
                    float(gl.get("strength", 0.5)),
                    int(gl.get("strobe", 0))))
            else:
                chain.append(fx.glitch_filter())
        df = effects.get("deflicker")
        if df:
            # (round 2) temporal luminance smoothing, after the
            # grade/glitch so it sees the final picture.
            if isinstance(df, dict):
                chain.append(fx.deflicker_filter(
                    int(df.get("size", 5)), str(df.get("mode", "am"))))
            else:
                chain.append(fx.deflicker_filter())
        fl = effects.get("flip")
        if fl:
            # (round 52, v0.20.0) mirror the frame: True = horizontal
            # (the default), or "horizontal" / "vertical" / "both".
            # Geometric only — runs before the transform keyframes,
            # so a flipped clip punches in about the mirrored frame.
            mode = "horizontal" if fl is True else str(fl)
            chain.append(fx.flip_filter(mode))
        mb = seg.get("motion_blur")
        if mb:
            frames = int(mb.get("frames", 8)) if isinstance(mb, dict) else 8
            chain.append(fx.motion_blur_filter(frames))
        kf = seg.get("keyframes") or {}
        if kf.get("rotation"):
            chain.append(fx.rotate_keyframe_filter(kf["rotation"]))
        if effects.get("lut"):
            chain.append(fx.lut3d_filter(str(effects["lut"])))
        chain.append(f"format={self.pix_fmt}")

        out = self._lbl("v")
        filters.append(f"{label}{','.join(chain)}[{out}]")
        cur = out

        # ---- full transform keyframes (position / scale / opacity).
        # Scale and opacity are evaluated per frame, so they run on
        # labels: scale the frame, rewrite the alpha plane with geq,
        # then composite onto a black canvas at the keyframed
        # position (anchor point = the position reference).
        if kf.get("position") or kf.get("scale") or kf.get("opacity"):
            out_dur = dur / speed
            anchor = kf.get("anchor", [0.5, 0.5])
            if kf.get("scale"):
                s = self._lbl("sc")
                sexpr = kf_mod.ffmpeg_escape(kf_mod.scalar_expr(kf["scale"], 1.0))
                filters.append(
                    f"[{cur}]scale=w='iw*({sexpr})':h='ih*({sexpr})':"
                    f"eval=frame[{s}]")
                cur = s
            if kf.get("opacity"):
                o = self._lbl("op")
                filters.append(
                    f"[{cur}]{fx.opacity_keyframe_filter(kf['opacity'])}[{o}]")
                cur = o
            bg = self._lbl("tbg")
            filters.append(
                f"color=c=0x000000:s={self.W}x{self.H}:r={self.fps}:"
                f"d={out_dur:.3f}[{bg}]")
            if kf.get("position"):
                px = kf_mod.ffmpeg_escape(
                    kf_mod.pair_expr(kf["position"], 0, 0.5))
                py = kf_mod.ffmpeg_escape(
                    kf_mod.pair_expr(kf["position"], 1, 0.5))
                x = f"({px})*W-({float(anchor[0]):.3f})*w"
                y = f"({py})*H-({float(anchor[1]):.3f})*h"
            else:
                x, y = "(W-w)/2", "(H-h)/2"
            comp = self._lbl("tc")
            filters.append(
                f"[{bg}][{cur}]overlay=x='{x}':y='{y}',"
                f"format={self.pix_fmt}[{comp}]")
            cur = comp

        # ---- canvas composite (image / blur): centre the clip on a
        # full-size background. Colour canvases were handled by pad.
        if canvas_type in ("image", "blur") and not has_zoom \
                and fit != "blur":
            out_dur = dur / speed
            if canvas_type == "image":
                bg = self._lbl("cbg")
                img = _escape_filter_path(
                    str(canvas["image"]))
                filters.append(
                    f"movie='{img}',"
                    f"scale={self.W}:{self.H}:"
                    f"force_original_aspect_ratio=increase,"
                    f"crop={self.W}:{self.H},"
                    f"loop=loop=-1:size=1:start=0,"
                    f"trim=duration={out_dur:.3f},setpts=PTS-STARTPTS,"
                    f"fps=fps={self.fps}[{bg}]")
            else:
                sigma = float(canvas.get("blur", 25) or 25)
                s1 = self._lbl("cs")
                s2 = self._lbl("cs")
                bg = self._lbl("cbg")
                filters.append(f"[{cur}]split[{s1}][{s2}]")
                filters.append(
                    f"[{s2}]{fx.blur_canvas_filter(self.W, self.H, sigma)}"
                    f"[{bg}]")
                cur = s1
            comp = self._lbl("cc")
            filters.append(
                f"[{bg}][{cur}]overlay=(W-w)/2:(H-h)/2[{comp}]")
            cur = comp

        ck = seg.get("chroma_key")
        if ck:
            bg = self._lbl("bg")
            filters.append(
                f"color=c=0x000000:s={self.W}x{self.H}:r={self.fps}:"
                f"d={dur / speed:.3f}[{bg}]")
            keyed = self._lbl("ck")
            filters.append(
                f"[{cur}]{fx.chroma_key_filter(ck.get('color', '0x00FF00'), float(ck.get('similarity', 0.3)), float(ck.get('blend', 0.1)))},format=yuva420p[{keyed}]")
            ov = self._lbl("cko")
            filters.append(
                f"[{bg}][{keyed}]overlay=0:0,format={self.pix_fmt}[{ov}]")
            cur = ov

        mask = seg.get("mask")
        if mask:
            m = self._lbl("msk")
            filters.append(
                f"nullsrc=s={self.W}x{self.H}:r={self.fps}:d={dur / speed:.3f}[{m}src];"
                f"[{m}src]{fx.mask_filter(mask.get('shape', 'circle'), float(mask.get('x', 0.5)), float(mask.get('y', 0.5)), float(mask.get('size', 0.4)), float(mask.get('feather', 0.05)), float(mask.get('angle', 45)))}[{m}]")
            a = self._lbl("ma")
            b = self._lbl("mb")
            filters.append(f"[{cur}]split[{a}][{b}]")
            _mask_eff = {
                "blur": "gblur=sigma=8",
                "darken": "eq=brightness=-0.45",
                "desaturate": "hue=s=0",
            }
            eff = _mask_eff.get(mask.get("effect", "blur"), "gblur=sigma=8")
            be = self._lbl("mbe")
            filters.append(f"[{b}]{eff}[{be}]")
            mg = self._lbl("mm")
            # maskedmerge semantics (verified 2026-11-20 with a
            # white/black probe): mask WHITE takes the SECOND input,
            # mask BLACK takes the FIRST. The shape interior (white)
            # must stay ORIGINAL and the exterior (black) EFFECTED,
            # so the effected copy goes FIRST. (Before v3 the order
            # was reversed — the effect landed INSIDE the shape; the
            # v2 test only checked duration, so it never caught it.)
            filters.append(f"[{be}][{a}][{m}]maskedmerge[{mg}]")
            cur = mg

        return cur

    def _audio_leg(self, idx: int, seg: Dict[str, Any],
                   filters: List[str], seek: float = 0.0) -> str:
        """Build one segment's audio chain. Returns its output label.

        ``seek`` is the input-level ``-ss`` offset already applied
        to this input (round 8): the atrim window shifts down by
        it, so the leg still cuts exactly the same source audio.
        """
        dur = seg["_duration"]
        speed = float(seg.get("speed") or 1.0)
        out_dur = dur / speed
        if seg.get("muted") or not seg.get("_has_audio"):
            lbl = self._lbl("sil")
            filters.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000:"
                f"duration={out_dur:.3f}[{lbl}]")
            return lbl
        start = float(seg.get("start", 0.0)) - seek
        chain = [f"atrim=start={start:.3f}:end={start + dur:.3f}",
                 "asetpts=PTS-STARTPTS"]
        atempo = _atempo_chain(speed)
        if atempo:
            chain.append(atempo)
        if seg.get("reverse"):
            chain.append("areverse")
        vol = float(seg.get("volume", 1.0))
        if abs(vol - 1.0) > 1e-6:
            chain.append(f"volume={vol:.3f}")
        chain.append("aresample=48000")
        chain.append("aformat=channel_layouts=stereo")
        out = self._lbl("a")
        filters.append(f"[{idx}:a]{','.join(chain)}[{out}]")
        return out

    # -- main build ---------------------------------------------------
    def build_command(self, output: str, audio_only: bool = False
                      ) -> Tuple[List[str], float, Dict[str, Any]]:
        """Build the ffmpeg command. Returns (cmd, total_duration, info).

        ``audio_only`` builds just the audio graph (wav/mp3 export):
        no video legs, no video filters. Before round 8 an
        audio-only export rendered a full video intermediate first
        (~104 s wall for an 8 s wav); the direct graph takes seconds
        and mixes identically.
        """
        proj = self.project
        filters: List[str] = []
        inputs: List[List[str]] = []  # per-input argv fragments
        self._resolve_codec(output)
        if not self.has_video_out and not audio_only:
            # Audio-only (wav/mp3) needs the audio_only flag: a plain
            # graph would leave the video branch unmapped and ffmpeg
            # errors on unconnected filter outputs.
            raise RenderError(
                "audio-only export (wav/mp3) must go through render() "
                "or build_command(audio_only=True)")
        canvas = proj.get("canvas") or {}
        if canvas.get("type") == "image":
            if not os.path.isfile(str(canvas.get("image", ""))):
                raise RenderError(
                    f"canvas image not found: {canvas.get('image')!r}")

        def add_input(pre: List[str], spec: str) -> int:
            inputs.append(pre + ["-i", spec])
            return len(inputs) - 1

        clips = [self._resolve_clip(c) for c in proj.get("clips") or []]
        if not clips:
            raise RenderError("project has no clips — nothing to render")

        # ---- main clip inputs + legs (with speed-curve expansion)
        clip_vlabels: List[str] = []
        clip_alabels: List[str] = []
        clip_durs: List[float] = []  # output duration of each top-level clip
        for ci, clip in enumerate(clips):
            src = clip["src"]
            seek = 0.0  # input-level -ss offset; 0 for generated/image
            if src.startswith("color:"):
                color = src.split(":", 1)[1].lstrip("#")
                idx = add_input(
                    ["-f", "lavfi"], f"color=c=0x{color}:s={self.W}x{self.H}:"
                       f"r={self.fps}:d={clip['_duration']:.3f}")
            elif src == "testsrc":
                idx = add_input(
                    ["-f", "lavfi"], f"testsrc2=size={self.W}x{self.H}:"
                       f"rate={self.fps}:duration={clip['_duration']:.3f}")
            elif _is_image(src):
                idx = add_input(["-loop", "1", "-t", f"{clip['_duration']:.3f}"], src)
            else:
                # (round 8) Fast input seek: ffmpeg used to decode
                # every clip from 0 and trim in the graph, so a clip
                # trimmed at 50 s paid 50 s of decode for 2 s of
                # footage. ``-ss`` before ``-i`` seeks to the
                # nearest keyframe and ffmpeg discards frames before
                # the seek point, so the stream starts exactly at
                # the trim point; the legs' trim windows shift down
                # by the same offset and cut identical frames
                # (verified by frame comparison in test_round8).
                seek = float(clip.get("start", 0.0))
                pre = ["-ss", f"{seek:.3f}"] if seek > 0 else []
                idx = add_input(pre, src)

            segments = self._expand_speed_curve(clip)
            seg_v, seg_a = [], []
            for seg in segments:
                if not audio_only:
                    seg_v.append(self._video_leg(idx, seg, filters,
                                                 add_input, seek=seek))
                seg_a.append(self._audio_leg(idx, seg, filters,
                                             seek=seek))
            if len(segments) == 1:
                if not audio_only:
                    clip_vlabels.append(seg_v[0])
                clip_alabels.append(seg_a[0])
            else:
                if not audio_only:
                    cv = self._lbl("cv")
                    v_in = "".join(f"[{l}]" for l in seg_v)
                    filters.append(f"{v_in}concat=n={len(seg_v)}:v=1:a=0[{cv}]")
                    clip_vlabels.append(cv)
                ca = self._lbl("ca")
                a_in = "".join(f"[{l}]" for l in seg_a)
                filters.append(f"{a_in}concat=n={len(seg_a)}:v=0:a=1[{ca}]")
                clip_alabels.append(ca)
            # (round 8) output duration is the segments' summed
            # output durations. For a speed-curve clip the source
            # duration is wrong here — it fed the wrong main_dur,
            # so the audio fade-out, the endcard total and the
            # reported duration were all off (a 1s@1x + 1s@2x clip
            # reported 2.0 s while the file is 1.5 s). For a plain
            # clip segments == [clip], so this is identical.
            clip_durs.append(sum(
                s["_duration"] / float(s.get("speed") or 1.0)
                for s in segments))

        # ---- join main clips (video xfade / concat, audio acrossfade)
        cur_v = clip_vlabels[0] if not audio_only else None
        cur_a = clip_alabels[0]
        cur_dur = clip_durs[0]
        # Timeline start time of each main clip (for object-lock
        # bindings, which read the anchor clip's keyframes in
        # main-timeline time). Clip i starts where the previous clip
        # ends, minus the transition overlap when xfading.
        clip_starts = [0.0] * len(clips)
        whoosh_times: List[float] = []
        for i in range(1, len(clips)):
            tr = (clips[i].get("transition_in") or {})
            ttype = tr.get("type", "none")
            td = float(tr.get("duration", 0.5) or 0)
            td = min(td, cur_dur * 0.9, clip_durs[i] * 0.9)
            clip_starts[i] = max(cur_dur - td, 0.0) \
                if ttype != "none" else cur_dur
            if ttype == "none" or td <= 0:
                na = self._lbl("cuta")
                filters.append(f"[{cur_a}][{clip_alabels[i]}]"
                               f"concat=n=2:v=0:a=1[{na}]")
                cur_a = na
                if not audio_only:
                    nv = self._lbl("cut")
                    filters.append(f"[{cur_v}][{clip_vlabels[i]}]"
                                   f"concat=n=2:v=1:a=0[{nv}]")
                    cur_v = nv
            else:
                na = self._lbl("xfa")
                xf = XFADE_MAP.get(ttype, "fade")
                off = max(cur_dur - td, 0)
                filters.append(
                    f"[{cur_a}][{clip_alabels[i]}]acrossfade=d={td:.3f}:"
                    f"c1=tri:c2=tri[{na}]")
                cur_a = na
                if not audio_only:
                    nv = self._lbl("xf")
                    filters.append(
                        f"[{cur_v}][{clip_vlabels[i]}]xfade=transition={xf}:"
                        f"duration={td:.3f}:offset={off:.3f}[{nv}]")
                    cur_v = nv
                if ttype == "whoosh":
                    whoosh_times.append(off + td / 2)
            cur_dur = cur_dur + clip_durs[i] - (td if ttype != "none" else 0)

        main_dur = cur_dur
        self._clip_starts = clip_starts

        def lock_coord(lock_to: Dict[str, Any], axis: int,
                       size_var: str) -> str:
            """Object-lock coordinate for one axis.

            The element's top-left corner follows the anchor clip's
            keyframed position point (fraction of the canvas) plus
            the dx/dy pixel offset. ``size_var`` is "W"/"H" for the
            overlay filter, "w"/"h" for drawtext (the two filters
            name the frame size differently). The anchor's keyframes
            are read in clip-local time, so the time variable is
            shifted by the anchor's timeline start; outside the
            anchor's range the expression holds its first/last
            value. Returns a comma-escaped expression.
            """
            ci = int(lock_to["clip"])
            anchor = clips[ci]
            kfs = (anchor.get("keyframes") or {}).get("position")
            start = clip_starts[ci]
            if kfs:
                var = f"(t-{start:.3f})" if start > 0 else "t"
                expr = kf_mod.pair_expr(kfs, axis, 0.5, var=var)
            else:
                expr = "0.5"
            off = float(lock_to.get("dx" if axis == 0 else "dy", 0) or 0)
            return kf_mod.ffmpeg_escape(
                f"({expr})*{size_var}+{off:.3f}")

        # ---- adjustment layers (pro): effects over a time range of
        # the composited picture. Each filter gets an enable window
        # so the grade only applies between start and start+duration.
        # (round 8) video-only sections are skipped for audio_only
        # exports by iterating nothing — the bodies reference cur_v,
        # which is None in that mode.
        for al in (proj.get("adjustment_layers") or []
                   if not audio_only else []):
            start = float(al.get("start", 0))
            end = start + float(al["duration"])
            fx_parts: List[str] = []
            cchain = fx.color_filter(al.get("effects") or {})
            if cchain:
                fx_parts += _split_filter_chain(cchain)
            gchain = fx.grade_filter(
                vignette=float((al.get("effects") or {}).get("vignette", 0) or 0),
                grain=float((al.get("effects") or {}).get("grain", 0) or 0))
            if gchain:
                fx_parts += _split_filter_chain(gchain)
            if not fx_parts:
                continue
            en = f"between(t,{start:.3f},{end:.3f})"
            applied = ",".join(f"{p}:enable='{en}'" for p in fx_parts)
            nxt = self._lbl("adj")
            filters.append(f"[{cur_v}]{applied}[{nxt}]")
            cur_v = nxt

        # ---- overlays (PiP / stickers / blend)
        overlay_idx: Dict[int, int] = {}
        for oi, ov in enumerate(proj.get("overlays") or []
                                if not audio_only else []):
            src = ov["src"]
            dur = float(ov["duration"])
            if _is_image(src):
                idx = add_input(["-loop", "1", "-t", f"{dur:.3f}"], src)
            else:
                idx = add_input([], src)
            overlay_idx[oi] = idx
        for oi, ov in enumerate(proj.get("overlays") or []
                                if not audio_only else []):
            idx = overlay_idx[oi]
            src = ov["src"]
            start = float(ov.get("start", 0))
            dur = float(ov["duration"])
            w = ov.get("width")
            scale = f"scale={int(w)}:-2" if w else "scale=-2:-2"
            ov_lbl = self._lbl("ov")
            if _is_image(src):
                # -loop 1 input is already a video stream of `dur`
                chain = "setpts=PTS-STARTPTS"
            else:
                off = float(ov.get("offset", 0))
                # Round 13: shift the overlay's timestamps to its
                # timeline start. Without the offset the overlay
                # stream spans 0..dur while the main timeline is at
                # start..start+dur, so the overlay filter holds the
                # LAST overlay frame for the whole window — a video
                # overlay with start>0 played frozen (found by the
                # round-13 foreground-pass mastery: the pass window
                # went black on the plate's opaque last frame).
                chain = (f"trim=start={off:.3f}:end={off + dur:.3f},"
                         f"setpts=PTS-STARTPTS+{start:.3f}/TB")
            filters.append(
                f"[{idx}:v]{chain},"
                f"{scale},format=yuva420p[{ov_lbl}]")
            x = self._pos_expr(ov.get("x", "center"), "x")
            y = self._pos_expr(ov.get("y", "center"), "y")
            if ov.get("lock_to"):
                # Object lock: follow the anchor clip's keyframed
                # position (overlay filter names the frame W/H).
                x = lock_coord(ov["lock_to"], 0, "W")
                y = lock_coord(ov["lock_to"], 1, "H")
            anim = ov.get("animation", "none")
            if anim == "fade":
                filters.append(
                    f"[{ov_lbl}]format=yuva420p,"
                    f"fade=t=in:st={start:.3f}:d=0.3:alpha=1,"
                    f"fade=t=out:st={start + dur - 0.3:.3f}:d=0.3:alpha=1[{ov_lbl}f]")
                ov_lbl = ov_lbl + "f"
            if ov.get("blend"):
                blended = self._lbl("bl")
                # Round 47: blend in RGB, not YUV. ffmpeg's blend on
                # YUV screens the chroma planes toward max (U and V
                # both -> 255), so a screen blend washed the whole
                # frame magenta — measured 89 MAE between a YUV
                # screen and an RGB screen of the same two frames,
                # and the first real blend render came out pink
                # (probe47). Convert both inputs to planar RGB
                # (gbrp), blend there, convert back to yuv420p:
                # the RGB screen matches the numpy formula to
                # 0.42 MAE. (A fade animation on a blend overlay
                # loses its alpha in the RGB conversion — the fade
                # is a non-blend overlay feature; blend overlays
                # are full-strength for their window.)
                b0 = self._lbl("brgb0")
                b1 = self._lbl("brgb1")
                bout = self._lbl("brgbo")
                filters.append(f"[{cur_v}]format=gbrp[{b0}]")
                filters.append(f"[{ov_lbl}]format=gbrp[{b1}]")
                filters.append(
                    f"[{b0}][{b1}]{fx.blend_filter(ov['blend'])}:"
                    f"enable='between(t,{start:.3f},{start + dur:.3f})'[{bout}]")
                filters.append(f"[{bout}]format=yuv420p[{blended}]")
                cur_v = blended
            else:
                over = self._lbl("ovr")
                filters.append(
                    f"[{cur_v}][{ov_lbl}]overlay=x={x}:y={y}:"
                    f"enable='between(t,{start:.3f},{start + dur:.3f})'[{over}]")
                cur_v = over

        # ---- text overlays
        for ti, t in enumerate(proj.get("texts") or []
                               if not audio_only else []):
            # (round 5) template defaults: a text with "template"
            # takes the preset's fields unless it sets them itself.
            t = tl_mod.resolve_text_template(
                t, proj.get("text_templates"))
            font = default_font_path(t.get("font", "Anton"))
            if t.get("size") is not None:
                size = int(t["size"])
            elif t.get("size_frac") is not None:
                size = int(self.H * float(t["size_frac"]))
            else:
                size = 64
            start = float(t.get("start", 0))
            dur = float(t["duration"])
            end = start + dur
            text = _escape_drawtext(str(t["text"]))
            color = (t.get("color", "#FFFFFF")).lstrip("#")
            hl = (t.get("highlight", "#FFD60A")).lstrip("#")
            x = self._pos_expr(t.get("x", "center"), "x", text_w=True)
            y = self._pos_expr(t.get("y", "lower"), "y", text_h=True)
            if t.get("lock_to"):
                # Object lock: follow the anchor clip's keyframed
                # position (drawtext names the frame w/h).
                x = lock_coord(t["lock_to"], 0, "w")
                y = lock_coord(t["lock_to"], 1, "h")
            anim = t.get("animation", "fade")
            alpha = "1"
            if anim == "fade":
                alpha = (f"if(lt(t,{start:.3f}),0,if(lt(t,{start + 0.3:.3f}),"
                         f"(t-{start:.3f})/0.3,if(lt(t,{end - 0.3:.3f}),1,"
                         f"({end:.3f}-t)/0.3)))")
            elif anim in ("slide-up", "slide-down",
                         "slide-left", "slide-right"):
                # (round 8) slide animations move the text INTO
                # place over the first 0.3 s, relative to wherever
                # the text sits (any x/y, including object-lock):
                # slide-up rises from 40 px below, slide-down
                # settles from 40 px above, slide-left/right from
                # 60 px to the side. Before this fix "slide-down"
                # was validated and documented but had no branch
                # (it rendered static), and "slide-up" only moved
                # for y="lower" — and in the wrong direction (it
                # started 40 px ABOVE and sank instead of rising).
                off = f"40*max(0,1-(t-{start:.3f})/0.3)"
                side = f"60*max(0,1-(t-{start:.3f})/0.3)"
                if anim == "slide-up":
                    y = f"'({y})+{off}'"
                elif anim == "slide-down":
                    y = f"'({y})-{off}'"
                elif anim == "slide-left":
                    x = f"'({x})+{side}'"
                else:  # slide-right
                    x = f"'({x})-{side}'"
            elif anim == "pulse":
                # (round 8) loop animation: gentle alpha breathing
                # (0.5..1.0 at 1.5 Hz) for the whole duration —
                # the CapCut "loop" family, which the text row
                # previously lacked entirely.
                alpha = f"0.75+0.25*sin(2*PI*(t-{start:.3f})*1.5)"
            elif anim == "loop-bounce":
                # (round 9) loop animation: the text bobs up/down
                # +/-8 px at 2 Hz for the whole duration — a second
                # loop-family member next to pulse (alpha breathing),
                # so the loop family is now two animations deep.
                y = (f"'({y})+8*sin(2*PI*(t-{start:.3f})*2)'")
            elif anim == "fade-out":
                # (round 8) out animation: full opacity, then fades
                # out over the last 0.3 s (no fade-in).
                alpha = (f"if(lt(t,{end - 0.3:.3f}),1,"
                         f"({end:.3f}-t)/0.3)")
            elif anim == "pop":
                alpha = (f"if(lt(t,{start:.3f}),0,if(lt(t,{start + 0.15:.3f}),"
                         f"(t-{start:.3f})/0.15,if(lt(t,{end - 0.15:.3f}),1,"
                         f"({end:.3f}-t)/0.15)))")
            # (round 3) text shape: one drawtext per character along
            # an arc / circle (upright glyphs; underline/bold are
            # ignored for shape text — see timeline.py).
            if t.get("shape"):
                sh = t["shape"]
                stype = str(sh.get("type", "arc"))
                radius = float(sh.get("radius")
                               or min(self.W, self.H) * 0.35)
                span = (float(sh.get("angle", 180))
                        if stype == "arc" else 360.0)
                centre = sh.get("center", [0.5, 0.5])
                cx = float(centre[0]) * self.W
                cy = float(centre[1]) * self.H
                chars = list(str(t["text"]))
                n = len(chars)
                t_sh = dict(t)
                t_sh.pop("glow", None)
                t_sh.pop("bubble", None)
                t_sh.pop("3d", None)
                t_sh.pop("shape", None)
                for ci, ch in enumerate(chars):
                    if ch == " ":
                        continue
                    a = math.radians(-90 - span / 2
                                     + span * (ci + 0.5) / n)
                    px = cx + radius * math.cos(a) - size / 2
                    py = cy + radius * math.sin(a) - size / 2
                    dtc = self._drawtext_filter(
                        t_sh, font, _escape_drawtext(ch), size, color,
                        f"{px:.0f}", f"{py:.0f}", alpha, start, end)
                    nxtc = self._lbl("tx")
                    filters.append(f"[{cur_v}]{dtc}[{nxtc}]")
                    cur_v = nxtc
                continue
            dt = self._drawtext_filter(t, font, text, size, color,
                                       x, y, alpha, start, end)
            # (round 3) 3D text: extrude copies first (each 1px
            # down-right in the depth colour), main text on top.
            d3 = t.get("3d")
            if d3:
                depth = 6
                dcol = "222222"
                if isinstance(d3, dict):
                    depth = int(d3.get("depth", 6))
                    dcol = (d3.get("depth_color", "#222222")).lstrip("#")
                t_ext = dict(t)
                t_ext.pop("glow", None)
                t_ext.pop("bubble", None)
                t_ext.pop("3d", None)
                t_ext.pop("shape", None)
                for i in range(depth, 0, -1):
                    dti = self._drawtext_filter(
                        t_ext, font, text, size, dcol,
                        _offset_expr(x, i), _offset_expr(y, i),
                        alpha, start, end)
                    nxti = self._lbl("tx")
                    filters.append(f"[{cur_v}]{dti}[{nxti}]")
                    cur_v = nxti
            nxt = self._lbl("tx")
            filters.append(f"[{cur_v}]{dt}[{nxt}]")
            cur_v = nxt
            bw = t.get("bold_width")
            if bw:
                # Fake bold: draw the same text again, shifted right.
                dt2 = self._drawtext_filter(
                    t, font, text, size, color,
                    f"{x}+{float(bw):.0f}", y, alpha, start, end)
                nxt2 = self._lbl("tx")
                filters.append(f"[{cur_v}]{dt2}[{nxt2}]")
                cur_v = nxt2
            if t.get("underline"):
                ux, uy, uw, uh = self._underline_box(t, size)
                ucol = (t.get("underline_color",
                              t.get("color", "#FFFFFF"))).lstrip("#")
                nxt3 = self._lbl("tx")
                filters.append(
                    f"[{cur_v}]drawbox=x={ux}:y={uy}:w={uw}:h={uh}:"
                    f"color=0x{ucol}:t=fill:"
                    f"enable='between(t,{start:.3f},{end:.3f})'[{nxt3}]")
                cur_v = nxt3

        # ---- captions (ASS burn-in)
        caps = proj.get("captions")
        if not audio_only and caps and (caps.get("words") or []):
            ass_path = os.path.join(self.workdir, "captions.ass")
            ass_kwargs: Dict[str, Any] = {
                "width": self.W, "height": self.H,
                "font_size": caps.get("font_size"),
            }
            # "font" is passed only when the project sets it — otherwise
            # words_to_ass resolves it from captions.style (round 6);
            # a hardcoded "Anton" here would override the style.
            for key in ("font", "ktv_color", "ktv_outline_color",
                        "ktv_shadow_color",
                        "underline", "bold", "italic", "bloom_color",
                        "bloom_strength", "bubble", "bubble_color",
                        "position", "word_colors", "animation",
                        "word_bloom", "active_size_ratio", "style"):
                if caps.get(key) is not None:
                    ass_kwargs[key] = caps[key]
            cap_mod.write_ass(caps["words"], ass_path, **ass_kwargs)
            nxt = self._lbl("cap")
            filters.append(f"[{cur_v}]ass='{_escape_filter_path(ass_path)}'[{nxt}]")
            cur_v = nxt

        # ---- letterbox (v3, project-level): black bars top/bottom of
        # the main video only. The end card is concatenated AFTER this
        # stage, so it is NOT letterboxed.
        lb = proj.get("letterbox")
        if not audio_only and lb:
            if isinstance(lb, bool):
                top_f, bot_f = 0.125, 0.125
            else:
                top_f = float(lb.get("top", 0.125))
                bot_f = float(lb.get("bottom", 0.125))
            top_px = int(round(top_f * self.H))
            bot_px = int(round(bot_f * self.H))
            if top_px > 0:
                nxt = self._lbl("lb")
                filters.append(
                    f"[{cur_v}]drawbox=x=0:y=0:w={self.W}:h={top_px}:"
                    f"color=black:t=fill[{nxt}]")
                cur_v = nxt
            if bot_px > 0:
                nxt = self._lbl("lb")
                filters.append(
                    f"[{cur_v}]drawbox=x=0:y={self.H - bot_px}:w={self.W}:"
                    f"h={bot_px}:color=black:t=fill[{nxt}]")
                cur_v = nxt

        # ---- audio tracks (voice / music / sfx)
        voice_lbl: Optional[str] = None
        music_lbls: List[str] = []
        sfx_lbls: List[str] = []
        for ai, tr in enumerate(proj.get("audio") or []):
            idx = add_input([], tr["src"])
            info = self._probe(tr["src"])
            start = float(tr.get("start", 0))
            offset = float(tr.get("offset", 0))
            dur = tr.get("duration")
            if dur is None:
                dur = max(info["duration"] - offset, 0.1)
            dur = float(dur)
            chain: List[str] = [
                f"atrim=start={offset:.3f}:end={offset + dur:.3f}",
                "asetpts=PTS-STARTPTS",
            ]
            if tr.get("loop"):
                chain.append(f"aloop=loop=-1:size={int(dur * 48000)}")
                chain.append(f"atrim=duration={dur:.3f}")
                chain.append("asetpts=PTS-STARTPTS")
            if tr.get("denoise") and not tr.get("vocal_beautify"):
                # vocal_beautify already includes its own afftdn denoise;
                # stacking a second afftdn over-denoises (musical noise).
                chain.append(audio_mod.denoise_filter())
            if tr.get("vocal_beautify"):
                vb = tr["vocal_beautify"]
                if isinstance(vb, dict):
                    chain.append(audio_mod.vocal_beautify_filter(
                        float(vb.get("strength", 1.0)),
                        bool(vb.get("remove_breaths", False))))
                else:
                    chain.append(audio_mod.vocal_beautify_filter())
            if tr.get("voice_change"):
                chain.append(audio_mod.voice_change_filter(tr["voice_change"]))
            vol = float(tr.get("volume", 1.0))
            if abs(vol - 1.0) > 1e-6:
                chain.append(f"volume={vol:.3f}")
            if tr.get("eq"):
                chain.append(audio_mod.eq_filter(tr["eq"]))
            if tr.get("compressor"):
                chain.append(audio_mod.compressor_filter(tr["compressor"]))
            if tr.get("volume_keyframes"):
                # Multiplier on top of the static volume above.
                chain.append(audio_mod.volume_keyframes_filter(
                    tr["volume_keyframes"]))
            if tr.get("balance"):
                bal = tr["balance"] if isinstance(tr["balance"], dict) else {}
                chain.append(audio_mod.balance_filter(
                    float(bal.get("target_i", -16.0)),
                    float(bal.get("target_tp", -1.5)),
                    float(bal.get("target_lra", 11.0))))
            if tr.get("fade_in"):
                chain.append(f"afade=t=in:st=0:d={float(tr['fade_in']):.2f}")
            if tr.get("fade_out"):
                chain.append(f"afade=t=out:st={max(dur - float(tr['fade_out']), 0):.2f}:"
                             f"d={float(tr['fade_out']):.2f}")
            chain.append(f"adelay={int(start * 1000)}:all=1")
            chain.append("aresample=48000")
            chain.append("aformat=channel_layouts=stereo")
            if tr.get("pan") is not None:
                # After aformat: stereotools needs a stereo stream.
                chain.append(audio_mod.pan_filter(float(tr["pan"])))
            lbl = self._lbl("tr")
            src_stream = f"[{idx}:a]" if info["has_audio"] else None
            if src_stream is None:
                raise RenderError(f"audio track has no audio stream: {tr['src']!r}")
            filters.append(f"{src_stream}{','.join(chain)}[{lbl}]")
            role = tr.get("role", "music")
            if role == "voice":
                voice_lbl = lbl
            elif role == "music":
                music_lbls.append((lbl, tr))
            else:
                sfx_lbls.append(lbl)

        # duck music under voice
        mixed_music: List[str] = []
        for lbl, tr in music_lbls:
            duck = tr.get("ducking")
            if duck and voice_lbl:
                d = self._lbl("dk")
                filters.append(
                    f"[{lbl}][{voice_lbl}]"
                    f"{audio_mod.ducking_filter(float(duck.get('threshold', 0.02)), float(duck.get('ratio', 8.0)))}[{d}]")
                mixed_music.append(d)
            else:
                mixed_music.append(lbl)

        # whoosh sfx at whoosh transitions (synthesised sweep)
        for wt in whoosh_times:
            w = self._lbl("wh")
            filters.append(
                f"aevalsrc='0.35*sin(2*PI*(180+2200*t/0.5)*t)':d=0.5:"
                f"sample_rate=48000,afade=t=out:st=0.3:d=0.2,"
                f"adelay={int(wt * 1000)}:all=1[{w}]")
            sfx_lbls.append(w)

        # final audio mix
        audio_legs = [cur_a]
        if voice_lbl:
            audio_legs.append(voice_lbl)
        audio_legs += mixed_music + sfx_lbls
        if len(audio_legs) == 1:
            final_a = audio_legs[0]
        else:
            am = self._lbl("am")
            ins = "".join(f"[{l}]" for l in audio_legs)
            filters.append(f"{ins}amix=inputs={len(audio_legs)}:normalize=0[{am}]")
            final_a = am
        lim = self._lbl("lm")
        filters.append(
            f"[{final_a}]afade=t=out:st={max(main_dur - 1.0, 0):.3f}:d=1.0,"
            f"alimiter=limit=0.95:level=false,aresample=48000[{lim}]")
        final_a = lim

        # ---- end card
        total_dur = main_dur
        final_v = cur_v
        ec = proj.get("endcard")
        if ec:
            ec_dur = float(ec.get("duration", 5))
            if not audio_only:
                bg = (ec.get("background", "#0B0B0B")).lstrip("#")
                idx = add_input(
                    ["-f", "lavfi"], f"color=c=0x{bg}:s={self.W}x{self.H}:"
                       f"r={self.fps}:d={ec_dur:.3f}")
                ec_lbl = f"[{idx}:v]"
                title = ec.get("title", "")
                if title:
                    font = default_font_path(ec.get("font", "Anton"))
                    dt = (f"drawtext=fontfile='{_escape_filter_path(font)}':"
                          f"text='{_escape_drawtext(str(title))}':fontsize={int(self.H * 0.09)}:"
                          f"fontcolor=0xFFFFFF:borderw=2:bordercolor=0x000000:"
                          f"expansion=none:x=(w-text_w)/2:y=(h-text_h)/2-40")
                    nxt = self._lbl("ect")
                    filters.append(f"{ec_lbl}{dt}[{nxt}]")
                    ec_lbl = f"[{nxt}]"
                subtitle = ec.get("subtitle", "")
                if subtitle:
                    font = default_font_path("Inter")
                    dt = (f"drawtext=fontfile='{_escape_filter_path(font)}':"
                          f"text='{_escape_drawtext(str(subtitle))}':fontsize={int(self.H * 0.045)}:"
                          f"fontcolor=0xFFD60A:borderw=1:bordercolor=0x000000:"
                          f"expansion=none:x=(w-text_w)/2:y=(h-text_h)/2+60")
                    nxt = self._lbl("ecs")
                    filters.append(f"{ec_lbl}{dt}[{nxt}]")
                    ec_lbl = f"[{nxt}]"
                if ec.get("thumbnail"):
                    tidx = add_input([], ec["thumbnail"])
                    th = self._lbl("th")
                    filters.append(
                        f"[{tidx}:v]scale={int(self.W * 0.28)}:-2,"
                        f"format=yuva420p[{th}]")
                    ov = self._lbl("tho")
                    filters.append(
                        f"{ec_lbl}[{th}]overlay=(W-w)/2:(H-h)/2-160[{ov}]")
                    ec_lbl = f"[{ov}]"
                if self._alpha:
                    # Keep the end card in the alpha pixel format so the
                    # concat with the alpha main track succeeds.
                    ef = self._lbl("ecf")
                    filters.append(f"{ec_lbl}format={self.pix_fmt}[{ef}]")
                    ec_lbl = f"[{ef}]"
            cv = self._lbl("ecv")
            ca = self._lbl("eca")
            sil = self._lbl("esil")
            filters.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000:"
                f"duration={ec_dur:.3f}[{sil}]")
            if not audio_only:
                filters.append(f"[{final_v}]{ec_lbl}concat=n=2:v=1:a=0[{cv}]")
                final_v = f"[{cv}]"
            filters.append(f"[{final_a}][{sil}]concat=n=2:v=0:a=1[{ca}]")
            final_a = f"[{ca}]"
            total_dur = main_dur + ec_dur
        else:
            final_a = f"[{final_a}]"
            final_v = f"[{final_v}]" if not audio_only else None

        # ---- HDR10 (round 3): convert the SDR timeline into a
        # PQ/BT.2020 10-bit container. This re-tags the signal via
        # zscale — it does NOT invent HDR highlights (see
        # timeline.py). The end card is converted too (it is part
        # of final_v already).
        if self._hdr and not audio_only:
            hlb = self._lbl("hdr")
            filters.append(
                f"{final_v}zscale=primariesin=bt709:primaries=bt2020:"
                f"transferin=bt709:transfer=smpte2084:matrixin=bt709:"
                f"matrix=bt2020nc:rangein=limited:range=limited,"
                f"format=yuv420p10le[{hlb}]")
            final_v = f"[{hlb}]"

        # ---- GIF: single-pass palette (no audio stream on a GIF)
        if self.codec_name == "gif" and not audio_only:
            gif_fps = min(self.fps, 15)
            pa, pb, pal, pu = (self._lbl("gp"), self._lbl("gp"),
                              self._lbl("gp"), self._lbl("gp"))
            filters.append(
                f"{final_v}fps=fps={gif_fps},split[{pa}][{pb}];"
                f"[{pa}]palettegen=stats_mode=diff[{pal}];"
                f"[{pb}][{pal}]paletteuse[{pu}]")
            final_v = f"[{pu}]"

        # ---- assemble command
        cmd = [self.ffmpeg, "-hide_banner", "-y"]
        for frag in inputs:
            cmd += frag
        if self._filter_threads:
            cmd += ["-filter_complex_threads",
                    str(self._filter_threads)]
        if audio_only:
            # Audio-only output: map just the mixed audio. The wav /
            # mp3 specs carry the codec as "encoder" (no separate
            # audio_codec) plus "extra" (mp3's -q:a 4).
            spec = self.spec
            cmd += ["-filter_complex", ";".join(filters),
                    "-map", final_a,
                    "-c:a", spec["encoder"]]
            cmd += list(spec.get("extra") or [])
            cmd.append(output)
            info = {"inputs": len(inputs), "filters": len(filters),
                    "whoosh_transitions": len(whoosh_times),
                    "codec": self.codec_name, "pix_fmt": self.pix_fmt,
                    "bitrate_ladder": False, "audio_only": True}
            return cmd, total_dur, info
        cmd += ["-filter_complex", ";".join(filters),
                "-map", final_v]
        if self.has_audio_out:
            cmd += ["-map", final_a]
        spec = self.spec
        cmd += ["-c:v", spec["encoder"]]
        if "profile" in spec:
            cmd += ["-profile:v", str(spec["profile"])]
        ladder = None
        if self.has_video_out and self.codec_name in ("h264", "h265", "av1"):
            ladder = self._ladder_entry()
        if ladder is not None:
            br = ladder["bitrate"]
            cmd += ["-b:v", str(br), "-maxrate", str(int(br * 1.5)),
                    "-bufsize", str(br * 2), "-g", str(ladder["gop"])]
            if self.codec_name == "h264":
                cmd += ["-profile:v", ladder["profile"]]
            elif self.codec_name == "h265":
                # HDR10 needs the 10-bit profile, whatever the ladder says.
                prof = ("main10" if self._hdr else
                        {"high": "main", "main10": "main10"}.get(
                            ladder["profile"], "main"))
                cmd += ["-profile:v", prof]
            elif self.codec_name == "av1":
                cmd += ["-preset", "8"]
        elif self.codec_name in ("h264", "h265"):
            cmd += ["-preset", self.preset, "-crf", str(self.crf)]
        elif self.codec_name == "av1":
            cmd += list(spec.get("extra") or [])
        elif self.codec_name == "dnxhd":
            # DNxHD/DNxHR need one of the encoder's valid bitrates;
            # the ladder is ignored (its values would be rejected).
            br, profile = _DNXHD_SETTINGS[(self.W, self.H)]
            cmd += ["-b:v", br, "-profile:v", profile]
        if self.has_video_out and self.codec_name != "gif":
            cmd += ["-pix_fmt", self.pix_fmt, "-r", str(self.fps)]
        if self._hdr:
            # HDR10 colour metadata (container-level signalling) +
            # libx265 mastering-display / MaxCLL (P3-D65, 1000 nits
            # peak / 400 nits MaxFALL — the standard defaults).
            cmd += ["-color_primaries", "bt2020",
                    "-color_trc", "smpte2084",
                    "-colorspace", "bt2020nc"]
            if self.codec_name == "h265":
                cmd += ["-x265-params",
                        "hdr-opt=1:master-display="
                        "G(13250,3450)B(7500,3000)R(34000,16000)"
                        "WP(15635,16450)L(10000000,50):max-cll=1000,400"]
        if self.has_audio_out:
            cmd += ["-c:a", spec["audio_codec"], "-b:a", "192k"]
        if spec["container"] in ("mp4", "mov"):
            cmd += ["-movflags", "+faststart"]
        cmd.append(output)
        if self.has_video_out and not self.has_audio_out:
            # GIF: the audio branch is still built (the graph is
            # shared), so consume it on a null output — ffmpeg
            # errors on unmapped filter outputs.
            cmd += ["-map", final_a, "-f", "null", "-"]
        info = {"inputs": len(inputs), "filters": len(filters),
                "whoosh_transitions": len(whoosh_times),
                "codec": self.codec_name, "pix_fmt": self.pix_fmt,
                "bitrate_ladder": bool(ladder)}
        return cmd, total_dur, info

    def _drawtext_filter(self, t: Dict[str, Any], font: str, text: str,
                         size: int, color: str, x: str, y: str,
                         alpha: str, start: float, end: float) -> str:
        """One drawtext filter string for a text overlay.

        ``glow`` (colour + strength) widens the border and paints a
        zero-offset shadow in the glow colour — a halo approximation.
        ``bubble`` draws a translucent box behind the text.
        """
        borderw = 2
        bordercolor = "0x000000"
        shadow = ""
        glow = t.get("glow") or {}
        if glow:
            gcol = (glow.get("color", "#FFD60A")).lstrip("#")
            gstr = float(glow.get("strength", 0.5) or 0.5)
            borderw = 2 + int(6 * gstr)
            bordercolor = f"0x{gcol}"
            shadow = f":shadowcolor=0x{gcol}:shadowx=0:shadowy=0"
        box = ""
        bubble = t.get("bubble") or {}
        if bubble:
            bcol = (bubble.get("color", "#000000")).lstrip("#")
            balpha = float(bubble.get("alpha", 0.6) or 0.6)
            bpad = int(bubble.get("padding", 12) or 12)
            box = (f":box=1:boxcolor=0x{bcol}@{balpha:.2f}:"
                   f"boxborderw={bpad}")
        return (
            f"drawtext=fontfile='{_escape_filter_path(font)}':"
            f"text='{text}':fontsize={size}:"
            f"fontcolor=0x{color}:borderw={borderw}:"
            f"bordercolor={bordercolor}{shadow}{box}:"
            f"expansion=none:x={x}:y={y}:alpha='{alpha}':"
            f"enable='between(t,{start:.3f},{end:.3f})'"
        )

    def _underline_box(self, t: Dict[str, Any], size: int
                       ) -> Tuple[str, str, str, str]:
        """Underline geometry for a text overlay.

        drawtext cannot report its own text width to another filter,
        so the width is *estimated* from the character count
        (Anton ≈ 0.5 em per glyph; other fonts ≈ 0.55 em). Exact for
        short centred titles, approximate for long mixed text — the
        ASS caption path has an exact underline instead.
        Returns (x_expr, y_expr, w, h) for a drawbox.
        """
        text = str(t["text"])
        est_w = max(int(len(text) * size * 0.5), size)
        th = size * 1.25  # estimated single-line text height
        xv = t.get("x", "center")
        if xv == "center":
            ux = f"(w-{est_w})/2"
        elif xv in ("left",):
            ux = "40"
        elif xv in ("right",):
            ux = f"w-{est_w}-40"
        elif isinstance(xv, (int, float)):
            ux = f"{xv:.3f}*w" if 0 <= xv <= 1 else str(int(xv))
        else:
            ux = f"(w-{est_w})/2"
        yv = t.get("y", "lower")
        if yv in ("lower", "bottom"):
            uy = f"h-90+4"
        elif yv in ("center",):
            uy = f"(h-{th:.0f})/2+{th:.0f}+4"
        elif yv in ("top",):
            uy = f"{40 + th + 4:.0f}"
        elif isinstance(yv, (int, float)):
            uy = f"{yv:.3f}*h+{th:.0f}+4" if 0 <= yv <= 1 \
                else f"{int(yv) + th + 4:.0f}"
        else:
            uy = "h-90+4"
        return ux, uy, str(est_w), str(max(2, size // 16))

    def _pos_expr(self, val: Any, axis: str, text_w: bool = False,
                  text_h: bool = False) -> str:
        """Position value -> drawtext/overlay expression.

        Accepts "center"/"left"/"right"/"top"/"bottom"/"lower" or a number
        (0..1 = fraction of the frame, >1 = pixels).
        """
        if axis == "x":
            if val == "center":
                return "(w-text_w)/2" if text_w else "(W-w)/2"
            if val in ("left",):
                return "40"
            if val in ("right",):
                return "w-text_w-40" if text_w else "W-w-40"
            if isinstance(val, (int, float)):
                return f"{val:.3f}*w" if 0 <= val <= 1 else str(int(val))
            return "(w-text_w)/2" if text_w else "(W-w)/2"
        # y axis
        if val in ("lower", "bottom"):
            return "h-text_h-90" if text_h else "H-h-90"
        if val in ("center",):
            return "(h-text_h)/2" if text_h else "(H-h)/2"
        if val in ("top",):
            return "40"
        if isinstance(val, (int, float)):
            return f"{val:.3f}*h" if 0 <= val <= 1 else str(int(val))
        return "h-text_h-90" if text_h else "H-h-90"

    # -- run ----------------------------------------------------------
    def _extract_audio(self, src: str, dst: str) -> None:
        """Extract the audio stream of ``src`` to ``dst`` (wav/mp3)."""
        ext = os.path.splitext(dst)[1].lower()
        if ext == ".wav":
            codec = ["-c:a", "pcm_s16le"]
        elif ext == ".mp3":
            codec = ["-c:a", "libmp3lame", "-q:a", "4"]
        else:
            codec = ["-c:a", "aac", "-b:a", "192k"]
        cmd = [self.ffmpeg, "-hide_banner", "-y", "-v", "error",
               "-i", src, "-vn"] + codec + [dst]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=audio_mod.ffmpeg_env())
        if proc.returncode != 0:
            raise RenderError(
                f"audio extraction failed: {(proc.stderr or '')[-1000:]}")
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            raise RenderError("audio extraction produced no file")

    def render(self, output: str, overwrite: bool = True,
               quiet: bool = False) -> RenderResult:
        """Render the project to ``output``.

        Video codecs (h264/h265/ProRes/AV1/GIF) render in one pass.
        Audio-only (wav/mp3) builds the audio graph directly
        (``build_command(audio_only=True)``) — before round 8 it
        rendered a full video intermediate first and extracted the
        audio (~104 s wall for an 8 s wav; the direct graph takes
        seconds and mixes identically).
        """
        t0 = time.time()
        self._resolve_codec(output)
        if not self.has_video_out:
            cmd, total_dur, info = self.build_command(output,
                                                      audio_only=True)
            env = audio_mod.ffmpeg_env()
            env["TMPDIR"] = self.workdir
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  env=env)
            if proc.returncode != 0:
                tail = (proc.stderr or "")[-3000:]
                raise RenderError(
                    f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
            if not os.path.isfile(output) or os.path.getsize(output) == 0:
                raise RenderError(
                    "ffmpeg exited 0 but no output file was produced")
            if not quiet:
                print(f"rendered {output} ({total_dur:.1f}s audio, "
                      f"{time.time() - t0:.1f}s wall)")
            return RenderResult(output, total_dur, self.W, self.H,
                                self.fps, time.time() - t0, cmd)
        cmd, total_dur, info = self.build_command(output)
        env = audio_mod.ffmpeg_env()
        env["TMPDIR"] = self.workdir
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            tail = (proc.stderr or "")[-3000:]
            raise RenderError(
                f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
        if not os.path.isfile(output) or os.path.getsize(output) == 0:
            raise RenderError("ffmpeg exited 0 but no output file was produced")
        if not quiet:
            print(f"rendered {output} ({total_dur:.1f}s, "
                  f"{info['inputs']} inputs, {info['filters']} filters, "
                  f"{time.time() - t0:.1f}s wall)")
        return RenderResult(output, total_dur, self.W, self.H, self.fps,
                            time.time() - t0, cmd)
