"""Headless talknest client: microphone in, speaker out, no browser."""

CLIENT_SR = 24000  # Qwen3-ASR native rate; the server hardcodes the same
FRAME_SAMPLES = 1024  # ~43 ms, matches the browser client
