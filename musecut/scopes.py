"""MuseCut colour scopes — data output (pro).

Pro editors judge colour with scopes (waveform, vectorscope,
histogram), not their eyes. MuseCut has no GUI, so the scopes here
are *data*: per-frame luma/chroma statistics from ffmpeg's
``signalstats`` filter, plus a histogram summary, returned as JSON
the agent can reason about (e.g. "is this shot clipped?", "is the
black level crushed?").

* :func:`scopes_data` — stats for one frame at time ``t``.
* :func:`scopes_series` — the same stats sampled across the clip.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Dict, List, Optional

from . import audio as audio_mod
from .render import RenderError

# signalstats prints one metadata block per frame; keys we report.
_SCOPE_KEYS = ("YAVG", "YMIN", "YMAX", "UAVG", "UMIN", "UMAX",
               "VAVG", "VMIN", "VMAX", "SATAVG", "SATMIN", "SATMAX",
               "HUEAVG", "HUEMED", "YHIGH", "YLOW", "YBITDEPTH")


def _run_signalstats(src: str, start: float, frames: int,
                     fps: int = 1) -> List[Dict[str, float]]:
    """Run signalstats over ``frames`` frames starting at ``start``.

    Returns one dict of float stats per frame.
    """
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "info",
           "-ss", str(start), "-i", src,
           "-vf", f"signalstats,metadata=print,fps=fps={fps}",
           "-frames:v", str(frames), "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=audio_mod.ffmpeg_env())
    if proc.returncode != 0:
        raise RenderError(
            f"scopes failed on {src!r}: {(proc.stderr or '')[-500:]}")
    out: List[Dict[str, float]] = []
    cur: Dict[str, float] = {}
    for line in (proc.stderr or "").splitlines():
        m = re.search(r"lavfi\.signalstats\.(\w+)=([-\d.]+)", line)
        if not m:
            continue
        key, val = m.group(1), float(m.group(2))
        if key not in _SCOPE_KEYS:
            continue
        if key == "YAVG" and "YAVG" in cur:
            # A second YAVG means a new frame's block has started.
            out.append(cur)
            cur = {}
        cur[key] = val
    if cur:
        out.append(cur)
    return out


def scopes_data(src: str, t: float = 0.0) -> Dict[str, Any]:
    """Colour-scope stats for the frame at time ``t`` (seconds).

    Returns Y/U/V averages + ranges (0..255), saturation average,
    and a plain-language read: ``clipped_highlights`` (YMAX hits 255),
    ``crushed_blacks`` (YMIN hits 0), ``mean_luma``.
    """
    frames = _run_signalstats(src, t, frames=1)
    if not frames:
        raise RenderError(f"no frame at t={t} in {src!r}")
    s = frames[0]
    return {
        "time": t,
        "source": src,
        "y_avg": s.get("YAVG"), "y_min": s.get("YMIN"),
        "y_max": s.get("YMAX"),
        "u_avg": s.get("UAVG"), "v_avg": s.get("VAVG"),
        "sat_avg": s.get("SATAVG"),
        "clipped_highlights": bool(s.get("YMAX", 0) >= 255),
        "crushed_blacks": bool(s.get("YMIN", 255) <= 0),
        "mean_luma": s.get("YAVG"),
    }


def scopes_series(src: str, duration: float, samples: int = 5
                  ) -> List[Dict[str, Any]]:
    """Sample scope stats at ``samples`` evenly spaced times."""
    if samples < 1:
        raise ValueError("samples must be >= 1")
    times = [duration * i / max(samples - 1, 1) * 0.9
             for i in range(samples)]
    return [scopes_data(src, round(t, 3)) for t in times]
