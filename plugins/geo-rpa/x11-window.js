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

function validDisplay(display) {
  return typeof display === 'string' && /^:[0-9]+$/.test(display);
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
