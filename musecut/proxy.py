"""MuseCut proxy workflow (pro).

Editing 4K originals on a weak machine is slow, so pro editors cut
with low-resolution proxies and relink the originals for export.
This module is that workflow, agent-native:

* :func:`make_proxy` — render one low-res proxy (H.264, small).
* :func:`make_proxies_for_project` — proxy every media file a project
  references and return a copy of the project with its sources
  swapped to the proxies, plus a ``proxy_map`` (proxy -> original)
  stored on the project itself.
* :func:`relink_project` — swap a proxy project back to its
  originals using that stored ``proxy_map``.

Proxies are plain files next to the project (or in a chosen dir);
nothing here touches the originals.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Tuple

from . import audio as audio_mod
from .render import RenderError, probe_media
from .timeline import validate_timeline

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def proxy_path(src: str, proxy_dir: str) -> str:
    """Proxy filename for ``src`` inside ``proxy_dir``."""
    stem = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(proxy_dir, f"{stem}_proxy.mp4")


def make_proxy(src: str, dst: str, width: int = 640, height: int = 360,
               crf: int = 28, preset: str = "veryfast") -> str:
    """Render a low-res proxy of ``src`` to ``dst``.

    Video inputs get a small H.264 picture proxy (audio dropped —
    the proxy is a picture stand-in; relink restores the original's
    audio at export). Audio-only inputs get a light MP3 proxy.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    info = probe_media(src)
    if info["has_video"]:
        cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
               "-i", src,
               "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                      f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
               "-an", "-c:v", "libx264", "-preset", preset,
               "-crf", str(crf), "-pix_fmt", "yuv420p", dst]
    elif info["has_audio"]:
        cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
               "-i", src, "-vn", "-c:a", "libmp3lame", "-q:a", "4", dst]
    else:
        raise RenderError(
            f"cannot proxy {src!r}: no audio or video stream")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=audio_mod.ffmpeg_env())
    if proc.returncode != 0:
        raise RenderError(
            f"proxy render failed for {src!r}: {(proc.stderr or '')[-500:]}")
    return dst


def _collect_media(doc: Dict[str, Any]) -> List[str]:
    """Unique media paths referenced by a project (video + audio)."""
    seen: List[str] = []
    for c in doc.get("clips") or []:
        src = c.get("src", "")
        if src and not src.startswith(("color:", "testsrc")):
            seen.append(src)
        nested = c.get("nested")
        if isinstance(nested, str) and nested.endswith(".json"):
            seen.append(nested)
    for o in doc.get("overlays") or []:
        if o.get("src"):
            seen.append(o["src"])
    for a in doc.get("audio") or []:
        if a.get("src"):
            seen.append(a["src"])
    out: List[str] = []
    for s in seen:
        if s not in out:
            out.append(s)
    return out


def make_proxies_for_project(doc: Dict[str, Any], proxy_dir: str,
                             width: int = 640, height: int = 360,
                             crf: int = 28) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Proxy every media file in ``doc``; return (proxy project, map).

    The returned project is a validated copy with every media ``src``
    replaced by its proxy path and a top-level ``proxy_map``
    (``{proxy_path: original_path}``) so :func:`relink_project` can
    restore it. Nested .json projects are NOT proxied (they are
    rendered, not edited, at export) — their paths are left alone.
    Audio files are proxied too (a light audio proxy keeps the edit
    responsive); the map restores the originals either way.
    """
    doc = validate_timeline(json.loads(json.dumps(doc)))
    os.makedirs(proxy_dir, exist_ok=True)
    mapping: Dict[str, str] = {}
    for src in _collect_media(doc):
        if src.endswith(".json"):
            continue
        if not os.path.isfile(src):
            raise RenderError(f"cannot proxy missing file: {src!r}")
        dst = proxy_path(src, proxy_dir)
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            make_proxy(src, dst, width=width, height=height, crf=crf)
        mapping[dst] = src

    def swap(obj: Any) -> Any:
        # mapping is {proxy: original}; swapping needs original->proxy.
        to_proxy = {orig: px for px, orig in mapping.items()}
        if isinstance(obj, dict):
            return {k: (to_proxy.get(v, v) if k == "src" and isinstance(v, str)
                        else swap(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [swap(v) for v in obj]
        return obj

    proxied = swap(doc)
    proxied["proxy_map"] = mapping
    return validate_timeline(proxied), mapping


def relink_project(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Restore a proxy project's original media paths via ``proxy_map``.

    Raises RenderError if the project has no ``proxy_map`` (it is not
    a proxy project). The returned project has no ``proxy_map`` left.
    """
    mapping = doc.get("proxy_map")
    if not mapping:
        raise RenderError("project has no proxy_map — nothing to relink")
    reverse = {p: o for p, o in mapping.items()}

    def swap(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: (reverse.get(v, v) if k == "src" and isinstance(v, str)
                        else swap(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [swap(v) for v in obj]
        return obj

    out = swap(doc)
    out.pop("proxy_map", None)
    return validate_timeline(out)


def proxy_info(path: str) -> Dict[str, Any]:
    """ffprobe a proxy file: size, resolution, codec, duration."""
    info = probe_media(path)
    info["size_bytes"] = os.path.getsize(path)
    return info
