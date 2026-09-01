import crypto from "node:crypto";
import express from "express";
import {
  createManualWindowIdentity,
  listTopLevelX11WindowIds,
  openManualPopup,
  waitForNewX11WindowId,
} from "./manual-window.js";
import {
  enforceManualWindowGeometry,
  readX11WindowTree,
  startManualWindowGeometryGuard,
} from "./x11-window.js";
import { startWindowVncPublisher } from "./window-vnc-publisher.js";

// Fast read probes should fail quickly, but a real chat submission can take
// longer while the page finishes its own event handlers. Keep the two budgets
// separate so slow provider UI does not turn a dispatched native click into a
// false task failure.
const LOCATOR_TIMEOUT_MS = 3000;
const LOCATOR_SCREENSHOT_TIMEOUT_MS = 15_000;
const SUBMISSION_CLICK_TIMEOUT_MS = 12_000;
const MAX_SELECTOR_LENGTH = 1024;
const MAX_CAPTURE_BYTES = 2 * 1024 * 1024;
const MAX_CAPTURES_PER_TAB = 2;
const MAX_MANUAL_TEXT_LENGTH = 10_000;
const MAX_MANUAL_COORDINATE = 10_000;
const MAX_MANUAL_WHEEL_DELTA = 10_000;
const VIEWPORT_READY_ATTEMPTS = 10;
const VIEWPORT_READY_RETRY_DELAY_MS = 150;
const MANUAL_MOUSE_TYPES = new Set(["move", "down", "up", "wheel"]);
const MANUAL_BUTTONS = new Set(["left", "middle", "right"]);
const MANUAL_MODIFIERS = new Set(["Alt", "Control", "Meta", "Shift"]);
const MANUAL_KEYS = new Set([
  "Enter",
  "Tab",
  "Escape",
  "Backspace",
  "Delete",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
  "PageUp",
  "PageDown",
  "Insert",
  "a",
  "c",
  "v",
  "x",
  "y",
  "z",
  "A",
  "C",
  "V",
  "X",
  "Y",
  "Z",
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

function validViewport(viewport) {
  return (
    viewport &&
    Number.isInteger(viewport.width) &&
    Number.isInteger(viewport.height) &&
    viewport.width > 0 &&
    viewport.height > 0
  );
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function readReadyViewport(page, wait = sleep) {
  let lastError = null;
  for (let attempt = 0; attempt < VIEWPORT_READY_ATTEMPTS; attempt += 1) {
    try {
      const configuredViewport = page.viewportSize();
      if (validViewport(configuredViewport)) return configuredViewport;

      // Camoufox 的 viewport:null Context 不会暴露 Playwright viewportSize。
      // 在虚拟显示器中 html 的布局框也可能恒为空，但已加载页面的 body 仍处于同一
      // CSS 坐标系。二者均不可用时才等待，不能使用截图像素尺寸替代鼠标输入坐标。
      for (const rootSelector of ["html", "body"]) {
        const rootBox = await page.locator(rootSelector).boundingBox();
        const viewport = rootBox && {
          width: Math.round(rootBox.width),
          height: Math.round(rootBox.height),
        };
        if (validViewport(viewport)) return viewport;
      }
      // 某些 Camoufox 指纹配置会令根元素的 Playwright boundingBox 恒为空，
      // 但页面窗口本身仍正常接收截图和原生鼠标事件。此处的表达式固定在服务端，
      // 不接收调用方脚本或页面内容，只读取 page.mouse 使用的 CSS 视口坐标。
      const windowViewport = await page.locator("body").evaluate((node) => {
        const view = node.ownerDocument.defaultView;
        return { width: view?.innerWidth, height: view?.innerHeight };
      });
      if (validViewport(windowViewport)) return windowViewport;
      lastError = new Error("Tab viewport is unavailable");
    } catch (error) {
      lastError = error;
    }
    if (attempt + 1 < VIEWPORT_READY_ATTEMPTS) {
      await wait(VIEWPORT_READY_RETRY_DELAY_MS);
    }
  }
  throw lastError || new Error("Tab viewport is unavailable");
}

function validSelector(value) {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.length <= MAX_SELECTOR_LENGTH
  );
}

function isFiniteNumber(value, minimum, maximum) {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= minimum &&
    value <= maximum
  );
}

function normalizeManualInput(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw))
    throw new Error("manual input event is required");
  if (raw.kind === "text") {
    if (
      typeof raw.text !== "string" ||
      raw.text.length === 0 ||
      raw.text.length > MAX_MANUAL_TEXT_LENGTH
    ) {
      throw new Error("manual text is invalid");
    }
    return { kind: "text", text: raw.text };
  }
  if (raw.kind === "key") {
    const modifiers = Array.isArray(raw.modifiers) ? raw.modifiers : [];
    if (
      (!MANUAL_KEYS.has(raw.key) && !MANUAL_MODIFIERS.has(raw.key)) ||
      modifiers.some((modifier) => !MANUAL_MODIFIERS.has(modifier))
    ) {
      throw new Error("manual key is invalid");
    }
    return { kind: "key", key: raw.key, modifiers };
  }
  if (raw.kind === "mouse") {
    if (
      !MANUAL_MOUSE_TYPES.has(raw.type) ||
      !isFiniteNumber(raw.x, 0, MAX_MANUAL_COORDINATE) ||
      !isFiniteNumber(raw.y, 0, MAX_MANUAL_COORDINATE)
    ) {
      throw new Error("manual mouse coordinates are invalid");
    }
    if (raw.type === "wheel") {
      if (
        !isFiniteNumber(
          raw.delta_x,
          -MAX_MANUAL_WHEEL_DELTA,
          MAX_MANUAL_WHEEL_DELTA,
        ) ||
        !isFiniteNumber(
          raw.delta_y,
          -MAX_MANUAL_WHEEL_DELTA,
          MAX_MANUAL_WHEEL_DELTA,
        )
      ) {
        throw new Error("manual wheel delta is invalid");
      }
      return {
        kind: "mouse",
        type: "wheel",
        x: raw.x,
        y: raw.y,
        deltaX: raw.delta_x,
        deltaY: raw.delta_y,
      };
    }
    const button = MANUAL_BUTTONS.has(raw.button) ? raw.button : "left";
    return { kind: "mouse", type: raw.type, x: raw.x, y: raw.y, button };
  }
  throw new Error("manual input kind is invalid");
}

async function dispatchManualInput(page, event) {
  if (event.kind === "text") {
    await page.keyboard.insertText(event.text);
    return;
  }
  if (event.kind === "key") {
    // Modifier keydown/keyup is reported separately by the canvas. The next
    // shortcut event carries the full modifier set, so no standalone input is needed.
    if (MANUAL_MODIFIERS.has(event.key)) return;
    const prefix = event.modifiers.join("+");
    await page.keyboard.press(prefix ? `${prefix}+${event.key}` : event.key);
    return;
  }
  if (event.type === "wheel") {
    await page.mouse.move(event.x, event.y);
    await page.mouse.wheel(event.deltaX, event.deltaY);
    return;
  }
  // Two short movement steps preserve a normal pointer transition without
  // turning a manual click into a visibly slow cursor animation.
  await page.mouse.move(event.x, event.y, {
    steps: event.type === "move" ? 1 : 2,
  });
  if (event.type === "down") await page.mouse.down({ button: event.button });
  if (event.type === "up") await page.mouse.up({ button: event.button });
}

async function dispatchManualPointer(tabState, userId, tabId, event) {
  if (event.kind !== "mouse" || event.type === "wheel") {
    await dispatchManualInput(tabState.page, event);
    return;
  }
  const key = manualPointerKey(userId, tabId);
  const pointer = manualPointers.get(key);
  if (event.type === "down") {
    await tabState.page.mouse.move(event.x, event.y, { steps: 2 });
    manualPointers.set(key, {
      x: event.x,
      y: event.y,
      button: event.button,
      dragging: false,
    });
    return;
  }
  if (event.type === "move" && pointer) {
    if (!pointer.dragging) {
      await tabState.page.mouse.down({ button: pointer.button });
      pointer.dragging = true;
    }
    await tabState.page.mouse.move(event.x, event.y, { steps: 1 });
    return;
  }
  if (event.type === "up" && pointer) {
    manualPointers.delete(key);
    if (!pointer.dragging) {
      // A click must be a single browser input sequence. Sending down and up
      // through separate HTTP requests is accepted by Playwright but can be
      // ignored by interactive Camoufox pages.
      await tabState.page.mouse.click(event.x, event.y, {
        button: pointer.button,
        delay: 50,
      });
      return;
    }
    await tabState.page.mouse.move(event.x, event.y, { steps: 1 });
    await tabState.page.mouse.up({ button: pointer.button });
    return;
  }
  if (event.type === "up") {
    await tabState.page.mouse.click(event.x, event.y, {
      button: event.button,
      delay: 50,
    });
  }
}

function targetLocator(tabState, target) {
  if (!target || typeof target !== "object" || Array.isArray(target)) {
    throw new Error("locator target is required");
  }
  const hasSelector = validSelector(target.selector);
  const hasText =
    typeof target.text === "string" &&
    target.text.length > 0 &&
    target.text.length <= 4096;
  if (Number(hasSelector) + Number(hasText) !== 1) {
    throw new Error("locator target requires exactly one selector or text");
  }
  const rawIndex = target.index === undefined ? 0 : target.index;
  if (!Number.isInteger(rawIndex) || rawIndex < -1) {
    throw new Error("locator index is invalid");
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
    throw new Error("locator not found");
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
  return message.includes("intercepts pointer events");
}

async function submitNearLocator(tabState, target) {
  const prompt = await selectedLocator(tabState, target);
  const promptBox = await prompt.boundingBox();
  if (!promptBox) throw new Error("input locator has no visible bounds");
  // SVG 只是按钮的视觉子节点，不是可提交控件。把它作为候选会绕过 disabled
  // 状态，并可能误点输入框中的“更多”图标；只保留真实可操作的按钮语义。
  const controls = tabState.page.locator('button, [role="button"]');
  const count = Math.min(await controls.count(), 128);
  let selected = null;
  let selectedRight = Number.NEGATIVE_INFINITY;
  for (let index = 0; index < count; index += 1) {
    const candidate = controls.nth(index);
    if (
      !(await candidate
        .isVisible({ timeout: LOCATOR_TIMEOUT_MS })
        .catch(() => false))
    )
      continue;
    if (
      !(await candidate
        .isEnabled({ timeout: LOCATOR_TIMEOUT_MS })
        .catch(() => false))
    )
      continue;
    const box = await candidate.boundingBox();
    if (!box) continue;
    const verticallyAdjacent =
      box.y + box.height >= promptBox.y - 64 &&
      box.y <= promptBox.y + promptBox.height + 64;
    const inRightHalf = box.x >= promptBox.x + promptBox.width / 2;
    const right = box.x + box.width;
    if (verticallyAdjacent && inRightHalf && right > selectedRight) {
      selected = candidate;
      selectedRight = right;
    }
  }
  if (!selected) throw new Error("input-adjacent submit control not found");
  armSubmitCaptures(tabState);
  await submitLocatorClick(selected);
}

function normalizeCaptureSpec(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw))
    throw new Error("capture spec is required");
  const method = typeof raw.method === "string" ? raw.method.toUpperCase() : "";
  const host =
    typeof raw.host === "string" ? raw.host.trim().toLowerCase() : "";
  const path = typeof raw.path === "string" ? raw.path.trim() : "";
  const pathPrefix =
    typeof raw.pathPrefix === "string" ? raw.pathPrefix.trim() : "";
  const maxBytes = Number.isInteger(raw.maxBytes)
    ? raw.maxBytes
    : MAX_CAPTURE_BYTES;
  if (
    !/^[A-Z]+$/.test(method) ||
    !/^[a-z0-9.-]+$/.test(host) ||
    !path.startsWith("/")
  ) {
    throw new Error("capture spec method, host, and path are required");
  }
  if (pathPrefix && !pathPrefix.startsWith("/"))
    throw new Error("capture spec pathPrefix is invalid");
  if (maxBytes <= 0 || maxBytes > MAX_CAPTURE_BYTES)
    throw new Error("capture spec maxBytes is invalid");
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
  const path = parsed.pathname.replace(/\/$/, "") || "/";
  const expectedPath = spec.path.replace(/\/$/, "") || "/";
  return spec.pathPrefix
    ? path.startsWith(spec.pathPrefix)
    : path === expectedPath;
}

function responseHeaders(response) {
  const headers = response.headers();
  return typeof headers["content-type"] === "string"
    ? headers["content-type"]
    : "";
}

function registerCapture(tabState, userId, tabId, spec) {
  const captureId = crypto.randomUUID();
  const key = captureKey(userId, tabId, captureId);
  const capture = {
    state: "waiting",
    status: null,
    contentType: "",
    bodyBase64: "",
    handler: null,
    spec,
    tabState,
    armed: !spec.activateOnSubmit,
    generation: 0,
  };
  const handler = (response) => {
    if (
      capture.state !== "waiting" ||
      !capture.armed ||
      !matchesResponse(response, spec)
    )
      return;
    // Each armed period accepts exactly one matching response. Its completion
    // callback is generation-bound, so an older background stream cannot
    // overwrite a later retry after the capture has been reset.
    capture.armed = false;
    const generation = ++capture.generation;
    capture.state = "streaming";
    capture.status = response.status();
    capture.contentType = responseHeaders(response);
    void response
      .finished()
      .then(async () => {
        const body = await response.body();
        if (body.length > spec.maxBytes)
          throw new Error("response body exceeded byte limit");
        if (capture.generation !== generation) return;
        capture.bodyBase64 = body.toString("base64");
        capture.state = "complete";
      })
      .catch(() => {
        // Do not expose upstream response details. The Python adapter maps this
        // terminal state to its stable provider error vocabulary.
        if (capture.generation === generation) capture.state = "failed";
      });
  };
  capture.handler = handler;
  tabState.page.on("response", handler);
  captures.set(key, capture);
  return captureId;
}

function armSubmitCaptures(tabState) {
  for (const capture of captures.values()) {
    if (!capture.spec.activateOnSubmit || capture.state !== "waiting") continue;
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
  tabState.page.removeListener("response", capture.handler);
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
export function register(app, ctx, pluginConfig = {}) {
  const { sessions, log, safeError, events } = ctx;
  const body = express.json({ limit: "8kb" });
  const manualWindows = new Map();
  const taskWindows = new Map();
  let browserDisplay = null;
  // 首期只允许一个人工租约，因此使用固定的容器回环端口即可。端口不透出
  // Camofox HTTP 响应；后续业务接入只能通过 FastAPI 的同源 WebSocket 代理。
  const manualWindowVnc = {
    enabled: pluginConfig.manualWindowVnc?.enabled === true,
    rfbPort: Number(pluginConfig.manualWindowVnc?.rfbPort || 5901),
    websocketPort: Number(pluginConfig.manualWindowVnc?.websocketPort || 6081),
  };
  // 任务观察器不使用固定端口：每个正在被观看的任务拥有独立的 x11vnc/websockify
  // 对。端口只在 Camofox 与应用容器之间传递，绝不出现在浏览器响应中。
  let nextTaskWindowPort = 6082;

  function allocateTaskWindowPorts() {
    if (nextTaskWindowPort > 65000)
      throw new Error("Task VNC port range is exhausted");
    const websocketPort = nextTaskWindowPort;
    nextTaskWindowPort += 1;
    return { rfbPort: websocketPort - 180, websocketPort };
  }

  events.on("browser:launched", ({ display }) => {
    browserDisplay = typeof display === "string" ? display : null;
  });

  async function closeManualWindow(handle) {
    const manualWindow = manualWindows.get(handle);
    if (!manualWindow) return false;
    manualWindows.delete(handle);
    manualWindow.stopGeometryGuard?.();
    await manualWindow.publisher?.stop().catch(() => {});
    await manualWindow.page.close().catch(() => {});
    return true;
  }

  /**
   * Context deletion bypasses the per-tab destroy event. Revoke every
   * publisher first, while the popup page is still valid, so its fixed ports
   * cannot survive into the next manual lease.
   */
  async function closeManualWindowsForUser(userId) {
    const handles = [...manualWindows.entries()]
      .filter(([, manualWindow]) => manualWindow.userId === String(userId))
      .map(([handle]) => handle);
    await Promise.all(handles.map((handle) => closeManualWindow(handle)));
  }

  async function closeTaskWindow(handle) {
    const taskWindow = taskWindows.get(handle);
    if (!taskWindow) return false;
    taskWindows.delete(handle);
    await taskWindow.publisher?.stop().catch(() => {});
    return true;
  }

  async function closeTaskWindowsForUser(userId) {
    const handles = [...taskWindows.entries()]
      .filter(([, taskWindow]) => taskWindow.userId === String(userId))
      .map(([handle]) => handle);
    await Promise.all(handles.map((handle) => closeTaskWindow(handle)));
  }

  function findManagedTabId(session, page) {
    for (const group of session.tabGroups.values()) {
      for (const [tabId, tabState] of group) {
        if (tabState.page === page) return tabId;
      }
    }
    return null;
  }

  async function waitForManagedTabId(session, page) {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const tabId = findManagedTabId(session, page);
      if (tabId) return tabId;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error("Manual popup was not registered as a managed tab");
  }

  events.on("tab:destroyed", ({ userId, tabId }) => {
    for (const [handle, manualWindow] of manualWindows) {
      if (
        manualWindow.userId === String(userId) &&
        manualWindow.tabId === tabId
      ) {
        void closeManualWindow(handle);
      }
    }
    for (const [handle, taskWindow] of taskWindows) {
      if (taskWindow.userId === String(userId) && taskWindow.tabId === tabId) {
        void closeTaskWindow(handle);
      }
    }
  });

  events.on("session:destroying", async ({ userId }) => {
    await closeManualWindowsForUser(userId);
    await closeTaskWindowsForUser(userId);
  });

  events.on("browser:closed", () => {
    for (const manualWindow of manualWindows.values()) {
      void manualWindow.publisher?.stop().catch(() => {});
    }
    manualWindows.clear();
    for (const taskWindow of taskWindows.values()) {
      void taskWindow.publisher?.stop().catch(() => {});
    }
    taskWindows.clear();
    browserDisplay = null;
  });

  // This is deliberately an internal feasibility endpoint. It creates a fixed
  // blank popup in the existing Context and returns only an opaque handle; it
  // cannot enumerate X11 windows, execute caller-provided JavaScript, or move
  // arbitrary tabs. The application layer must not consume it before the spike
  // has shown that Firefox exposes a distinct top-level X11 window.
  app.post("/rpa/tabs/:tabId/manual-window", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    if (!browserDisplay)
      return res.status(409).json({ error: "X11 display is unavailable" });
    if (
      [...manualWindows.values()].some(
        (entry) =>
          entry.userId === userId &&
          (entry.tabId === req.params.tabId ||
            entry.sourceTabId === req.params.tabId),
      )
    ) {
      return res
        .status(409)
        .json({ error: "Manual window already exists for this tab" });
    }

    const identity = createManualWindowIdentity();
    let popup = null;
    try {
      const existingWindowIds = new Set(
        listTopLevelX11WindowIds(await readX11WindowTree(browserDisplay)),
      );
      popup = await openManualPopup(
        found.tabState.page,
        identity.title,
        found.tabState.page.url(),
        { manualWindow: true },
      );
      const popupTabId = await waitForManagedTabId(found.session, popup);
      const windowId = await waitForNewX11WindowId({
        display: browserDisplay,
        existingWindowIds,
        readWindowTree: readX11WindowTree,
      });
      // Page-level resize requests are advisory in Firefox. Enforce the native
      // X11 geometry before x11vnc snapshots its framebuffer dimensions.
      await enforceManualWindowGeometry(browserDisplay, windowId);
      const manualWindow = {
        userId,
        tabId: popupTabId,
        sourceTabId: req.params.tabId,
        page: popup,
        state: "window_ready",
        publisher: null,
        stopGeometryGuard: null,
      };
      manualWindows.set(identity.handle, manualWindow);
      popup.once("close", () => {
        const activeWindow = manualWindows.get(identity.handle);
        if (activeWindow !== manualWindow) return;
        manualWindows.delete(identity.handle);
        activeWindow.stopGeometryGuard?.();
        void activeWindow.publisher?.stop().catch(() => {});
      });
      manualWindow.stopGeometryGuard = startManualWindowGeometryGuard(
        browserDisplay,
        windowId,
      );
      if (manualWindowVnc.enabled) {
        manualWindow.publisher = await startWindowVncPublisher({
          display: browserDisplay,
          windowId,
          rfbPort: manualWindowVnc.rfbPort,
          websocketPort: manualWindowVnc.websocketPort,
          onExit: () => {
            manualWindow.state = "failed";
            void closeManualWindow(identity.handle);
          },
        });
        manualWindow.state = "published";
      }
      // windowId is intentionally neither stored nor returned by this route.
      // targetId is an account-scoped Camofox tab id consumed only by FastAPI
      // for health checks and cleanup. It is never forwarded to the browser.
      return res.status(201).json({
        handle: identity.handle,
        state: manualWindow.state,
        targetId: popupTabId,
      });
    } catch (error) {
      await popup?.close().catch(() => {});
      return res.status(409).json({ error: safeError(error) });
    }
  });

  app.get("/rpa/manual-windows/:handle", async (req, res) => {
    const userId = typeof req.query.userId === "string" ? req.query.userId : "";
    const manualWindow = manualWindows.get(req.params.handle);
    if (
      !manualWindow ||
      manualWindow.userId !== userId ||
      manualWindow.page.isClosed()
    ) {
      return res.status(404).json({ error: "Manual window not found" });
    }
    return res.json({ state: manualWindow.state });
  });

  app.delete("/rpa/manual-windows/:handle", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const manualWindow = manualWindows.get(req.params.handle);
    if (!manualWindow || manualWindow.userId !== userId) {
      return res.status(404).json({ error: "Manual window not found" });
    }
    await closeManualWindow(req.params.handle);
    return res.status(204).end();
  });

  /**
   * Promote a real task tab into its own Firefox window before the provider starts
   * interacting with it. The popup becomes the task's only managed tab; this is
   * deliberately different from a screenshot mirror so VNC follows live DOM and
   * rendering updates from the actual automation target.
   */
  app.post("/rpa/tabs/:tabId/task-window", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    if (!browserDisplay)
      return res.status(409).json({ error: "X11 display is unavailable" });

    const identity = createManualWindowIdentity();
    let popup = null;
    try {
      const existingWindowIds = new Set(
        listTopLevelX11WindowIds(await readX11WindowTree(browserDisplay)),
      );
      popup = await openManualPopup(
        found.tabState.page,
        identity.title,
        found.tabState.page.url(),
      );
      const popupTabId = await waitForManagedTabId(found.session, popup);
      const windowId = await waitForNewX11WindowId({
        display: browserDisplay,
        existingWindowIds,
        readWindowTree: readX11WindowTree,
      });
      const taskWindow = {
        userId,
        tabId: popupTabId,
        page: popup,
        windowId,
        state: "window_ready",
        publisher: null,
        websocketPort: null,
      };
      taskWindows.set(identity.handle, taskWindow);
      popup.once("close", () => {
        const activeWindow = taskWindows.get(identity.handle);
        if (activeWindow !== taskWindow) return;
        taskWindows.delete(identity.handle);
        void activeWindow.publisher?.stop().catch(() => {});
      });
      // The provider executor has not started yet, so replacing the source tab
      // here cannot lose pending input or a response stream.
      await found.tabState.page.close();
      return res.status(201).json({
        handle: identity.handle,
        state: taskWindow.state,
        targetId: popupTabId,
      });
    } catch (error) {
      await popup?.close().catch(() => {});
      return res.status(409).json({ error: safeError(error) });
    }
  });

  /** Start or reuse the private publisher for one real task window. */
  app.post("/rpa/task-windows/:handle/observer", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const taskWindow = taskWindows.get(req.params.handle);
    if (
      !taskWindow ||
      taskWindow.userId !== userId ||
      taskWindow.page.isClosed()
    ) {
      return res.status(404).json({ error: "Task window not found" });
    }
    if (taskWindow.publisher) {
      return res.json({
        state: taskWindow.state,
        websocketPort: taskWindow.websocketPort,
      });
    }
    try {
      const { rfbPort, websocketPort } = allocateTaskWindowPorts();
      taskWindow.publisher = await startWindowVncPublisher({
        display: browserDisplay,
        windowId: taskWindow.windowId,
        rfbPort,
        websocketPort,
        onExit: () => {
          taskWindow.publisher = null;
          taskWindow.websocketPort = null;
          taskWindow.state = "failed";
        },
      });
      taskWindow.websocketPort = websocketPort;
      taskWindow.state = "published";
      return res.json({ state: taskWindow.state, websocketPort });
    } catch (error) {
      taskWindow.publisher = null;
      taskWindow.websocketPort = null;
      taskWindow.state = "failed";
      return res.status(409).json({ error: safeError(error) });
    }
  });

  /** Stop only the observer publisher. The provider task keeps running in its tab. */
  app.delete("/rpa/task-windows/:handle/observer", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const taskWindow = taskWindows.get(req.params.handle);
    if (!taskWindow || taskWindow.userId !== userId) {
      return res.status(404).json({ error: "Task window not found" });
    }
    await taskWindow.publisher?.stop().catch(() => {});
    taskWindow.publisher = null;
    taskWindow.websocketPort = null;
    taskWindow.state = "window_ready";
    return res.status(204).end();
  });

  events.on("tab:destroyed", ({ userId, tabId }) => {
    const normalizedUserId = String(userId);
    removeTabCaptures(normalizedUserId, tabId);
    manualPointers.delete(manualPointerKey(normalizedUserId, tabId));
  });

  // The application can only send a bounded input event to a tab it owns. This
  // deliberately offers no generic evaluate, selector, or navigation surface.
  app.post("/rpa/tabs/:tabId/manual-input", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
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

  app.get("/rpa/tabs/:tabId/viewport", async (req, res) => {
    const userId = typeof req.query.userId === "string" ? req.query.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    try {
      const viewport = await readReadyViewport(found.tabState.page);
      return res.json({
        ok: true,
        width: viewport.width,
        height: viewport.height,
      });
    } catch (error) {
      return res.status(409).json({ error: safeError(error) });
    }
  });

  app.post("/rpa/tabs/:tabId/network-captures", body, (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    try {
      let active = 0;
      for (const key of captures.keys())
        if (key.startsWith(`${userId}:${req.params.tabId}:`)) active += 1;
      if (active >= MAX_CAPTURES_PER_TAB)
        return res.status(429).json({ error: "Too many response captures" });
      const captureId = registerCapture(
        found.tabState,
        userId,
        req.params.tabId,
        normalizeCaptureSpec(req.body?.spec),
      );
      return res.status(201).json({ captureId });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  app.get("/rpa/tabs/:tabId/network-captures/:captureId", (req, res) => {
    const userId = typeof req.query.userId === "string" ? req.query.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    const capture =
      found &&
      captures.get(captureKey(userId, req.params.tabId, req.params.captureId));
    if (!capture)
      return res.status(404).json({ error: "Network capture not found" });
    if (capture.state === "waiting" || capture.state === "streaming") {
      return res.status(202).json({ state: capture.state });
    }
    if (capture.state === "failed")
      return res.status(200).json({ state: "failed" });
    return res.json({
      state: "complete",
      status: capture.status,
      contentType: capture.contentType,
      bodyBase64: capture.bodyBase64,
    });
  });

  app.post(
    "/rpa/tabs/:tabId/network-captures/:captureId/reset",
    body,
    (req, res) => {
      const userId =
        typeof req.body?.userId === "string" ? req.body.userId : "";
      const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
      const capture =
        found &&
        captures.get(
          captureKey(userId, req.params.tabId, req.params.captureId),
        );
      if (!capture)
        return res.status(404).json({ error: "Network capture not found" });
      // A reset is only valid after a terminal response. It must never cut off a
      // currently generating response merely because the caller observed a UI toast.
      if (capture.state !== "complete" && capture.state !== "failed") {
        return res.status(409).json({
          error: "Network capture is still active",
          code: "network_capture_active",
        });
      }
      capture.state = "waiting";
      capture.status = null;
      capture.contentType = "";
      capture.bodyBase64 = "";
      capture.armed = !capture.spec.activateOnSubmit;
      capture.generation += 1;
      return res.json({ ok: true });
    },
  );

  app.delete(
    "/rpa/tabs/:tabId/network-captures/:captureId",
    body,
    (req, res) => {
      const userId =
        typeof req.body?.userId === "string" ? req.body.userId : "";
      const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
      if (
        !found ||
        !removeCapture(
          found.tabState,
          userId,
          req.params.tabId,
          req.params.captureId,
        )
      ) {
        return res.status(404).json({ error: "Network capture not found" });
      }
      return res.json({ ok: true });
    },
  );

  app.post("/rpa/tabs/:tabId/locator-read", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    const operation = req.body?.operation;
    try {
      const { locator } = targetLocator(found.tabState, req.body?.target);
      if (operation === "count")
        return res.json({ ok: true, result: await locator.count() });
      // Playwright 的 isVisible/isEnabled/isEditable 在目标尚未挂载时可用于轮询。
      // 将这类只读探针返回 false，而不是把正常加载过程升级为 400；写入、点击和
      // 文本读取仍必须经 selectedLocator 严格验证，不能借此扩大操作范围。
      const count = await locator.count();
      if (
        count === 0 &&
        [
          "visible",
          "enabled",
          "editable",
          "has_value_property",
          "content_editable",
        ].includes(operation)
      ) {
        return res.json({ ok: true, result: false });
      }
      let selected;
      try {
        selected = await selectedLocator(found.tabState, req.body?.target);
      } catch (error) {
        // 聊天页提交后会在同一帧替换输入框与用户消息节点。只读状态探针的
        // count 与实际读取之间若发生此类替换，等价于本轮尚未观察到目标；
        // 不能把它升级为任务失败。文本读取、滚动、点击和输入仍严格报错。
        if (
          [
            "visible",
            "enabled",
            "editable",
            "has_value_property",
            "content_editable",
          ].includes(operation)
        ) {
          return res.json({ ok: true, result: false });
        }
        throw error;
      }
      let result;
      switch (operation) {
        case "visible":
          result = await selected.isVisible({ timeout: LOCATOR_TIMEOUT_MS });
          break;
        case "enabled":
          result = await selected.isEnabled({ timeout: LOCATOR_TIMEOUT_MS });
          break;
        case "editable":
          result = await selected.isEditable({ timeout: LOCATOR_TIMEOUT_MS });
          break;
        case "inner_text":
          result = await selected.innerText({ timeout: LOCATOR_TIMEOUT_MS });
          break;
        case "answer_markdown":
          result = await selected.evaluate((node) => {
            // 固定在受控插件内的只读转换：只读取已选择的助手答案容器，不接收调用方脚本。
            const text = (node.innerText || "").trim();
            const seen = new Set();
            const citations = [];
            for (const link of node.querySelectorAll("a[href]")) {
              let url;
              try {
                url = new URL(link.getAttribute("href"), document.baseURI);
              } catch (_) {
                continue;
              }
              if (
                !["http:", "https:"].includes(url.protocol) ||
                seen.has(url.href)
              )
                continue;
              seen.add(url.href);
              citations.push({
                url: url.href,
                title: (
                  link.innerText ||
                  link.getAttribute("title") ||
                  ""
                ).trim(),
              });
            }
            return { markdown: text, citations };
          });
          break;
        case "input_value":
          result = await selected.inputValue({ timeout: LOCATOR_TIMEOUT_MS });
          break;
        case "has_value_property":
          result = await selected
            .inputValue({ timeout: LOCATOR_TIMEOUT_MS })
            .then(() => true)
            .catch(() => false);
          break;
        case "content_editable":
          result = await selected.isEditable({ timeout: LOCATOR_TIMEOUT_MS });
          break;
        case "generation_stop_visible":
          result = await selected.evaluate((node) => {
            // 仅检查已定位输入框附近的停止/取消控件，不能执行调用方提供的脚本，
            // 也不读取聊天正文。它用于确认 Kimi 仍在生成，避免提前关闭任务页面。
            const promptRect = node.getBoundingClientRect();
            return [...document.querySelectorAll('button, [role="button"]')]
              .filter((control) => {
                const style = window.getComputedStyle(control);
                const rect = control.getBoundingClientRect();
                return (
                  style.visibility !== "hidden" &&
                  style.display !== "none" &&
                  rect.width > 0 &&
                  rect.height > 0 &&
                  rect.bottom >= promptRect.top &&
                  rect.top <= promptRect.bottom &&
                  rect.left >= promptRect.left + promptRect.width / 2
                );
              })
              .some((control) => {
                const semantics = [
                  control.getAttribute("aria-label"),
                  control.getAttribute("title"),
                  control.getAttribute("data-testid"),
                  control.className,
                ]
                  .filter((value) => typeof value === "string")
                  .join(" ")
                  .toLowerCase();
                return (
                  /stop|cancel|abort|pause|停止|终止|取消/.test(semantics) ||
                  control.querySelector("svg rect") !== null
                );
              });
          });
          break;
        case "scroll_into_view":
          await selected.scrollIntoViewIfNeeded({
            timeout: LOCATOR_TIMEOUT_MS,
          });
          result = true;
          break;
        default:
          throw new Error("locator operation is not allowed");
      }
      return res.json({ ok: true, result });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  // 回答截图只允许截取当前账号拥有的任务 Tab 内、由工作流声明的 CSS Locator。
  // 不接受 text ref、坐标或页面脚本，避免该只读能力退化为任意页面内容导出接口。
  app.post("/rpa/tabs/:tabId/locator-screenshot", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    const target = req.body?.target;
    if (
      !target ||
      typeof target !== "object" ||
      Array.isArray(target) ||
      !validSelector(target.selector) ||
      target.text !== undefined
    ) {
      return res
        .status(400)
        .json({ error: "locator screenshot requires a CSS selector target" });
    }
    try {
      const locator = await selectedLocator(found.tabState, target);
      // Locator.screenshot() 会在目标离开视口时自动滚动，并且 animations/caret
      // 选项会临时改写页面渲染状态。回答完成后的取证不能制造这些可观察行为，
      // 因此只读取元素已有的布局框，再由浏览器截图接口按该矩形裁剪。
      const clip = await locator.boundingBox({ timeout: LOCATOR_TIMEOUT_MS });
      if (!clip || clip.width <= 0 || clip.height <= 0) {
        throw new Error("answer locator has no visible bounds");
      }
      const screenshotBroker = app.locals.screenshotBroker;
      if (!screenshotBroker)
        throw new Error("Screenshot coordinator is unavailable");
      const buffer = await screenshotBroker.capture({
        tabKey: `${userId}:${req.params.tabId}`,
        // 同一工作流 Locator 的短期截图允许复用；布局变化后的下一采样窗口会重拍。
        variant: `locator:${target.selector}:${target.index || 0}`,
        capture: () =>
          found.tabState.page.screenshot({
            type: "png",
            clip,
            timeout: LOCATOR_SCREENSHOT_TIMEOUT_MS,
          }),
      });
      res.set("Content-Type", "image/png");
      return res.send(buffer);
    } catch (error) {
      return res.status(error.statusCode || 400).json({
        error: safeError(error),
        ...(error.code ? { code: error.code } : {}),
      });
    }
  });

  // 仅返回当前受控 Tab 的自然地址，用于保存已提交会话的服务端定位信息；不接受导航参数。
  app.get("/rpa/tabs/:tabId/current-url", async (req, res) => {
    const userId =
      typeof req.query?.userId === "string" ? req.query.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    return res.json({ url: found.tabState.page.url() });
  });

  app.post("/rpa/tabs/:tabId/locator-focus", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    try {
      await (
        await selectedLocator(found.tabState, req.body?.target)
      ).focus({ timeout: LOCATOR_TIMEOUT_MS });
      return res.json({ ok: true });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  app.post("/rpa/tabs/:tabId/locator-key", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    if (req.body?.key !== "Enter")
      return res.status(400).json({ error: "locator key is not allowed" });
    try {
      await (
        await selectedLocator(found.tabState, req.body?.target)
      ).press("Enter", { timeout: LOCATOR_TIMEOUT_MS });
      return res.json({ ok: true });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  app.post("/rpa/tabs/:tabId/locator-input", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const text = req.body?.text;
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    if (typeof text !== "string" || text.length === 0 || text.length > 4096)
      return res.status(400).json({ error: "locator input text is invalid" });
    try {
      await (
        await selectedLocator(found.tabState, req.body?.target)
      ).pressSequentially(text, { delay: 0, timeout: LOCATOR_TIMEOUT_MS });
      return res.json({ ok: true });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  app.post("/rpa/tabs/:tabId/locator-click", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    try {
      await submitLocatorClick(
        await selectedLocator(found.tabState, req.body?.target),
      );
      return res.json({ ok: true });
    } catch (error) {
      const message = safeError(error);
      return res.status(400).json({
        error: message,
        ...(isSubmissionNotDispatched(error)
          ? { code: "submission_not_dispatched" }
          : {}),
      });
    }
  });

  app.post("/rpa/tabs/:tabId/locator-submit", body, async (req, res) => {
    const userId = typeof req.body?.userId === "string" ? req.body.userId : "";
    const found = userId && findOwnedTab(sessions, userId, req.params.tabId);
    if (!found) return res.status(404).json({ error: "Tab not found" });
    try {
      await submitNearLocator(found.tabState, req.body?.target);
      return res.json({ ok: true });
    } catch (error) {
      return res.status(400).json({ error: safeError(error) });
    }
  });

  log("info", "GEO RPA plugin enabled", { protocolVersion: 1 });
}
