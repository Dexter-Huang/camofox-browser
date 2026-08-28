import crypto from 'node:crypto';
import express from 'express';

// Fast read probes should fail quickly, but a real chat submission can take
// longer while the page finishes its own event handlers. Keep the two budgets
// separate so slow provider UI does not turn a dispatched native click into a
// false task failure.
const LOCATOR_TIMEOUT_MS = 3000;
const SUBMISSION_CLICK_TIMEOUT_MS = 12_000;
const MAX_SELECTOR_LENGTH = 1024;
const MAX_CAPTURE_BYTES = 2 * 1024 * 1024;
const MAX_CAPTURES_PER_TAB = 2;
const MAX_MANUAL_TEXT_LENGTH = 10_000;
const MAX_MANUAL_COORDINATE = 10_000;
const MAX_MANUAL_WHEEL_DELTA = 10_000;
const MANUAL_MOUSE_TYPES = new Set(['move', 'down', 'up', 'wheel']);
const MANUAL_BUTTONS = new Set(['left', 'middle', 'right']);
const MANUAL_MODIFIERS = new Set(['Alt', 'Control', 'Meta', 'Shift']);
const MANUAL_KEYS = new Set([
  'Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown',
  'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', 'Insert',
  'a', 'c', 'v', 'x', 'y', 'z', 'A', 'C', 'V', 'X', 'Y', 'Z',
]);
const captures = new Map();
const manualPointers = new Map();

function captureKey(userId, tabId, captureId) {
  return `${userId}:${tabId}:${captureId}`;
}

function manualPointerKey(userId, tabId) {
  return `${userId}:${tabId}`;
}

function findOwnedTab(sessions, userId, tabId) {
  const session = sessions.get(String(userId));
  if (!session) return null;
  for (const group of session.tabGroups.values()) {
    const tabState = group.get(tabId);
    if (tabState) return { session, tabState };
  }
  return null;
}

function validSelector(value) {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= MAX_SELECTOR_LENGTH;
}

function isFiniteNumber(value, minimum, maximum) {
  return typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum;
}

function normalizeManualInput(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('manual input event is required');
  if (raw.kind === 'text') {
    if (typeof raw.text !== 'string' || raw.text.length === 0 || raw.text.length > MAX_MANUAL_TEXT_LENGTH) {
      throw new Error('manual text is invalid');
    }
    return { kind: 'text', text: raw.text };
  }
  if (raw.kind === 'key') {
    const modifiers = Array.isArray(raw.modifiers) ? raw.modifiers : [];
    if ((!MANUAL_KEYS.has(raw.key) && !MANUAL_MODIFIERS.has(raw.key))
      || modifiers.some((modifier) => !MANUAL_MODIFIERS.has(modifier))) {
      throw new Error('manual key is invalid');
    }
    return { kind: 'key', key: raw.key, modifiers };
  }
  if (raw.kind === 'mouse') {
    if (!MANUAL_MOUSE_TYPES.has(raw.type)
      || !isFiniteNumber(raw.x, 0, MAX_MANUAL_COORDINATE)
      || !isFiniteNumber(raw.y, 0, MAX_MANUAL_COORDINATE)) {
      throw new Error('manual mouse coordinates are invalid');
    }
    if (raw.type === 'wheel') {
      if (!isFiniteNumber(raw.delta_x, -MAX_MANUAL_WHEEL_DELTA, MAX_MANUAL_WHEEL_DELTA)
        || !isFiniteNumber(raw.delta_y, -MAX_MANUAL_WHEEL_DELTA, MAX_MANUAL_WHEEL_DELTA)) {
        throw new Error('manual wheel delta is invalid');
      }
      return {
        kind: 'mouse', type: 'wheel', x: raw.x, y: raw.y,
        deltaX: raw.delta_x, deltaY: raw.delta_y,
      };
    }
    const button = MANUAL_BUTTONS.has(raw.button) ? raw.button : 'left';
    return { kind: 'mouse', type: raw.type, x: raw.x, y: raw.y, button };
  }
  throw new Error('manual input kind is invalid');
}

async function dispatchManualInput(page, event) {
  if (event.kind === 'text') {
    await page.keyboard.insertText(event.text);
    return;
  }
  if (event.kind === 'key') {
    // Modifier keydown/keyup is reported separately by the canvas. The next
    // shortcut event carries the full modifier set, so no standalone input is needed.
    if (MANUAL_MODIFIERS.has(event.key)) return;
    const prefix = event.modifiers.join('+');
    await page.keyboard.press(prefix ? `${prefix}+${event.key}` : event.key);
    return;
  }
  if (event.type === 'wheel') {
    await page.mouse.move(event.x, event.y);
    await page.mouse.wheel(event.deltaX, event.deltaY);
    return;
  }
  // Two short movement steps preserve a normal pointer transition without
  // turning a manual click into a visibly slow cursor animation.
  await page.mouse.move(event.x, event.y, { steps: event.type === 'move' ? 1 : 2 });
  if (event.type === 'down') await page.mouse.down({ button: event.button });
  if (event.type === 'up') await page.mouse.up({ button: event.button });
}

async function dispatchManualPointer(tabState, userId, tabId, event) {
  if (event.kind !== 'mouse' || event.type === 'wheel') {
    await dispatchManualInput(tabState.page, event);
    return;
  }
  const key = manualPointerKey(userId, tabId);
  const pointer = manualPointers.get(key);
  if (event.type === 'down') {
    await tabState.page.mouse.move(event.x, event.y, { steps: 2 });
    manualPointers.set(key, { x: event.x, y: event.y, button: event.button, dragging: false });
    return;
  }
  if (event.type === 'move' && pointer) {
    if (!pointer.dragging) {
      await tabState.page.mouse.down({ button: pointer.button });
      pointer.dragging = true;
    }
    await tabState.page.mouse.move(event.x, event.y, { steps: 1 });
    return;
  }
  if (event.type === 'up' && pointer) {
    manualPointers.delete(key);
    if (!pointer.dragging) {
      // A click must be a single browser input sequence. Sending down and up
      // through separate HTTP requests is accepted by Playwright but can be
      // ignored by interactive Camoufox pages.
      await tabState.page.mouse.click(event.x, event.y, { button: pointer.button, delay: 50 });
      return;
    }
    await tabState.page.mouse.move(event.x, event.y, { steps: 1 });
    await tabState.page.mouse.up({ button: pointer.button });
    return;
  }
  if (event.type === 'up') {
    await tabState.page.mouse.click(event.x, event.y, { button: event.button, delay: 50 });
  }
}

function targetLocator(tabState, target) {
  if (!target || typeof target !== 'object' || Array.isArray(target)) {
    throw new Error('locator target is required');
  }
  const hasSelector = validSelector(target.selector);
  const hasText = typeof target.text === 'string' && target.text.length > 0 && target.text.length <= 4096;
  if (Number(hasSelector) + Number(hasText) !== 1) {
    throw new Error('locator target requires exactly one selector or text');
  }
  const rawIndex = target.index === undefined ? 0 : target.index;
  if (!Number.isInteger(rawIndex) || rawIndex < -1) {
    throw new Error('locator index is invalid');
  }
  const locator = hasSelector
    ? tabState.page.locator(target.selector)
    : tabState.page.getByText(target.text, { exact: true });
  return { locator, index: rawIndex };
}

async function selectedLocator(tabState, target) {
  const { locator, index } = targetLocator(tabState, target);
  const count = await locator.count();
  const selectedIndex = index === -1 ? count - 1 : index;
  if (selectedIndex < 0 || selectedIndex >= count) {
    throw new Error('locator not found');
  }
  return locator.nth(selectedIndex);
}

async function submitLocatorClick(locator) {
  // This remains Playwright's normal, actionability-checked click. It does not
  // force through overlays or inject DOM events. Chat pages often start a
  // navigation or replace their composer after the click, neither of which is
  // evidence that the click itself failed, so do not wait for that transition.
  await locator.click({
    timeout: SUBMISSION_CLICK_TIMEOUT_MS,
    noWaitAfter: true,
  });
}

function isSubmissionNotDispatched(error) {
  // Playwright 明确报告遮罩层拦截指针事件时，正常 click 尚未送达目标控件。只有这一
  // 可证明的动作前失败才允许主系统创建一次新任务；超时、连接断开和导航竞态都不能
  // 推断为未提交，仍由提交确认与原会话恢复路径处理。
  const message = error instanceof Error ? error.message : String(error);
  return message.includes('intercepts pointer events');
}

async function submitNearLocator(tabState, target) {
  const prompt = await selectedLocator(tabState, target);
  const promptBox = await prompt.boundingBox();
  if (!promptBox) throw new Error('input locator has no visible bounds');
  // SVG 只是按钮的视觉子节点，不是可提交控件。把它作为候选会绕过 disabled
  // 状态，并可能误点输入框中的“更多”图标；只保留真实可操作的按钮语义。
  const controls = tabState.page.locator('button, [role="button"]');
  const count = Math.min(await controls.count(), 128);
  let selected = null;
  let selectedRight = Number.NEGATIVE_INFINITY;
  for (let index = 0; index < count; index += 1) {
    const candidate = controls.nth(index);
    if (!(await candidate.isVisible({ timeout: LOCATOR_TIMEOUT_MS }).catch(() => false))) continue;
    if (!(await candidate.isEnabled({ timeout: LOCATOR_TIMEOUT_MS }).catch(() => false))) continue;
    const box = await candidate.boundingBox();
    if (!box) continue;
    const verticallyAdjacent = box.y + box.height >= promptBox.y - 64
      && box.y <= promptBox.y + promptBox.height + 64;
    const inRightHalf = box.x >= promptBox.x + promptBox.width / 2;
    const right = box.x + box.width;
    if (verticallyAdjacent && inRightHalf && right > selectedRight) {
      selected = candidate;
      selectedRight = right;
    }
  }
  if (!selected) throw new Error('input-adjacent submit control not found');
  armSubmitCaptures(tabState);
  await submitLocatorClick(selected);
}

function normalizeCaptureSpec(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('capture spec is required');
  const method = typeof raw.method === 'string' ? raw.method.toUpperCase() : '';
  const host = typeof raw.host === 'string' ? raw.host.trim().toLowerCase() : '';
  const path = typeof raw.path === 'string' ? raw.path.trim() : '';
  const pathPrefix = typeof raw.pathPrefix === 'string' ? raw.pathPrefix.trim() : '';
  const maxBytes = Number.isInteger(raw.maxBytes) ? raw.maxBytes : MAX_CAPTURE_BYTES;
  if (!/^[A-Z]+$/.test(method) || !/^[a-z0-9.-]+$/.test(host) || !path.startsWith('/')) {
    throw new Error('capture spec method, host, and path are required');
  }
  if (pathPrefix && !pathPrefix.startsWith('/')) throw new Error('capture spec pathPrefix is invalid');
  if (maxBytes <= 0 || maxBytes > MAX_CAPTURE_BYTES) throw new Error('capture spec maxBytes is invalid');
  return {
    method,
    host,
    path,
    pathPrefix: pathPrefix || null,
    maxBytes,
    activateOnSubmit: raw.activateOnSubmit === true,
  };
}

function matchesResponse(response, spec) {
  const request = response.request();
  if (request.method().toUpperCase() !== spec.method) return false;
  let parsed;
  try {
    parsed = new URL(response.url());
  } catch {
    return false;
  }
  if (parsed.hostname.toLowerCase() !== spec.host) return false;
  const path = parsed.pathname.replace(/\/$/, '') || '/';
  const expectedPath = spec.path.replace(/\/$/, '') || '/';
  return spec.pathPrefix ? path.startsWith(spec.pathPrefix) : path === expectedPath;
}

function responseHeaders(response) {
  const headers = response.headers();
  return typeof headers['content-type'] === 'string' ? headers['content-type'] : '';
}

function registerCapture(tabState, userId, tabId, spec) {
  const captureId = crypto.randomUUID();
  const key = captureKey(userId, tabId, captureId);
  const capture = {
    state: 'waiting', status: null, contentType: '', bodyBase64: '', handler: null,
    spec, tabState, armed: !spec.activateOnSubmit, generation: 0,
  };
  const handler = (response) => {
    if (capture.state !== 'waiting' || !capture.armed || !matchesResponse(response, spec)) return;
    // Each armed period accepts exactly one matching response. Its completion
    // callback is generation-bound, so an older background stream cannot
    // overwrite a later retry after the capture has been reset.
    capture.armed = false;
    const generation = ++capture.generation;
    capture.state = 'streaming';
    capture.status = response.status();
    capture.contentType = responseHeaders(response);
    void response.finished()
      .then(async () => {
        const body = await response.body();
        if (body.length > spec.maxBytes) throw new Error('response body exceeded byte limit');
        if (capture.generation !== generation) return;
        capture.bodyBase64 = body.toString('base64');
        capture.state = 'complete';
      })
      .catch(() => {
        // Do not expose upstream response details. The Python adapter maps this
        // terminal state to its stable provider error vocabulary.
        if (capture.generation === generation) capture.state = 'failed';
      });
  };
  capture.handler = handler;
  tabState.page.on('response', handler);
  captures.set(key, capture);
  return captureId;
}

function armSubmitCaptures(tabState) {
  for (const capture of captures.values()) {
    if (!capture.spec.activateOnSubmit || capture.state !== 'waiting') continue;
    // Captures are registered per tab; comparing the handler page avoids
    // accepting a response from another account even when providers share a
    // browser process.
    if (capture.tabState !== tabState) continue;
    capture.armed = true;
  }
}

function removeCapture(tabState, userId, tabId, captureId) {
  const key = captureKey(userId, tabId, captureId);
  const capture = captures.get(key);
  if (!capture) return false;
  tabState.page.removeListener('response', capture.handler);
  captures.delete(key);
  return true;
}

function removeTabCaptures(userId, tabId) {
  for (const [key, capture] of captures) {
    if (!key.startsWith(`${userId}:${tabId}:`)) continue;
    captures.delete(key);
    // Core owns the page close. Removing its listener is unnecessary once the
    // page is gone and may itself throw while Firefox tears down the context.
    void capture;
  }
}

/** Register GEO's account-scoped, no-script RPA transport. */
export function register(app, ctx) {
  const { sessions, log, safeError, events } = ctx;
  const body = express.json({ limit: '8kb' });

  events.on('tab:destroyed', ({ userId, tabId }) => {
    const normalizedUserId = String(userId);
    removeTabCaptures(normalizedUserId, tabId);
    manualPointers.delete(manualPointerKey(normalizedUserId, tabId));
  });

  // The application can only send a bounded input event to a tab it owns. This
  // deliberately offers no generic evaluate, selector, or navigation surface.
  app.post('/rpa/tabs/:tabId/manual-input', body, async (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    try {
      await dispatchManualPointer(
        found.tabState,
        userId,
        req.params.tabId,
        normalizeManualInput(req.body?.event),
      );
      return res.json({ ok: true });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  app.get('/rpa/tabs/:tabId/viewport', async (req, res) => {
    const userId = typeof req.query.userId === 'string' ? req.query.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    try {
      let viewport = found.tabState.page.viewportSize();
      if (!viewport) {
        // Camoufox contexts configured from a screen size may not expose
        // Playwright's viewportSize. The root box still yields the CSS width;
        // the client uses that uniform device scale for both input axes.
        const rootBox = await found.tabState.page.locator('html').boundingBox();
        if (!rootBox) throw new Error('Tab viewport is unavailable');
        viewport = { width: Math.round(rootBox.width), height: Math.round(rootBox.height) };
      }
      if (!Number.isInteger(viewport.width) || !Number.isInteger(viewport.height)
        || viewport.width <= 0 || viewport.height <= 0) {
        throw new Error('Tab viewport is unavailable');
      }
      return res.json({ ok: true, width: viewport.width, height: viewport.height });
    } catch (error) {
      return res.status(409).json({ error: safeError(error) });
    }
  });

  app.post('/rpa/tabs/:tabId/network-captures', body, (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    try {
      let active = 0;
      for (const key of captures.keys()) if (key.startsWith(`${userId}:${req.params.tabId}:`)) active += 1;
      if (active >= MAX_CAPTURES_PER_TAB) return res.status(429).json({ error: 'Too many response captures' });
      const captureId = registerCapture(found.tabState, userId, req.params.tabId, normalizeCaptureSpec(req.body?.spec));
      return res.status(201).json({ captureId });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  app.get('/rpa/tabs/:tabId/network-captures/:captureId', (req, res) => {
    const userId = typeof req.query.userId === 'string' ? req.query.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    const capture = found && captures.get(captureKey(userId, req.params.tabId, req.params.captureId));
    if (!capture) return res.status(404).json({ error: 'Network capture not found' });
    if (capture.state === 'waiting' || capture.state === 'streaming') {
      return res.status(202).json({ state: capture.state });
    }
    if (capture.state === 'failed') return res.status(200).json({ state: 'failed' });
    return res.json({ state: 'complete', status: capture.status, contentType: capture.contentType, bodyBase64: capture.bodyBase64 });
  });

  app.post('/rpa/tabs/:tabId/network-captures/:captureId/reset', body, (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    const capture = found && captures.get(captureKey(userId, req.params.tabId, req.params.captureId));
    if (!capture) return res.status(404).json({ error: 'Network capture not found' });
    // A reset is only valid after a terminal response. It must never cut off a
    // currently generating response merely because the caller observed a UI toast.
    if (capture.state !== 'complete' && capture.state !== 'failed') {
      return res.status(409).json({
        error: 'Network capture is still active',
        code: 'network_capture_active',
      });
    }
    capture.state = 'waiting';
    capture.status = null;
    capture.contentType = '';
    capture.bodyBase64 = '';
    capture.armed = !capture.spec.activateOnSubmit;
    capture.generation += 1;
    return res.json({ ok: true });
  });

  app.delete('/rpa/tabs/:tabId/network-captures/:captureId', body, (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found || !removeCapture(found.tabState, userId, req.params.tabId, req.params.captureId)) {
      return res.status(404).json({ error: 'Network capture not found' });
    }
    return res.json({ ok: true });
  });

  app.post('/rpa/tabs/:tabId/locator-read', body, async (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    const operation = req.body?.operation;
    try {
      const { locator } = targetLocator(found.tabState, req.body?.target);
      if (operation === 'count') return res.json({ ok: true, result: await locator.count() });
      // Playwright 的 isVisible/isEnabled/isEditable 在目标尚未挂载时可用于轮询。
      // 将这类只读探针返回 false，而不是把正常加载过程升级为 400；写入、点击和
      // 文本读取仍必须经 selectedLocator 严格验证，不能借此扩大操作范围。
      const count = await locator.count();
      if (count === 0 && ['visible', 'enabled', 'editable', 'has_value_property', 'content_editable'].includes(operation)) {
        return res.json({ ok: true, result: false });
      }
      let selected;
      try {
        selected = await selectedLocator(found.tabState, req.body?.target);
      } catch (error) {
        // 聊天页提交后会在同一帧替换输入框与用户消息节点。只读状态探针的
        // count 与实际读取之间若发生此类替换，等价于本轮尚未观察到目标；
        // 不能把它升级为任务失败。文本读取、滚动、点击和输入仍严格报错。
        if (['visible', 'enabled', 'editable', 'has_value_property', 'content_editable'].includes(operation)) {
          return res.json({ ok: true, result: false });
        }
        throw error;
      }
      let result;
      switch (operation) {
        case 'visible': result = await selected.isVisible({ timeout: LOCATOR_TIMEOUT_MS }); break;
        case 'enabled': result = await selected.isEnabled({ timeout: LOCATOR_TIMEOUT_MS }); break;
        case 'editable': result = await selected.isEditable({ timeout: LOCATOR_TIMEOUT_MS }); break;
        case 'inner_text': result = await selected.innerText({ timeout: LOCATOR_TIMEOUT_MS }); break;
        case 'answer_markdown': result = await selected.evaluate((node) => {
          // 固定在受控插件内的只读转换：只读取已选择的助手答案容器，不接收调用方脚本。
          const text = (node.innerText || '').trim();
          const seen = new Set();
          const citations = [];
          for (const link of node.querySelectorAll('a[href]')) {
            let url;
            try { url = new URL(link.getAttribute('href'), document.baseURI); } catch (_) { continue; }
            if (!['http:', 'https:'].includes(url.protocol) || seen.has(url.href)) continue;
            seen.add(url.href);
            citations.push({ url: url.href, title: (link.innerText || link.getAttribute('title') || '').trim() });
          }
          return { markdown: text, citations };
        }); break;
        case 'input_value': result = await selected.inputValue({ timeout: LOCATOR_TIMEOUT_MS }); break;
        case 'has_value_property': result = await selected.inputValue({ timeout: LOCATOR_TIMEOUT_MS }).then(() => true).catch(() => false); break;
        case 'content_editable': result = await selected.isEditable({ timeout: LOCATOR_TIMEOUT_MS }); break;
        case 'scroll_into_view': await selected.scrollIntoViewIfNeeded({ timeout: LOCATOR_TIMEOUT_MS }); result = true; break;
        default: throw new Error('locator operation is not allowed');
      }
      return res.json({ ok: true, result });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  // 仅返回当前受控 Tab 的自然地址，用于保存已提交会话的服务端定位信息；不接受导航参数。
  app.get('/rpa/tabs/:tabId/current-url', async (req, res) => {
    const userId = typeof req.query?.userId === 'string' ? req.query.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    return res.json({ url: found.tabState.page.url() });
  });

  app.post('/rpa/tabs/:tabId/locator-focus', body, async (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    try { await (await selectedLocator(found.tabState, req.body?.target)).focus({ timeout: LOCATOR_TIMEOUT_MS }); return res.json({ ok: true }); }
    catch (error) { return res.status(400).json({ error: safeError(error) }); }
  });

  app.post('/rpa/tabs/:tabId/locator-key', body, async (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    if (req.body?.key !== 'Enter') return res.status(400).json({ error: 'locator key is not allowed' });
    try { await (await selectedLocator(found.tabState, req.body?.target)).press('Enter', { timeout: LOCATOR_TIMEOUT_MS }); return res.json({ ok: true }); }
    catch (error) { return res.status(400).json({ error: safeError(error) }); }
  });

  app.post('/rpa/tabs/:tabId/locator-input', body, async (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const text = req.body?.text;
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    if (typeof text !== 'string' || text.length === 0 || text.length > 4096) return res.status(400).json({ error: 'locator input text is invalid' });
    try { await (await selectedLocator(found.tabState, req.body?.target)).pressSequentially(text, { delay: 0, timeout: LOCATOR_TIMEOUT_MS }); return res.json({ ok: true }); }
    catch (error) { return res.status(400).json({ error: safeError(error) }); }
  });

  app.post('/rpa/tabs/:tabId/locator-click', body, async (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    try { await submitLocatorClick(await selectedLocator(found.tabState, req.body?.target)); return res.json({ ok: true }); }
    catch (error) {
      const message = safeError(error);
      return res.status(400).json({
        error: message,
        ...(isSubmissionNotDispatched(error) ? { code: 'submission_not_dispatched' } : {}),
      });
    }
  });

  app.post('/rpa/tabs/:tabId/locator-submit', body, async (req, res) => {
    const userId = typeof req.body?.userId === 'string' ? req.body.userId : '';
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: 'Tab not found' });
    try { await submitNearLocator(found.tabState, req.body?.target); return res.json({ ok: true }); }
    catch (error) { return res.status(400).json({ error: safeError(error) }); }
  });

  log('info', 'GEO RPA plugin enabled', { protocolVersion: 1 });
}
