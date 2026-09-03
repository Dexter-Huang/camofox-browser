import { describe, expect, jest, test } from '@jest/globals';
import {
  MANUAL_WINDOW_POPUP_FEATURES,
  MANUAL_PAGE_VIEWPORT_SIZE,
  createManualWindowIdentity,
  findX11WindowId,
  openManualPopup,
  listTopLevelX11WindowIds,
  waitForNewX11WindowId,
  waitForX11WindowId,
} from './manual-window.js';

describe('manual X11 window helpers', () => {
  test('finds only the window carrying the generated title marker', () => {
    const { title } = createManualWindowIdentity();
    const tree = [
      '     0x200001 "Firefox": ("camoufox-default" "Camoufox-default")  10x10+10+10',
      `     0x200040 "${title} - Mozilla Firefox": ("Navigator" "Firefox")  1440x900+0+0`,
    ].join('\n');

    expect(findX11WindowId(tree, title)).toBe('0x200040');
    expect(findX11WindowId(tree, 'GEO_MANUAL_WINDOW_missing')).toBeNull();
  });

  test('waits until Firefox has added the popup window to the X11 tree', async () => {
    const wait = jest.fn(async () => undefined);
    const readWindowTree = jest
      .fn()
      .mockResolvedValueOnce('     0x200001 "Firefox": ()')
      .mockResolvedValueOnce('     0x200040 "GEO_MANUAL_WINDOW_test - Mozilla Firefox": ()');

    await expect(waitForX11WindowId({
      display: ':0',
      title: 'GEO_MANUAL_WINDOW_test',
      readWindowTree,
      wait,
      attempts: 2,
    })).resolves.toBe('0x200040');
    expect(wait).toHaveBeenCalledTimes(1);
  });

  test('identifies the new top-level window when Firefox omits the page title', async () => {
    const before = [
      '     0x200001 "Firefox": ()',
      '        0x200002 (has no name): ()',
    ].join('\n');
    const after = [
      '     0x200040 "Camoufox": ()',
      before,
    ].join('\n');
    const existingWindowIds = new Set(listTopLevelX11WindowIds(before));

    await expect(waitForNewX11WindowId({
      display: ':0',
      existingWindowIds,
      readWindowTree: jest.fn(async () => after),
      attempts: 1,
    })).resolves.toBe('0x200040');
  });

  test('opens a fixed popup and assigns the controlled title', async () => {
    const popup = {
      waitForLoadState: jest.fn(async () => undefined),
      viewportSize: jest.fn(() => MANUAL_PAGE_VIEWPORT_SIZE),
      setViewportSize: jest.fn(async () => undefined),
      evaluate: jest.fn(async () => undefined),
      goto: jest.fn(async () => undefined),
    };
    const page = {
      waitForEvent: jest.fn(async () => popup),
      evaluate: jest.fn(async () => true),
    };

    await expect(openManualPopup(
      page,
      'GEO_MANUAL_WINDOW_test',
      'https://example.com/',
      { manualWindow: true },
    )).resolves.toBe(popup);
    expect(page.waitForEvent).toHaveBeenCalledWith('popup', { timeout: 5000 });
    expect(page.evaluate).toHaveBeenCalledWith(expect.any(Function), MANUAL_WINDOW_POPUP_FEATURES);
    expect(popup.evaluate).toHaveBeenCalledWith(expect.any(Function), 'GEO_MANUAL_WINDOW_test');
    expect(popup.goto).toHaveBeenCalledWith('https://example.com/', {
      waitUntil: 'commit', timeout: 90_000,
    });
    expect(popup.setViewportSize).toHaveBeenCalledWith(MANUAL_PAGE_VIEWPORT_SIZE);
  });

  test('uses the virtual display size for manual control', () => {
    expect(MANUAL_WINDOW_POPUP_FEATURES).toBe('popup=yes,width=1920,height=1009');
  });
});
