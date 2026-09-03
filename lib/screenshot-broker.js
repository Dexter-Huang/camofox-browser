/**
 * 同一 Camofox 进程内的 PNG 截图协调器。
 *
 * 截图 Buffer 使用 Node 的外部内存，多个 HTTP 接口同时调用 page.screenshot()
 * 时不会完全反映在 V8 heap 指标上，却仍可能耗尽进程内存。协调器把限流放在
 * 唯一的截图出口：相同 Tab 和规格共享一次结果，不同规格在同一 Tab 串行执行，
 * 并用全局并发和有界队列保护整个浏览器进程。
 */

export class ScreenshotBusyError extends Error {
  constructor(message = "Screenshot capacity is temporarily exhausted") {
    super(message);
    this.name = "ScreenshotBusyError";
    this.code = "screenshot_busy";
    this.statusCode = 429;
  }
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function assertKey(value, fieldName) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${fieldName} must be a non-empty string`);
  }
}

/**
 * 创建进程级截图协调器。
 *
 * @param {object} options
 * @param {number} [options.intervalMs=500] 单个 Tab 两次实际截图起始时间的最小间隔。
 * @param {number} [options.maxConcurrent=2] 整个进程允许同时执行的截图数。
 * @param {number} [options.maxQueued=16] 全局等待队列上限；满时快速拒绝。
 * @param {number} [options.maxQueuedPerTab=3] 单个 Tab 的运行中与等待中非合并请求上限。
 * @param {number} [options.maxCacheBytes=33554432] 短期 PNG 缓存的总内存上限。
 */
export function createScreenshotBroker({
  intervalMs = 500,
  maxConcurrent = 2,
  maxQueued = 16,
  maxQueuedPerTab = 3,
  maxCacheBytes = 32 * 1024 * 1024,
} = {}) {
  if (!Number.isFinite(intervalMs) || intervalMs < 0)
    throw new TypeError("intervalMs must be a non-negative number");
  if (!Number.isInteger(maxConcurrent) || maxConcurrent < 1)
    throw new TypeError("maxConcurrent must be an integer >= 1");
  if (!Number.isInteger(maxQueued) || maxQueued < 0)
    throw new TypeError("maxQueued must be an integer >= 0");
  if (!Number.isInteger(maxQueuedPerTab) || maxQueuedPerTab < 1)
    throw new TypeError("maxQueuedPerTab must be an integer >= 1");
  if (!Number.isInteger(maxCacheBytes) || maxCacheBytes < 1)
    throw new TypeError("maxCacheBytes must be an integer >= 1");

  let activeCount = 0;
  let cacheBytes = 0;
  const globalQueue = [];
  const tabs = new Map();
  const inFlight = new Map();
  const cache = new Map();

  function cacheKey(tabKey, variant) {
    return `${tabKey}\u0000${variant}`;
  }

  function deleteCacheEntry(key) {
    const entry = cache.get(key);
    if (!entry) return;
    cache.delete(key);
    cacheBytes -= entry.buffer.byteLength + (entry.base64Bytes || 0);
    if (entry.timer) clearTimeout(entry.timer);
  }

  function evictCacheToBudget() {
    while (cacheBytes > maxCacheBytes && cache.size > 0) {
      const oldestKey = cache.keys().next().value;
      deleteCacheEntry(oldestKey);
    }
  }

  function getCached(key) {
    const entry = cache.get(key);
    if (!entry) return null;
    if (entry.expiresAt <= Date.now()) {
      deleteCacheEntry(key);
      return null;
    }
    // Map 的插入顺序同时承担 LRU 顺序，避免热点请求被无关截图挤掉。
    cache.delete(key);
    cache.set(key, entry);
    return entry.buffer;
  }

  function storeCached(key, buffer) {
    if (buffer.byteLength > maxCacheBytes) return;
    deleteCacheEntry(key);
    const entry = {
      buffer,
      base64: null,
      base64Bytes: 0,
      expiresAt: Date.now() + intervalMs,
      timer: null,
    };
    entry.timer = setTimeout(() => deleteCacheEntry(key), intervalMs);
    entry.timer.unref?.();
    cache.set(key, entry);
    cacheBytes += buffer.byteLength;
    evictCacheToBudget();
  }

  function runNextGlobal() {
    while (activeCount < maxConcurrent && globalQueue.length > 0) {
      const next = globalQueue.shift();
      startGlobal(next);
    }
  }

  function startGlobal({ capture, resolve, reject }) {
    activeCount += 1;
    let result;
    try {
      result = capture();
    } catch (error) {
      activeCount -= 1;
      reject(error);
      runNextGlobal();
      return;
    }
    Promise.resolve(result)
      .then(resolve, reject)
      .finally(() => {
        activeCount -= 1;
        runNextGlobal();
      });
  }

  function runWithGlobalBudget(capture) {
    return new Promise((resolve, reject) => {
      const item = { capture, resolve, reject };
      if (activeCount < maxConcurrent) {
        startGlobal(item);
        return;
      }
      if (globalQueue.length >= maxQueued) {
        reject(new ScreenshotBusyError());
        return;
      }
      globalQueue.push(item);
    });
  }

  function getTabState(tabKey) {
    let state = tabs.get(tabKey);
    if (!state) {
      state = {
        tail: null,
        pending: 0,
        lastCaptureStartedAt: Number.NEGATIVE_INFINITY,
      };
      tabs.set(tabKey, state);
    }
    return state;
  }

  function cleanupTabState(tabKey, state) {
    if (state.pending === 0 && state.tail && tabs.get(tabKey) === state) {
      tabs.delete(tabKey);
    }
  }

  function capture({ tabKey, variant, capture: takeScreenshot }) {
    assertKey(tabKey, "tabKey");
    assertKey(variant, "variant");
    if (typeof takeScreenshot !== "function")
      throw new TypeError("capture must be a function");

    const key = cacheKey(tabKey, variant);
    const cached = getCached(key);
    if (cached) return Promise.resolve(cached);

    const shared = inFlight.get(key);
    if (shared) return shared;

    const state = getTabState(tabKey);
    if (state.pending >= maxQueuedPerTab) {
      return Promise.reject(
        new ScreenshotBusyError("Screenshot queue for this tab is full"),
      );
    }
    state.pending += 1;

    const execute = async () => {
      // 等待期间另一个相同规格可能已经成功；再次读取缓存避免无意义截图。
      const fresh = getCached(key);
      if (fresh) return fresh;

      const waitedMs = intervalMs - (Date.now() - state.lastCaptureStartedAt);
      if (waitedMs > 0) await delay(waitedMs);
      state.lastCaptureStartedAt = Date.now();
      const buffer = await runWithGlobalBudget(takeScreenshot);
      if (!Buffer.isBuffer(buffer))
        throw new TypeError("capture must resolve to a Buffer");
      storeCached(key, buffer);
      return buffer;
    };

    const previous = state.tail;
    const promise = previous
      ? previous.catch(() => undefined).then(execute)
      : execute();
    state.tail = promise;
    inFlight.set(key, promise);

    promise
      .finally(() => {
        state.pending -= 1;
        if (inFlight.get(key) === promise) inFlight.delete(key);
        cleanupTabState(tabKey, state);
      })
      .catch(() => undefined);

    return promise;
  }

  async function captureBase64(options) {
    const buffer = await capture(options);
    const key = cacheKey(options.tabKey, options.variant);
    const entry = cache.get(key);
    if (!entry || entry.buffer !== buffer) return buffer.toString("base64");
    if (entry.base64 !== null) return entry.base64;

    const base64 = buffer.toString("base64");
    const base64Bytes = Buffer.byteLength(base64, "utf8");
    // Base64 JSON 是 V8 堆内字符串，必须和 PNG Buffer 共用同一个预算。超出
    // 预算时仅服务当前请求，不能把这份字符串留在缓存中挤占后续截图。
    if (entry.buffer.byteLength + base64Bytes > maxCacheBytes) return base64;
    entry.base64 = base64;
    entry.base64Bytes = base64Bytes;
    cacheBytes += base64Bytes;
    evictCacheToBudget();
    return base64;
  }

  return { capture, captureBase64 };
}
