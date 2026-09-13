"""PortAudio access, optional so that file based runs need no sound card.

sounddevice raises OSError at import time when libportaudio is absent, which
would break --input-file/--output-file and --aec-eval on a machine without an
audio stack. Import failure is therefore recorded and reported only when a
device is actually needed.
"""

try:
    import sounddevice as sd
except OSError:
    sd = None

HINT = ("[audio] PortAudio is not installed; needed for real devices only.\n"
        "       Debian/Ubuntu: sudo apt-get install -y libportaudio2\n"
        "       Raspberry Pi OS: sudo apt-get install -y libportaudio2")


def require_portaudio() -> None:
    if sd is None:
        raise SystemExit(HINT)
