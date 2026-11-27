"""MuseCut text-to-speech hookup point.

MuseCut is renderer-first: it does not bundle a TTS voice, because
voices are account- and vendor-specific. Instead it defines a tiny
provider protocol. Any agent can register its own TTS backend (Muse
TTS, Gemini, a local model, …) and the CLI/MCP will use it to turn a
script into a voice track.

Example — register a provider in your own code::

    from musecut import tts

    class MyTTS(tts.TTSProvider):
        name = "my-tts"
        def synthesize(self, text, output, voice=None, speed=1.0):
            ...  # write a wav/mp3 to `output`
            return output

    tts.register_provider(MyTTS())

    # then:  musecut render --tts-script script.txt ...
    # or via MCP: synthesize_voice(text, voice="...")

Built-in providers
------------------
* :class:`NullTTSProvider` — always present; raises a clear error so a
  missing voice is never silently replaced by silence.
* :class:`CommandTTSProvider` — wraps any command-line TTS binary
  (``espeak-ng``, ``festival``, a vendor CLI) via a ``{text}`` /
  ``{output}`` template.
* :class:`MuseTTSProvider` — Muse's own built-in TTS backend
  (``/opt/hatch/bin/tts``), voice ``avocado_v2:MAI_03`` by default.
  This is the production voice of the Dollar Sense / CarSecretsUSA
  channels: no quota cap, direct speed control, STT-verified in
  production. Registered by default under provider name ``"muse"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from typing import Dict, List, Optional

from .audio import ffmpeg_env

_PROVIDERS: Dict[str, "TTSProvider"] = {}


class TTSProvider:
    """Protocol for a text-to-speech backend."""

    name = "base"

    def synthesize(self, text: str, output: str,
                   voice: Optional[str] = None,
                   speed: float = 1.0) -> str:
        """Write speech audio for ``text`` to ``output``. Returns output."""
        raise NotImplementedError


class NullTTSProvider(TTSProvider):
    """Placeholder that fails loudly — never fake a voiceover."""

    name = "none"

    def synthesize(self, text: str, output: str,
                   voice: Optional[str] = None,
                   speed: float = 1.0) -> str:
        raise RuntimeError(
            "No TTS provider is registered. Register one with "
            "musecut.tts.register_provider(...) — see musecut/tts.py for "
            "the provider protocol — or render a voice track yourself and "
            "add it as an audio track with role='voice'."
        )


class CommandTTSProvider(TTSProvider):
    """Wrap a command-line TTS binary.

    ``command`` is a shell-style template list, e.g.
    ``["espeak-ng", "-v", "{voice}", "-s", "150", "-w", "{output}",
    "{text}"]``. ``{text}``, ``{output}`` and ``{voice}`` are replaced.
    """

    def __init__(self, name: str, command: List[str],
                 default_voice: Optional[str] = None):
        self.name = name
        self.command = command
        self.default_voice = default_voice

    def synthesize(self, text: str, output: str,
                   voice: Optional[str] = None,
                   speed: float = 1.0) -> str:
        voice = voice or self.default_voice or ""
        argv = []
        for tok in self.command:
            argv.append(tok.format(text=text, output=output, voice=voice))
        subprocess.run(argv, check=True, env=ffmpeg_env())
        if not os.path.isfile(output):
            raise RuntimeError(
                f"TTS command {self.command[0]!r} did not produce {output!r}")
        return output


# ---------------------------------------------------------------- Muse TTS
# Production backend for the channels (see
# ~/workspace/youtube-finance/muse_vo.py). Speed lesson (Ep7, 2026-10-06):
# the backend CLIPS word endings above speed 90 — the production cap is
# 90 and the hard ceiling is 100. Never silently switch voices on
# failure: retry the SAME voice, then raise.
MUSE_TTS_BIN = "/opt/hatch/bin/tts"
DEFAULT_MUSE_VOICE = "avocado_v2:MAI_03"
DEFAULT_MUSE_SPEED = 90
MAX_MUSE_SPEED = 100
MUSE_TTS_TIMEOUT = 180

# Voices verified in production. Only MAI_03 is STT-verified as the
# channel voice; other Muse voice ids can be passed through explicitly
# but are NOT claimed to be verified here.
MUSE_VOICES: Dict[str, str] = {
    "avocado_v2:MAI_03": "Muse TTS MAI_03 — production voice "
                         "(deep, natural; Dollar Sense + CarSecretsUSA)",
}


def list_muse_voices() -> Dict[str, str]:
    """Verified Muse voices: {voice id: description}."""
    return dict(MUSE_VOICES)


def _muse_cli_speed(speed: Optional[float]) -> int:
    """Normalize a speed to the Muse CLI scale (1..100).

    ``None`` -> production default 90. A multiplier (``0.9``) maps to
    its CLI value (``90``); a CLI-scale int (``90``) passes through.
    Clamped to 1..100 — never above 100 (Ep7 truncation lesson).
    """
    if speed is None:
        return DEFAULT_MUSE_SPEED
    if speed <= 2.0:
        cli = int(round(speed * 100))
    else:
        cli = int(round(speed))
    return max(1, min(MAX_MUSE_SPEED, cli))


class MuseTTSProvider(TTSProvider):
    """Muse's own built-in TTS backend (``/opt/hatch/bin/tts``).

    Renders real speech with ``tts speak`` (mp3) and converts to the
    requested output — ``.wav`` becomes 24 kHz mono (the production
    voiceover format), ``.mp3`` is written directly. Retries the SAME
    voice on failure (the backend is occasionally flaky); never
    silently falls back to another voice or to silence.
    """

    name = "muse"

    def __init__(self, tts_bin: str = MUSE_TTS_BIN,
                 default_voice: str = DEFAULT_MUSE_VOICE,
                 default_speed: int = DEFAULT_MUSE_SPEED,
                 retries: int = 3,
                 timeout: int = MUSE_TTS_TIMEOUT):
        self.tts_bin = tts_bin
        self.default_voice = default_voice
        self.default_speed = default_speed
        self.retries = retries
        self.timeout = timeout

    def list_voices(self) -> Dict[str, str]:
        return list_muse_voices()

    def synthesize(self, text: str, output: str,
                   voice: Optional[str] = None,
                   speed: float = 1.0) -> str:
        # `speed` here follows the protocol (1.0 = normal) but the
        # production default is 90, not 100: a caller passing the
        # protocol default 1.0 gets the production cap 90 unless they
        # explicitly asked for a different speed. An explicit speed is
        # honoured (clamped to <= 100).
        voice = voice or self.default_voice
        if speed is None or speed == 1.0:
            cli_speed = self.default_speed
        else:
            cli_speed = _muse_cli_speed(speed)
        if not text or not text.strip():
            raise ValueError("cannot synthesize empty text")
        out_dir = os.path.dirname(os.path.abspath(output))
        os.makedirs(out_dir, exist_ok=True)
        ext = os.path.splitext(output)[1].lower()
        if ext not in (".wav", ".mp3"):
            raise ValueError(
                f"MuseTTSProvider writes .wav or .mp3, not {ext!r}")
        tmp_mp3 = None
        try:
            if ext == ".mp3":
                target_mp3 = output
            else:
                fd, tmp_mp3 = tempfile.mkstemp(suffix=".mp3",
                                               prefix="musecut-tts-")
                os.close(fd)
                target_mp3 = tmp_mp3
            last_exc: Optional[Exception] = None
            for attempt in range(1, self.retries + 1):
                try:
                    self._render_once(text, voice, cli_speed, target_mp3)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 - retry, then raise
                    last_exc = exc
                    if attempt < self.retries:
                        time.sleep(3)
            if last_exc is not None:
                raise RuntimeError(
                    f"Muse TTS failed after {self.retries} attempts "
                    f"(voice {voice!r}, speed {cli_speed}): {last_exc}")
            if ext == ".wav":
                self._to_wav(target_mp3, output)
            if (not os.path.isfile(output)
                    or os.path.getsize(output) == 0):
                raise RuntimeError(
                    f"Muse TTS produced no audio at {output!r}")
            return output
        finally:
            if tmp_mp3 and os.path.exists(tmp_mp3):
                os.unlink(tmp_mp3)

    def _render_once(self, text: str, voice: str, cli_speed: int,
                     out_mp3: str) -> str:
        if not shutil.which(self.tts_bin) and not os.path.isfile(
                self.tts_bin):
            raise RuntimeError(
                f"Muse TTS binary not found: {self.tts_bin!r}")
        cmd = [self.tts_bin, "speak", "--text", text, "--voice", voice,
               "--speed", str(cli_speed), "--format", "mp3",
               "--output", out_mp3]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=self.timeout, env=ffmpeg_env())
        if r.returncode != 0 or not os.path.exists(out_mp3):
            raise RuntimeError(
                f"tts speak failed: "
                f"{(r.stderr or r.stdout or '')[-300:]}")
        return out_mp3

    def _to_wav(self, mp3: str, wav: str) -> str:
        # Production voiceover format: 24 kHz mono wav.
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp3,
                        "-ar", "24000", "-ac", "1", wav],
                       check=True, timeout=120, env=ffmpeg_env())
        return wav


def register_provider(provider: TTSProvider) -> None:
    _PROVIDERS[provider.name] = provider


def get_provider(name: str = "none") -> TTSProvider:
    if name in _PROVIDERS:
        return _PROVIDERS[name]
    if name == "none":
        return NullTTSProvider()
    raise ValueError(
        f"unknown TTS provider {name!r} (registered: "
        f"{sorted(_PROVIDERS) or ['none']})")


def list_providers() -> List[str]:
    return sorted(set(_PROVIDERS) | {"none"})


# The Muse backend is registered by default: `get_provider("muse")`
# works out of the box on a machine with /opt/hatch/bin/tts.
register_provider(MuseTTSProvider())
