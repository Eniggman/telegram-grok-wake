// endpoints/box.js — the box's admin channel, used ONLY through
// `npx tgcloud run endpoints/box '<json>'` (needs the CLI access token).
//
// Endpoints are normally called by a Mini App with verified init data. This bot
// has no Mini App, and this endpoint refuses every call that carries init data,
// so the only way in is `tgcloud run` (where ctx is supplied by the caller and
// has no initData). Results come back hex-encoded ("TGB:<hex>") so the box can
// parse them exactly from the CLI output. Secrets are never returned: only
// presence, length and a short SHA-256 fingerprint.
import { api, db, EndpointError } from 'sdk';
import { sql } from 'sdk/db';
import { VERSION, OWNER_MENU, GUEST_MENU, toHex } from '../lib/core.js';
import { renderUsageCard, publishUsageCard, usageCardStoreKey } from '../lib/usage_card.js';
import * as store from '../lib/store.js';

const CONFIG_KEYS = new Set(['routine_url', 'routine_key', 'owner_id', 'allowed_ids', 'bin_dir',
  'history_messages', 'history_max_chars', 'history_days', 'max_wakes_per_min', 'reaction', 'placeholder', 'sticker_pack']);
const SECRET_KEYS = new Set(['routine_key']);

async function fingerprint(value) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest).slice(0, 4)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function int(v, name) {
  const n = Number(v);
  if (!Number.isInteger(n)) throw new EndpointError(`${name} must be an integer`);
  return n;
}

async function configStatus() {
  const rows = await db.all(sql`SELECT k, v FROM kv`);
  const out = {};
  for (const r of rows) {
    if (r.k.startsWith('_')) continue;
    if (SECRET_KEYS.has(r.k)) out[r.k] = { set: true, length: r.v.length, sha256_8: await fingerprint(r.v) };
    else if (r.k === 'routine_url') out[r.k] = { set: true, host: (r.v.match(/^https?:\/\/([^/]+)/) || [])[1] || null, length: r.v.length };
    else out[r.k] = r.v;
  }
  const cfg = await store.loadConfig();
  return { version: VERSION, ready: cfg.ready, config: out };
}

async function commands_get_impl(ownerId) {
  const scopes = { default: { type: 'default' }, all_private_chats: { type: 'all_private_chats' },
    owner_chat: { type: 'chat', chat_id: ownerId } };
  const out = {};
  for (const [name, scope] of Object.entries(scopes)) out[name] = await api.getMyCommands({ scope });
  out.menu_button = await api.getChatMenuButton({});
  return { commands: out };
}

const OPS = {
  async ping() { return { version: VERSION, now: store.now() }; },

  async config_set({ values }) {
    if (!values || typeof values !== 'object') throw new EndpointError('values required');
    for (const k of Object.keys(values)) if (!CONFIG_KEYS.has(k)) throw new EndpointError(`unknown key ${k}`);
    await store.setConfig(values);
    return configStatus();
  },

  async config_status() { return configStatus(); },

  // Bot menu, idempotent (setMyCommands replaces the list for a scope). Owner chat
  // gets the full menu; default + all private chats get only /new. Menu button = commands.
  async commands_set() {
    const cfg = await store.loadConfig();
    if (cfg.owner_id == null) throw new EndpointError('owner_id is not configured');
    await api.setMyCommands({ commands: GUEST_MENU, scope: { type: 'default' } });
    await api.setMyCommands({ commands: GUEST_MENU, scope: { type: 'all_private_chats' } });
    await api.setMyCommands({ commands: OWNER_MENU, scope: { type: 'chat', chat_id: cfg.owner_id } });
    await api.setChatMenuButton({ menu_button: { type: 'commands' } });
    return commands_get_impl(cfg.owner_id);
  },

  async commands_get() {
    const cfg = await store.loadConfig();
    return commands_get_impl(cfg.owner_id);
  },

  // Store memory rows (tg-send replies, migration). Idempotent by
  // (chat, role, tg_message_id).
  async record({ items = [], topics = [] }) {
    let added = 0;
    for (const it of items) {
      if (it.role !== 'user' && it.role !== 'assistant') throw new EndpointError('bad role');
      const id = await store.addMessage({
        chatId: int(it.chat_id, 'chat_id'), threadId: it.thread_id ? int(it.thread_id, 'thread_id') : 0,
        role: it.role, text: String(it.text || ''),
        attachments: it.attachments ? (typeof it.attachments === 'string' ? it.attachments : JSON.stringify(it.attachments)) : null,
        forwarded: Boolean(it.forwarded),
        tgMessageId: it.tg_message_id == null ? null : int(it.tg_message_id, 'tg_message_id'),
        ts: it.ts == null ? null : int(it.ts, 'ts') });
      if (id) added++;
    }
    for (const t of topics) await store.rememberTopic(int(t.chat_id, 'chat_id'), int(t.thread_id, 'thread_id'), String(t.name));
    return { added, skipped: items.length - added, topics: topics.length };
  },

  async history({ chat_id, thread_id = 0, limit = 30 }) {
    const n = Math.min(Math.max(int(limit, 'limit'), 1), 500);
    const items = await store.recentHistory(int(chat_id, 'chat_id'), thread_id ? int(thread_id, 'thread_id') : 0, n,
      { itemMaxChars: null });
    return { items };
  },

  async reset({ chat_id, thread_id = 0 }) {
    return { removed: await store.resetConversation(int(chat_id, 'chat_id'), thread_id ? int(thread_id, 'thread_id') : 0) };
  },

  async stats() {
    const s = await store.deliveryStats();
    const cfg = await store.loadConfig();
    return { version: VERSION, ready: cfg.ready, ...s };
  },

  async deadletters({ status = 'dead', limit = 50, with_event = true }) {
    if (!['dead', 'pending', 'delivered', 'redelivered', 'discarded'].includes(status)) throw new EndpointError('bad status');
    const rows = await db.all(sql`SELECT id, update_id, chat_id, thread_id, message_id, status, attempts, http_status,
        detail, event, created, updated FROM deliveries WHERE status = ${status} ORDER BY id LIMIT ${Math.min(int(limit, 'limit'), 200)}`);
    return { items: rows.map((r) => {
      const o = { ...r };
      if (with_event) { try { o.event = JSON.parse(r.event); } catch { o.event = null; } } else delete o.event;
      return o;
    }) };
  },

  async mark({ ids = [], status, http_status = null }) {
    if (!['redelivered', 'discarded', 'dead'].includes(status)) throw new EndpointError('bad status');
    let n = 0;
    for (const id of ids) {
      const r = await db.run(sql`UPDATE deliveries SET status = ${status}, http_status = coalesce(${http_status}, http_status),
          updated = ${store.now()} WHERE id = ${int(id, 'id')}`);
      n += r.rowsAffected;
    }
    return { updated: n };
  },

  async stop_check({ chat_id, draft_id }) {
    return { stopped: await store.isStopped(int(chat_id, 'chat_id'), int(draft_id, 'draft_id')) };
  },

  // v4.3: tg-send takes the bridge's "🗨️ Думаю..." placeholder (single use) to
  // turn it into the answer; gives it back if it could not send anything.
  async placeholder_claim({ chat_id, reply_to = null, thread_id = 0 }) {
    const ph = await store.takePlaceholder(int(chat_id, 'chat_id'), reply_to ? int(reply_to, 'reply_to') : null,
      'claimed', { threadId: thread_id ? int(thread_id, 'thread_id') : 0 });
    return { placeholder: ph, now: store.now() };
  },

  async placeholder_release({ chat_id, message_id }) {
    return { released: await store.releasePlaceholder(int(chat_id, 'chat_id'), int(message_id, 'message_id')) };
  },

  async prune() {
    await db.run(sql`DELETE FROM kv WHERE k = '_last_prune'`);
    return { removed: await store.housekeep(await store.loadConfig()) };
  },


  // /usage card: edit the "Думаю..." placeholder (or a card being refreshed)
  // into an HTML usage message with an «Обновить» inline button.
  async usage_card({ chat_id, reply_to = null, message_id = null, thread_id = 0, data }) {
    if (!data || typeof data !== 'object') throw new EndpointError('data required');
    let card;
    try { card = renderUsageCard(data); }
    catch (e) { throw new EndpointError(String(e && e.message || e)); }
    const chatId = int(chat_id, 'chat_id');
    const replyTo = reply_to != null && reply_to !== '' ? int(reply_to, 'reply_to') : null;
    const midIn = message_id != null && message_id !== '' ? int(message_id, 'message_id') : null;
    const threadId = thread_id ? int(thread_id, 'thread_id') : 0;
    let ph = null;
    if (replyTo) ph = await store.takePlaceholder(chatId, replyTo, 'claimed', { threadId });
    const targetId = (ph && ph.message_id) || midIn;
    if (ph && ph.sticker_message_id) {
      try { await api.deleteMessage({ chat_id: chatId, message_id: ph.sticker_message_id }); }
      catch (_) { /* sticker already gone is fine */ }
    }
    try {
      return await publishUsageCard({
        api, card, chatId, targetId, threadId,
        onEditFail: async () => { if (ph) await store.releasePlaceholder(chatId, targetId); },
        save: async (c, mid, savedCard) => {
          await store.setConfig({
            [usageCardStoreKey(c, mid)]: JSON.stringify({
              text: savedCard.text, parse_mode: savedCard.parse_mode, reply_markup: savedCard.reply_markup,
            }),
          });
        },
      });
    } catch (e) {
      throw new EndpointError(String(e && e.message || e));
    }
  },

  // Test helper: remove everything stored for a (fake) test chat id.
  async purge_chat({ chat_id }) {
    const c = int(chat_id, 'chat_id');
    if (c > 0 && c < 1000) {
      for (const t of ['messages', 'deliveries', 'topics', 'album_parts', 'stops', 'placeholders']) {
        await db.run(sql`DELETE FROM ${sql.raw(t)} WHERE chat_id = ${c}`);
      }
      return { purged: c };
    }
    throw new EndpointError('purge_chat is only for test chat ids 1..999');
  },
};

export default async function (input, ctx) {
  if (ctx && ctx.initData) throw new EndpointError('forbidden');
  const op = input && input.op;
  if (!Object.prototype.hasOwnProperty.call(OPS, op)) throw new EndpointError(`unknown op ${op}`);
  const result = await OPS[op](input);
  return 'TGB:' + toHex(JSON.stringify({ ok: true, ...result }));
}
