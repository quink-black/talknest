/* Talknest free-talk client.

The microphone runs from page load until it is paused: PCM goes up
continuously, the server cuts the stream into turns with a streaming VAD
and pushes a reply back. Playback shares the capture AudioContext so the
browser's echo cancellation has the assistant's voice as a reference. */
"use strict";

const $ = (id) => document.getElementById(id);
const CLIENT_SR = 24000;  // Qwen3-ASR native rate

const STATUS_TEXT = {
  listening: "直接说话就行",
  hearing: "我在听…",
  thinking: "让我想想…",
  speaking: "我在说…",
};

let ws = null;
let audioCtx = null, micStream = null, worklet = null;
let streaming = false;
let playNextAt = 0, sources = [];
let playbackActive = false, audioEnded = false, playSr = CLIENT_SR;
let replyBubble = null;

function setStatus(text, busy) {
  $("status").textContent = STATUS_TEXT[text] || text;
  $("statusDot").className = busy ? "dot busy" : "dot idle";
}

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    setStatus("listening", false);
    send({ type: "mode", value: $("mode").value });
    startSession();
  };
  ws.onclose = () => {
    setStatus("disconnected", false);
    stopPlayback();
    reportPlaybackEnded();
    const retry = streaming;
    streaming = false;
    if (retry) setTimeout(connect, 3000);
  };
  ws.onerror = () => setStatus("error", false);
  ws.onmessage = onMessage;
}

async function onMessage(ev) {
  if (ev.data instanceof ArrayBuffer) { playChunk(ev.data); return; }
  const msg = JSON.parse(ev.data);
  switch (msg.type) {
    case "mode":
      $("mode").value = msg.value;
      break;
    case "state":
      setStatus(msg.value, msg.value !== "listening");
      break;
    case "user_text":
      addBubble("user", msg.text);
      replyBubble = addBubble("reply", "");
      break;
    case "reply_delta":
      if (replyBubble) replyBubble.textContent += msg.text;
      scrollTranscript();
      break;
    case "audio_start":
      playbackActive = true;
      audioEnded = false;
      playSr = msg.sample_rate || CLIENT_SR;
      break;
    case "audio_end":
      audioEnded = true;
      maybePlaybackEnded();
      break;
    case "stop_playback":
      stopPlayback();
      reportPlaybackEnded();
      if (msg.reason === "manual") setStatus("listening", false);
      break;
    case "interrupted":
      stopPlayback();
      reportPlaybackEnded();
      break;
    case "reply_done":
      showMarks(msg.marks);
      setStatus("listening", false);
      replyBubble = null;
      break;
    case "error":
    case "tts_error":
      setStatus("error: " + msg.message, false);
      break;
  }
}

function addBubble(kind, text) {
  const div = document.createElement("div");
  div.className = "bubble " + kind;
  div.textContent = text;
  $("transcript").appendChild(div);
  scrollTranscript();
  return div;
}
function scrollTranscript() {
  const t = $("transcript");
  t.scrollTop = t.scrollHeight;
}

function showMarks(marks) {
  if (!marks || !Object.keys(marks).length) return;
  const row = document.createElement("tr");
  row.innerHTML = ["asr_done", "tts_first_audio"]
    .map((k) => `<td>${marks[k] != null ? marks[k] + " ms" : "—"}</td>`).join("");
  $("marksBody").appendChild(row);
}

/* ---- capture ---- */
function mediaAvailable() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

function showBanner(html) {
  const b = $("banner");
  b.innerHTML = html;
  b.style.display = "block";
}

function checkSecureContext() {
  if (mediaAvailable()) return;
  showBanner(
    "Microphone unavailable: this page is not in a secure context " +
    "(plain HTTP over LAN). Two fixes:<br>" +
    "1. Chrome flag: open <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>, " +
    "add <code>" + location.origin + "</code>, enable and relaunch.<br>" +
    "2. Or serve over TLS: run <code>scripts/gen_dev_cert.sh</code>, then " +
    "<code>TLS_CERT=certs/dev.pem TLS_KEY=certs/dev.key scripts/run_server.sh</code> " +
    "and reload via https://" + location.hostname + ":8890 (accept the warning)."
  );
  setStatus("mic blocked (insecure context)", false);
}

function showStartBtn(message) {
  const b = $("startBtn");
  b.textContent = message || "点我开始对话";
  b.style.display = "block";
}

async function startMic() {
  if (!mediaAvailable()) {
    checkSecureContext();
    return false;
  }
  if (!micStream) {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  }
  if (!audioCtx) {
    audioCtx = new AudioContext({ sampleRate: CLIENT_SR });
    await audioCtx.audioWorklet.addModule("pcm-worklet.js");
  }
  await audioCtx.resume();
  // Autoplay policy keeps the context suspended until a real gesture.
  if (audioCtx.state !== "running") return false;
  if (!worklet) {
    worklet = new AudioWorkletNode(audioCtx, "pcm-processor");
    worklet.port.onmessage = (e) => {
      if (streaming && ws && ws.readyState === 1) ws.send(e.data);
    };
    audioCtx.createMediaStreamSource(micStream).connect(worklet);
    // worklet is a sink; do not connect to destination (avoids feedback)
  }
  return true;
}

async function startSession() {
  if (streaming) return;
  let ready = false;
  try {
    ready = await startMic();
  } catch (e) {
    showStartBtn("麦克风不可用（" + e.name + "），点我重试");
    return;
  }
  if (!ready) {
    showStartBtn();
    return;
  }
  $("startBtn").style.display = "none";
  streaming = true;
  $("pauseBtn").textContent = "暂停";
  send({ type: "start" });
  setStatus("listening", false);
}

function pauseSession() {
  if (!streaming) return;
  streaming = false;
  $("pauseBtn").textContent = "继续";
  send({ type: "stop" });
  stopPlayback();
  reportPlaybackEnded();
  setStatus("已暂停", false);
}

/* ---- playback ---- */
function playChunk(buf) {
  if (!audioCtx) return;
  const pcm = new Int16Array(buf);
  // The browser resamples a buffer whose rate differs from the context.
  const audioBuf = audioCtx.createBuffer(1, pcm.length, playSr);
  const ch = audioBuf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = audioCtx.createBufferSource();
  src.buffer = audioBuf;
  src.connect(audioCtx.destination);
  const now = audioCtx.currentTime;
  if (playNextAt < now) playNextAt = now + 0.05;
  src.start(playNextAt);
  playNextAt += audioBuf.duration;
  sources.push(src);
  src.onended = () => {
    sources = sources.filter((s) => s !== src);
    maybePlaybackEnded();
  };
}

function stopPlayback() {
  for (const s of sources) {
    s.onended = null;
    s.stop();
  }
  sources = [];
  playNextAt = 0;
}

function maybePlaybackEnded() {
  if (audioEnded && sources.length === 0) reportPlaybackEnded();
}

function reportPlaybackEnded() {
  if (!playbackActive) return;
  playbackActive = false;
  audioEnded = false;
  send({ type: "playback", value: "ended" });
}

/* ---- controls ---- */
$("interruptBtn").addEventListener("click", () => send({ type: "interrupt" }));
$("startBtn").addEventListener("click", startSession);
$("pauseBtn").addEventListener("click", () => {
  if (streaming) pauseSession();
  else startSession();
});
$("mode").addEventListener("change", (e) => send({ type: "mode", value: e.target.value }));

/* Modes are free-form in the server config; the dropdown is built from
   GET /modes so a new age group there shows up without touching this
   page. The static options in index.html are only a fetch fallback. */
async function loadModes() {
  try {
    const r = await fetch("modes");
    if (!r.ok) return;
    const data = await r.json();
    const sel = $("mode");
    sel.innerHTML = "";
    for (const m of data.modes) {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.label;
      sel.appendChild(opt);
    }
    sel.value = data.default;
  } catch (e) { /* offline or old server: keep the static options */ }
}

loadModes().finally(connect);
checkSecureContext();
