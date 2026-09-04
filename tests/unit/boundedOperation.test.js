import { describe, expect, test } from '@jest/globals';
import { waitForOperation } from '../../lib/bounded-operation.js';

describe('waitForOperation', () => {
  test('returns a completed operation result', async () => {
    await expect(waitForOperation(Promise.resolve('ok'), 50, 'test')).resolves.toBe('ok');
  });

  test('rejects when the operation exceeds its deadline', async () => {
    await expect(
      waitForOperation(new Promise(() => {}), 10, 'session close'),
    ).rejects.toThrow('session close timed out after 10ms');
  });
});
