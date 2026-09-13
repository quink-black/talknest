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
  preparing: "马上就说…",
  speaking: "我在说…",
};

let ws = null;
let audioCtx = null, micStream = null, micSource = null, worklet = null;
let streaming = false;      // capture is running right now
let wantStreaming = true;   // what the user asked for, survives device loss
let deviceProblem = "";     // non-empty while we are paused waiting for a device
let blocked = false;        // only a user action can clear this (permission, gesture)
let recheckTimer = null, checking = false, starting = false;
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
    streaming = false;
    // Reconnect on intent, not on the capture flag: a session paused for a
    // missing device still wants its socket back.
    if (wantStreaming) setTimeout(connect, 3000);
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
      // A paused session is not listening: the server still reports the
      // state it fell back to after the stop, which would overwrite the
      // reason we are paused.
      if (streaming) setStatus(msg.value, msg.value !== "listening");
      break;
    case "user_text":
      addBubble("user", msg.text);
      // Text takes a moment to arrive while audio takes longer still, so
      // the empty bubble has to look alive or the page reads as broken.
      replyBubble = addBubble("reply", "");
      replyBubble.classList.add("typing");
      break;
    case "reply_delta":
      if (replyBubble) {
        replyBubble.classList.remove("typing");
        replyBubble.textContent += msg.text;
      }
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
      // No further deltas are coming for this turn, so the placeholder
      // must not keep blinking under whatever text did arrive.
      if (replyBubble) { replyBubble.classList.remove("typing"); replyBubble = null; }
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
  row.innerHTML = ["asr_done", "llm_first_sentence", "tts_first_audio"]
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

function hideBanner() {
  $("banner").style.display = "none";
}

/* ---- device presence ----

A Bluetooth headset leaves and returns as a unit: the mic track ends and
the device list changes, more than once while the link settles. No single
signal is dependable on its own - some removals never end the track, and
some reconnects never fire devicechange - so every signal (track ended,
devicechange, a periodic look) funnels into one debounced check that
compares what is plugged in against what the session wants to do. */
const RECHECK_DELAY_MS = 800;  // let a flapping device list settle
const WATCHDOG_MS = 3000;      // periodic look, both directions

let lastDevices = [];
let sawOutput = false;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function deviceStatus() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  lastDevices = devices.map((d) => `${d.kind}:${d.label || "?"}`);
  let inputs = 0, outputs = 0;
  for (const d of devices) {
    if (d.kind === "audioinput") inputs++;
    else if (d.kind === "audiooutput") outputs++;
  }
  if (outputs > 0) sawOutput = true;
  return { inputs, outputs };
}

/* Only a machine that has shown an output device can lose one: a host
   with no sound card at all would otherwise never start, even though
   capture and transcription work fine without playback. */
function missingDeviceText(s) {
  if (!s.inputs) return sawOutput && !s.outputs ? "麦克风和扬声器" : "麦克风";
  if (sawOutput && !s.outputs) return "扬声器";
  return "";
}

function trackState() {
  if (!micStream) return "no stream";
  return micStream.getAudioTracks()
    .map((t) => `${t.label || "?"}/${t.readyState}${t.muted ? "/muted" : ""}`)
    .join(", ");
}

/* A track that is still live but muted delivers silence, which is what a
   suspended Bluetooth link looks like. No stream yet is not a failure:
   opening one is the test. */
function micTrackOk() {
  if (!micStream) return true;
  const tracks = micStream.getAudioTracks();
  return tracks.length > 0
    && tracks.every((t) => t.readyState === "live" && !t.muted);
}

function micTrackLive() {
  if (!micStream) return false;
  const tracks = micStream.getAudioTracks();
  return tracks.length > 0 && tracks.every((t) => t.readyState === "live");
}

function scheduleRecheck(delay = RECHECK_DELAY_MS) {
  clearTimeout(recheckTimer);
  recheckTimer = setTimeout(tick, delay);
}

async function tick() {
  if (checking) { scheduleRecheck(WATCHDOG_MS); return; }
  checking = true;
  try {
    await checkDevices();
  } finally {
    checking = false;
    // Keep looking as long as the session is meant to run: a device can
    // leave or arrive without ever raising an event.
    if (wantStreaming) scheduleRecheck(WATCHDOG_MS);
  }
}

async function checkDevices() {
  if (!mediaAvailable() || !ws || ws.readyState !== 1) return;
  // A null status means enumeration is unavailable here; the track state
  // and getUserMedia below are then the only tests left.
  const status = await deviceStatus().catch(() => null);
  const missing = status ? missingDeviceText(status) : "";
  if (missing) {
    if (streaming) pauseForMissingDevice(missing);
    else if (wantStreaming) waitForDevice(missing);
    return;
  }
  // The device list can still name a device whose track has died, so the
  // track wins when they disagree.
  if (!micTrackOk()) {
    if (streaming) pauseForMissingDevice("麦克风");
    else if (wantStreaming) waitForDevice("麦克风", "设备未就绪");
    return;
  }
  // The device came back. A denied permission or a suspended AudioContext
  // needs a click instead, so the watchdog must not retry those.
  if (wantStreaming && !streaming && !blocked) await startSession();
}

function waitForDevice(missing, detail) {
  const problem = detail ? `${missing}（${detail}）` : missing;
  if (deviceProblem === problem) return;  // already on screen
  deviceProblem = problem;
  console.warn("[talknest] paused:", problem, "| devices:",
               lastDevices.join(", ") || "none", "| track:", trackState());
  $("startBtn").style.display = "none";
  if (!streaming) $("pauseBtn").textContent = "继续";
  setStatus("已暂停：等待" + problem, false);
  showBanner(
    "对话已暂停：" + esc(problem) + "。设备可用后会自动继续，也可以点“继续”重试。" +
    "<br><span class='hint'>浏览器看到的设备：" +
    esc(lastDevices.join("、") || "无") + "</span>"
  );
}

function pauseForMissingDevice(missing) {
  stopStreaming();
  // Keep a suspended track so an unmute can resume it; a dead one has to
  // be re-acquired.
  if (!micTrackLive()) teardownMic();
  waitForDevice(missing);
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
  if (micStream && !micTrackLive()) teardownMic();  // cannot be reused
  if (!micStream) {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    for (const t of micStream.getAudioTracks()) {
      // A Bluetooth or USB device can go away without any devicechange
      // event, so the track ending is the reliable signal.
      t.addEventListener("ended", onMicLost);
    }
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
    micSource = audioCtx.createMediaStreamSource(micStream);
    micSource.connect(worklet);
    // worklet is a sink; do not connect to destination (avoids feedback)
  }
  return true;
}

function teardownMic() {
  if (worklet) { worklet.port.onmessage = null; worklet.disconnect(); worklet = null; }
  if (micSource) { micSource.disconnect(); micSource = null; }
  if (micStream) {
    for (const t of micStream.getTracks()) t.stop();
    micStream = null;
  }
}

function onMicLost() {
  if (streaming) pauseForMissingDevice("麦克风");  // tears down the dead track
  else if (wantStreaming) waitForDevice("麦克风", "已断开");
  scheduleRecheck();  // re-read the device list once it has settled
}

async function startSession() {
  if (streaming || starting) return;
  starting = true;
  try {
    wantStreaming = true;
    // Nothing to send to: the socket was closed while paused, so no
    // onclose retry is pending. Reconnect and let onopen start us. While
    // a reconnect is in flight, onopen is what starts us.
    if (!ws || ws.readyState > 1) { connect(); return; }
    if (ws.readyState !== 1) return;
    if (!mediaAvailable()) { checkSecureContext(); return; }
    const status = await deviceStatus().catch(() => null);
    const missing = status ? missingDeviceText(status) : "";
    if (missing) { waitForDevice(missing); return; }
    let ready = false;
    try {
      ready = await startMic();
    } catch (e) {
      console.warn("[talknest] getUserMedia failed:", e.name, e.message);
      if (e.name === "NotAllowedError" || e.name === "SecurityError") {
        showStartBtn("麦克风权限被拒绝（" + e.name + "），点我重试");
        blocked = true;   // only a user action can clear a denial
        return;
      }
      // A device can be listed and still refuse to open (just unplugged,
      // busy, or mid-switch), so this is a wait rather than a hard error.
      waitForDevice("麦克风", e.name);
      return;
    }
    if (!ready) {
      // Autoplay policy: the context stays suspended until a real gesture.
      blocked = true;
      deviceProblem = "";
      hideBanner();
      showStartBtn();
      return;
    }
    if (!micTrackOk()) { waitForDevice("麦克风", "设备未就绪"); return; }
    deviceProblem = "";
    hideBanner();
    $("startBtn").style.display = "none";
    streaming = true;
    $("pauseBtn").textContent = "暂停";
    send({ type: "start" });
    setStatus("listening", false);
  } finally {
    starting = false;
    // Arm the periodic look here too: this is the path a click takes, and
    // a paused-then-resumed session has no tick left to reschedule it.
    if (wantStreaming) scheduleRecheck(WATCHDOG_MS);
  }
}

/* Leaves the turn but keeps the socket, so resuming does not lose history. */
function stopStreaming() {
  streaming = false;
  $("pauseBtn").textContent = "继续";
  send({ type: "stop" });   // ends the turn the server may be processing
  stopPlayback();
  reportPlaybackEnded();    // the server waits for this before listening again
}

function pauseSession() {
  wantStreaming = false;    // a manual pause is never auto-resumed
  deviceProblem = "";
  hideBanner();
  if (!streaming) return;
  stopStreaming();
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
$("startBtn").addEventListener("click", () => { blocked = false; startSession(); });
$("pauseBtn").addEventListener("click", () => {
  blocked = false;   // a click is the only thing that can unblock us
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

if (mediaAvailable()) {
  navigator.mediaDevices.addEventListener("devicechange", () => scheduleRecheck());
}

loadModes().finally(connect);
checkSecureContext();
scheduleRecheck(WATCHDOG_MS);  // start the periodic look
