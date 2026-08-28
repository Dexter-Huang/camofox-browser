import net from 'node:net';
import { spawn } from './spawn.js';

const READINESS_ATTEMPTS = 20;
const READINESS_DELAY_MS = 150;

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function validDisplay(display) {
  return typeof display === 'string' && /^:[0-9]+$/.test(display);
}

function validWindowId(windowId) {
  return typeof windowId === 'string' && /^0x[0-9a-f]+$/i.test(windowId);
}

function validPort(port) {
  return Number.isInteger(port) && port >= 1024 && port <= 65535;
}

/** Keep publisher command construction deterministic and independently testable. */
export function buildWindowPublisherCommands({ display, windowId, rfbPort, websocketPort }) {
  if (!validDisplay(display) || !validWindowId(windowId) || !validPort(rfbPort) || !validPort(websocketPort)) {
    throw new Error('Window publisher configuration is invalid');
  }
  return {
    x11vnc: [
      '-display', display,
      '-id', windowId,
      '-localhost',
      '-nopw',
      '-forever',
      '-shared',
      '-rfbport', String(rfbPort),
      '-noxdamage',
      '-quiet',
    ],
    websockify: [
      '--web', '/usr/share/novnc',
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
    const socket = net.createConnection({ host: '127.0.0.1', port });
    const finish = (ready) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(ready);
    };
    socket.setTimeout(250);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

export async function waitForTcpPort(port, { probe = tcpProbe, wait = sleep } = {}) {
  for (let attempt = 0; attempt < READINESS_ATTEMPTS; attempt += 1) {
    if (await probe(port)) return;
    if (attempt + 1 < READINESS_ATTEMPTS) await wait(READINESS_DELAY_MS);
  }
  throw new Error(`Window VNC publisher did not listen on port ${port}`);
}

function stopProcess(process) {
  if (process && process.exitCode === null && !process.killed) process.kill('SIGTERM');
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
  const commands = buildWindowPublisherCommands({ display, windowId, rfbPort, websocketPort });
  const x11vnc = spawn('x11vnc', commands.x11vnc, { stdio: 'ignore' });
  let websockify = null;
  let stopped = false;
  const notifyExit = (source) => {
    if (!stopped) onExit(source);
  };
  x11vnc.once('exit', () => notifyExit('x11vnc'));
  x11vnc.once('error', () => notifyExit('x11vnc'));

  try {
    await waitForTcpPort(rfbPort);
    websockify = spawn('websockify', commands.websockify, { stdio: 'ignore' });
    websockify.once('exit', () => notifyExit('websockify'));
    websockify.once('error', () => notifyExit('websockify'));
    await waitForTcpPort(websocketPort);
  } catch (error) {
    stopped = true;
    stopProcess(websockify);
    stopProcess(x11vnc);
    throw error;
  }

  return {
    async stop() {
      stopped = true;
      stopProcess(websockify);
      stopProcess(x11vnc);
    },
  };
}
