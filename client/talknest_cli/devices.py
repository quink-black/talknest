"""Device enumeration, mic/speaker pairing and hotplug detection.

The browser pairs a microphone with a speaker through `groupId`. PortAudio
exposes no such field, so the physical device is approximated from the name:
the ALSA card number (`hw:C,D`), or the bus id (`pci-...`, `usb-...`) that
PulseAudio and PipeWire keep in their device names. Two devices sharing a key
are the two halves of one headset or one sound card.
"""

import re
import subprocess

from .portaudio import sd, require_portaudio

_HW_RE = re.compile(r"hw:(\d+)(?:,(\d+))?")
_BUS_RE = re.compile(r"\b((?:pci|usb)-[0-9a-zA-Z._:-]+)")
_MAC_RE = re.compile(r"bluez_(?:input|output)\.([0-9A-Fa-f_]+)")


class Device:
    def __init__(self, index: int, info: dict):
        self.index = index
        self.name = info["name"]
        self.hostapi = sd.query_hostapis(info["hostapi"])["name"]
        self.max_input_channels = info["max_input_channels"]
        self.max_output_channels = info["max_output_channels"]
        self.default_samplerate = info["default_samplerate"]
        self.key = _physical_key(self.name, self.hostapi)

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.max_output_channels > 0

    @property
    def is_bluetooth(self) -> bool:
        # HFP devices show up through the bluez ALSA plugin with the MAC
        # embedded in the PortAudio name.
        return _MAC_RE.search(self.name) is not None


def _physical_key(name: str, hostapi: str) -> str:
    m = _HW_RE.search(name)
    if m:
        return f"alsa-card:{m.group(1)}"
    m = _MAC_RE.search(name)  # the two halves of one bluetooth device
    if m:
        return f"bluez:{m.group(1).lower()}"
    m = _BUS_RE.search(name)
    if m:
        return m.group(1)
    return name.split(":")[0].strip().lower()


def list_devices() -> list[Device]:
    require_portaudio()
    try:
        raw = sd.query_devices()
    except Exception as exc:
        raise SystemExit(f"[devices] cannot query audio devices: {exc}") from exc
    if isinstance(raw, dict):
        raw = [raw]
    return [Device(i, info) for i, info in enumerate(raw)]


def format_devices(devices: list[Device]) -> str:
    rows = [f"{'idx':>4}  {'in':>2} {'out':>3}  {'rate':>7}  {'api':<12} name"]
    for d in devices:
        rows.append(f"{d.index:>4}  {d.max_input_channels:>2} "
                    f"{d.max_output_channels:>3}  {d.default_samplerate:>7.0f}  "
                    f"{d.hostapi[:12]:<12} {d.name}")
    return "\n".join(rows)


def find(spec: str, kind: str) -> int | None:
    """Resolve an index or a case-insensitive name substring to a device index."""
    devices = list_devices()
    if kind == "input":
        wanted = [d for d in devices if d.is_input]
    else:
        wanted = [d for d in devices if d.is_output]
    if spec.isdigit() and int(spec) in {d.index for d in wanted}:
        return int(spec)
    needle = spec.lower()
    for d in wanted:
        if needle in d.name.lower():
            return d.index
    return None


def pair(input_spec: str | None = None, output_spec: str | None = None) -> tuple:
    """Return (input_device, output_device), preferring one physical device."""
    devices = list_devices()
    inputs = [d for d in devices if d.is_input]
    outputs = [d for d in devices if d.is_output]
    if not inputs or not outputs:
        raise SystemExit("[devices] no usable input and output pair found")

    in_dev = _resolve(input_spec, inputs, "input") if input_spec else None
    out_dev = _resolve(output_spec, outputs, "output") if output_spec else None
    if in_dev is not None and out_dev is not None:
        return in_dev, out_dev

    if in_dev is None:
        in_dev = _pair_source(inputs, out_dev) or _prefer_default(inputs)
    if out_dev is None:
        out_dev = _pair_source(outputs, in_dev) or _prefer_default(outputs)
    return in_dev, out_dev


def _resolve(spec: str, candidates: list[Device], kind: str) -> Device | None:
    idx = find(spec, kind)
    if idx is None:
        raise SystemExit(f"[devices] no {kind} device matching {spec!r}")
    for d in candidates:
        if d.index == idx:
            return d
    raise SystemExit(f"[devices] device {spec!r} is not a {kind}")


def _pair_source(candidates: list[Device], other: Device | None) -> Device | None:
    """Pick the candidate on the same physical device as `other`."""
    if other is None:
        return None
    for d in candidates:
        if d.key == other.key:
            return d
    return None


def _prefer_default(candidates: list[Device]) -> Device:
    try:
        default_index = sd.default.device[0 if candidates[0].is_input else 1]
        for d in candidates:
            if d.index == default_index:
                return d
    except Exception:
        pass
    return candidates[0]


def same_physical_device(in_dev, out_dev) -> bool:
    inputs = {d.index: d for d in list_devices() if d.is_input}
    outputs = {d.index: d for d in list_devices() if d.is_output}
    a, b = inputs.get(in_dev), outputs.get(out_dev)
    return bool(a and b and a.key == b.key)


def default_route_is_bluetooth() -> bool:
    """Ask the sound server whether a default route goes over bluetooth.

    The PortAudio `default` device routes through PipeWire and hides the
    actual transport behind a generic name, so the classification comes
    from PulseAudio/PipeWire. Returns False when no control tool exists
    (pure-ALSA headless setups fall back to name-based detection only).
    """
    for kind in ("sink", "source"):
        try:
            r = subprocess.run(["pactl", f"get-default-{kind}"],
                               capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode == 0 and "bluez" in r.stdout.strip().lower():
            return True
    return False


def still_present(index, kind: str) -> bool:
    devices = list_devices()
    if index is None or not (0 <= index < len(devices)):
        return False
    d = devices[index]
    return d.is_input if kind == "input" else d.is_output
