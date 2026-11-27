"""MuseCut keyframe expressions.

Pro editors keyframe any numeric property over time. ffmpeg evaluates
per-frame expressions with the variable ``t`` (seconds into the
filtered stream), so a list of ``{"time", "value"}`` keyframes becomes
one nested ``if(lt(t, ...), ...)`` expression with interpolation
between keyframes. Each keyframe may carry ``"easing"`` — "linear"
(default), "ease-in", "ease-out" or "ease-in-out" — which shapes the
segment starting at that keyframe (quadratic smoothsteps, evaluated
per frame by ffmpeg).

The expressions this module builds contain commas (inside ``if()`` /
``lt()`` calls). They are always embedded inside single quotes in a
filter argument (e.g. ``rotate=a='<expr>'``), which is what keeps the
filter-graph parser from splitting on those commas. Never splice a
bare expression into a filter string without quoting it.

``colorchannelmixer`` does NOT accept time expressions in this ffmpeg
build (its coefficients parse as plain numbers — verified 2026-11-20),
so opacity keyframes go through ``geq`` alpha instead (see
:func:`musecut.effects.opacity_keyframe_filter`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


EASINGS = {"linear", "ease-in", "ease-out", "ease-in-out"}


def _eased(u: str, easing: str) -> str:
    """Easing of the normalised segment position ``u`` (0..1).

    ``u`` is an ffmpeg expression string. The formulas are the
    standard quadratic smoothsteps, all evaluable per frame:

    * linear — ``u``
    * ease-in — ``pow(u,2)`` (slow start, fast finish)
    * ease-out — ``1-pow(1-u,2)`` (fast start, slow finish)
    * ease-in-out — quadratic smoothstep, slow at both ends
    """
    if easing == "linear":
        return u
    if easing == "ease-in":
        return f"pow({u},2)"
    if easing == "ease-out":
        # Parenthesised: the result is used as a multiplicand in
        # scalar_expr ("v0+(v1-v0)*<eased>"), and the bare form
        # "1-pow(1-u,2)" parses as "v0+(v1-v0)*1-pow(...)" — the
        # "*1" swallows the (v1-v0) term and the pow subtracts from
        # v0 instead. Found 2026-11-21 by frame-checking a whip-pan
        # demo: an ease-out position keyframe 0.65->0.5 evaluated to
        # -0.5 at t=0 (frame off-screen, 97.8% black) instead of
        # 0.65. Round 2's render test missed it: it only asserted
        # ease-out > linear, and the buggy value is even larger.
        return f"(1-pow(1-{u},2))"
    if easing == "ease-in-out":
        return (f"if(lt({u},0.5),2*pow({u},2),"
                f"1-pow(-2*{u}+2,2)/2)")
    raise ValueError(f"unknown easing: {easing!r} "
                     f"(want one of {sorted(EASINGS)})")


def _apply_easing(u: float, easing: str) -> float:
    """Python-side easing — mirrors the ffmpeg formulas in :func:`_eased`.

    Used by :func:`evaluate` / :func:`shift_to_window` so Python can
    evaluate a keyframed curve at an exact time (e.g. to re-express
    keyframes for one segment of a speed-curve clip).
    """
    if easing == "linear":
        return u
    if easing == "ease-in":
        return u * u
    if easing == "ease-out":
        return 1 - (1 - u) ** 2
    if easing == "ease-in-out":
        return 2 * u * u if u < 0.5 else 1 - (-2 * u + 2) ** 2 / 2
    raise ValueError(f"unknown easing: {easing!r} "
                     f"(want one of {sorted(EASINGS)})")


def _pts(kfs: Sequence[Dict[str, Any]]) -> List[Tuple[float, Any, str]]:
    return sorted(
        (float(k["time"]), k["value"], str(k.get("easing", "linear")))
        for k in kfs)


def evaluate(kfs: Sequence[Dict[str, Any]], t: float,
             pair: bool = False) -> Any:
    """Value of a keyframed curve at time ``t`` (scalar, or [x, y]
    when ``pair``).

    Mirrors :func:`scalar_expr` / :func:`pair_expr` semantics exactly:
    hold the first value before the first keyframe, hold the last
    value after the last, easing shapes the segment starting at each
    keyframe. A single keyframe is constant.
    """
    pts = _pts(kfs)
    if not pts:
        raise ValueError("evaluate needs a non-empty keyframe list")
    if len(pts) == 1:
        return list(pts[0][1]) if pair else pts[0][1]
    if t <= pts[0][0]:
        return list(pts[0][1]) if pair else pts[0][1]
    if t >= pts[-1][0]:
        return list(pts[-1][1]) if pair else pts[-1][1]
    for i in range(len(pts) - 1):
        t0, v0, easing = pts[i]
        t1, v1 = pts[i + 1][0], pts[i + 1][1]
        if t0 <= t < t1:
            u = _apply_easing((t - t0) / (t1 - t0), easing)
            if pair:
                return [v0[a] + (v1[a] - v0[a]) * u for a in (0, 1)]
            return v0 + (v1 - v0) * u
    return list(pts[-1][1]) if pair else pts[-1][1]


def _easing_at(kfs: Sequence[Dict[str, Any]], t: float) -> str:
    """Easing of the segment containing time ``t`` (the easing of the
    keyframe that starts it). Before the first keyframe / after the
    last the curve holds constant, so "linear" is returned."""
    pts = _pts(kfs)
    if len(pts) <= 1 or t < pts[0][0] or t >= pts[-1][0]:
        return "linear"
    for i in range(len(pts) - 1):
        if pts[i][0] <= t < pts[i + 1][0]:
            return pts[i][2]
    return "linear"


def shift_to_window(kfs: Sequence[Dict[str, Any]], start: float,
                    end: float, pair: bool = False) -> List[Dict[str, Any]]:
    """Re-express keyframes for a sub-window ``[start, end)`` of the
    clip's output timeline, in window-local time (0 at ``start``).

    The returned list starts at time 0 with the interpolated value
    at ``start`` (carrying the easing of the segment ``start`` falls
    in), keeps interior keyframes shifted by ``-start`` with their
    own easing, and ends at time ``end - start`` with the
    interpolated value at ``end``. A single keyframe (constant) is
    returned unchanged.

    This is what a speed-curve segment needs: each segment's leg
    evaluates keyframes against its own local ``t``, so the
    clip-level keyframes must be window-shifted per segment instead
    of copied verbatim (verbatim copies replay the whole curve in
    every segment — found in round 8 by exact-value render).
    """
    pts = _pts(kfs)
    if len(pts) <= 1:
        return [dict(k) for k in kfs]
    dur = end - start
    out: List[Dict[str, Any]] = [{
        "time": 0.0,
        "value": evaluate(kfs, start, pair=pair),
        "easing": _easing_at(kfs, start),
    }]
    for t, v, easing in pts:
        if start < t < end:
            out.append({"time": t - start,
                        "value": list(v) if pair else v,
                        "easing": easing})
    out.append({"time": dur,
                "value": evaluate(kfs, end, pair=pair)})
    # A keyframe exactly at `start` or `end` would duplicate the
    # boundary entry — drop exact duplicates, keeping the boundary.
    dedup: List[Dict[str, Any]] = []
    for k in out:
        if dedup and abs(k["time"] - dedup[-1]["time"]) < 1e-9:
            continue
        dedup.append(k)
    return dedup


def _fmt(v: float) -> str:
    """Short, parseable float literal (avoids scientific notation)."""
    if v == int(v) and abs(v) < 1e9:
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def scalar_expr(kfs: Sequence[Dict[str, Any]], default: float = 0.0,
                var: str = "t") -> str:
    """Linear-interpolated expression for scalar keyframes.

    ``kfs`` is a list of ``{"time": float, "value": float}`` with
    strictly increasing times. Before the first keyframe the value
    holds at the first value; after the last it holds at the last.
    With no keyframes the expression is the constant ``default``.

    Each keyframe may carry ``"easing"`` — "linear" (default),
    "ease-in", "ease-out" or "ease-in-out" — which shapes the segment
    STARTING at that keyframe (the last keyframe's easing is unused).
    The easing is applied to the normalised segment position
    ``u = (t-t0)/(t1-t0)`` before the value is interpolated.

    ``var`` is the time variable name — ``"t"`` for most filters,
    ``"T"`` for ``geq`` (which only knows capital ``T``).
    """
    pts = sorted(
        (float(k["time"]), float(k["value"]),
         str(k.get("easing", "linear")))
        for k in kfs)
    if not pts:
        return _fmt(default)
    if len(pts) == 1:
        return _fmt(pts[0][1])
    # Innermost: hold the last value after the final keyframe.
    expr = _fmt(pts[-1][1])
    for i in range(len(pts) - 2, -1, -1):
        t0, v0, easing = pts[i]
        t1, v1 = pts[i + 1][0], pts[i + 1][1]
        u = f"({var}-{_fmt(t0)})/{_fmt(t1 - t0)}"
        seg = (f"{_fmt(v0)}+({_fmt(v1)}-{_fmt(v0)})*{_eased(u, easing)}")
        expr = f"if(lt({var},{_fmt(t1)}),{seg},{expr})"
    # Before the first keyframe, hold the first value.
    expr = f"if(lt({var},{_fmt(pts[0][0])}),{_fmt(pts[0][1])},{expr})"
    return expr


def pair_expr(kfs: Sequence[Dict[str, Any]], axis: int,
              default: float = 0.5, var: str = "t") -> str:
    """Scalar expression for one axis of [x, y] pair keyframes.

    ``var`` is the time variable — normally ``"t"``; a lock binding
    passes ``"(t-<start>)"`` to read the anchor clip's keyframes in
    main-timeline time.
    """
    pts = [{"time": float(k["time"]),
            "value": float(k["value"][axis]),
            "easing": str(k.get("easing", "linear"))} for k in kfs]
    return scalar_expr(pts, default, var=var)


def ffmpeg_escape(expr: str) -> str:
    """Escape an expression for embedding in a filter argument.

    The filter-graph parser splits option values on bare commas even
    inside single quotes (verified 2026-11-20 — ``geq`` choked on a
    bare ``if(lt(t,2),...)``), so every comma inside an expression
    must be written ``\\,``. The parser unescapes it before the
    expression evaluator sees it.
    """
    return expr.replace(",", "\\,")


def validate_keyframes(kfs: Any, where: str, pair: bool = False) -> None:
    """Validate a keyframe list; raises ValueError on the first problem."""
    if not isinstance(kfs, list) or not kfs:
        raise ValueError(f"{where} must be a non-empty list of keyframes")
    prev_t: Optional[float] = None
    for i, k in enumerate(kfs):
        if not isinstance(k, dict):
            raise ValueError(f"{where}[{i}] must be an object")
        t, v = k.get("time"), k.get("value")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0:
            raise ValueError(f"{where}[{i}].time must be a number >= 0")
        if prev_t is not None and t <= prev_t:
            raise ValueError(f"{where} times must be strictly increasing")
        prev_t = float(t)
        if k.get("easing") is not None:
            if k["easing"] not in EASINGS:
                raise ValueError(
                    f"{where}[{i}].easing unknown: {k['easing']!r} "
                    f"(want one of {sorted(EASINGS)})")
        if pair:
            if (not isinstance(v, (list, tuple)) or len(v) != 2
                    or not all(isinstance(x, (int, float))
                               and not isinstance(x, bool) for x in v)):
                raise ValueError(
                    f"{where}[{i}].value must be [x, y] numbers")
        else:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{where}[{i}].value must be a number")
