import { access, chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { startOpencodeServer, stopAllOpencodeServers } from './server';

// Answers health checks like `opencode serve` until it is killed.
const FAKE_OPENCODE = `#!/usr/bin/env node
const http = require('node:http');
const port = Number(process.argv.find((arg) => arg.startsWith('--port=')).slice('--port='.length));
http
  .createServer((_request, response) => {
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify({ healthy: true }));
  })
  .listen(port, '127.0.0.1', () => console.log('opencode server listening on http://127.0.0.1:' + port));
`;

let root: string;
let binaryPath: string;

beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), 'opencode-server-test-'));
  binaryPath = join(root, 'opencode');
  await writeFile(binaryPath, FAKE_OPENCODE);
  await chmod(binaryPath, 0o755);
});

afterEach(async () => {
  await stopAllOpencodeServers();
  await rm(root, { recursive: true, force: true });
});

async function exists(path: string): Promise<boolean> {
  return access(path).then(
    () => true,
    () => false
  );
}

it.skipIf(process.platform === 'win32')(
  'stops every running server and removes its config home',
  async () => {
    const start = () =>
      startOpencodeServer({
        binaryPath,
        cwd: root,
        env: { HOME: root, PATH: process.env.PATH ?? '' },
        config: { $schema: 'https://opencode.ai/config.json', permission: {}, mcp: {} },
        startupTimeoutMs: 10000,
        skills: [],
      });
    const servers = [await start(), await start()];
    for (const server of servers) expect(await exists(server.configHome)).toBe(true);

    await stopAllOpencodeServers();

    for (const server of servers) {
      expect(server.process.exitCode !== null || server.process.signalCode !== null).toBe(true);
      expect(await exists(server.configHome)).toBe(false);
    }
  }
);
