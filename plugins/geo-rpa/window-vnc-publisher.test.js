import { describe, expect, jest, test } from '@jest/globals';
import net from 'node:net';
import {
  assertTcpPortAvailable,
  buildWindowPublisherCommands,
  waitForRfbGreeting,
  waitForTcpPort,
} from './window-vnc-publisher.js';

describe('window VNC publisher', () => {
  test('binds x11vnc to exactly one display and one native window', () => {
    expect(buildWindowPublisherCommands({
      display: ':0', windowId: '0x200066', rfbPort: 5901, websocketPort: 6081,
    })).toEqual({
      x11vnc: expect.arrayContaining(['-display', ':0', '-id', '0x200066', '-rfbport', '5901']),
      websockify: ['--web', '/usr/share/novnc', '0.0.0.0:6081', '127.0.0.1:5901'],
    });
  });

  test('rejects an invalid X11 identifier instead of spawning a shell command', () => {
    expect(() => buildWindowPublisherCommands({
      display: ':0; rm -rf /', windowId: 'not-a-window', rfbPort: 5901, websocketPort: 6081,
    })).toThrow('Window publisher configuration is invalid');
  });

  test('waits for the local publisher port to accept connections', async () => {
    const probe = jest.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const wait = jest.fn(async () => undefined);

    await expect(waitForTcpPort(6081, { probe, wait })).resolves.toBeUndefined();
    expect(wait).toHaveBeenCalledTimes(1);
  });

  test('requires an RFB greeting instead of accepting a listening stale publisher', async () => {
    const probe = jest.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const wait = jest.fn(async () => undefined);

    await expect(waitForRfbGreeting(5901, { probe, wait })).resolves.toBeUndefined();
    expect(wait).toHaveBeenCalledTimes(1);
  });

  test('rejects an already occupied fixed publisher port', async () => {
    const server = net.createServer();
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();

    await expect(assertTcpPortAvailable(address.port)).rejects.toThrow('already in use');
    await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  });
});
