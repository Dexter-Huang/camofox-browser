import crypto from 'node:crypto';

const WINDOW_DISCOVERY_ATTEMPTS = 20;
const WINDOW_DISCOVERY_DELAY_MS = 150;
const POPUP_TIMEOUT_MS = 5000;
const TASK_WINDOW_POPUP_FEATURES = 'popup=yes,width=1440,height=900';
export const MANUAL_WINDOW_POPUP_FEATURES = 'popup=yes,width=1920,height=1009';
const MANUAL_WINDOW_OUTER_SIZE = { width: 1920, height: 1080 };

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function detachedSleep(milliseconds) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, milliseconds);
    timer.unref?.();
  });
}

/**
 * Keep a headed popup at the VNC geometry without delaying publisher startup.
 *
 * Provider pages can resize their native Firefox window several seconds after
 * navigation commit. A short one-shot retry finishes too early and leaves a
 * gray edge in the captured VNC framebuffer, so keep this lightweight repair
 * active through the provider's first layout phase.
 */
async function stabilizeManualPopup(popup) {
  await detachedSleep(250);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (popup.isClosed()) return;
    await popup.evaluate(({ width, height }) => window.resizeTo(width, height), MANUAL_WINDOW_OUTER_SIZE);
    await detachedSleep(100);
  }
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
 * JavaScript, URL, title, or raw feature string, so this cannot become a
 * generic script-evaluation surface. Manual control may select the wider
 * fixed geometry; task windows retain their existing footprint.
 */
export async function openManualPopup(page, title, targetUrl, { manualWindow = false } = {}) {
  const popupPromise = page.waitForEvent('popup', { timeout: POPUP_TIMEOUT_MS });
  const popupFeatures = manualWindow ? MANUAL_WINDOW_POPUP_FEATURES : TASK_WINDOW_POPUP_FEATURES;
  const opened = await page.evaluate((features) => {
    const popup = window.open('about:blank', '_blank', features);
    if (!popup) return false;
    return true;
  }, popupFeatures);
  if (!opened) throw new Error('Firefox blocked the manual popup window');

  const popup = await popupPromise;
  await popup.waitForLoadState('domcontentloaded').catch(() => {});
  if (manualWindow) {
    // Headed Firefox owns native window geometry. Let the script-opened
    // window fill the Xvfb directly instead of combining it with Playwright's
    // viewport emulation, which can leave an unpainted right edge.
    await popup.evaluate(({ width, height }) => window.resizeTo(width, height), MANUAL_WINDOW_OUTER_SIZE);
  }
  await popup.evaluate((popupTitle) => { document.title = popupTitle; }, title);
  // The source page already resolved the provider's entry URL in this account
  // Context. Navigate the popup to that final URL so the visible window owns
  // the real manual page while retaining the same persistent profile state.
  if (typeof targetUrl === 'string' && targetUrl) {
    // 页面提交后即可启动窗口级 VNC；无需阻塞到 DOM 完整加载，后续加载过程会自然呈现。
    await popup.goto(targetUrl, { waitUntil: 'commit', timeout: 90_000 });
    if (manualWindow) {
      // Navigation can restore the opener's native geometry in headed mode;
      // re-apply the fixed outer size after commit before the VNC publisher starts.
      await popup.evaluate(({ width, height }) => window.resizeTo(width, height), MANUAL_WINDOW_OUTER_SIZE);
      // Give the compositor one frame to repaint before x11vnc starts publishing
      // the resized native window.
      await popup.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
      // The publisher can start after the first synchronized geometry. Keep
      // correcting late provider-driven native resizes asynchronously so this
      // work does not delay the manual-session HTTP response.
      void stabilizeManualPopup(popup).catch(() => {});
    }
  }
  return popup;
}
