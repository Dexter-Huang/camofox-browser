import crypto from 'node:crypto';

const WINDOW_DISCOVERY_ATTEMPTS = 20;
const WINDOW_DISCOVERY_DELAY_MS = 150;
const POPUP_TIMEOUT_MS = 5000;

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** Return an opaque handle and a title marker controlled entirely by the fork. */
export function createManualWindowIdentity() {
  const handle = crypto.randomUUID();
  return {
    handle,
    title: `GEO_MANUAL_WINDOW_${handle}`,
  };
}

/**
 * Locate a Firefox top-level X11 window by the unique title written by this
 * module. Matching the full title avoids treating another page's title as the
 * controlled window; the returned id never leaves the Camofox process.
 */
export function findX11WindowId(windowTree, title) {
  if (typeof windowTree !== 'string' || typeof title !== 'string') return null;
  const escapedTitle = title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = windowTree.match(new RegExp(`^\\s*(0x[0-9a-f]+) ".*${escapedTitle}.*"`, 'im'));
  return match ? match[1] : null;
}

/**
 * Return the direct children of the X11 root window. Camoufox does not always
 * propagate document.title to WM_NAME, so a generated title cannot be the
 * only identity mechanism during this spike.
 */
export function listTopLevelX11WindowIds(windowTree) {
  if (typeof windowTree !== 'string') return [];
  const candidates = [];
  for (const line of windowTree.split('\n')) {
    const match = line.match(/^(\s+)(0x[0-9a-f]+) /i);
    if (match) candidates.push({ indent: match[1].length, id: match[2] });
  }
  if (candidates.length === 0) return [];
  const rootIndent = Math.min(...candidates.map((candidate) => candidate.indent));
  return candidates
    .filter((candidate) => candidate.indent === rootIndent)
    .map((candidate) => candidate.id);
}

/** Retry X11 discovery because Firefox creates the X window asynchronously. */
export async function waitForX11WindowId({
  display,
  title,
  readWindowTree,
  wait = sleep,
  attempts = WINDOW_DISCOVERY_ATTEMPTS,
}) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const windowId = findX11WindowId(await readWindowTree(display), title);
      if (windowId) return windowId;
    } catch (error) {
      lastError = error;
    }
    if (attempt + 1 < attempts) await wait(WINDOW_DISCOVERY_DELAY_MS);
  }
  if (lastError) throw lastError;
  throw new Error('Manual Firefox window was not found in X11');
}

/**
 * The initial spike permits one global manual lease. Under that invariant, the
 * only new root window created between the fixed popup request and discovery
 * is its window. Later production work must replace this with a Firefox-level
 * stable handle before allowing concurrent manual windows.
 */
export async function waitForNewX11WindowId({
  display,
  existingWindowIds,
  readWindowTree,
  wait = sleep,
  attempts = WINDOW_DISCOVERY_ATTEMPTS,
}) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const discovered = listTopLevelX11WindowIds(await readWindowTree(display));
      const windowId = discovered.find((id) => !existingWindowIds.has(id));
      if (windowId) return windowId;
    } catch (error) {
      lastError = error;
    }
    if (attempt + 1 < attempts) await wait(WINDOW_DISCOVERY_DELAY_MS);
  }
  if (lastError) throw lastError;
  throw new Error('Manual Firefox window was not found in X11');
}

/**
 * Open a native popup using a fixed page script. The caller supplies no page
 * JavaScript, URL, title, or feature string, so this cannot become a generic
 * script-evaluation surface. It is a feasibility probe only: the source tab
 * remains intact until a later protocol can safely transfer ownership.
 */
export async function openManualPopup(page, title, targetUrl) {
  const popupPromise = page.waitForEvent('popup', { timeout: POPUP_TIMEOUT_MS });
  const opened = await page.evaluate(() => {
    const popup = window.open('about:blank', '_blank', 'popup=yes,width=1440,height=900');
    if (!popup) return false;
    return true;
  });
  if (!opened) throw new Error('Firefox blocked the manual popup window');

  const popup = await popupPromise;
  await popup.waitForLoadState('domcontentloaded').catch(() => {});
  await popup.evaluate((popupTitle) => { document.title = popupTitle; }, title);
  // The source page already resolved the provider's entry URL in this account
  // Context. Navigate the popup to that final URL so the visible window owns
  // the real manual page while retaining the same persistent profile state.
  if (typeof targetUrl === 'string' && targetUrl) {
    await popup.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 90_000 });
  }
  return popup;
}
