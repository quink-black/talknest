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
let enrolling = null;      // the voiceprint recording in progress, if any

/* Teacher avatar: decorative, so it is driven entirely from state the page
   already has -- never from anything the server would have to add. */
let avatarOn = false, avatarKind = "idle";
let analyser = null, avatarBuf = null, avatarRaf = 0;
/* Turntable: yaw is the smoothed angle, yawTarget where she is heading.
   Four frames cover 0/90/180/270 degrees. */
let yaw = 0, yawTarget = 0, yawRaf = 0, mouseStillSince = 0;
const REDUCE_MOTION = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Picked in the UI and remembered across reloads. An empty id means auto:
   the microphone follows the browser default, the speaker follows whatever
   device the microphone belongs to. */
const PREF_KEY = { mic: "talknest.mic", spk: "talknest.spk",
                   learner: "talknest.learner", avatar: "talknest.avatar" };
let audioInputs = [], audioOutputs = [];
let preferredMicId = "", preferredSpkId = "";
let appliedSpkId = "";   // null forces applyOutputDevice to try again
let sinkWarned = null;   // only warn once per failed target

let avatarHearing = false;   // the child is mid-utterance, not just on-mic

function setStatus(text, busy) {
  $("status").textContent = STATUS_TEXT[text] || text;
  $("statusDot").className = busy ? "dot busy" : "dot idle";
  avatarKind = (text === "thinking" || text === "preparing") ? "thinking"
             : (text === "listening" || text === "hearing") ? "listening"
             : "idle";
  avatarHearing = text === "hearing";
  refreshAvatar();
}

/* Speaking is not a status the server sends reliably, so it comes from the
   audio itself: playbackActive is on between audio_start and the last chunk
   actually draining. */
function refreshAvatar() {
  const el = $("avatar");
  if (!el) return;
  el.classList.remove("idle", "listening", "thinking", "speaking");
  el.classList.add(playbackActive ? "speaking" : avatarKind);
  el.classList.toggle("hearing", avatarHearing && !playbackActive);
  // Nothing else is guaranteed to run once the last chunk drains, so the
  // transform (bounce, lean) is rederived here rather than cleared.
  if (!playbackActive) applyYaw();
  // Face the child while she talks, and while replying.
  if (playbackActive || avatarHearing) yawTo(0);
}

function yawFrameIndex(a) {
  a = ((a % 360) + 360) % 360;
  return a < 45 || a >= 315 ? 0 : a < 135 ? 1 : a < 225 ? 2 : 3;
}

function applyYaw() {
  const frames = document.querySelectorAll("#avChar .av-frame");
  const on = yawFrameIndex(yaw);
  frames.forEach((f, i) => f.classList.toggle("on", i === on));
  // Four frames switch only at 45-degree boundaries, so between them she
  // leans toward the target: without this, tracking the cursor is
  // invisible until the angle crosses into the next frame.
  const res = ((yaw - on * 90 + 540) % 360) - 180;
  const lean = Math.max(-8, Math.min(8, res * 0.18));
  if (!playbackActive) {
    $("avChar").style.transform =
      Math.abs(lean) > 0.2 ? "rotate(" + lean.toFixed(2) + "deg)" : "";
  }
}

function yawTick() {
  yawRaf = 0;
  const d = yawTarget - yaw;
  if (Math.abs(d) < 0.5) { yaw = yawTarget; applyYaw(); return; }
  yaw += d * 0.08;
  applyYaw();
  yawRaf = requestAnimationFrame(yawTick);
}

/* Aim at the equivalent of target nearest to where she already is, so a
   request to face forward never sweeps the long way round. */
function yawTo(target) {
  target += Math.round((yaw - target) / 360) * 360;
  if (target === yawTarget) return;
  yawTarget = target;
  if (avatarOn && !yawRaf) yawRaf = requestAnimationFrame(yawTick);
}

/* Idle behaviour: wander a little, follow the mouse a little, and every
   so often do a full turn because the back view exists. Nothing here may
   cut into a turn already in progress. */
function yawBusy() { return Math.abs(yawTarget - yaw) > 120; }

function scheduleYawWander() {
  setTimeout(() => {
    if (avatarOn && !REDUCE_MOTION && !playbackActive && !avatarHearing
        && !yawBusy() && performance.now() - mouseStillSince > 2000) {
      yawTo((Math.random() * 2 - 1) * 70);
    }
    scheduleYawWander();
  }, 5000 + Math.random() * 3000);
}

function scheduleYawSpin() {
  setTimeout(() => {
    if (avatarOn && !REDUCE_MOTION && !playbackActive && !avatarHearing
        && !yawBusy() && performance.now() - mouseStillSince > 4000) {
      yawTo(yawTarget + 360 * (Math.random() < 0.5 ? 1 : -1));
    }
    scheduleYawSpin();
  }, 45000 + Math.random() * 40000);
}

/* Measured on resize instead of per event: reading a rect during
   mousemove forces a layout on every move of the pointer. */
let avatarCenterX = 0;
function measureAvatar() {
  const box = $("avatar").getBoundingClientRect();
  avatarCenterX = box.left + box.width / 2;
}
addEventListener("resize", measureAvatar);

document.addEventListener("mousemove", (e) => {
  mouseStillSince = performance.now();
  if (!avatarOn || REDUCE_MOTION || playbackActive || avatarHearing
      || yawBusy()) return;
  // Near her she tracks the cursor properly; further away she just leans
  // with the page.
  const dx = e.clientX - avatarCenterX;
  if (Math.abs(dx) < 400) yawTo(Math.max(-55, Math.min(55, dx / 7)));
  else yawTo((e.clientX / innerWidth - 0.5) * 50);
});

/* A pat on the head: squash and stretch, then a happy full turn. */
$("avatar").addEventListener("click", () => {
  if (!avatarOn) return;
  const el = $("avatar");
  el.classList.remove("boing");
  void el.offsetWidth;   // let the class re-add restart the keyframes
  el.classList.add("boing");
  if (!REDUCE_MOTION && !playbackActive) yawTo(yawTarget + 360);
});
$("avatar").addEventListener("animationend", (e) => {
  if (e.animationName === "av-boing") e.currentTarget.classList.remove("boing");
});

function startAvatarLoop() {
  if (!avatarOn || avatarRaf) return;
  avatarRaf = requestAnimationFrame(avatarTick);
}

function avatarTick() {
  avatarRaf = 0;
  const ch = $("avChar");
  if (!avatarOn || !playbackActive || !analyser || !ch) {
    if (ch) ch.style.transform = "";
    return;
  }
  analyser.getByteTimeDomainData(avatarBuf);
  let sum = 0;
  for (let i = 0; i < avatarBuf.length; i++) {
    const v = (avatarBuf[i] - 128) / 128;
    sum += v * v;
  }
  // Speech sits around 0.05-0.25 RMS. A bitmap has no mouth to move, so
  // she bounces with the volume instead -- the same illusion in coarser
  // form.
  const level = Math.min(1, Math.sqrt(sum / avatarBuf.length) * 5);
  ch.style.transform = "scaleY(" + (1 + level * 0.05).toFixed(3) + ")";
  avatarRaf = requestAnimationFrame(avatarTick);
}

function syncAvatar() {
  const box = $("avatarToggle");
  if (!box) return;
  avatarOn = box.checked;
  $("avatar").hidden = !avatarOn;
  refreshAvatar();
  if (avatarOn) {
    measureAvatar();   // has a box now that it is visible
    if (playbackActive) startAvatarLoop();
  }
}

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    setStatus("listening", false);
    // Learner first: picking one loads the curriculum for whichever mode is
    // current at that moment, and the mode right after reloads it.
    send({ type: "learner", value: $("learner").value });
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
      addBubble("user", msg.text, msg.speaker);
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
    case "nudge":
      addBubble("reply", msg.text);
      break;
    case "audio_start":
      playbackActive = true;
      audioEnded = false;
      playSr = msg.sample_rate || CLIENT_SR;
      refreshAvatar();
      startAvatarLoop();
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
      refreshParent();   // a turn can finish a unit, so the counts moved
      break;
    case "error":
    case "tts_error":
      setStatus("error: " + msg.message, false);
      break;
  }
}

function addBubble(kind, text, who) {
  const div = document.createElement("div");
  div.className = "bubble " + kind;
  if (who && ROLE_LABEL[who]) {
    div.classList.add(who);
    const tag = document.createElement("span");
    tag.className = "who";
    tag.textContent = ROLE_LABEL[who];
    div.appendChild(tag);
  }
  div.appendChild(document.createTextNode(text));
  $("transcript").appendChild(div);
  scrollTranscript();
  return div;
}
function scrollTranscript() {
  const t = $("transcript");
  t.scrollTop = t.scrollHeight;
}

function clearTranscript() {
  $("transcript").innerHTML = "";
  replyBubble = null;
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

/* Before permission is granted a browser may hand back entries with an
   empty deviceId; those cannot be selected or matched. */
const PSEUDO_ID = new Set(["default", "communications"]);

function listed(list) {
  return list.filter((d) => d.deviceId);
}
function usable(list) {
  return list.filter((d) => d.deviceId && !PSEUDO_ID.has(d.deviceId));
}

function loadPref(key) {
  try { return localStorage.getItem(key) || ""; } catch (e) { return ""; }
}
function savePref(key, value) {
  try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); }
  catch (e) { /* private mode: keep the choice for this page only */ }
}

async function refreshDevices() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const pick = (kind) => devices
    .filter((d) => d.kind === kind)
    .map((d) => ({ deviceId: d.deviceId, groupId: d.groupId || "", label: d.label || "" }));
  audioInputs = pick("audioinput");
  audioOutputs = pick("audiooutput");
  lastDevices = devices.map((d) => `${d.kind}:${d.label || "?"}`);
  if (audioOutputs.length) sawOutput = true;
  syncDeviceUi();
  return { inputs: audioInputs.length, outputs: audioOutputs.length };
}

/* Where the capture is really coming from. getSettings reports the device
   the browser actually opened, which is the only truth once a Bluetooth
   headset has taken over. */
function micDeviceInfo() {
  const track = micStream && micStream.getAudioTracks()[0];
  if (!track) return null;
  const s = track.getSettings ? track.getSettings() : {};
  return {
    deviceId: s.deviceId || "",
    groupId: s.groupId || "",
    label: track.label || "",
  };
}

/* A removed device drops out of the list while its track can still look
   live, so the list is the stronger signal - but only once the list is
   real: empty ids would call every device gone. */
function micDeviceGone() {
  const info = micDeviceInfo();
  if (!info || !info.deviceId || !usable(audioInputs).length) return false;
  return !audioInputs.some((d) => d.deviceId === info.deviceId);
}

/* The speaker to use: an explicit choice wins, otherwise the output that
   shares a groupId with the microphone - the other half of the same
   headset. That pairing is what a reconnect has to restore, because the
   AudioContext keeps whatever sink it was created with. */
function resolveOutputDevice() {
  if (preferredSpkId && audioOutputs.some((d) => d.deviceId === preferredSpkId)) {
    return preferredSpkId;
  }
  const info = micDeviceInfo();
  if (info && info.groupId) {
    const pair = usable(audioOutputs)
      .find((d) => d.groupId && d.groupId === info.groupId);
    if (pair) return pair.deviceId;
  }
  return "";   // empty string is the system default sink
}

function currentSinkId() {
  if (!audioCtx) return "";
  return typeof audioCtx.sinkId === "string" ? audioCtx.sinkId : appliedSpkId || "";
}

/* setSinkId reroutes the whole context, so capture and playback stay on one
   clock and the browser's echo cancellation keeps its reference signal.
   Only Chrome implements it; elsewhere playback follows the OS default. */
async function applyOutputDevice() {
  if (!audioCtx || typeof audioCtx.setSinkId !== "function") return;
  const want = resolveOutputDevice();
  if (want === appliedSpkId) return;
  appliedSpkId = want;
  try {
    await audioCtx.setSinkId(want);
    if (audioCtx.state !== "running") await audioCtx.resume();
  } catch (e) {
    // A device that just reappeared can refuse the first attempt, so leave
    // the door open for the next look instead of sticking on the old sink.
    appliedSpkId = null;
    if (sinkWarned !== want) {
      sinkWarned = want;
      console.warn("[talknest] setSinkId failed:", want || "default", e.name, e.message);
    }
  }
  syncDeviceUi();
}

/* ---- device panel ---- */
function deviceName(list, id) {
  if (!id) return "";
  const d = list.find((x) => x.deviceId === id);
  if (!d) return id;
  return d.label || (PSEUDO_ID.has(id) ? "系统默认" : id);
}

function fillSelect(sel, list, chosen, inUse, noun) {
  const opts = [{ value: "", text: inUse ? `自动（当前：${inUse}）` : "自动" }];
  for (const d of listed(list)) {
    const name = d.label || (PSEUDO_ID.has(d.deviceId) ? "系统默认" : noun);
    opts.push({ value: d.deviceId, text: name });
  }
  // Rebuilding an open dropdown would close it, so only touch the DOM when
  // the set of choices really changed.
  const sig = opts.map((o) => `${o.value}=${o.text}`).join("|");
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = "";
    for (const o of opts) {
      const el = document.createElement("option");
      el.value = o.value;
      el.textContent = o.text;
      sel.appendChild(el);
    }
    sel.dataset.sig = sig;
  }
  // A remembered device that is temporarily absent falls back to auto here
  // and comes back on its own once it is listed again.
  sel.value = opts.some((o) => o.value === chosen) ? chosen : "";
}

function syncDeviceUi() {
  const micSel = $("micSelect"), spkSel = $("spkSelect");
  if (!micSel || !spkSel) return;
  const info = micDeviceInfo();
  const inUseMic = info ? (info.label || deviceName(audioInputs, info.deviceId)) : "";
  // No context yet means nothing has been routed; an empty sinkId on a live
  // context means it really is on the system default.
  const sink = currentSinkId();
  const inUseSpk = !audioCtx ? "" : (sink ? deviceName(audioOutputs, sink) : "系统默认");
  fillSelect(micSel, audioInputs, preferredMicId, inUseMic, "麦克风");
  fillSelect(spkSel, audioOutputs, preferredSpkId, inUseSpk, "扬声器");

  const canPick = typeof AudioContext === "function"
    && typeof AudioContext.prototype.setSinkId === "function";
  spkSel.disabled = !canPick || !listed(audioOutputs).length;

  const out = info && sink ? audioOutputs.find((d) => d.deviceId === sink) : null;
  const split = !!(info && out && info.groupId && out.groupId
                   && info.groupId !== out.groupId);
  const hint = $("deviceHint");
  if (!canPick) {
    hint.textContent = "当前浏览器不支持选择扬声器，声音走系统默认输出设备。";
  } else if (split) {
    hint.textContent = `麦克风和扬声器不是同一个设备（${inUseMic || "麦克风"} → `
      + `${inUseSpk || "扬声器"}），回声消除会变弱，助手可能自我打断。`;
  } else {
    hint.textContent = "";
  }
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
  const status = await refreshDevices().catch(() => null);
  const missing = status ? missingDeviceText(status) : "";
  if (missing) {
    if (streaming) pauseForMissingDevice(missing);
    else if (wantStreaming) waitForDevice(missing);
    return;
  }
  // A headset that went away takes its deviceId out of the list, which is
  // a stronger signal than a track that still claims to be live.
  if (micDeviceGone()) {
    if (streaming) pauseForMissingDevice("麦克风");
    else if (wantStreaming) waitForDevice("麦克风", "设备已移除");
    return;
  }
  // The device list can still name a device whose track has died, so the
  // track wins when they disagree.
  if (!micTrackOk()) {
    if (streaming) pauseForMissingDevice("麦克风");
    else if (wantStreaming) waitForDevice("麦克风", "设备未就绪");
    return;
  }
  // Route to whatever speaker belongs to the microphone now.
  await applyOutputDevice();
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
  // Keep a suspended track so an unmute can resume it; a dead one, or one
  // whose device is no longer listed, has to be re-acquired.
  if (!micTrackLive() || micDeviceGone()) teardownMic();
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
  if (micStream && (!micTrackLive() || micDeviceGone())) teardownMic();
  if (!micStream) {
    const audio = {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    };
    // A remembered device that is not plugged in would make the whole call
    // fail with OverconstrainedError, so only pin it while it is listed.
    if (preferredMicId && audioInputs.some((d) => d.deviceId === preferredMicId)) {
      audio.deviceId = { exact: preferredMicId };
    }
    micStream = await navigator.mediaDevices.getUserMedia({ audio });
    for (const t of micStream.getAudioTracks()) {
      // A Bluetooth or USB device can go away without any devicechange
      // event, so the track ending is the reliable signal.
      t.addEventListener("ended", onMicLost);
    }
    syncDeviceUi();   // name the device the browser actually opened
  }
  if (!audioCtx) {
    audioCtx = new AudioContext({ sampleRate: CLIENT_SR });
    audioCtx.addEventListener("sinkchange", syncDeviceUi);
    await audioCtx.audioWorklet.addModule("pcm-worklet.js");
  }
  await audioCtx.resume();
  // Autoplay policy keeps the context suspended until a real gesture.
  if (audioCtx.state !== "running") return false;
  await applyOutputDevice();
  if (!worklet) {
    worklet = new AudioWorkletNode(audioCtx, "pcm-processor");
    worklet.port.onmessage = (e) => {
      // A voiceprint recording keeps the voice off the socket, or the
      // tutor would answer the person who is enrolling.
      if (enrolling) { collectEnroll(e.data); return; }
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
    const status = await refreshDevices().catch(() => null);
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
  // A late reply would be picked up by the mic and end up in the voiceprint.
  if (!audioCtx || enrolling) return;
  const pcm = new Int16Array(buf);
  // The browser resamples a buffer whose rate differs from the context.
  const audioBuf = audioCtx.createBuffer(1, pcm.length, playSr);
  const ch = audioBuf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = audioCtx.createBufferSource();
  src.buffer = audioBuf;
  if (avatarOn && !analyser) {
    // Pass-through node: no gain, no delay, so playback and the echo
    // reference the browser derives from it are both untouched.
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    avatarBuf = new Uint8Array(analyser.fftSize);
    analyser.connect(audioCtx.destination);
  }
  src.connect(avatarOn && analyser ? analyser : audioCtx.destination);
  startAvatarLoop();   // a turn already playing when the avatar is switched on
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
  refreshAvatar();   // the mouth loop stops itself on its next tick
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
$("avatarToggle").addEventListener("change", () => {
  savePref(PREF_KEY.avatar, $("avatarToggle").checked ? "1" : "");
  syncAvatar();
});
$("mode").addEventListener("change", (e) => {
  // The server drops the conversation on a mode switch, so the page has to
  // match it: bubbles the tutor can no longer see would be a lie.
  clearTranscript();
  send({ type: "mode", value: e.target.value });
  refreshParent();
});

$("learner").addEventListener("change", (e) => {
  savePref(PREF_KEY.learner, e.target.value);
  clearTranscript();
  send({ type: "learner", value: e.target.value });
  cancelEnroll();
  refreshParent();
  loadSpeakers();
});

$("unit").addEventListener("change", (e) => {
  send({ type: "unit", value: e.target.value });
  refreshParent();
});

$("micSelect").addEventListener("change", (e) => {
  preferredMicId = e.target.value;
  savePref(PREF_KEY.mic, preferredMicId);
  // The open track is on the old device, so drop it and let the watchdog
  // reopen on the new one.
  if (streaming) stopStreaming();
  teardownMic();
  scheduleRecheck(0);
});

$("spkSelect").addEventListener("change", (e) => {
  preferredSpkId = e.target.value;
  savePref(PREF_KEY.spk, preferredSpkId);
  appliedSpkId = null;   // the same id may still have to be re-applied
  applyOutputDevice();
});

/* Learners are directories of learner state on the server. The unit list
   comes back with how many words of each are left, so the parent's picker
   reads "颜色（2/4）" instead of a bare list of names. */
async function loadLearners() {
  try {
    const r = await fetch("learners");
    if (!r.ok) return;
    const data = await r.json();
    const sel = $("learner");
    const remembered = loadPref(PREF_KEY.learner);
    sel.innerHTML = "";
    for (const name of data.learners) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
    sel.value = data.learners.includes(remembered) ? remembered : data.default;
  } catch (e) { /* offline or old server: keep whatever is there */ }
}

/* The words of the chosen unit and what the learner has done with each.
   "出现" is only a record of what came up in conversation, not a claim
   that the word is known -- deciding that is what the buttons are for. */
async function loadWords() {
  const learner = $("learner").value;
  const unit = $("unit").value;
  if (!learner || !unit) { $("words").innerHTML = ""; return; }
  try {
    const r = await fetch(`learners/${encodeURIComponent(learner)}/progress?mode=`
      + `${encodeURIComponent($("mode").value)}&unit=${encodeURIComponent(unit)}`);
    if (!r.ok) return;
    const data = await r.json();
    if (!data.words.length) { $("words").innerHTML = ""; return; }
    const rows = data.words.map((w) => `
      <tr>
        <td>${w.zh ? esc(w.zh) : "—"}</td>
        <td>${esc(w.lemma)}</td>
        <td>${esc(w.state)}${w.manual ? "（家长）" : ""}</td>
        <td>${w.practices} 次 / ${w.sessions_seen} 次会话</td>
        <td>
          <button data-lemma="${esc(w.lemma)}" data-act="know">已会</button>
          <button data-lemma="${esc(w.lemma)}" data-act="again">重学</button>
          <button data-lemma="${esc(w.lemma)}" data-act="auto">自动</button>
        </td>
      </tr>`).join("");
    $("words").innerHTML = `<table><thead><tr><th>中文</th><th>词</th>`
      + `<th>状态</th><th>出现</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
    for (const b of $("words").querySelectorAll("button")) {
      b.addEventListener("click", () => setWord(b.dataset.lemma, b.dataset.act));
    }
  } catch (e) { /* offline or old server: keep whatever is there */ }
}

async function setWord(lemma, act) {
  const body = act === "auto" ? { release: true } : { known: act === "know" };
  await fetch(`learners/${encodeURIComponent($("learner").value)}/words/`
    + `${encodeURIComponent(lemma)}?mode=${encodeURIComponent($("mode").value)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  refreshParent();
}

function refreshParent() {
  loadUnits().finally(loadWords);
}

async function loadUnits() {
  const learner = $("learner").value;
  if (!learner) return;
  try {
    const r = await fetch(`learners/${encodeURIComponent(learner)}/units?mode=`
      + encodeURIComponent($("mode").value));
    if (!r.ok) return;
    const data = await r.json();
    const sel = $("unit");
    sel.innerHTML = "";
    const auto = document.createElement("option");
    auto.value = "";
    auto.textContent = "自动（按顺序往下走）";
    sel.appendChild(auto);
    for (const u of data.units) {
      const opt = document.createElement("option");
      opt.value = u.id;
      opt.textContent = `${u.title}（${u.words - u.left}/${u.words}）`;
      sel.appendChild(opt);
    }
    sel.value = data.current;
    const current = data.units.find((u) => u.id === data.current);
    $("unitHint").textContent = current
      ? `当前：${current.title}，还剩 ${current.left} 个新词`
      : "当前：自动，取第一个还有新词的单元";
  } catch (e) { /* offline or old server: keep whatever is there */ }
}

/* ---- voiceprint enrollment ----
   Each family member records a few clips; the server keeps only the
   voiceprint. Recording reuses the capture worklet, so the clip is exactly
   what the server's VAD would otherwise have heard. */
const ROLE_LABEL = { child: "孩子", dad: "爸爸", mom: "妈妈" };
const VP_MAX_MS = 8000;       // a clip stops by itself after this long
const VP_GOAL_S = 15;         // enough voice for a stable voiceprint
const VP_PROMPT = "请像平时一样说话，比如：“Hello! 今天天气真好，我想吃苹果，"
  + "还想玩一会儿积木。”";

function vpMessage(text) { $("vpMessage").textContent = text; }

function vpRow(role) { return document.querySelector(`.vprow[data-role="${role}"]`); }

function renderSpeakers(status) {
  for (const role of Object.keys(ROLE_LABEL)) {
    const row = vpRow(role);
    const info = status.roles[role];
    const recording = enrolling && enrolling.role === role;
    let text = "未录入";
    if (info.samples) {
      text = `${info.samples} 段，共 ${info.seconds} 秒`
        + (info.seconds >= VP_GOAL_S ? "，已录入" : "，建议再录一段");
    }
    row.querySelector(".vpinfo").textContent = text;
    const rec = row.querySelector(".vpRec");
    rec.textContent = recording ? "停止" : "录一段";
    rec.disabled = !status.ready || (!!enrolling && !recording);
    row.querySelector(".vpClear").disabled = !!enrolling || !info.samples;
  }
  $("vpState").textContent = !status.ready
    ? "服务端没有声纹模型，无法录入"
    : status.enabled
      ? "识别已启用：对话里会标出是谁在说话"
      : "孩子和至少一位家长都录入后才会启用识别";
}

async function loadSpeakers() {
  const learner = $("learner").value;
  if (!learner) return;
  try {
    const r = await fetch(`learners/${encodeURIComponent(learner)}/speakers`);
    if (!r.ok) return;
    lastSpeakers = await r.json();
    renderSpeakers(lastSpeakers);
  } catch (e) { /* offline or old server: leave the panel as it is */ }
}
let lastSpeakers = null;

async function startEnroll(role) {
  if (enrolling) return;
  let ready = false;
  try { ready = await startMic(); } catch (e) {
    vpMessage("麦克风不可用：" + e.name);
    return;
  }
  if (!ready) { vpMessage("请先点页面上的“点我开始对话”，允许使用麦克风"); return; }
  // The mic hears the speaker: whatever the tutor is saying has to stop.
  if (streaming) send({ type: "interrupt" });
  stopPlayback();
  reportPlaybackEnded();
  enrolling = { role, chunks: [], samples: 0, startedAt: performance.now(), timer: 0 };
  enrolling.timer = setInterval(tickEnroll, 200);
  vpMessage(VP_PROMPT);
  if (lastSpeakers) renderSpeakers(lastSpeakers);
}

function collectEnroll(buf) {
  const pcm = new Int16Array(buf);
  enrolling.chunks.push(pcm);
  enrolling.samples += pcm.length;
}

function tickEnroll() {
  if (!enrolling) return;
  const ms = performance.now() - enrolling.startedAt;
  vpMessage(`录音中 ${(ms / 1000).toFixed(1)} / ${VP_MAX_MS / 1000} 秒。` + VP_PROMPT);
  if (ms >= VP_MAX_MS) finishEnroll();
}

function cancelEnroll() {
  if (!enrolling) return;
  clearInterval(enrolling.timer);
  enrolling = null;
  vpMessage("");
  if (lastSpeakers) renderSpeakers(lastSpeakers);
}

async function finishEnroll() {
  if (!enrolling) return;
  const { role, chunks, samples, timer } = enrolling;
  clearInterval(timer);
  enrolling = null;
  const pcm = new Int16Array(samples);
  let at = 0;
  for (const c of chunks) { pcm.set(c, at); at += c.length; }
  vpMessage("正在处理录音…");
  if (lastSpeakers) renderSpeakers(lastSpeakers);
  const learner = $("learner").value;
  try {
    const r = await fetch(
      `learners/${encodeURIComponent(learner)}/speakers/${role}`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" },
        body: pcm.buffer });
    const data = await r.json();
    if (!r.ok) { vpMessage(data.error || "录入失败"); return; }
    vpMessage(`${ROLE_LABEL[role]}：这段有效语音 ${data.seconds} 秒，已保存`);
    lastSpeakers = data;
  } catch (e) {
    vpMessage("录入失败：网络或服务端错误");
  } finally {
    if (lastSpeakers) renderSpeakers(lastSpeakers);
    loadSpeakers();
  }
}

async function clearSpeaker(role) {
  const learner = $("learner").value;
  if (!confirm(`清除${ROLE_LABEL[role]}的全部声纹？`)) return;
  try {
    const r = await fetch(
      `learners/${encodeURIComponent(learner)}/speakers/${role}`, { method: "DELETE" });
    if (r.ok) { lastSpeakers = await r.json(); renderSpeakers(lastSpeakers); vpMessage(""); }
  } catch (e) { vpMessage("清除失败：网络或服务端错误"); }
}

for (const row of document.querySelectorAll(".vprow")) {
  const role = row.dataset.role;
  row.querySelector(".vpRec").addEventListener("click", () => {
    if (enrolling && enrolling.role === role) finishEnroll();
    else startEnroll(role);
  });
  row.querySelector(".vpClear").addEventListener("click", () => clearSpeaker(role));
}

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

preferredMicId = loadPref(PREF_KEY.mic);
preferredSpkId = loadPref(PREF_KEY.spk);
$("avatarToggle").checked = !!loadPref(PREF_KEY.avatar);
syncAvatar();
scheduleYawWander();
scheduleYawSpin();

loadModes().then(loadLearners).then(loadUnits).then(loadWords)
  .then(loadSpeakers).finally(connect);
checkSecureContext();
syncDeviceUi();
scheduleRecheck(WATCHDOG_MS);  // start the periodic look
