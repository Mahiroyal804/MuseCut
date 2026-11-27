"""MuseCut MCP server (stdio, JSON-RPC 2.0, MCP 2024-11-05).

No third-party dependency: the server speaks the Model Context
Protocol over stdin/stdout using only the standard library, so any
MCP-capable agent (Muse included) can drive the editor directly.

Tools exposed
-------------
create_project   start a new timeline (name, width, height, fps)
add_clip         append a video/image clip to the main track
add_overlay      add a PiP / sticker / blend overlay
add_text         add an animated text overlay
add_audio        add a voice / music / sfx track
add_captions     attach word-level captions (or auto-transcribe a file)
add_transition   set the transition into a clip
set_endcard      set the end card
set_export       set the export block (codec, bitrate ladder)
set_canvas       set the canvas background (color / image / blur)
add_marker       add an editorial marker to the timeline
list_voices      list TTS providers and their voices (Muse MAI_03 default)
synthesize_voice render a voiceover segment with a TTS provider
validate         validate the current timeline
render           render the timeline to a video file
get_status       project summary + render state
list_templates   list bundled JSON timeline templates

Wire-up
-------
Add to your MCP client config::

    {
      "mcpServers": {
        "musecut": {
          "command": "python3",
          "args": ["-m", "musecut.mcp_server"],
          "cwd": "/path/to/MuseCut"
        }
      }
    }

Or run the CLI: ``musecut mcp``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

from . import __version__, ai as ai_mod
from . import audio as audio_mod
from . import captions as cap_mod
from . import tts as tts_mod
from .timeline import TimelineError, new_project, validate_timeline
from .render import RenderError, Renderer, probe_media

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "musecut", "version": __version__}

TOOLS = [
    {
        "name": "create_project",
        "description": "Start a new MuseCut video project (timeline).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 720},
                "fps": {"type": "integer", "default": 30},
                "canvas": {"type": "object",
                           "description": "background behind the main "
                           "track: {type: color|image|blur, color, "
                           "image, blur}"},
                "export": {"type": "object",
                           "description": "export block: {codec, "
                           "bitrate_ladder}"},
            },
        },
    },
    {
        "name": "add_clip",
        "description": "Append a video/image clip to the main track. "
                       "Needs 'src' or 'nested'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "nested": {"description": "a .json project path, "
                           "'sequence:<name>', or an inline project "
                           "to use as a nested sequence / compound clip"},
                "start": {"type": "number", "default": 0},
                "duration": {"type": "number"},
                "speed": {"type": "number", "default": 1},
                "speed_curve": {"type": "array"},
                "reverse": {"type": "boolean", "default": False},
                "freeze_at": {"type": "number"},
                "transition_in": {"type": "object"},
                "keyframes": {"type": "object",
                             "description": "transform keyframes: "
                             "position/scale/rotation/opacity lists of "
                             "{time, value}, anchor [x, y]; legacy "
                             "zoom/pan from/to also accepted"},
                "effects": {"type": "object",
                            "description": "colour preset + sliders, "
                            "incl. 'lut' (.cube path)"},
                "chroma_key": {"type": "object"},
                "stabilize": {"type": "boolean", "default": False},
                "mask": {"type": "object"},
                "motion_blur": {"description": "true or {frames: 2..16}"},
                "volume": {"type": "number", "default": 1},
                "muted": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "add_overlay",
        "description": "Add a picture-in-picture, sticker or blend overlay.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "start": {"type": "number", "default": 0},
                "duration": {"type": "number"},
                "x": {}, "y": {}, "width": {"type": "number"},
                "animation": {"type": "string", "default": "none"},
                "blend": {"type": "string"},
            },
            "required": ["src", "duration"],
        },
    },
    {
        "name": "add_text",
        "description": "Add an animated text overlay.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "start": {"type": "number", "default": 0},
                "duration": {"type": "number"},
                "font": {"type": "string", "default": "Anton"},
                "size": {"type": "integer", "default": 64},
                "color": {"type": "string", "default": "#FFFFFF"},
                "animation": {"type": "string", "default": "fade"},
                "glow": {"type": "object",
                         "description": "{color, strength 0..1}"},
                "bubble": {"type": "object",
                           "description": "{color, alpha, padding}"},
                "underline": {"type": "boolean", "default": False},
                "bold_width": {"type": "number"},
                "x": {}, "y": {},
            },
            "required": ["text", "duration"],
        },
    },
    {
        "name": "add_audio",
        "description": "Add a voice / music / sfx audio track.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "role": {"type": "string", "default": "music"},
                "start": {"type": "number", "default": 0},
                "offset": {"type": "number", "default": 0},
                "duration": {"type": "number"},
                "volume": {"type": "number", "default": 1},
                "loop": {"type": "boolean", "default": False},
                "ducking": {"type": "object"},
                "denoise": {"type": "boolean", "default": False},
                "voice_change": {"type": "object"},
                "balance": {"description": "true or {target_i, "
                            "target_tp, target_lra} — loudnorm "
                            "per-track volume balancing"},
                "eq": {"type": "object",
                       "description": "{low, mid, high} gains in dB "
                       "(-24..24)"},
                "compressor": {"type": "object",
                               "description": "{threshold dB, ratio, "
                               "attack ms, release ms, makeup dB}"},
                "volume_keyframes": {"type": "array",
                                     "description": "[{time, value}] "
                                     "volume multiplier curve"},
                "pan": {"type": "number",
                        "description": "-1..1 stereo pan "
                        "(full left .. centre .. full right)"},
            },
            "required": ["src"],
        },
    },
    {
        "name": "add_captions",
        "description": "Attach word-level captions, or auto-transcribe a "
                       "media file with faster-whisper.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "words": {"type": "array"},
                "transcribe_from": {"type": "string"},
                "model": {"type": "string", "default": "base"},
                "language": {"type": "string"},
                "style": {"type": "string", "default": "viral-gold"},
                "ktv_color": {"type": "string"},
                "ktv_outline_color": {"type": "string"},
                "ktv_shadow_color": {"type": "string"},
                "underline": {"type": "boolean", "default": False},
                "bold": {"type": "boolean", "default": False},
                "italic": {"type": "boolean", "default": False},
                "bloom_color": {"type": "string"},
                "bloom_strength": {"type": "number"},
                "bubble": {"type": "boolean", "default": False},
                "bubble_color": {"type": "string"},
                "animation": {"type": "string",
                              "enum": ["none", "pop", "slide",
                                       "typewriter"]},
                "word_bloom": {"type": "object"},
            },
        },
    },
    {
        "name": "add_transition",
        "description": "Set the transition into clip N (0-based).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clip_index": {"type": "integer"},
                "type": {"type": "string"},
                "duration": {"type": "number", "default": 0.5},
            },
            "required": ["clip_index", "type"],
        },
    },
    {
        "name": "set_endcard",
        "description": "Set the end card (title, subtitle, thumbnail).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "duration": {"type": "number", "default": 5},
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "background": {"type": "string", "default": "#0B0B0B"},
                "thumbnail": {"type": "string"},
            },
        },
    },
    {
        "name": "set_export",
        "description": "Set the export block: codec and bitrate ladder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "codec": {"type": "string",
                          "description": "h264|h265|prores422|"
                          "prores422lt|prores422hq|prores4444|"
                          "prores422proxy|av1|gif|wav|mp3|dnxhd"},
                "bitrate_ladder": {"type": "object",
                                   "description": "{sd, hd, fullhd, 2k, "
                                   "4k, gop, profile}"},
            },
        },
    },
    {
        "name": "set_canvas",
        "description": "Set the canvas background behind the main track.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string",
                         "description": "color|image|blur"},
                "color": {"type": "string"},
                "image": {"type": "string"},
                "blur": {"type": "number"},
            },
            "required": ["type"],
        },
    },
    {
        "name": "add_marker",
        "description": "Add an editorial marker to the timeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "time": {"type": "number"},
                "name": {"type": "string"},
            },
            "required": ["time", "name"],
        },
    },
    {
        "name": "list_voices",
        "description": "List TTS providers and their voices. The Muse "
                       "provider ('muse') ships MAI_03, the production "
                       "voice, by default.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "synthesize_voice",
        "description": "Render a voiceover segment with a TTS provider. "
                       "Default provider 'muse' (Muse TTS, voice "
                       "avocado_v2:MAI_03, speed 90) writes real speech "
                       "to a .wav (24 kHz mono) or .mp3 file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "output": {"type": "string",
                           "description": ".wav or .mp3 file to write"},
                "provider": {"type": "string", "default": "muse"},
                "voice": {"type": "string",
                          "description": "voice id, e.g. "
                          "avocado_v2:MAI_03"},
                "speed": {"type": "number",
                          "description": "multiplier (0.9) or Muse CLI "
                          "scale (90); default 90, never above 100"},
            },
            "required": ["text", "output"],
        },
    },
    {
        "name": "validate",
        "description": "Validate the current timeline and report problems.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "render",
        "description": "Render the timeline to a video file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output": {"type": "string"},
                "workdir": {"type": "string"},
                "codec": {"type": "string",
                          "description": "override the export codec "
                          "for this render (h264|h265|prores422|"
                          "prores422hq|av1|gif|wav|mp3|dnxhd|...)"},
            },
            "required": ["output"],
        },
    },
    {
        "name": "get_status",
        "description": "Project summary: clips, duration, capabilities.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_templates",
        "description": "List bundled JSON timeline templates.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Session:
    """One MCP session = one in-memory project."""

    def __init__(self) -> None:
        self.project: Optional[Dict[str, Any]] = None
        self.last_render: Optional[Dict[str, Any]] = None

    # -- helpers ------------------------------------------------------
    def _need_project(self) -> Dict[str, Any]:
        if self.project is None:
            raise TimelineError("no project yet — call create_project first")
        return self.project

    # -- tools --------------------------------------------------------
    def create_project(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self.project = new_project(
            args.get("name", "untitled"),
            int(args.get("width", 1280)), int(args.get("height", 720)),
            int(args.get("fps", 30)))
        if args.get("canvas") is not None:
            self.project["canvas"] = args["canvas"]
        if args.get("export") is not None:
            self.project["export"] = args["export"]
        validate_timeline(self.project)
        return {"ok": True, "project": self.project["name"],
                "size": f"{self.project['width']}x{self.project['height']}@"
                        f"{self.project['fps']}"}

    def add_clip(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        clip = {k: v for k, v in args.items() if v is not None}
        p["clips"].append(clip)
        validate_timeline(p)
        return {"ok": True, "clips": len(p["clips"])}

    def add_overlay(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        p["overlays"].append({k: v for k, v in args.items() if v is not None})
        validate_timeline(p)
        return {"ok": True, "overlays": len(p["overlays"])}

    def add_text(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        p["texts"].append({k: v for k, v in args.items() if v is not None})
        validate_timeline(p)
        return {"ok": True, "texts": len(p["texts"])}

    def add_audio(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        p["audio"].append({k: v for k, v in args.items() if v is not None})
        validate_timeline(p)
        return {"ok": True, "audio_tracks": len(p["audio"])}

    def add_captions(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        if args.get("transcribe_from"):
            words = cap_mod.transcribe(
                args["transcribe_from"],
                model=args.get("model", "base"),
                language=args.get("language"))
        elif args.get("words"):
            words = args["words"]
        else:
            raise TimelineError("add_captions needs words or transcribe_from")
        caps: Dict[str, Any] = {"words": words,
                                "style": args.get("style", "viral-gold")}
        for key in ("ktv_color", "ktv_outline_color", "ktv_shadow_color",
                    "underline", "bold", "italic", "bloom_color",
                    "bloom_strength", "bubble", "bubble_color",
                    "animation", "word_bloom"):
            if args.get(key) is not None:
                caps[key] = args[key]
        p["captions"] = caps
        validate_timeline(p)
        return {"ok": True, "words": len(words)}

    def add_transition(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        i = int(args["clip_index"])
        if not 0 <= i < len(p["clips"]):
            raise TimelineError(f"clip_index {i} out of range")
        p["clips"][i]["transition_in"] = {
            "type": args["type"],
            "duration": float(args.get("duration", 0.5)),
        }
        validate_timeline(p)
        return {"ok": True, "clip": i, "transition": args["type"]}

    def set_endcard(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        p["endcard"] = {k: v for k, v in args.items() if v is not None}
        validate_timeline(p)
        return {"ok": True, "endcard": p["endcard"]}

    def set_export(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        exp = dict(p.get("export") or {})
        for k in ("codec", "bitrate_ladder"):
            if args.get(k) is not None:
                exp[k] = args[k]
        p["export"] = exp
        validate_timeline(p)
        return {"ok": True, "export": p["export"]}

    def set_canvas(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        p["canvas"] = {k: v for k, v in args.items() if v is not None}
        validate_timeline(p)
        return {"ok": True, "canvas": p["canvas"]}

    def add_marker(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        p.setdefault("markers", []).append(
            {"time": float(args["time"]), "name": str(args["name"])})
        validate_timeline(p)
        return {"ok": True, "markers": len(p["markers"])}

    def list_voices(self, args: Dict[str, Any]) -> Dict[str, Any]:
        providers: Dict[str, Any] = {}
        for name in tts_mod.list_providers():
            if name == "muse":
                providers[name] = {
                    "voices": tts_mod.list_muse_voices(),
                    "default_voice": tts_mod.DEFAULT_MUSE_VOICE,
                    "default_speed": tts_mod.DEFAULT_MUSE_SPEED,
                }
            else:
                providers[name] = {}
        return {"ok": True, "providers": providers}

    def synthesize_voice(self, args: Dict[str, Any]) -> Dict[str, Any]:
        text = args.get("text") or ""
        output = args.get("output") or ""
        if not text.strip():
            raise ValueError("synthesize_voice needs text")
        if not output:
            raise ValueError("synthesize_voice needs output (.wav/.mp3)")
        provider_name = args.get("provider") or "muse"
        provider = tts_mod.get_provider(provider_name)
        out = provider.synthesize(text, output, voice=args.get("voice"),
                                  speed=args.get("speed"))
        dur = probe_media(out)["duration"]
        mean, maxv = audio_mod.mix_levels(out)
        return {"ok": True, "output": out, "provider": provider_name,
                "voice": args.get("voice") or tts_mod.DEFAULT_MUSE_VOICE,
                "duration_s": round(dur, 2),
                "mean_db": round(mean, 1), "max_db": round(maxv, 1)}

    def validate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        validate_timeline(p)
        return {"ok": True, "valid": True}

    def render(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self._need_project()
        renderer = Renderer(p, workdir=args.get("workdir"),
                            codec=args.get("codec"))
        # quiet=True: render progress prints to stdout, which is the
        # JSON-RPC channel — a bare print would corrupt the protocol.
        result = renderer.render(args["output"], quiet=True)
        self.last_render = result.as_dict()
        return {"ok": True, "render": self.last_render}

    def get_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        p = self.project
        if p is None:
            return {"ok": True, "project": None,
                    "capabilities": ai_mod.capabilities()}
        from .timeline import timeline_duration
        return {
            "ok": True,
            "project": p["name"],
            "size": f"{p['width']}x{p['height']}@{p['fps']}",
            "clips": len(p.get("clips") or []),
            "overlays": len(p.get("overlays") or []),
            "texts": len(p.get("texts") or []),
            "audio_tracks": len(p.get("audio") or []),
            "has_captions": bool(p.get("captions")),
            "has_endcard": bool(p.get("endcard")),
            "markers": len(p.get("markers") or []),
            "sequences": len(p.get("sequences") or {}),
            "export": (p.get("export") or {}).get("codec"),
            "canvas": (p.get("canvas") or {}).get("type"),
            "estimated_duration": round(timeline_duration(p), 2),
            "last_render": self.last_render,
            "capabilities": ai_mod.capabilities(),
        }

    def list_templates(self, args: Dict[str, Any]) -> Dict[str, Any]:
        base = os.path.join(os.path.dirname(__file__), "..", "examples")
        names = sorted(f for f in os.listdir(base) if f.endswith(".json"))
        return {"ok": True, "templates": names}


# ------------------------------------------------------------------ server
class MCPServer:
    def __init__(self) -> None:
        self.session = Session()

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }}
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                fn = getattr(self.session, name, None)
                if fn is None or name.startswith("_"):
                    raise ValueError(f"unknown tool: {name!r}")
                result = fn(args)
                return {"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, indent=2)}],
                    "isError": False,
                }}
            except Exception as exc:  # noqa: BLE001 - report to client
                return {"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text",
                                 "text": f"error: {exc}"}],
                    "isError": True,
                }}
        return {"jsonrpc": "2.0", "id": msg_id, "error": {
            "code": -32601, "message": f"method not found: {method}"}}

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                resp = self.handle(msg)
            except Exception:  # noqa: BLE001 - never die on one message
                resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32603,
                                  "message": traceback.format_exc()[-500:]}}
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()


def main() -> None:
    MCPServer().run()


if __name__ == "__main__":
    main()
