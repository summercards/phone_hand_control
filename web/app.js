import { FilesetResolver, HandLandmarker } from "./vendor/mediapipe/vision_bundle.mjs";

const $ = (id) => document.getElementById(id);
const elements = {
  video: $("camera"),
  overlay: $("overlay"),
  stage: $("stage"),
  placeholder: $("camera-placeholder"),
  start: $("start-button"),
  stop: $("stop-button"),
  connectionText: $("connection-text"),
  connectionDot: $("connection-dot"),
  trackingBadge: $("tracking-badge"),
  inference: $("metric-inference"),
  fps: $("metric-fps"),
  rtt: $("metric-rtt"),
  sent: $("metric-sent"),
  diagnostics: $("diagnostics"),
  cameraSelect: $("camera-select"),
  mirror: $("mirror-input"),
  swapHands: $("swap-hands-input"),
  delegate: $("delegate-select"),
  sendFps: $("send-fps-select"),
  confidence: $("confidence-input"),
  confidenceOutput: $("confidence-output"),
};

const WS_MAGIC = [0x50, 0x48, 0x43, 0x57];
const PROTOCOL_VERSION = 1;
const WS_HEADER_BYTES = 18;
const WS_HAND_BYTES = 510;
const MAX_HANDS = 2;
const HAND_CONNECTIONS = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16],
  [13, 17], [17, 18], [18, 19], [19, 20],
  [0, 17],
];

const APP_VERSION = "20260913b";
if (localStorage.getItem("phc.version") !== APP_VERSION) {
  localStorage.setItem("phc.version", APP_VERSION);
  localStorage.setItem("phc.delegate", "CPU");
  localStorage.setItem("phc.confidence", "0.5");
}

const settings = {
  mirror: localStorage.getItem("phc.mirror") !== "false",
  swapHands: localStorage.getItem("phc.swapHands") === "true",
  delegate: localStorage.getItem("phc.delegate") || "CPU",
  sendFps: Number(localStorage.getItem("phc.sendFps") || 60),
  confidence: Number(localStorage.getItem("phc.confidence") || 0.5),
  cameraId: localStorage.getItem("phc.cameraId") || "",
};

const state = {
  stream: null,
  landmarker: null,
  running: false,
  ws: null,
  wsConnected: false,
  reconnectTimer: null,
  reconnectDelay: 350,
  sequence: 0,
  sendCount: 0,
  sendWindowStart: performance.now(),
  lastSendMs: 0,
  dropped: 0,
  lastInferenceMs: 0,
  inferenceEma: 0,
  trackingFpsEma: 0,
  lastProcessedAt: 0,
  lastVideoTime: -1,
  rttMs: 0,
  handCount: 0,
  lastResult: null,
  token: new URLSearchParams(location.search).get("token") || "",
  lastTelemetryAt: 0,
  modelBuffer: null,
  modelLoadPromise: null,
};

function setConnection(mode, text) {
  elements.connectionDot.className = `status-dot ${mode}`;
  elements.connectionText.textContent = text;
}

function applySettingsToUi() {
  elements.mirror.checked = settings.mirror;
  elements.swapHands.checked = settings.swapHands;
  elements.delegate.value = settings.delegate;
  elements.sendFps.value = String(settings.sendFps);
  elements.confidence.value = String(settings.confidence);
  elements.confidenceOutput.value = settings.confidence.toFixed(2);
  elements.stage.classList.toggle("mirrored", settings.mirror);
}

function persistSettings() {
  localStorage.setItem("phc.mirror", String(settings.mirror));
  localStorage.setItem("phc.swapHands", String(settings.swapHands));
  localStorage.setItem("phc.delegate", settings.delegate);
  localStorage.setItem("phc.sendFps", String(settings.sendFps));
  localStorage.setItem("phc.confidence", String(settings.confidence));
  localStorage.setItem("phc.cameraId", settings.cameraId);
}

function connectWebSocket() {
  if (!state.token) {
    setConnection("offline", "缺少会话令牌，请从二维码或启动窗口提供的链接进入");
    return;
  }
  if (state.ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.ws.readyState)) return;
  setConnection("connecting", "正在连接 Blender 桥接器...");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  state.ws = ws;

  ws.addEventListener("open", () => {
    state.wsConnected = true;
    state.reconnectDelay = 350;
    setConnection("online", `已连接，${state.handCount ? "正在发送手部数据" : "等待手部"}`);
  });
  ws.addEventListener("message", (event) => {
    if (typeof event.data !== "string") return;
    try {
      const message = JSON.parse(event.data);
      if (message.type === "pong" && Number.isFinite(message.client_t)) {
        const rtt = performance.now() - message.client_t;
        state.rttMs = state.rttMs ? state.rttMs * 0.8 + rtt * 0.2 : rtt;
      }
    } catch (_) {}
  });
  ws.addEventListener("close", () => {
    state.wsConnected = false;
    state.ws = null;
    if (state.running) {
      setConnection("connecting", "连接中断，正在自动重连...");
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(connectWebSocket, state.reconnectDelay);
      state.reconnectDelay = Math.min(3000, state.reconnectDelay * 1.7);
    } else {
      setConnection("offline", "桥接器未连接");
    }
  });
  ws.addEventListener("error", () => ws.close());
}

function sendJson(value) {
  const ws = state.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(value));
}

async function refreshCameras() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const cameras = devices.filter((device) => device.kind === "videoinput");
    elements.cameraSelect.replaceChildren(new Option("默认前置摄像头", ""));
    for (const [index, camera] of cameras.entries()) {
      const label = camera.label || `摄像头 ${index + 1}`;
      elements.cameraSelect.add(new Option(label, camera.deviceId));
    }
    if ([...elements.cameraSelect.options].some((option) => option.value === settings.cameraId)) {
      elements.cameraSelect.value = settings.cameraId;
    }
  } catch (error) {
    console.warn("enumerateDevices failed", error);
  }
}

function withTimeout(promise, milliseconds, label) {
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(() => reject(new Error(`${label} 超时（${milliseconds} ms）`)), milliseconds);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timeoutId));
}

async function loadModelBuffer() {
  if (state.modelBuffer) return state.modelBuffer;
  if (state.modelLoadPromise) return state.modelLoadPromise;
  state.modelLoadPromise = (async () => {
    const response = await fetch("./vendor/mediapipe/hand_landmarker.task", { cache: "force-cache" });
    if (!response.ok) throw new Error(`手部模型下载失败：HTTP ${response.status}`);
    const total = Number(response.headers.get("Content-Length") || 0);
    if (!response.body) {
      const buffer = new Uint8Array(await response.arrayBuffer());
      state.modelBuffer = buffer;
      return buffer;
    }
    const reader = response.body.getReader();
    const chunks = [];
    let received = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      if (total) {
        setConnection("connecting", `正在下载手部模型 ${Math.round(received / total * 100)}%`);
      }
    }
    const buffer = new Uint8Array(received);
    let offset = 0;
    for (const chunk of chunks) {
      buffer.set(chunk, offset);
      offset += chunk.length;
    }
    state.modelBuffer = buffer;
    return buffer;
  })().catch((error) => {
    state.modelLoadPromise = null;
    throw error;
  });
  return state.modelLoadPromise;
}

function handLandmarkerOptions(vision, delegate) {
  return {
    baseOptions: {
      modelAssetBuffer: state.modelBuffer,
      delegate,
    },
    runningMode: "VIDEO",
    numHands: MAX_HANDS,
    minHandDetectionConfidence: settings.confidence,
    minHandPresenceConfidence: settings.confidence,
    minTrackingConfidence: settings.confidence,
  };
}

async function createLandmarker() {
  if (state.landmarker) {
    state.landmarker.close();
    state.landmarker = null;
  }
  setConnection("connecting", "正在加载 MediaPipe WASM...");
  const vision = await withTimeout(
    FilesetResolver.forVisionTasks("./vendor/mediapipe/wasm"),
    15000,
    "MediaPipe WASM 加载",
  );
  await loadModelBuffer();
  const preferred = settings.delegate === "CPU" ? "CPU" : "GPU";
  setConnection("connecting", `正在初始化手部模型（${preferred}）...`);
  try {
    state.landmarker = await withTimeout(
      HandLandmarker.createFromOptions(vision, handLandmarkerOptions(vision, preferred)),
      preferred === "GPU" ? 18000 : 25000,
      `${preferred} 手部模型初始化`,
    );
  } catch (error) {
    console.warn(`${preferred} delegate failed or timed out, retrying CPU`, error);
    if (state.landmarker) {
      try { state.landmarker.close(); } catch (_) {}
      state.landmarker = null;
    }
    if (preferred === "CPU") throw error;
    setConnection("connecting", "GPU 初始化失败，正在回退 CPU...");
    state.landmarker = await withTimeout(
      HandLandmarker.createFromOptions(vision, handLandmarkerOptions(vision, "CPU")),
      25000,
      "CPU 手部模型初始化",
    );
    settings.delegate = "CPU";
    elements.delegate.value = "CPU";
    persistSettings();
  }
  setConnection("connecting", "手部模型已加载，正在请求摄像头...");
}

async function startCamera() {
  if (!window.isSecureContext) {
    throw new Error("当前页面不是可信 HTTPS。请先在手机安装项目提供的本地 CA 证书。");
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("此浏览器不支持 getUserMedia，请使用最新版 Chrome、Edge 或 Safari。");
  }
  elements.start.disabled = true;
  setConnection("connecting", "正在加载手部模型...");
  await createLandmarker();

  const videoConstraints = {
    width: { ideal: 1280 },
    height: { ideal: 720 },
    frameRate: { ideal: 60, max: 60 },
    facingMode: { ideal: "user" },
  };
  if (settings.cameraId) {
    videoConstraints.deviceId = { exact: settings.cameraId };
    delete videoConstraints.facingMode;
  }
  state.stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: videoConstraints,
  });
  elements.video.srcObject = state.stream;
  await elements.video.play();
  await refreshCameras();
  elements.placeholder.classList.add("hidden");
  elements.stop.disabled = false;
  state.running = true;
  state.lastVideoTime = -1;
  connectWebSocket();
  scheduleVideoFrame();
  updateDiagnostics("摄像头已启动，等待手部进入画面");
}

function scheduleVideoFrame() {
  if (!state.running) return;
  if ("requestVideoFrameCallback" in elements.video) {
    elements.video.requestVideoFrameCallback(onVideoFrame);
  } else {
    requestAnimationFrame(onVideoFrame);
  }
}

function onVideoFrame(now) {
  if (!state.running) return;
  if (elements.video.currentTime !== state.lastVideoTime) {
    state.lastVideoTime = elements.video.currentTime;
    processFrame(now);
  }
  scheduleVideoFrame();
}

function processFrame(now) {
  if (!state.landmarker || elements.video.readyState < 2) return;
  const inferenceStarted = performance.now();
  let result;
  try {
    result = state.landmarker.detectForVideo(elements.video, now);
  } catch (error) {
    updateDiagnostics(`推理错误: ${error?.message || error}`);
    return;
  }
  const inferenceMs = performance.now() - inferenceStarted;
  state.inferenceEma = state.inferenceEma ? state.inferenceEma * 0.85 + inferenceMs * 0.15 : inferenceMs;
  state.lastInferenceMs = inferenceMs;
  const delta = now - state.lastProcessedAt;
  if (state.lastProcessedAt && delta > 0) {
    const instantFps = 1000 / delta;
    state.trackingFpsEma = state.trackingFpsEma ? state.trackingFpsEma * 0.9 + instantFps * 0.1 : instantFps;
  }
  state.lastProcessedAt = now;
  state.lastResult = result;
  state.handCount = result?.landmarks?.length || 0;
  drawOverlay(result);
  updateTrackingBadge(result);
  sendPose(result, now);
  updateMetrics(now);
}

function handId(result, index) {
  const categories = result?.handedness || result?.handednesses || [];
  const label = categories[index]?.[0]?.categoryName?.toLowerCase() || "unknown";
  let id = label.startsWith("left") ? 1 : label.startsWith("right") ? 2 : 0;
  if (settings.swapHands && id !== 0) id = id === 1 ? 2 : 1;
  return id;
}

function handScore(result, index) {
  const categories = result?.handedness || result?.handednesses || [];
  return Number(categories[index]?.[0]?.score ?? 1);
}

function encodePose(result, captureMs) {
  const landmarks = result?.landmarks || [];
  const world = result?.worldLandmarks || [];
  const count = Math.min(MAX_HANDS, landmarks.length);
  const buffer = new ArrayBuffer(WS_HEADER_BYTES + count * WS_HAND_BYTES);
  const view = new DataView(buffer);
  WS_MAGIC.forEach((byte, index) => view.setUint8(index, byte));
  view.setUint8(4, PROTOCOL_VERSION);
  view.setUint8(5, count);
  view.setUint32(6, state.sequence++ >>> 0, true);
  view.setFloat64(10, captureMs, true);

  let offset = WS_HEADER_BYTES;
  for (let handIndex = 0; handIndex < count; handIndex += 1) {
    view.setUint8(offset, handId(result, handIndex));
    view.setUint8(offset + 1, 0x01 | (settings.mirror ? 0x02 : 0));
    view.setFloat32(offset + 2, handScore(result, handIndex), true);
    offset += 6;

    for (let pointIndex = 0; pointIndex < 21; pointIndex += 1) {
      const point = landmarks[handIndex][pointIndex] || { x: 0, y: 0, z: 0 };
      view.setFloat32(offset, point.x, true);
      view.setFloat32(offset + 4, point.y, true);
      view.setFloat32(offset + 8, point.z, true);
      offset += 12;
    }
    const worldHand = world[handIndex] || [];
    for (let pointIndex = 0; pointIndex < 21; pointIndex += 1) {
      const point = worldHand[pointIndex] || { x: 0, y: 0, z: 0 };
      view.setFloat32(offset, point.x, true);
      view.setFloat32(offset + 4, point.y, true);
      view.setFloat32(offset + 8, point.z, true);
      offset += 12;
    }
  }
  return buffer;
}

function sendPose(result, now) {
  const ws = state.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const minInterval = 1000 / settings.sendFps;
  if (now - state.lastSendMs < minInterval - 0.5) return;
  state.lastSendMs = now;
  if (ws.bufferedAmount > 128 * 1024) {
    state.dropped += 1;
    return;
  }
  ws.send(encodePose(result, now));
  state.sendCount += 1;
}

function drawOverlay(result) {
  const canvas = elements.overlay;
  const width = elements.video.videoWidth || 1280;
  const height = elements.video.videoHeight || 720;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, width, height);
  const hands = result?.landmarks || [];
  const colors = ["#44e39a", "#5c8dff"];
  context.lineCap = "round";
  context.lineJoin = "round";
  for (let handIndex = 0; handIndex < hands.length; handIndex += 1) {
    const points = hands[handIndex];
    context.strokeStyle = colors[handIndex % colors.length];
    context.fillStyle = colors[handIndex % colors.length];
    context.lineWidth = Math.max(2, width / 480);
    for (const [a, b] of HAND_CONNECTIONS) {
      const p1 = points[a];
      const p2 = points[b];
      if (!p1 || !p2) continue;
      context.beginPath();
      context.moveTo(p1.x * width, p1.y * height);
      context.lineTo(p2.x * width, p2.y * height);
      context.stroke();
    }
    const radius = Math.max(3, width / 260);
    for (const point of points) {
      context.beginPath();
      context.arc(point.x * width, point.y * height, radius, 0, Math.PI * 2);
      context.fill();
    }
  }
}

function updateTrackingBadge(result) {
  const count = result?.landmarks?.length || 0;
  elements.trackingBadge.textContent = count ? `${count} 只手已追踪` : "未追踪";
  elements.trackingBadge.classList.toggle("active", count > 0);
}

function updateMetrics(now) {
  const elapsed = now - state.sendWindowStart;
  if (elapsed >= 500) {
    const hz = state.sendCount * 1000 / elapsed;
    elements.sent.textContent = `${hz.toFixed(1)} Hz`;
    state.sendCount = 0;
    state.sendWindowStart = now;
    elements.inference.textContent = `${state.inferenceEma.toFixed(1)} ms`;
    elements.fps.textContent = `${state.trackingFpsEma.toFixed(1)} Hz`;
    elements.rtt.textContent = state.wsConnected ? `${state.rttMs.toFixed(1)} ms` : "-- ms";
    updateDiagnostics();
    if (now - state.lastTelemetryAt > 500) {
      state.lastTelemetryAt = now;
      sendJson({
        type: "telemetry",
        stats: {
          inference_ms: Number(state.inferenceEma.toFixed(2)),
          tracking_hz: Number(state.trackingFpsEma.toFixed(2)),
          rtt_ms: Number(state.rttMs.toFixed(2)),
          dropped: state.dropped,
          hands: state.handCount,
          model_loaded: Boolean(state.landmarker),
          delegate: settings.delegate,
        },
      });
    }
  }
}

function updateDiagnostics(prefix = "") {
  const lines = [];
  if (prefix) lines.push(prefix);
  lines.push(`模型已加载: ${state.landmarker ? "是" : "否"}`);
  lines.push(`计算设备: ${settings.delegate}`);
  lines.push(`协议: PHCW v${PROTOCOL_VERSION}`);
  lines.push(`安全上下文: ${window.isSecureContext ? "是" : "否"}`);
  lines.push(`WebSocket: ${state.wsConnected ? "已连接" : "未连接"}`);
  lines.push(`手部数量: ${state.handCount}`);
  lines.push(`跟踪帧率: ${state.trackingFpsEma.toFixed(1)} Hz`);
  lines.push(`推理耗时 EMA: ${state.inferenceEma.toFixed(2)} ms`);
  lines.push(`网络 RTT EMA: ${state.rttMs.toFixed(2)} ms`);
  lines.push(`丢弃发送: ${state.dropped}`);
  lines.push(`视频: ${elements.video.videoWidth}x${elements.video.videoHeight}`);
  elements.diagnostics.textContent = lines.join("\n");
}

function updateRawDiagnostics() {
  if (state.running) updateDiagnostics();
}
setInterval(updateRawDiagnostics, 1000);
setInterval(() => {
  if (state.wsConnected) sendJson({ type: "ping", t: performance.now() });
}, 1000);

async function stopCamera() {
  state.running = false;
  elements.stop.disabled = true;
  elements.start.disabled = false;
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
  }
  elements.video.srcObject = null;
  elements.placeholder.classList.remove("hidden");
  elements.overlay.getContext("2d")?.clearRect(0, 0, elements.overlay.width, elements.overlay.height);
  setConnection("offline", "摄像头已停止");
}

elements.start.addEventListener("click", async () => {
  try {
    await startCamera();
  } catch (error) {
    console.error(error);
    elements.start.disabled = false;
    setConnection("offline", error?.message || "摄像头启动失败");
    updateDiagnostics(`错误: ${error?.message || error}`);
  }
});
elements.stop.addEventListener("click", stopCamera);

elements.mirror.addEventListener("change", () => {
  settings.mirror = elements.mirror.checked;
  elements.stage.classList.toggle("mirrored", settings.mirror);
  persistSettings();
});
elements.swapHands.addEventListener("change", () => {
  settings.swapHands = elements.swapHands.checked;
  persistSettings();
});
elements.delegate.addEventListener("change", () => {
  settings.delegate = elements.delegate.value;
  persistSettings();
});
elements.sendFps.addEventListener("change", () => {
  settings.sendFps = Number(elements.sendFps.value);
  persistSettings();
});
elements.confidence.addEventListener("input", () => {
  settings.confidence = Number(elements.confidence.value);
  elements.confidenceOutput.value = settings.confidence.toFixed(2);
  persistSettings();
});
elements.cameraSelect.addEventListener("change", async () => {
  settings.cameraId = elements.cameraSelect.value;
  persistSettings();
  if (state.running) {
    await stopCamera();
    await startCamera();
  }
});

window.addEventListener("pagehide", stopCamera);
applySettingsToUi();
loadModelBuffer().catch((error) => console.warn("model preload failed", error));
connectWebSocket();
if (new URLSearchParams(location.search).get("autostart") === "1") {
  setTimeout(() => elements.start.click(), 600);
}
updateDiagnostics();
