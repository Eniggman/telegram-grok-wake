// tg-bridge 4 — database access (history, outbox / dead letters, settings).
import { db } from 'sdk';
import { sql } from 'sdk/db';
import { parseConfig, formatRecent, ITEM_MAX_CHARS } from './core.js';

export const now = () => Math.floor(Date.now() / 1000);

export async function loadConfig() {
  const rows = await db.all(sql`SELECT k, v FROM kv`);
  return parseConfig(rows);
}

export async function setConfig(values) {
  for (const [k, v] of Object.entries(values)) {
    if (v === null || v === undefined || v === '') {
      await db.run(sql`DELETE FROM kv WHERE k = ${k}`);
    } else {
      await db.run(sql`INSERT INTO kv (k, v) VALUES (${k}, ${String(v)})
                       ON CONFLICT(k) DO UPDATE SET v = excluded.v`);
    }
  }
}

/** Signing key for reply tokens (auto-created, never returned in full). */
export async function getOrCreateReplySigningKey() {
  const row = await db.get(sql`SELECT v FROM kv WHERE k = 'reply_signing_key'`);
  if (row && row.v) return row.v;
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const key = Array.from(bytes).map((b) => b.toString(16).padStart(2, '0')).join('');
  await db.run(sql`INSERT INTO kv (k, v) VALUES ('reply_signing_key', ${key})
                   ON CONFLICT(k) DO NOTHING`);
  const again = await db.get(sql`SELECT v FROM kv WHERE k = 'reply_signing_key'`);
  return again.v;
}

/**
 * Atomically start a send with a reply token. Returns null when the token is
 * already finalized (a final send was accepted earlier); otherwise {claimed}.
 * `claimed` is the exact value a FINAL send wrote (null for a non-final send):
 * pass it to reopenReply() if the send then fails, so the routine can retry.
 * Of two concurrent final sends only one gets a claim (first final wins).
 * State lives in kv under _reply_final:<fingerprint> as "<ts>|open" or
 * "<ts>|final|<nonce>".
 */
export async function beginReplySend(fingerprint, isFinal) {
  const k = '_reply_final:' + fingerprint;
  if (isFinal) {
    const nonce = Array.from(crypto.getRandomValues(new Uint8Array(4)))
      .map((b) => b.toString(16).padStart(2, '0')).join('');
    const fin = `${now()}|final|${nonce}`;
    const r = await db.run(sql`INSERT INTO kv (k, v) VALUES (${k}, ${fin})
      ON CONFLICT(k) DO UPDATE SET v = ${fin} WHERE kv.v LIKE '%|open'`);
    return r.rowsAffected > 0 ? { claimed: fin } : null;
  }
  const open = `${now()}|open`;
  const r = await db.run(sql`INSERT INTO kv (k, v) VALUES (${k}, ${open})
    ON CONFLICT(k) DO UPDATE SET v = kv.v WHERE kv.v LIKE '%|open'`);
  return r.rowsAffected > 0 ? { claimed: null } : null;
}

/** Give a token back after a failed final send. Touches only our own claim. */
export async function reopenReply(fingerprint, claimed) {
  if (!claimed) return;
  const k = '_reply_final:' + fingerprint;
  await db.run(sql`UPDATE kv SET v = ${`${now()}|open`} WHERE k = ${k} AND v = ${claimed}`);
}

/** Value is "unixTs|telegram_message_id". Taken once, on the first send. */
export async function setReplyPlaceholder(fingerprint, messageId) {
  const v = `${now()}|${messageId}`;
  await db.run(sql`INSERT INTO kv (k, v) VALUES (${'_reply_ph:' + fingerprint}, ${v})
                   ON CONFLICT(k) DO UPDATE SET v = excluded.v`);
}

export async function takeReplyPlaceholder(fingerprint) {
  const k = '_reply_ph:' + fingerprint;
  const r = await db.run(sql`DELETE FROM kv WHERE k = ${k} RETURNING v`);
  const row = (r.rows && r.rows[0]) || null;
  if (!row || row.v == null) return null;
  const raw = String(row.v);
  const mid = raw.includes('|') ? raw.split('|').pop() : raw;
  const n = Number(mid);
  return Number.isFinite(n) ? n : null;
}

/** True the first time an update_id is seen (Telegram may re-send). */
export async function markSeen(updateId) {
  const r = await db.run(sql`INSERT OR IGNORE INTO seen (update_id, ts) VALUES (${updateId}, ${now()})`);
  return r.rowsAffected > 0;
}

// -- memory ---------------------------------------------------------------
export async function addMessage({ chatId, threadId, role, text, attachments = null, forwarded = false,
  tgMessageId = null, ts = null }) {
  const r = await db.run(sql`INSERT OR IGNORE INTO messages
      (chat_id, thread_id, role, text, attachments, forwarded, tg_message_id, ts)
      VALUES (${chatId}, ${threadId || 0}, ${role}, ${text || ''}, ${attachments},
              ${forwarded ? 1 : 0}, ${tgMessageId}, ${ts ?? now()})`);
  return r.rowsAffected > 0 ? Number(r.lastInsertRowid) : null;
}

export async function removeMessages(ids) {
  for (const id of ids.filter(Boolean)) await db.run(sql`DELETE FROM messages WHERE id = ${id}`);
}

export async function resetConversation(chatId, threadId) {
  const r = await db.run(sql`DELETE FROM messages WHERE chat_id = ${chatId} AND thread_id = ${threadId || 0}`);
  return r.rowsAffected;
}

export async function recentHistory(chatId, threadId, limit, { maxChars = null, excludeUserIds = [],
  itemMaxChars = ITEM_MAX_CHARS } = {}) {
  if (!limit || limit <= 0) return [];
  const excl = excludeUserIds.filter((x) => x != null).map(Number);
  let rows = await db.all(sql`SELECT role, text, attachments, forwarded, tg_message_id, ts FROM messages
      WHERE chat_id = ${chatId} AND thread_id = ${threadId || 0}
      ORDER BY id DESC LIMIT ${limit + excl.length}`);
  if (excl.length) {
    rows = rows.filter((r) => !(r.role === 'user' && excl.includes(Number(r.tg_message_id))));
  }
  return formatRecent(rows.slice(0, limit), { maxChars, itemMaxChars });
}

// -- topics ---------------------------------------------------------------
export async function rememberTopic(chatId, threadId, name) {
  if (!threadId || !name) return;
  await db.run(sql`INSERT INTO topics (chat_id, thread_id, name, ts) VALUES (${chatId}, ${threadId}, ${name}, ${now()})
                   ON CONFLICT(chat_id, thread_id) DO UPDATE SET name = excluded.name, ts = excluded.ts`);
}

export async function topicName(chatId, threadId) {
  if (!threadId) return null;
  const r = await db.get(sql`SELECT name FROM topics WHERE chat_id = ${chatId} AND thread_id = ${threadId}`);
  return r ? r.name : null;
}

// -- outbox / dead letters ------------------------------------------------
export async function createDelivery({ updateId, chatId, threadId, messageId, event }) {
  const t = now();
  const r = await db.run(sql`INSERT OR IGNORE INTO deliveries
      (update_id, chat_id, thread_id, message_id, status, attempts, event, created, updated)
      VALUES (${updateId}, ${chatId}, ${threadId || 0}, ${messageId}, 'pending', 0, ${JSON.stringify(event)}, ${t}, ${t})`);
  return r.rowsAffected > 0 ? Number(r.lastInsertRowid) : null;
}

export async function updateDelivery(id, { status, attempts, httpStatus = null, detail = null }) {
  if (!id) return;
  await db.run(sql`UPDATE deliveries SET status = ${status}, attempts = ${attempts},
      http_status = ${httpStatus}, detail = ${detail}, updated = ${now()} WHERE id = ${id}`);
}

export async function noteAttempt(id, attempts) {
  if (!id) return;
  await db.run(sql`UPDATE deliveries SET attempts = ${attempts}, updated = ${now()} WHERE id = ${id}`);
}

/** Wakes in the last hour across ALL chats (global rate limit). */
export async function recentWakesGlobal() {
  const r = await db.get(sql`SELECT count(*) AS n FROM deliveries WHERE created > ${now() - 3600}`);
  return r ? Number(r.n) : 0;
}

/** Wakes in the last minute for one chat (rate limit). */
export async function recentWakes(chatId) {
  const r = await db.get(sql`SELECT count(*) AS n FROM deliveries WHERE chat_id = ${chatId} AND created > ${now() - 60}`);
  return r ? Number(r.n) : 0;
}

/** Pending rows whose handler died mid-delivery (older than `age` seconds). */
export async function stalePending(age = 120) {
  return db.all(sql`SELECT id, chat_id, thread_id, message_id, attempts FROM deliveries
      WHERE status = 'pending' AND updated < ${now() - age} ORDER BY id LIMIT 20`);
}

export async function deliveryStats() {
  const counts = await db.all(sql`SELECT status, count(*) AS n FROM deliveries GROUP BY status`);
  const last = await db.get(sql`SELECT id, status, http_status, attempts, created, updated, chat_id
      FROM deliveries ORDER BY id DESC LIMIT 1`);
  const lastOk = await db.get(sql`SELECT updated FROM deliveries WHERE status = 'delivered' ORDER BY id DESC LIMIT 1`);
  const msgs = await db.get(sql`SELECT count(*) AS n, max(ts) AS last FROM messages`);
  const by = {};
  for (const c of counts) by[c.status] = Number(c.n);
  return { deliveries: by, last: last || null, last_delivered: lastOk ? Number(lastOk.updated) : null,
    messages: Number(msgs ? msgs.n : 0), last_message_ts: msgs && msgs.last != null ? Number(msgs.last) : null };
}

// -- album parts ----------------------------------------------------------
export async function addAlbumPart(msg, updateId) {
  await db.run(sql`INSERT OR IGNORE INTO album_parts (media_group_id, chat_id, message_id, update_id, message, claimed, ts)
      VALUES (${String(msg.media_group_id)}, ${msg.chat.id}, ${msg.message_id}, ${updateId}, ${JSON.stringify(msg)}, 0, ${now()})`);
}

export async function newerAlbumPartExists(msg) {
  const r = await db.get(sql`SELECT count(*) AS n FROM album_parts WHERE chat_id = ${msg.chat.id}
      AND media_group_id = ${String(msg.media_group_id)} AND message_id > ${msg.message_id}`);
  return Number(r ? r.n : 0) > 0;
}

/** Atomically claim every unclaimed part of the album; returns them in order. */
export async function claimAlbum(msg) {
  const r = await db.run(sql`UPDATE album_parts SET claimed = 1 WHERE chat_id = ${msg.chat.id}
      AND media_group_id = ${String(msg.media_group_id)} AND claimed = 0
      RETURNING message_id, update_id, message`);
  const rows = (r.rows || []).slice().sort((a, b) => Number(a.message_id) - Number(b.message_id));
  return rows.map((x) => ({ updateId: x.update_id == null ? null : Number(x.update_id), msg: JSON.parse(x.message) }));
}

// -- stops ----------------------------------------------------------------
export async function addStop(chatId, draftId) {
  await db.run(sql`INSERT OR IGNORE INTO stops (chat_id, draft_id, ts) VALUES (${chatId}, ${draftId}, ${now()})`);
}

export async function isStopped(chatId, draftId) {
  const r = await db.get(sql`SELECT 1 AS x FROM stops WHERE chat_id = ${chatId} AND draft_id = ${draftId}`);
  return Boolean(r);
}

// -- retention ------------------------------------------------------------
/** Prune at most once an hour: memory older than history_days (max 2000 per
 *  conversation), all deliveries (incl. dead) > 30 days, seen/album/stops > 2 days. */
export async function housekeep(cfg) {
  const t = now();
  const last = await db.get(sql`SELECT v FROM kv WHERE k = '_last_prune'`);
  if (last && t - Number(last.v) < 3600) return null;
  await db.run(sql`INSERT INTO kv (k, v) VALUES ('_last_prune', ${String(t)})
                   ON CONFLICT(k) DO UPDATE SET v = excluded.v`);
  const days = Math.max(1, Number(cfg.history_days) || 30);
  let n = 0;
  n += (await db.run(sql`DELETE FROM messages WHERE ts < ${t - days * 86400}`)).rowsAffected;
  n += (await db.run(sql`DELETE FROM messages WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
         PARTITION BY chat_id, thread_id ORDER BY id DESC) AS rn FROM messages) WHERE rn > 2000)`)).rowsAffected;
  // All deliveries older than 30 days go, including failed ('dead') and stuck 'pending' ones:
  // they hold the full message text, so they must not outlive the history window.
  await db.run(sql`DELETE FROM deliveries WHERE created < ${t - 30 * 86400}`);
  await db.run(sql`DELETE FROM seen WHERE ts < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM kv WHERE k GLOB '_reply_final:*' AND CAST(v AS INTEGER) < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM kv WHERE k GLOB '_reply_ph:*' AND CAST(v AS INTEGER) < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM album_parts WHERE ts < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM stops WHERE ts < ${t - 2 * 86400}`);
  return n;
}
