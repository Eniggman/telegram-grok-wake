// v4.3.2: intake must not send "typing…" — Serverless has no timers.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '../..');
const src = readFileSync(join(root, 'serverless/tgcloud/lib/bridge.js'), 'utf8');

test('v4.3.2: handleMessage still sends the placeholder, but not sendChatAction', () => {
  assert.match(src, /sendPlaceholder/);
  assert.match(src, /🗨️ Думаю/);
  assert.equal(src.includes('sendChatAction'), false);
  assert.equal(src.includes("action: 'typing'"), false);
});
