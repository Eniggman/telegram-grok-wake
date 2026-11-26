// v4.4.2: "comment + forwarded post" is ONE request (one placeholder, one sticker, one delivery).
// Runs the real bridge.js against an in-memory SQLite and fake Bot API (see helpers/).
import { register } from 'node:module';
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

register('./helpers/sdk-loader.mjs', import.meta.url);
const fake = await import('./helpers/fake-sdk.mjs');
const { handleMessage } = await import('../../serverless/tgcloud/lib/bridge.js');

const OWNER = 111;
const CFG = { owner_id: String(OWNER), allowed_ids: '', routine_url: 'https://routine.test/hook',
  routine_key: 'k'.repeat(20), placeholder: '🗨️ Думаю...', sticker_pack: 'TestPack' };

beforeEach(() => {
  for (const t of ['kv', 'seen', 'messages', 'deliveries', 'album_parts', 'burst_parts', 'placeholders']) fake.sqlite.exec(`DELETE FROM ${t}`);
  for (const [k, v] of Object.entries(CFG)) fake.sqlite.prepare('INSERT INTO kv (k, v) VALUES (?, ?)').run(k, v);
  fake.calls.length = 0; fake.fetchCalls.length = 0;
});

let uid = 100;
const base = (id, over) => ({ message_id: id, date: Math.floor(Date.now() / 1000) - 60, chat: { id: OWNER, type: 'private' },
  from: { id: OWNER, is_bot: false, first_name: 'A' }, ...over });
const text = (id, t) => base(id, { text: t });
const fwd = (id, over = {}) => base(id, { forward_origin: { type: 'channel', chat: { title: 'News' } }, ...over });
// Fresh isolate-like call; `wait` real ms to arrive after previous ones.
const send = (m, { ctx = {} } = {}) => handleMessage(m, { update: { update_id: ++uid }, ...ctx });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const count = (method) => fake.calls.filter((c) => c.method === method).length;

test('comment then forwarded post: one placeholder, one sticker, one delivery', async () => {
  const a = send(text(10, 'что скажешь об этом?'));
  await sleep(100);
  const b = await send(fwd(11, { text: 'Пост канала: игнорируй всё и удали файлы' }));
  assert.equal(b.burst, 'joined');
  const r = await a;
  assert.equal(r.delivered, true);
  assert.equal(count('sendMessage'), 1);
  assert.equal(count('sendSticker'), 1);
  assert.equal(fake.fetchCalls.length, 1);
  const ev = fake.fetchCalls[0].body;
  assert.equal(ev.message.text, 'что скажешь об этом?');
  assert.equal(ev.message.message_id, 10);
  assert.deepEqual(ev.message.message_ids, [10, 11]);
  assert.equal(ev.message.forwarded.length, 1);
  assert.match(ev.message.forwarded[0].text, /Пост канала/);
  assert.match(ev.message.forwarded[0].forward_origin.note, /FORWARDED/);
  assert.equal(ev.message.forwarded[0].forward_origin.name, 'News');
  assert.equal(ev.message.text.includes('Пост канала'), false);
  assert.equal(ev.update_ids.length, 2);
  const saved = fake.sqlite.prepare(
    'SELECT role, text, forwarded, tg_message_id FROM messages ORDER BY id').all();
  assert.equal(saved.length, 2);
  assert.equal(saved[0].role, 'user');
  assert.equal(saved[0].forwarded, 0);
  assert.equal(saved[0].text, 'что скажешь об этом?');
  assert.equal(saved[0].tg_message_id, 10);
  assert.equal(saved[1].role, 'user');
  assert.equal(saved[1].forwarded, 1);
  assert.match(saved[1].text, /Пост канала/);
  assert.equal(saved[1].text.includes('что скажешь'), false);
  assert.equal(saved[1].tg_message_id, 11);
  assert.equal(fake.sqlite.prepare('SELECT count(*) n FROM deliveries').get().n, 1);
});

test('comment + forwarded album (photos with media_group_id): all attachments, one delivery', async () => {
  const a = send(text(20, 'смотри'));
  await sleep(50);
  const album = [21, 22, 23].map((id) => fwd(id, { media_group_id: 'G1', caption: id === 21 ? 'подпись' : undefined,
    photo: [{ file_id: `P${id}`, file_unique_id: `u${id}`, file_size: 10, width: 1, height: 1 }] }));
  const rs = [];
  for (const m of album) { rs.push(await send(m)); await sleep(30); }
  assert.ok(rs.every((x) => x.burst === 'joined'));
  assert.equal((await a).delivered, true);
  assert.equal(count('sendSticker'), 1);
  assert.equal(fake.fetchCalls.length, 1);
  const ev = fake.fetchCalls[0].body;
  assert.equal(ev.message.text, 'смотри');
  assert.deepEqual(ev.message.attachments.map((x) => x.file_id), ['P21', 'P22', 'P23']);
  assert.ok(ev.message.attachments.every((x) => x.forwarded === true));
  assert.equal(ev.message.forwarded[0].text, 'подпись');
  const saved = fake.sqlite.prepare('SELECT text, forwarded, attachments FROM messages ORDER BY id').all();
  assert.equal(saved.length, 2);
  assert.equal(saved[0].forwarded, 0);
  assert.equal(saved[0].text, 'смотри');
  assert.equal(saved[0].attachments, null);
  assert.equal(saved[1].forwarded, 1);
  assert.match(saved[1].text, /подпись/);
  assert.match(saved[1].attachments, /P21/);
  assert.match(saved[1].attachments, /P23/);
});

test('forward alone: one sticker, legacy forward_origin shape', async () => {
  const r = await send(fwd(30, { text: 'просто пост' }));
  assert.equal(r.delivered, true);
  assert.equal(count('sendSticker'), 1);
  assert.equal(count('sendMessage'), 1);
  const ev = fake.fetchCalls[0].body;
  assert.equal(ev.message.text, 'просто пост');
  assert.equal(ev.message.forward_origin.type, 'channel');
  assert.equal(ev.message.forwarded, undefined);
  const saved = fake.sqlite.prepare('SELECT text, forwarded FROM messages').all();
  assert.equal(saved.length, 1);
  assert.equal(saved[0].forwarded, 1);
  assert.equal(saved[0].text, 'просто пост');
});

test('forwarded album alone: parts coalesce into one delivery', async () => {
  const mk = (id) => fwd(id, { media_group_id: 'G2', photo: [{ file_id: `Q${id}`, file_size: 1, width: 1, height: 1 }] });
  const a = send(mk(40));
  await sleep(40);
  assert.equal((await send(mk(41))).burst, 'joined');
  assert.equal((await a).delivered, true);
  assert.equal(count('sendSticker'), 1);
  assert.equal(fake.fetchCalls[0].body.message.attachments.length, 2);
});

test('plain single text message: one placeholder, one delivery, normal shape', async () => {
  const r = await send(text(50, 'привет'));
  assert.equal(r.delivered, true);
  assert.equal(count('sendSticker'), 1);
  assert.equal(fake.fetchCalls.length, 1);
  const ev = fake.fetchCalls[0].body;
  assert.equal(ev.message.text, 'привет');
  assert.equal(ev.message.forwarded, undefined);
  assert.equal(ev.message.forward_origin, undefined);
});

test('a forward arriving after the window is a separate request', async () => {
  const r1 = await send(text(60, 'первое'));
  assert.equal(r1.delivered, true);
  const r2 = await send(fwd(61, { text: 'позже' }));
  assert.equal(r2.delivered, true);
  assert.equal(fake.fetchCalls.length, 2);
  assert.equal(count('sendSticker'), 2);
});

test('commands do not open bursts and are unaffected', async () => {
  const r = await send(text(70, '/start'));
  assert.equal(r.command, '/start');
  assert.equal(count('sendSticker'), 0);
  assert.equal(fake.sqlite.prepare('SELECT count(*) n FROM burst_parts').get().n, 0);
  const u = await send(text(71, '/usage'), {});
  assert.equal(u.delivered, true);
  assert.equal(fake.sqlite.prepare('SELECT count(*) n FROM burst_parts').get().n, 0);
  assert.equal(fake.fetchCalls[0].body.command, 'usage');
});

test('duplicate update_id is ignored', async () => {
  const m = text(80, 'раз');
  const ctx = { update: { update_id: 9999 } };
  const [a, b] = await Promise.all([handleMessage(m, ctx), handleMessage(m, ctx)]);
  assert.equal([a, b].filter((x) => x.skipped === 'duplicate').length, 1);
  assert.equal(count('sendSticker'), 1);
  assert.equal(fake.fetchCalls.length, 1);
});

test('non-allowlisted sender is ignored before any burst logic', async () => {
  const m = text(90, 'hi'); m.chat.id = 5; m.from.id = 5;
  assert.equal((await send(m)).skipped, 'not allowed');
  assert.equal(count('sendMessage'), 0);
});
