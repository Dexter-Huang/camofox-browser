/**
 * X11 window inspection used by the manual-window feasibility spike.
 *
 * This module is intentionally the only geo-rpa module that starts a system
 * command. Route handlers receive an opaque handle and never expose a display
 * identifier or an X11 window id to their callers.
 */
import { execFile as execFileCallback } from 'node:child_process';
import { promisify } from 'node:util';

const execFile = promisify(execFileCallback);
const MANUAL_WINDOW_WIDTH = 1920;
const MANUAL_WINDOW_HEIGHT = 1080;

function validDisplay(display) {
  return typeof display === 'string' && /^:[0-9]+$/.test(display);
}

function validWindowId(windowId) {
  return typeof windowId === 'string' && /^0x[0-9a-f]+$/i.test(windowId);
}

function parseWindowSize(output) {
  const width = output.match(/^\s*Width:\s*(\d+)\s*$/m);
  const height = output.match(/^\s*Height:\s*(\d+)\s*$/m);
  if (!width || !height) throw new Error('X11 window geometry is unavailable');
  return { width: Number(width[1]), height: Number(height[1]) };
}

/** Read the X11 root window tree without invoking a shell. */
export async function readX11WindowTree(display) {
  if (!validDisplay(display)) throw new Error('X11 display is unavailable');
  const { stdout } = await execFile('xwininfo', ['-display', display, '-root', '-tree'], {
    encoding: 'utf8',
    timeout: 3000,
    maxBuffer: 256 * 1024,
  });
  return stdout;
}

/**
 * Keep the native Firefox popup equal to the VNC framebuffer.
 *
 * Camoufox can restore its default 1440px window after provider navigation,
 * even when `window.resizeTo()` has already succeeded. Use the X11 window id
 * owned by this plugin so that the browser cannot leave gray desktop strips
 * on the right and bottom of a manual-control session.
 */
export async function enforceManualWindowGeometry(
  display,
  windowId,
  { run = execFile } = {},
) {
  if (!validDisplay(display) || !validWindowId(windowId)) {
    throw new Error('X11 window geometry target is invalid');
  }
  const environment = { ...process.env, DISPLAY: display };
  const inspect = () => run('xwininfo', ['-display', display, '-id', windowId], {
    encoding: 'utf8',
    timeout: 3000,
    maxBuffer: 64 * 1024,
  });
  const current = parseWindowSize((await inspect()).stdout);
  if (current.width === MANUAL_WINDOW_WIDTH && current.height === MANUAL_WINDOW_HEIGHT) return;
  await run('xdotool', ['windowmove', '--sync', windowId, '0', '0'], {
    env: environment,
    timeout: 3000,
  });
  await run(
    'xdotool',
    ['windowsize', '--sync', windowId, String(MANUAL_WINDOW_WIDTH), String(MANUAL_WINDOW_HEIGHT)],
    { env: environment, timeout: 3000 },
  );
  const { stdout } = await inspect();
  const size = parseWindowSize(stdout);
  if (size.width !== MANUAL_WINDOW_WIDTH || size.height !== MANUAL_WINDOW_HEIGHT) {
    throw new Error(
      `Manual Firefox window has invalid geometry ${size.width}x${size.height}`,
    );
  }
}

/** Re-assert native geometry while a manual lease is alive. */
export function startManualWindowGeometryGuard(display, windowId, { onError = () => {} } = {}) {
  let stopped = false;
  let running = false;
  const enforce = async () => {
    if (stopped || running) return;
    running = true;
    try {
      await enforceManualWindowGeometry(display, windowId);
    } catch (error) {
      if (!stopped) onError(error);
    } finally {
      running = false;
    }
  };
  const timer = setInterval(() => void enforce(), 1000);
  timer.unref?.();
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}
