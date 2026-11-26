// v4.4: thinking sticker (pack cache, random pick, send path, event field)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as core from '../../serverless/tgcloud/lib/core.js';

const OWNER = 111;
const cfg = core.parseConfig([
  { k: 'owner_id', v: String(OWNER) }, { k: 'allowed_ids', v: '' },
  { k: 'routine_url', v: 'https://example.test/hook' }, { k: 'routine_key', v: 'k'.repeat(20) },
]);

function msg() {
  return { message_id: 10, date: 1795380000, text: 'привет', chat: { id: OWNER, type: 'private' },
    from: { id: OWNER, is_bot: false, first_name: 'A', username: 'a' } };
}

test('v4.4: sticker_pack defaults to VikostVSpack; off / URL / invalid', () => {
  assert.equal(core.VERSION, '4.6.0');
  assert.equal(cfg.sticker_pack, 'VikostVSpack');
  assert.equal(core.DEFAULT_STICKER_PACK, 'VikostVSpack');
  assert.equal(core.STICKER_PACK_TTL, 86400);
  for (const v of ['off', 'OFF', 'none', '0', 'false', 'no', '']) {
    assert.equal(core.normalizeStickerPack(v), '', v);
    assert.equal(core.parseConfig([{ k: 'sticker_pack', v }]).sticker_pack, '', v);
  }
  assert.equal(core.normalizeStickerPack('https://t.me/addstickers/VikostVSpack'), 'VikostVSpack');
  assert.equal(core.normalizeStickerPack('https://t.me/addstickers/OtherPack/'), 'OtherPack');
  assert.equal(core.parseConfig([{ k: 'sticker_pack', v: 'My_Pack2' }]).sticker_pack, 'My_Pack2');
  assert.equal(core.normalizeStickerPack('not a pack!!!'), '');
  assert.equal(core.normalizeStickerPack('https://evil.example/VikostVSpack'), '');
});

test('v4.4: cache hit, expiry, wrong pack, serialize', () => {
  const pack = 'VikostVSpack';
  const raw = core.serializeStickerCache(pack, ['idA', 'idB'], 1000);
  assert.deepEqual(core.parseStickerCache(raw, { pack, now: 1000 + 10, ttl: 86400 }), ['idA', 'idB']);
  assert.equal(core.parseStickerCache(raw, { pack, now: 1000 + 86400, ttl: 86400 }), null); // expired
  assert.equal(core.parseStickerCache(raw, { pack: 'Other', now: 1010, ttl: 86400 }), null);
  assert.equal(core.parseStickerCache('not-json', { pack, now: 1010, ttl: 86400 }), null);
  assert.equal(core.parseStickerCache('', { pack, now: 1010, ttl: 86400 }), null);
  assert.equal(core.stickerCacheKey(pack), '_sticker_set:VikostVSpack');
  assert.deepEqual(core.fileIdsFromStickerSet({ stickers: [{ file_id: 'a' }, { file_id: '  ' }, {}, { file_id: 'b' }] }),
    ['a', 'b']);
  assert.deepEqual(core.fileIdsFromStickerSet(null), []);
  assert.equal(core.pickRandom(['a', 'b', 'c'], () => 0), 'a');
  assert.equal(core.pickRandom(['a', 'b', 'c'], () => 0.99), 'c');
});

test('v4.4: sendThinkingSticker uses cache, sends into the topic, new random each call', async () => {
  const calls = [];
  const cache = new Map();
  const cached = core.serializeStickerCache('VikostVSpack', ['file_one', 'file_two'], 50);
  cache.set(core.stickerCacheKey('VikostVSpack'), cached);
  const res = await core.sendThinkingSticker({
    pack: 'VikostVSpack', ttl: 86400, now: 100, chatId: 7, threadId: 55,
    getCache: async (k) => cache.get(k),
    setCache: async (k, v) => { cache.set(k, v); },
    getStickerSet: async (name) => { calls.push(['get', name]); return { stickers: [] }; },
    sendSticker: async (params) => { calls.push(['send', params]); return { message_id: 321 }; },
    random: () => 0,
  });
  assert.deepEqual(res, { sent: 'file_one', message_id: 321 });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], ['send', { chat_id: 7, sticker: 'file_one', message_thread_id: 55 }]);
});

test('v4.4: cache miss fetches getStickerSet, stores ids, and a failure never throws', async () => {
  const cache = new Map();
  const set = { stickers: [{ file_id: 'AA' }, { file_id: 'BB' }] };
  let res = await core.sendThinkingSticker({
    pack: 'VikostVSpack', ttl: 86400, now: 10, chatId: 1, threadId: null,
    getCache: async (k) => cache.get(k),
    setCache: async (k, v) => { cache.set(k, v); },
    getStickerSet: async () => set,
    sendSticker: async (params) => ({ message_id: 9, ...params }),
    random: () => 0.99,
  });
  assert.equal(res.sent, 'BB');
  assert.equal(res.message_id, 9);
  const stored = core.parseStickerCache(cache.get(core.stickerCacheKey('VikostVSpack')),
    { pack: 'VikostVSpack', now: 11, ttl: 86400 });
  assert.deepEqual(stored, ['AA', 'BB']);

  res = await core.sendThinkingSticker({
    pack: '', ttl: 86400, now: 10, chatId: 1,
    getCache: async () => { throw new Error('no'); },
    setCache: async () => {}, getStickerSet: async () => { throw new Error('no'); },
    sendSticker: async () => { throw new Error('no'); },
  });
  assert.deepEqual(res, { skipped: 'off' });

  res = await core.sendThinkingSticker({
    pack: 'VikostVSpack', ttl: 86400, now: 10, chatId: 1,
    getCache: async () => null,
    setCache: async () => {},
    getStickerSet: async () => { throw new Error('boom'); },
    sendSticker: async () => ({ message_id: 1 }),
  });
  assert.equal(res.error && res.error.includes('boom'), true);

  res = await core.sendThinkingSticker({
    pack: 'VikostVSpack', ttl: 86400, now: 10, chatId: 1,
    getCache: async () => null,
    setCache: async () => {},
    getStickerSet: async () => ({ stickers: [{ file_id: 'X' }] }),
    sendSticker: async () => { throw Object.assign(new Error('fail'), { description: 'Bad Request' }); },
  });
  assert.equal(res.error, 'Bad Request');
});

test('v4.4: buildEvent carries sticker_message_id only when a sticker was sent', () => {
  const ev = core.buildEvent({ updateIds: [1], msgs: [msg()], cfg, history: [],
    placeholderMessageId: 77, stickerMessageId: 88 });
  assert.equal(ev.placeholder_message_id, 77);
  assert.equal(ev.sticker_message_id, 88);
  const plain = core.buildEvent({ updateIds: [1], msgs: [msg()], cfg, history: [], placeholderMessageId: 77 });
  assert.equal('sticker_message_id' in plain, false);
});
