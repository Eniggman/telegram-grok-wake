// endpoints/box.js — the box's admin channel, used ONLY through
// `npx tgcloud run endpoints/box '<json>'` (needs the CLI access token).
//
// Endpoints are normally called by a Mini App with verified init data. This bot
// has no Mini App, and this endpoint refuses every call that carries init data,
// so the only way in is `tgcloud run` (where ctx is supplied by the caller and
// has no initData). Results come back hex-encoded ("TGB:<hex>") so the box can
// parse them exactly from the CLI output. Secrets are never returned: only
// presence, length and a short SHA-256 fingerprint.
import { db, api, EndpointError } from 'sdk';
import { sql } from 'sdk/db';
import { VERSION, toHex, splitTelegramText } from '../lib/core.js';
import * as store from '../lib/store.js';
import { verifyReplyToken, tokenFingerprint } from '../lib/reply_token.js';

const CONFIG_KEYS = new Set(['routine_url', 'routine_key', 'owner_id', 'allowed_ids',
  'history_messages', 'history_max_chars', 'history_days', 'max_wakes_per_min', 'max_wakes_per_hour', 'reaction']);
const SECRET_KEYS = new Set(['routine_key', 'reply_signing_key']);

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

const OPS = {
  async ping() { return { version: VERSION, now: store.now() }; },

  async config_set({ values }) {
    if (!values || typeof values !== 'object') throw new EndpointError('values required');
    for (const k of Object.keys(values)) if (!CONFIG_KEYS.has(k)) throw new EndpointError(`unknown key ${k}`);
    const url = 'routine_url' in values ? String(values.routine_url ?? '') : '';
    if (url !== '' && !/^https:\/\/[^\s/]+/i.test(url))
      throw new EndpointError('routine_url must start with https:// (otherwise the routine key would travel unencrypted)');
    await store.setConfig(values);
    return configStatus();
  },

  async config_status() { return configStatus(); },

  // Store memory rows (replies, migration). Idempotent by
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

  async history({ chat_id, thread_id = 0, limit = 50 }) {
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

  // Reply path used by the Grok routine. Auth: CLI token (tgcloud run) plus a
  // short-lived HMAC token bound to one chat/topic. First send edits the
  // «думаю…» placeholder; if edit fails, sendMessage and try to delete it.
  // A final send retires the token, but only once the send has actually gone out.
  // BOT_TOKEN is never accepted here — the SDK talks to Telegram.
  async send({ token, text, final }) {
    if (typeof text !== 'string' || !text) throw new EndpointError('text required');
    const key = await store.getOrCreateReplySigningKey();
    const claims = await verifyReplyToken(key, token);
    if (!claims) throw new EndpointError('unauthorized');
    const cfg = await store.loadConfig();
    const chatId = Number(claims.chatId);
    if (!Number.isFinite(chatId) || !cfg.allowed.has(chatId)) throw new EndpointError('unauthorized');
    const parts = splitTelegramText(text);
    if (!parts.length) throw new EndpointError('text required');
    // Everything that can be refused is checked above; only now is the token claimed.
    const fp = await tokenFingerprint(token);
    const claim = await store.beginReplySend(fp, Boolean(final));
    if (!claim) throw new EndpointError('unauthorized');
    const messageIds = [];
    try {
      const phId = await store.takeReplyPlaceholder(fp);
      let firstSent = false;
      if (phId) {
        try {
          await api.editMessageText({ chat_id: chatId, message_id: phId, text: parts[0] });
          messageIds.push(phId);
          firstSent = true;
        } catch (e) {
          try { await api.deleteMessage({ chat_id: chatId, message_id: phId }); } catch { /* ignore */ }
        }
      }
      for (const part of firstSent ? parts.slice(1) : parts) {
        const params = { chat_id: chatId, text: part };
        if (claims.threadId != null) params.message_thread_id = claims.threadId;
        const result = await api.sendMessage(params);
        if (result && result.message_id != null) messageIds.push(result.message_id);
      }
    } catch (e) {
      // Sending failed: hand a final token back so the routine can retry instead of
      // losing the answer. If some parts already went out, a retry may repeat them;
      // that is better than a silent, truncated answer.
      try { await store.reopenReply(fp, claim.claimed); } catch { /* ignore */ }
      throw e;
    }
    const lastId = messageIds.length ? messageIds[messageIds.length - 1] : null;
    try {
      await store.addMessage({ chatId, threadId: claims.threadId, role: 'assistant', text, tgMessageId: lastId });
    } catch (e) {
      console.warn('history not saved', String(e && (e.message || e))); // the answer itself was delivered
    }
    return { parts: parts.length, message_ids: messageIds };
  },

  async prune() {
    await db.run(sql`DELETE FROM kv WHERE k = '_last_prune'`);
    return { removed: await store.housekeep(await store.loadConfig()) };
  },

  // Test helper: remove everything stored for a (fake) test chat id.
  async purge_chat({ chat_id }) {
    const c = int(chat_id, 'chat_id');
    if (c > 0 && c < 1000) {
      for (const t of ['messages', 'deliveries', 'topics', 'album_parts', 'stops']) {
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
