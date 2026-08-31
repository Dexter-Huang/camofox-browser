import { describe, expect, jest, test } from '@jest/globals';
import { enforceManualWindowGeometry } from './x11-window.js';

describe('manual X11 window geometry', () => {
  test('forces and verifies the native manual window size', async () => {
    const run = jest
      .fn()
      .mockResolvedValueOnce({ stdout: '  Width: 1440\n  Height: 931\n' })
      .mockResolvedValueOnce({ stdout: '' })
      .mockResolvedValueOnce({ stdout: '' })
      .mockResolvedValueOnce({ stdout: '  Width: 1920\n  Height: 1080\n' });

    await expect(enforceManualWindowGeometry(':0', '0x200040', { run })).resolves.toBeUndefined();
    expect(run).toHaveBeenNthCalledWith(
      3,
      'xdotool',
      ['windowsize', '--sync', '0x200040', '1920', '1080'],
      expect.objectContaining({ timeout: 3000 }),
    );
  });

  test('rejects a window that remains smaller than the VNC framebuffer', async () => {
    const run = jest
      .fn()
      .mockResolvedValueOnce({ stdout: '  Width: 1440\n  Height: 931\n' })
      .mockResolvedValueOnce({ stdout: '' })
      .mockResolvedValueOnce({ stdout: '' })
      .mockResolvedValueOnce({ stdout: '  Width: 1440\n  Height: 931\n' });

    await expect(enforceManualWindowGeometry(':0', '0x200040', { run })).rejects.toThrow(
      '1440x931',
    );
  });
});
