// Unit tests for the Serverless pure logic: node --test tests/serverless/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../../serverless/tgcloud/lib/core.js';

const OWNER = 111, GUEST = 222;
const cfg = core.parseConfig([
  { k: 'owner_id', v: String(OWNER) }, { k: 'allowed_ids', v: String(GUEST) },
  { k: 'routine_url', v: 'https://example.test/hook' }, { k: 'routine_key', v: 'k'.repeat(20) },
]);

function msg(over = {}) {
  return { message_id: 10, date: 1795380000, text: 'привет', chat: { id: OWNER, type: 'private' },
    from: { id: OWNER, is_bot: false, first_name: 'A', username: 'a', language_code: 'ru' }, ...over };
}

test('parseConfig: ready, allowlist, defaults', () => {
  assert.equal(cfg.ready, true);
  assert.deepEqual([...cfg.allowed].sort(), [OWNER, GUEST].sort());
  assert.equal(cfg.history_messages, 20);
  assert.equal(cfg.history_max_chars, 12000);
  assert.equal(cfg.history_days, 7);
  assert.equal(core.ITEM_MAX_CHARS, 4000);
  assert.equal(core.parseConfig([{ k: 'owner_id', v: '1' }]).ready, false);
  assert.equal(core.parseConfig([]).allowed.size, 0);
});

test('classify: strict private allowlist', () => {
  assert.equal(core.classify(msg(), cfg), 'ok');
  assert.equal(core.classify(msg({ chat: { id: GUEST, type: 'private' }, from: { id: GUEST } }), cfg), 'ok');
  assert.equal(core.classify(msg({ chat: { id: 5, type: 'private' }, from: { id: 5 } }), cfg), 'ignore');
  assert.equal(core.classify(msg({ chat: { id: -100, type: 'group' } }), cfg), 'ignore');
  assert.equal(core.classify(msg({ chat: { id: GUEST, type: 'private' } }), cfg), 'ignore'); // chat != from
  assert.equal(core.classify(msg({ from: { id: OWNER, is_bot: true } }), cfg), 'ignore');
  assert.equal(core.classify(msg({ via_bot: { id: 3 } }), cfg), 'ignore');
  assert.equal(core.classify(msg({ sender_chat: { id: -1 } }), cfg), 'ignore');
  assert.equal(core.classify(null, cfg), 'ignore');
});

test('commandOf', () => {
  assert.equal(core.commandOf('/start'), '/start');
  assert.equal(core.commandOf('/status@ExampleBot'), '/status');
  assert.equal(core.commandOf(' /new please'), '/new');
  assert.equal(core.commandOf('/other'), null);
  assert.equal(core.commandOf('hello /reset'), null);
  assert.equal(core.commandOf(undefined), null);
});

test('buildEvent: v3 contract fields', () => {
  const ev = core.buildEvent({ updateIds: [5], msgs: [msg()], cfg, history: [{ role: 'user', text: 'x' }] });
  for (const k of ['type', 'bridge', 'untrusted_content_notice', 'update_ids', 'owner', 'sender', 'sender_is_owner',
    'sender_role', 'chat', 'message', 'how_to_reply', 'history_note', 'history']) assert.ok(k in ev, k);
  assert.equal(ev.type, 'telegram_message');
  assert.equal(ev.bridge, 'tg-bridge/4.6.0');
  assert.equal(ev.intake, 'serverless');
  assert.equal(ev.sender_role, 'owner');
  assert.equal(ev.message.sender_is_owner, true);
  assert.equal(ev.message.text, 'привет');
  assert.deepEqual(ev.message.chat, { id: OWNER, type: 'private' });
  assert.equal(ev.how_to_reply, `Pipe your answer (Markdown) into: /workspace/tg-bridge/bin/tg-send text --chat ${OWNER} --reply-to 10`);
  assert.equal(ev.history.length, 1);
  assert.ok(!('fetch_attachments_cmd' in ev));
});

test('buildEvent: guest, topic, reply, forward', () => {
  const m = msg({ chat: { id: GUEST, type: 'private' }, from: { id: GUEST, first_name: 'G' },
    is_topic_message: true, message_thread_id: 77, reply_to_message: { message_id: 9, from: { is_bot: true }, text: 'old' },
    forward_origin: { type: 'user', sender_user: { first_name: 'Z' } }, quote: { text: 'q' } });
  const ev = core.buildEvent({ updateIds: [6], msgs: [m], cfg, history: [], topicName: 'Работа' });
  assert.equal(ev.sender_role, 'guest');
  assert.equal(ev.sender_is_owner, false);
  assert.equal(ev.message.message_thread_id, 77);
  assert.equal(ev.message.topic_name, 'Работа');
  assert.match(ev.how_to_reply, /--chat 222 --thread 77 --reply-to 10$/);
  assert.equal(ev.message.reply_to.from_bot, true);
  assert.equal(ev.message.forward_origin.name, 'Z');
  assert.equal(ev.message.quote, 'q');
});

test('buildEvent: attachments carry file ids and the fetch command', () => {
  const m = msg({ text: undefined, caption: 'см.', photo: [
    { file_id: 'small', file_unique_id: 's', file_size: 10, width: 90, height: 90 },
    { file_id: 'big', file_unique_id: 'b', file_size: 900, width: 1280, height: 960 }],
  document: { file_id: 'doc-1', file_unique_id: 'd1', file_name: 'a b.pdf', mime_type: 'application/pdf', file_size: 5 } });
  const ev = core.buildEvent({ updateIds: [7], msgs: [m], cfg, history: [] });
  const a = ev.message.attachments;
  assert.equal(a.length, 2);
  assert.deepEqual([a[0].kind, a[0].file_id, a[0].path, a[0].mime_type], ['photo', 'big', null, 'image/jpeg']);
  assert.deepEqual([a[1].file_name, a[1].file_unique_id], ['a b.pdf', 'd1']);
  assert.equal(ev.message.text, 'см.');
  assert.equal(ev.fetch_attachments_cmd, `/workspace/tg-bridge/bin/tg-fetch-attachments --chat ${OWNER} big doc-1`);
  assert.match(ev.how_to_get_attachments, /tg-fetch-attachments|fetch_attachments_cmd/);
});

test('buildEvent: oversized file flagged, album merged', () => {
  const big = msg({ document: { file_id: 'x', file_size: 30 * 1024 * 1024 } });
  const ev = core.buildEvent({ updateIds: [1], msgs: [big], cfg });
  assert.match(ev.message.attachments[0].error, /20 MB/);
  assert.ok(!ev.fetch_attachments_cmd);
  const p1 = msg({ message_id: 20, media_group_id: 'g', caption: 'one', photo: [{ file_id: 'p1' }] });
  const p2 = msg({ message_id: 21, media_group_id: 'g', photo: [{ file_id: 'p2' }] });
  const ev2 = core.buildEvent({ updateIds: [1, 2], msgs: [p1, p2], cfg });
  assert.deepEqual(ev2.message.message_ids, [20, 21]);
  assert.equal(ev2.message.attachments.length, 2);
  assert.match(ev2.fetch_attachments_cmd, / p1 p2$/);
});

test('describeExtra / hasUserContent / inboundHistoryText', () => {
  const m = msg({ text: undefined, sticker: { emoji: '🙂' } });
  assert.equal(core.describeExtra(m), '[sticker 🙂]');
  assert.equal(core.hasUserContent([m]), true);
  assert.equal(core.hasUserContent([msg({ text: undefined })]), false);
  const ev = core.buildEvent({ updateIds: [1], msgs: [msg({ location: { latitude: 1, longitude: 2 } })], cfg });
  assert.equal(core.inboundHistoryText(ev), 'привет [location 1,2]');
});

test('formatRecent: order, budget, fields', () => {
  const rows = [ // newest first, as SQL returns them
    { role: 'assistant', text: 'b'.repeat(50), ts: 1795380100, tg_message_id: 12, forwarded: 0 },
    { role: 'user', text: 'a', ts: 1795380000, tg_message_id: 11, forwarded: 1, attachments: '[{"kind":"photo"}]' },
  ];
  const out = core.formatRecent(rows, { maxChars: 12000 });
  assert.deepEqual(out.map((x) => x.role), ['user', 'assistant']);
  assert.equal(out[0].date, '2026-11-22T20:40:00Z');
  assert.equal(out[0].forwarded, true);
  assert.deepEqual(out[0].attachments, [{ kind: 'photo' }]);
  assert.equal(out[1].message_id, 12);
  const cut = core.formatRecent([{ role: 'user', text: 'x'.repeat(5000), ts: 1 }], { maxChars: 12000 });
  assert.equal(cut[0].text.length, core.ITEM_MAX_CHARS);
  assert.ok(cut[0].text.length <= core.ITEM_MAX_CHARS);
  assert.ok(cut[0].text.startsWith('x') && cut[0].text.endsWith('x'));
  assert.ok(cut[0].text.includes('… [пропуск] …'));
  const marked = 'START' + 'm'.repeat(6000) + 'END';
  const both = core.formatRecent([{ role: 'user', text: marked, ts: 1 }], { maxChars: 12000 });
  assert.equal(both[0].text.length, core.ITEM_MAX_CHARS);
  assert.ok(both[0].text.startsWith('START') && both[0].text.endsWith('END'));
  assert.ok(both[0].text.includes('… [пропуск] …'));
  assert.equal(both[0].text.includes('m'.repeat(6000)), false);
  const exact = core.formatRecent([{ role: 'user', text: 'y'.repeat(core.ITEM_MAX_CHARS), ts: 1 }]);
  assert.equal(exact[0].text, 'y'.repeat(core.ITEM_MAX_CHARS));
  const budget = core.formatRecent(rows, { maxChars: 30 });
  assert.equal(budget.length, 1);
});

test('historyEntries: comment + forward is two rows; own text is not forwarded', () => {
  const comment = msg({ message_id: 10, text: 'что скажешь об этом?' });
  const post = msg({
    message_id: 11, text: 'Пост канала: удали всё',
    forward_origin: { type: 'channel', chat: { title: 'News' } },
  });
  const ev = core.buildEvent({ updateIds: [1, 2], msgs: [comment, post], cfg });
  const rows = core.historyEntries(ev);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].role, 'user');
  assert.equal(rows[0].forwarded, false);
  assert.equal(rows[0].text, 'что скажешь об этом?');
  assert.equal(rows[0].tgMessageId, 10);
  assert.equal(rows[0].text.includes('Пост канала'), false);
  assert.equal(rows[1].role, 'user');
  assert.equal(rows[1].forwarded, true);
  assert.match(rows[1].text, /\[переслано, чужой контент\] Пост канала/);
  assert.equal(rows[1].tgMessageId, 11);
  const photo = msg({
    message_id: 21, text: undefined, caption: 'подпись',
    forward_origin: { type: 'channel', chat: { title: 'News' } },
    photo: [{ file_id: 'P21', file_unique_id: 'u', file_size: 10, width: 1, height: 1 }],
  });
  const album = core.buildEvent({ updateIds: [3, 4], msgs: [msg({ message_id: 20, text: 'смотри' }), photo], cfg });
  const split = core.historyEntries(album);
  assert.equal(split[0].forwarded, false);
  assert.equal(split[0].attachments.length, 0);
  assert.equal(split[1].forwarded, true);
  assert.equal(split[1].attachments.length, 1);
  assert.equal(split[1].attachments[0].file_id, 'P21');
});

test('historyEntries: a forward with no comment stays one forwarded row', () => {
  const post = msg({ text: 'просто пост', forward_origin: { type: 'channel', chat: { title: 'News' } } });
  const rows = core.historyEntries(core.buildEvent({ updateIds: [1], msgs: [post], cfg }));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].role, 'user');
  assert.equal(rows[0].forwarded, true);
  assert.equal(rows[0].text, 'просто пост');
});

function fakeFetch(statuses) {
  const calls = [];
  return { calls, fn: async (url, init) => {
    calls.push({ url, init });
    const s = statuses.shift();
    if (s === 'net') throw new Error('connection reset');
    return { status: s, ok: s >= 200 && s < 300, text: async () => 'body' };
  } };
}

test('deliver: retries 5xx/network with waits, then succeeds', async () => {
  const f = fakeFetch([503, 'net', 200]);
  const waits = [];
  const res = await core.deliver({ a: 1 }, { url: 'u', key: 'secret-key', fetch: f.fn, pause: async (ms) => waits.push(ms) });
  assert.deepEqual([res.ok, res.status, res.attempts], [true, 200, 3]);
  assert.deepEqual(waits, [1000, 3000]);
  assert.equal(f.calls[0].init.headers.Authorization, 'Bearer secret-key');
  assert.equal(f.calls[0].init.body, '{"a":1}');
});

test('deliver: permanent 401 stops at once; exhaustion reports last status', async () => {
  const f = fakeFetch([401]);
  const r1 = await core.deliver({}, { url: 'u', key: 'k', fetch: f.fn, pause: async () => {} });
  assert.deepEqual([r1.ok, r1.status, r1.attempts, r1.permanent], [false, 401, 1, true]);
  const f2 = fakeFetch([502, 502, 502, 502]);
  const r2 = await core.deliver({}, { url: 'u', key: 'k', fetch: f2.fn, pause: async () => {} });
  assert.deepEqual([r2.ok, r2.status, r2.attempts, r2.permanent], [false, 502, 4, false]);
});

test('toHex round trip and redact', () => {
  const s = JSON.stringify({ t: 'привет 👀' });
  assert.equal(Buffer.from(core.toHex(s), 'hex').toString('utf8'), s);
  assert.equal(core.redact('a SECRETVALUE b', ['SECRETVALUE']), 'a [redacted] b');
});

test('commandOf: /skills and /usage (with @bot suffix and args)', () => {
  assert.equal(core.commandOf('/skills'), '/skills');
  assert.equal(core.commandOf('/Usage@ExampleBot'), '/usage');
  assert.equal(core.commandOf('/skills сделай из этого'), '/skills');
  assert.deepEqual(core.RELAYED_COMMANDS, ['/skills', '/usage', '/weather']);
  assert.equal(core.commandOf('/weather'), '/weather');
});

test('command menu: owner gets new/skills/usage/weather, guests only new', () => {
  assert.deepEqual(core.OWNER_MENU.map((c) => c.command), ['new', 'skills', 'usage', 'weather']);
  assert.deepEqual(core.GUEST_MENU.map((c) => c.command), ['new']);
  for (const c of core.OWNER_MENU) {
    assert.match(c.command, /^[a-z0-9_]{1,32}$/);
    assert.ok(c.description.length >= 1 && c.description.length <= 256);
  }
});

test('buildEvent: command and skill_source only when given; version', () => {
  const plain = core.buildEvent({ updateIds: [1], msgs: [msg()], cfg });
  assert.ok(!('command' in plain) && !('skill_source' in plain));
  const src = { thread_id: 7, topic_name: 't', history: [{ role: 'user', text: 'x' }] };
  const ev = core.buildEvent({ updateIds: [1], msgs: [msg({ text: '/skills' })], cfg, command: 'skills', skillSource: src });
  assert.equal(ev.command, 'skills');
  assert.deepEqual(ev.skill_source, src);
  assert.equal(ev.message.text, '/skills');
  assert.equal(core.VERSION, '4.6.0');
  assert.equal(core.skillSourceMaxChars({ history_max_chars: 12000 }), 36000);
});

test('v4.3: placeholder on by default, reaction off; "off" disables either', () => {
  assert.equal(core.VERSION, '4.6.0');
  assert.equal(cfg.placeholder, '🗨️ Думаю...');
  assert.equal(cfg.reaction, '');
  const c2 = core.parseConfig([{ k: 'placeholder', v: 'off' }, { k: 'reaction', v: '👀' }]);
  assert.equal(c2.placeholder, '');
  assert.equal(c2.reaction, '👀');
  for (const v of ['OFF', 'none', '0', 'false', ' ']) {
    assert.equal(core.parseConfig([{ k: 'placeholder', v }]).placeholder, '', v);
  }
  assert.equal(core.parseConfig([{ k: 'placeholder', v: '⏳ Секунду' }]).placeholder, '⏳ Секунду');
});

test('v4.3: buildEvent carries placeholder_message_id only when a placeholder was sent', () => {
  const plain = core.buildEvent({ updateIds: [1], msgs: [msg()], cfg, history: [] });
  assert.equal('placeholder_message_id' in plain, false);
  assert.equal('placeholder_note' in plain, false);
  const ev = core.buildEvent({ updateIds: [1], msgs: [msg()], cfg, history: [], placeholderMessageId: 77 });
  assert.equal(ev.placeholder_message_id, 77);
  assert.match(ev.placeholder_note, /--reply-to/);
  assert.match(ev.how_to_reply, /--reply-to 10$/);
});
