const token = process.env.PHC_TOKEN;
if (!token) throw new Error("PHC_TOKEN is required");
const url = `wss://127.0.0.1:8443/ws?token=${encodeURIComponent(token)}`;
const ws = new WebSocket(url);
ws.binaryType = "arraybuffer";
let sequence = 0;
let sent = 0;
const started = performance.now();

function packet(seq) {
  const buffer = new ArrayBuffer(18 + 510);
  const view = new DataView(buffer);
  [0x50, 0x48, 0x43, 0x57].forEach((value, index) => view.setUint8(index, value));
  view.setUint8(4, 1);
  view.setUint8(5, 1);
  view.setUint32(6, seq, true);
  view.setFloat64(10, performance.now(), true);
  let offset = 18;
  view.setUint8(offset, 2);
  view.setUint8(offset + 1, 1);
  view.setFloat32(offset + 2, 1.0, true);
  offset += 6;
  for (let i = 0; i < 126; i += 1) {
    const landmark = Math.floor(i / 3);
    const axis = i % 3;
    const value = (axis === 0 ? 0.5 + Math.sin(landmark) * 0.03 : axis === 1 ? 0.55 + Math.cos(landmark) * 0.03 : 0.001 * landmark) + (axis === 0 ? Math.sin(seq * 0.25) * 0.03 : 0);
    view.setFloat32(offset, i < 63 ? value : value * 0.02, true);
    offset += 4;
  }
  return buffer;
}

ws.addEventListener("open", () => {
  const timer = setInterval(() => {
    if (sent >= 30) {
      clearInterval(timer);
      ws.send(JSON.stringify({ type: "ping", t: performance.now() }));
      return;
    }
    ws.send(packet(sequence++));
    sent += 1;
  }, 16);
});
ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.type === "pong") {
    console.log(JSON.stringify({ sent, rtt_ms: performance.now() - message.client_t, elapsed_ms: performance.now() - started }));
    ws.close();
    process.exit(0);
  }
});
ws.addEventListener("error", (event) => {
  console.error(event.message || event);
  process.exitCode = 1;
});
setTimeout(() => {
  console.error("timeout");
  process.exit(1);
}, 5000);



