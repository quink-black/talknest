"""Command line interface: argument parsing, device setup, offline tools."""

import argparse
import asyncio
import os
import pathlib
import signal
import ssl
import sys
import threading
import time
import urllib.request

import numpy as np

from .aec import DtlnAec, EchoCanceller, NullAec, erle_db
from .audio import AudioEngine, read_wav_mono, resample
from .devices import (default_route_is_bluetooth, format_devices, list_devices,
                      pair, pick_device, same_physical_device, still_present)
from .portaudio import sd, require_portaudio
from .session import Session

# DTLN-aec cancels best when the residual delay it must handle sits near the
# middle of its training band; measured on synthetic loops, see the design doc.
LIVE_DELAY_MS = 25


def default_url() -> str:
    """Pick ws/wss by asking the orchestrator which scheme it serves."""
    port = os.environ.get("TALKNEST_PORT", "8890")
    host = "127.0.0.1"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for scheme, ws_scheme in (("https", "wss"), ("http", "ws")):
        try:
            req = urllib.request.Request(f"{scheme}://{host}:{port}/modes")
            with urllib.request.urlopen(req, timeout=1.0, context=ctx) as r:
                if r.status == 200:
                    return f"{ws_scheme}://{host}:{port}/ws"
        except Exception:
            continue
    return f"ws://{host}:{port}/ws"


def default_model_prefix() -> str:
    root = os.environ.get("TALKNEST_MODEL_ROOT")
    if not root:
        root = str(pathlib.Path(__file__).resolve().parents[3] / "model")
    return str(pathlib.Path(root) / "dtln-aec" / "dtln_aec_128")


def model_present(prefix: str) -> bool:
    return all(os.path.exists(f"{prefix}_{i}.tflite") for i in (1, 2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="talknest-cli", description="Headless talknest voice client")
    # help must stay static: default_url() probes the network and would run
    # on every invocation, including --help and --list-devices.
    p.add_argument("--url", default=None,
                   help="websocket url (default: auto-detect ws/wss on "
                        "127.0.0.1:${TALKNEST_PORT:-8890}/ws)")
    p.add_argument("--mode", default="kid", help="conversation mode")
    p.add_argument("--input", default=None, help="capture device (index or name substring)")
    p.add_argument("--output", default=None, help="playback device (index or name substring)")
    p.add_argument("--list-devices", action="store_true", help="print devices and exit")
    p.add_argument("--input-file", default=None, help="use a WAV as the microphone")
    p.add_argument("--output-file", default=None, help="write reply audio to a WAV")
    p.add_argument("--aec", default="auto", choices=("auto", "none", "dtln"),
                   help="auto: DTLN on stable-clock devices, half duplex "
                        "over bluetooth HFP")
    p.add_argument("--aec-model", default=None, help="DTLN-aec model prefix")
    p.add_argument("--aec-delay-ms", type=int, default=None,
                   help="reference shift in ms; omit to let --aec-bench "
                        "sweep for the best value (live default: "
                        f"{LIVE_DELAY_MS})")
    p.add_argument("--half-duplex", action="store_true",
                   help="mute the microphone while the assistant speaks")
    p.add_argument("--device-poll-s", type=float, default=2.0,
                   help="exit when a selected device disappears (0 disables, "
                        "default 2.0)")
    p.add_argument("--tls-ca", default=None, metavar="PEM",
                   help="CA bundle to verify the wss connection "
                        "(default: no verification, for self-signed certs)")
    p.add_argument("--aec-eval", nargs=2, metavar=("REF_WAV", "MIC_WAV"),
                   help="offline ERLE for a recorded loop")
    p.add_argument("--aec-bench", action="store_true",
                   help="play and record on the selected devices, report ERLE")
    return p


def build_aec(args) -> EchoCanceller:
    if args.aec == "none":
        return NullAec()
    prefix = args.aec_model or default_model_prefix()
    try:
        return DtlnAec(prefix)
    except Exception as exc:
        if args.aec == "dtln":
            raise SystemExit(f"[aec] cannot load DTLN-aec from {prefix}: {exc}")
        print(f"[aec] DTLN-aec unavailable ({exc}); running half duplex", flush=True)
        if not model_present(prefix) and not os.environ.get("TALKNEST_MODEL_ROOT"):
            print("[aec] hint: set TALKNEST_MODEL_ROOT to the directory "
                  "containing dtln-aec/*.tflite", flush=True)
        return NullAec()


def aec_delay_samples(aec: EchoCanceller, delay_ms: int) -> int:
    return int(delay_ms * aec.proc_sr / 1000.0)


# -- offline tools -------------------------------------------------------

def _run_through_aec(aec: EchoCanceller, ref: np.ndarray, mic: np.ndarray,
                     delay_ms: int) -> float:
    """Process a recorded loop and return ERLE in dB."""
    aec.reset(aec_delay_samples(aec, delay_ms))
    step = 128 if aec.proc_sr == 16000 else 1024
    out = []
    for off in range(0, len(mic) - step + 1, step):
        aec.push_reference(ref[off : off + step])
        out.append(aec.process(mic[off : off + step]))
    residual = np.concatenate(out) if out else np.zeros(0)
    n = min(len(residual), len(mic))
    return erle_db(mic[:n], residual[:n])


def _sweep_delay(aec: EchoCanceller, ref: np.ndarray, mic: np.ndarray,
                 step: int = 20, cap_ms: int = 2000) -> tuple[int, float]:
    """Sweep the compensation and report the best delay.

    The window keeps extending while the peak sits at its upper edge: HFP
    loop delays reach ~1.1 s, far past the first 400 ms window.
    """
    lo, hi = 0, 400
    best_d, best_db = 0, -np.inf
    while True:
        sweep = [(d, _run_through_aec(aec, ref, mic, d))
                 for d in range(lo, hi + 1, step)]
        for d, db in sweep:
            print(f"  delay_ms={d:<4} erle={db:6.1f} dB", flush=True)
        d_best, db_best = max(sweep, key=lambda x: x[1])
        if db_best > best_db:
            best_d, best_db = d_best, db_best
        # Interior peak (or the hard cap) ends the search; measured shape:
        # under-compensation degrades gently, over-compensation collapses.
        if d_best <= hi - 3 * step or hi >= cap_ms:
            return best_d, best_db
        lo, hi = hi + step, min(hi + 400, cap_ms)


def run_aec_eval(ref_path: str, mic_path: str, aec: EchoCanceller,
                 delay_ms: int | None) -> int:
    """Replay a recorded loop through the backend and report ERLE.

    Without an explicit delay the compensation is swept, since the best
    shift is a property of the backend's training band, not of the recording.
    """
    ref, ref_sr = read_wav_mono(ref_path)
    mic, mic_sr = read_wav_mono(mic_path)
    ref = resample(ref, ref_sr, aec.proc_sr)
    mic = resample(mic, mic_sr, aec.proc_sr)
    n = min(len(ref), len(mic))
    if delay_ms is None:
        best_d, best_db = _sweep_delay(aec, ref[:n], mic[:n])
        print(f"backend={aec.name} proc_sr={aec.proc_sr} "
              f"best delay_ms={best_d} erle={best_db:.1f} dB")
    else:
        db = _run_through_aec(aec, ref[:n], mic[:n], delay_ms)
        print(f"backend={aec.name} proc_sr={aec.proc_sr} delay_ms={delay_ms} "
              f"erle={db:.1f} dB")
    return 0


def _lag_ms(played: np.ndarray, recorded: np.ndarray, sr: float) -> float:
    n = 1 << int(np.ceil(np.log2(len(played) + len(recorded))))
    spec = np.fft.rfft(played, n) * np.conj(np.fft.rfft(recorded, n))
    corr = np.fft.irfft(spec, n)
    return float(np.argmax(np.abs(corr[: n // 2]))) / sr * 1000.0


def run_aec_bench(in_dev, out_dev, aec: EchoCanceller, delay_ms: int | None,
                  seconds: float = 6.0) -> int:
    """Play a test signal, record the echo, report loop delay and ERLE.

    DTLN-aec cancels best when the reference it sees lags the echo by its
    training delay band, not by the raw acoustic delay, so the bench sweeps
    the compensation and reports the peak instead of trusting one value.
    """
    require_portaudio()
    in_sr = int(sd.query_devices(in_dev.index, "input")["default_samplerate"])
    out_sr = int(sd.query_devices(out_dev.index, "output")["default_samplerate"])
    if in_sr != out_sr:
        # playrec needs one rate for both directions of the same call.
        raise SystemExit(f"[bench] input and output default rates differ "
                         f"({in_sr} vs {out_sr}); run the bench on a paired "
                         "device or pass --input/--output with matching rates")
    sr = in_sr
    n = int(seconds * sr)
    rng = np.random.default_rng(0)
    # Speech-shaped noise: white noise rolled off towards high frequencies.
    sig = np.cumsum(rng.normal(0, 1, n))
    sig = sig - sig.mean()
    sig = sig / np.max(np.abs(sig)) * 0.25

    recorded = sd.playrec(sig.astype(np.float32), samplerate=sr,
                          device=(in_dev.index, out_dev.index),
                          channels=1, dtype="float32")
    sd.wait()
    recorded = recorded[:, 0].astype(np.float32)
    print(f"loop delay ~{_lag_ms(sig, recorded, sr):.0f} ms")

    ref = resample(sig, sr, aec.proc_sr)
    mic = resample(recorded, sr, aec.proc_sr)
    m = min(len(ref), len(mic))

    if delay_ms is None:
        # Sweep centered on the measured loop delay: bluetooth HFP loops sit
        # at several hundred ms, far outside the model's tolerance band, so
        # the compensation has to land near the true value to work at all.
        center = _lag_ms(sig, recorded, sr)
        low = max(0, int(center) - 200)
        sweep = [(d, _run_through_aec(aec, ref[:m], mic[:m], d))
                 for d in range(low, int(center) + 201, 20)]
        for d, db in sweep:
            print(f"  delay_ms={d:<4} erle={db:6.1f} dB")
        best_d, best_db = max(sweep, key=lambda x: x[1])
        print(f"backend={aec.name} best delay_ms={best_d} erle={best_db:.1f} dB")
        print(f"use: --aec-delay-ms {best_d}")
    else:
        db = _run_through_aec(aec, ref[:m], mic[:m], delay_ms)
        print(f"backend={aec.name} delay_ms={delay_ms} erle={db:.1f} dB")
    return 0


# -- main ----------------------------------------------------------------

def _keyboard(session: Session, loop: asyncio.AbstractEventLoop) -> None:
    print("Enter = interrupt, q + Enter = quit", flush=True)
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd in ("q", "quit", "exit"):
            loop.call_soon_threadsafe(session.stop)
            return
        asyncio.run_coroutine_threadsafe(session.interrupt(), loop)
    # stdin EOF (nohup, systemd, CI): the keyboard is gone, say how to stop.
    print("[input] stdin closed; send SIGTERM or SIGINT to quit", flush=True)


def _watch_devices(session: Session, in_idx, out_idx, interval: float,
                   loop: asyncio.AbstractEventLoop) -> None:
    """Exit when a selected device disappears (bluetooth blips out, USB
    unplug). A supervisor such as systemd restarts the client, which then
    re-pairs with whatever is present."""
    while not session._stop:
        time.sleep(interval)
        try:
            gone = [kind for idx, kind in ((in_idx, "input"), (out_idx, "output"))
                    if idx is not None and not still_present(idx, kind)]
        except SystemExit:
            # A device query can fail while the bus is resettling. Raising
            # here would end monitoring silently, since threading swallows
            # SystemExit from a thread.
            continue
        if gone:
            print(f"[devices] {', '.join(gone)} device disappeared; exiting",
                  flush=True)
            loop.call_soon_threadsafe(session.stop)
            return


async def _run(args, aec: EchoCanceller) -> None:
    in_dev = out_dev = None
    if not args.input_file and not args.output_file:
        in_dev, out_dev = pair(args.input, args.output)
    elif not args.input_file:
        # File on the other side: resolve only this side, so a machine
        # without a microphone can still play --input-file conversations.
        in_dev = pick_device(args.input, "input")
    elif not args.output_file:
        out_dev = pick_device(args.output, "output")
    if (args.aec == "auto" and aec.name != "none"
            and (in_dev is not None or out_dev is not None)
            and _over_bluetooth(in_dev, out_dev, args)):
        # Measured on HFP hardware: per-second loop delays wander across
        # 172-1090 ms with coherence near zero, so no reference-aligned
        # canceller can track them. Half duplex is the correct mode.
        aec.close()
        aec = NullAec()
        print("[audio] bluetooth device detected: HFP clock jitter "
              "defeats reference-aligned AEC, running half duplex "
              "(--aec dtln to override)", flush=True)
    engine = AudioEngine(
        aec,
        in_dev.index if in_dev else None,
        out_dev.index if out_dev else None,
        input_file=args.input_file, output_file=args.output_file)
    delay_ms = args.aec_delay_ms if args.aec_delay_ms is not None else LIVE_DELAY_MS
    aec.reset(aec_delay_samples(aec, delay_ms))
    engine.start()
    print(f"[audio] in={_describe(in_dev, 'input')} "
          f"out={_describe(out_dev, 'output')} "
          f"rates={engine.in_sr}/{engine.out_sr} aec={aec.name}", flush=True)
    if (in_dev is not None and out_dev is not None
            and not same_physical_device(in_dev, out_dev)):
        print("[audio] warning: mic and speaker are not the same physical "
              "device; echo cancellation will be weaker", flush=True)

    # Half duplex is mandatory without a canceller: the speaker feeds the mic.
    half_duplex = args.half_duplex or aec.name == "none"
    session = Session(args.url or default_url(), engine, mode=args.mode,
                      half_duplex=half_duplex, ca_file=args.tls_ca)
    loop = asyncio.get_running_loop()
    threading.Thread(target=_keyboard, args=(session, loop), daemon=True).start()
    if args.device_poll_s > 0 and (in_dev is not None or out_dev is not None):
        threading.Thread(
            target=_watch_devices,
            args=(session, in_dev.index if in_dev else None,
                  out_dev.index if out_dev else None,
                  args.device_poll_s, loop),
            daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, session.stop)
        except NotImplementedError:
            pass  # not on this platform/event loop
    try:
        await session.run()
    finally:
        engine.stop()


def _over_bluetooth(in_dev, out_dev, args) -> bool:
    """Whether the selected devices are (or route over) bluetooth HFP.

    An explicit --input/--output choice is trusted by its name; when any
    side was picked by default, the PortAudio `default` device hides its
    routing, so the sound server's default route decides.
    """
    if in_dev is not None and in_dev.is_bluetooth:
        return True
    if out_dev is not None and out_dev.is_bluetooth:
        return True
    return (args.input is None or args.output is None) \
        and default_route_is_bluetooth()


def _describe(device, kind: str) -> str:
    if device is None:
        return f"{kind}-file"
    return f"[{device.index}] {device.name}"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        print(format_devices(list_devices()))
        return 0

    aec = build_aec(args)
    if args.aec_eval:
        ref_wav, mic_wav = args.aec_eval
        return run_aec_eval(ref_wav, mic_wav, aec, args.aec_delay_ms)
    if args.aec_bench:
        in_dev, out_dev = pair(args.input, args.output)
        return run_aec_bench(in_dev, out_dev, aec, args.aec_delay_ms)
    try:
        asyncio.run(_run(args, aec))
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    return 0
