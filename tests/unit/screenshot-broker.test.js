import { createScreenshotBroker } from "../../lib/screenshot-broker.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("createScreenshotBroker", () => {
  test("同一 Tab 与截图规格的并发请求共用一次截图，并在采样窗口内复用结果", async () => {
    const gate = deferred();
    const broker = createScreenshotBroker({
      intervalMs: 500,
      maxConcurrent: 1,
    });
    let calls = 0;
    const capture = async () => {
      calls += 1;
      await gate.promise;
      return Buffer.from("png");
    };

    const first = broker.capture({
      tabKey: "user:tab",
      variant: "page:viewport",
      capture,
    });
    const second = broker.capture({
      tabKey: "user:tab",
      variant: "page:viewport",
      capture,
    });
    expect(calls).toBe(1);

    gate.resolve();
    const [firstImage, secondImage] = await Promise.all([first, second]);
    expect(firstImage).toEqual(Buffer.from("png"));
    expect(secondImage).toEqual(Buffer.from("png"));

    const cachedImage = await broker.capture({
      tabKey: "user:tab",
      variant: "page:viewport",
      capture,
    });
    expect(cachedImage).toEqual(Buffer.from("png"));
    expect(calls).toBe(1);
  });

  test("同一 Tab 的不同截图规格串行执行，避免重叠渲染", async () => {
    const firstGate = deferred();
    const broker = createScreenshotBroker({ intervalMs: 0, maxConcurrent: 2 });
    const calls = [];

    const first = broker.capture({
      tabKey: "user:tab",
      variant: "page:viewport",
      capture: async () => {
        calls.push("first");
        await firstGate.promise;
        return Buffer.from("first");
      },
    });
    const second = broker.capture({
      tabKey: "user:tab",
      variant: "page:full",
      capture: async () => {
        calls.push("second");
        return Buffer.from("second");
      },
    });

    expect(calls).toEqual(["first"]);
    firstGate.resolve();
    await expect(Promise.all([first, second])).resolves.toEqual([
      Buffer.from("first"),
      Buffer.from("second"),
    ]);
    expect(calls).toEqual(["first", "second"]);
  });

  test("全局并发受限，队列满时立即拒绝而不继续积压请求", async () => {
    const gate = deferred();
    const broker = createScreenshotBroker({
      intervalMs: 0,
      maxConcurrent: 1,
      maxQueued: 0,
      maxQueuedPerTab: 2,
    });
    const first = broker.capture({
      tabKey: "user:first",
      variant: "page:viewport",
      capture: async () => {
        await gate.promise;
        return Buffer.from("first");
      },
    });

    await expect(
      broker.capture({
        tabKey: "user:second",
        variant: "page:viewport",
        capture: async () => Buffer.from("second"),
      }),
    ).rejects.toMatchObject({ code: "screenshot_busy", statusCode: 429 });

    gate.resolve();
    await expect(first).resolves.toEqual(Buffer.from("first"));
  });

  test("缓存按总字节数裁剪，不保留不受限的 PNG Buffer", async () => {
    const broker = createScreenshotBroker({
      intervalMs: 500,
      maxCacheBytes: 4,
    });
    let firstCalls = 0;
    const firstCapture = async () => {
      firstCalls += 1;
      return Buffer.from("1111");
    };

    await broker.capture({
      tabKey: "user:first",
      variant: "page:viewport",
      capture: firstCapture,
    });
    await broker.capture({
      tabKey: "user:second",
      variant: "page:viewport",
      capture: async () => Buffer.from("2222"),
    });
    await broker.capture({
      tabKey: "user:first",
      variant: "page:viewport",
      capture: firstCapture,
    });

    expect(firstCalls).toBe(2);
  });

  test("嵌入 Snapshot JSON 的 Base64 也在采样窗口内复用，避免重复占用 V8 堆", async () => {
    const broker = createScreenshotBroker({
      intervalMs: 500,
      maxCacheBytes: 1024,
    });
    const image = Buffer.from("png-response");
    const originalToString = image.toString.bind(image);
    let base64Encodes = 0;
    image.toString = (encoding, ...args) => {
      if (encoding === "base64") base64Encodes += 1;
      return originalToString(encoding, ...args);
    };

    const options = {
      tabKey: "user:tab",
      variant: "page:viewport",
      capture: async () => image,
    };
    await broker.captureBase64(options);
    await broker.captureBase64(options);

    expect(base64Encodes).toBe(1);
  });
});
