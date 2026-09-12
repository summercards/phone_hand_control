const port = process.env.CDP_PORT || "9223";
const pages = await fetch(`http://127.0.0.1:${port}/json`).then((response) => response.json());
const page = pages.find((item) => item.type === "page");
if (!page) throw new Error("no Chrome page found");
const socket = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
let nextId = 1;
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});
socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result);
  }
  if (message.method === "Runtime.exceptionThrown") {
    console.error("browser-exception", message.params.exceptionDetails.text);
  }
});
function send(method, params = {}) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
await send("Runtime.enable");
await send("Page.enable");
await sleep(1500);
await send("Runtime.evaluate", { expression: "document.getElementById('start-button').click()" });
await sleep(12000);
const result = await send("Runtime.evaluate", {
  returnByValue: true,
  expression: `(() => ({
    title: document.title,
    secure: window.isSecureContext,
    connection: document.getElementById('connection-text').textContent,
    diagnostics: document.getElementById('diagnostics').textContent,
    startDisabled: document.getElementById('start-button').disabled,
    stopDisabled: document.getElementById('stop-button').disabled,
    video: [document.getElementById('camera').videoWidth, document.getElementById('camera').videoHeight]
  }))()`,
});
console.log(JSON.stringify(result.result.value, null, 2));
socket.close();
