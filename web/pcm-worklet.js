// Mic capture worklet: runs inside an AudioContext created at 24 kHz
// (the browser resamples the mic input for us), so this processor only
// converts float32 frames to Int16 PCM chunks.
class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // 1024 samples = 43 ms: short enough that a child cutting in is
    // noticed quickly, long enough to keep message overhead low.
    this.acc = new Float32Array(1024);
    this.accLen = 0;
  }

  process(inputs) {
    const input = inputs[0][0]; // mono channel
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      this.acc[this.accLen++] = input[i];
      if (this.accLen === this.acc.length) this.flush();
    }
    return true;
  }

  flush() {
    const pcm = new Int16Array(this.accLen);
    for (let i = 0; i < this.accLen; i++) {
      const s = Math.max(-1, Math.min(1, this.acc[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    this.accLen = 0;
  }
}
registerProcessor("pcm-processor", PCMProcessor);
