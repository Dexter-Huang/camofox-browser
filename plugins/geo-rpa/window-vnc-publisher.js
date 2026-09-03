import net from "node:net";
import { spawn } from "./spawn.js";

const READINESS_ATTEMPTS = 20;
const READINESS_DELAY_MS = 150;

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function validDisplay(display) {
  return typeof display === "string" && /^:[0-9]+$/.test(display);
}

function validWindowId(windowId) {
  return typeof windowId === "string" && /^0x[0-9a-f]+$/i.test(windowId);
}

function validPort(port) {
  return Number.isInteger(port) && port >= 1024 && port <= 65535;
}

/** Keep publisher command construction deterministic and independently testable. */
export function buildWindowPublisherCommands({
  display,
  windowId,
  rfbPort,
  websocketPort,
}) {
  if (
    !validDisplay(display) ||
    !validWindowId(windowId) ||
    !validPort(rfbPort) ||
    !validPort(websocketPort)
  ) {
    throw new Error("Window publisher configuration is invalid");
  }
  return {
    x11vnc: [
      "-display",
      display,
      // 只允许导出目标窗口。-sid 从共享 Xvfb 根显示器取像素，窗口重绘或
      // 焦点变化时会把其他账户的窗口带入画面，不能用于受账户隔离约束的预览。
      "-id",
      windowId,
      "-localhost",
      "-nopw",
      "-forever",
      "-shared",
      "-rfbport",
      String(rfbPort),
      // Firefox popup 在 Xvfb 上会经历合成层切换和缩放重绘；有些更新不会产生
      // 可用的 DAMAGE 矩形，x11vnc 因而把有效画面保留为黑帧。观察器只在用户
      // 打开预览时存在，优先使用完整刷新保证验证码、拖拽和流式页面持续可见。
      "-noxdamage",
      // Keep interactive input responsive while retaining a small coalescing
      // window for rapid redraws such as animated login and verification UIs.
      "-wait",
      "10",
      "-defer",
      "10",
      "-wait_ui",
      "1",
      "-setdefer",
      "-1",
      "-quiet",
    ],
    websockify: [
      "--web",
      "/usr/share/novnc",
      // FastAPI lives in another Docker container. Do not bind to loopback:
      // it would make the publisher unreachable to the authenticated proxy.
      // Compose deliberately publishes no host port for this listener.
      `0.0.0.0:${websocketPort}`,
      `127.0.0.1:${rfbPort}`,
    ],
  };
}

function tcpProbe(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    const finish = (ready) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(ready);
    };
    socket.setTimeout(250);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

function rfbGreetingProbe(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    let received = Buffer.alloc(0);
    const finish = (ready) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(ready);
    };
    socket.setTimeout(1000);
    socket.once("connect", () => {});
    socket.on("data", (chunk) => {
      received = Buffer.concat([received, chunk]);
      if (received.length >= 4)
        finish(received.subarray(0, 4).toString("ascii") === "RFB ");
    });
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

/** Refuse stale listeners instead of treating another session's port as ready. */
export function assertTcpPortAvailable(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", () =>
      reject(new Error(`Window VNC port ${port} is already in use`)),
    );
    server.listen({ host: "127.0.0.1", port, exclusive: true }, () => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  });
}

export async function waitForTcpPort(
  port,
  { probe = tcpProbe, wait = sleep } = {},
) {
  for (let attempt = 0; attempt < READINESS_ATTEMPTS; attempt += 1) {
    if (await probe(port)) return;
    if (attempt + 1 < READINESS_ATTEMPTS) await wait(READINESS_DELAY_MS);
  }
  throw new Error(`Window VNC publisher did not listen on port ${port}`);
}

/** Verify that x11vnc owns a live X11 window and has started the RFB protocol. */
export async function waitForRfbGreeting(
  port,
  { probe = rfbGreetingProbe, wait = sleep } = {},
) {
  for (let attempt = 0; attempt < READINESS_ATTEMPTS; attempt += 1) {
    if (await probe(port)) return;
    if (attempt + 1 < READINESS_ATTEMPTS) await wait(READINESS_DELAY_MS);
  }
  throw new Error(
    `Window VNC publisher did not emit an RFB greeting on port ${port}`,
  );
}

function processExited(process) {
  return !process || process.exitCode !== null || process.signalCode !== null;
}

function waitForProcessExit(process, timeoutMs) {
  if (processExited(process)) return Promise.resolve(true);
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timeout);
      process.removeListener("exit", finish);
      process.removeListener("error", finish);
      resolve(true);
    };
    const timeout = setTimeout(() => {
      process.removeListener("exit", finish);
      process.removeListener("error", finish);
      resolve(false);
    }, timeoutMs);
    process.once("exit", finish);
    process.once("error", finish);
  });
}

/**
 * x11vnc can keep serving an orphaned X11 window after SIGTERM in the
 * container image. Wait for a graceful stop, then force the process down so
 * the next manual session cannot attach to the stale fixed RFB port.
 */
export async function stopWindowVncProcess(
  process,
  {
    gracefulTimeoutMs = 500,
    forceTimeoutMs = 1000,
    wait = waitForProcessExit,
  } = {},
) {
  if (processExited(process)) return;
  try {
    process.kill("SIGTERM");
  } catch {
    return;
  }
  if (await wait(process, gracefulTimeoutMs)) return;
  try {
    process.kill("SIGKILL");
  } catch {
    return;
  }
  await wait(process, forceTimeoutMs);
}

/**
 * Publish exactly one X11 window through a private RFB and websockify pair.
 * Ports are bound to loopback and never included in an HTTP response. The
 * caller receives only lifecycle methods and decides how a trusted proxy
 * reaches the private websockify endpoint.
 */
export async function startWindowVncPublisher({
  display,
  windowId,
  rfbPort,
  websocketPort,
  onExit = () => {},
}) {
  const commands = buildWindowPublisherCommands({
    display,
    windowId,
    rfbPort,
    websocketPort,
  });
  // A previous publisher must never be silently reused: its X11 window may
  // already be gone, which leaves the browser WebSocket connected but idle.
  await assertTcpPortAvailable(rfbPort);
  await assertTcpPortAvailable(websocketPort);
  const x11vnc = spawn("x11vnc", commands.x11vnc, { stdio: "ignore" });
  let websockify = null;
  let stopped = false;
  const notifyExit = (source) => {
    if (!stopped) onExit(source);
  };
  x11vnc.once("exit", () => notifyExit("x11vnc"));
  x11vnc.once("error", () => notifyExit("x11vnc"));

  try {
    await waitForTcpPort(rfbPort);
    await waitForRfbGreeting(rfbPort);
    websockify = spawn("websockify", commands.websockify, { stdio: "ignore" });
    websockify.once("exit", () => notifyExit("websockify"));
    websockify.once("error", () => notifyExit("websockify"));
    await waitForTcpPort(websocketPort);
  } catch (error) {
    stopped = true;
    await stopWindowVncProcess(websockify);
    await stopWindowVncProcess(x11vnc);
    throw error;
  }

  return {
    async stop() {
      stopped = true;
      await stopWindowVncProcess(websockify);
      await stopWindowVncProcess(x11vnc);
    },
  };
}
