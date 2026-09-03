import { describe, expect, jest, test } from '@jest/globals';
import { readReadyViewport } from './index.js';

describe('readReadyViewport', () => {
  test('uses the body box when Camoufox does not expose the html box', async () => {
    const htmlBox = jest.fn().mockResolvedValue(null);
    const bodyBox = jest.fn().mockResolvedValue({ width: 1280, height: 720 });
    const page = {
      viewportSize: jest.fn(() => null),
      locator: jest.fn((selector) => ({
        boundingBox: selector === 'html' ? htmlBox : bodyBox,
      })),
    };
    const wait = jest.fn(async () => undefined);

    await expect(readReadyViewport(page, wait)).resolves.toEqual({ width: 1280, height: 720 });
    expect(wait).not.toHaveBeenCalled();
  });

  test('waits for the virtual-display layout boxes after a page is created', async () => {
    const htmlBox = jest
      .fn()
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce({ width: 1280, height: 720 });
    const bodyBox = jest.fn().mockResolvedValue(null);
    const page = {
      viewportSize: jest.fn(() => null),
      locator: jest.fn((selector) => ({
        boundingBox: selector === 'html' ? htmlBox : bodyBox,
      })),
    };
    const wait = jest.fn(async () => undefined);

    await expect(readReadyViewport(page, wait)).resolves.toEqual({ width: 1280, height: 720 });
    expect(wait).toHaveBeenCalledTimes(1);
  });

  test('uses the window CSS viewport when Camoufox exposes no layout box', async () => {
    const page = {
      viewportSize: jest.fn(() => null),
      locator: jest.fn(() => ({
        boundingBox: jest.fn().mockResolvedValue(null),
        evaluate: jest.fn().mockResolvedValue({ width: 1485, height: 835 }),
      })),
    };
    const wait = jest.fn(async () => undefined);

    await expect(readReadyViewport(page, wait)).resolves.toEqual({ width: 1485, height: 835 });
    expect(wait).not.toHaveBeenCalled();
  });

  test('uses the configured viewport without waiting', async () => {
    const page = {
      viewportSize: jest.fn(() => ({ width: 1280, height: 720 })),
      locator: jest.fn(),
    };
    const wait = jest.fn(async () => undefined);

    await expect(readReadyViewport(page, wait)).resolves.toEqual({ width: 1280, height: 720 });
    expect(page.locator).not.toHaveBeenCalled();
    expect(wait).not.toHaveBeenCalled();
  });
});
