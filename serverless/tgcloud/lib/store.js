// tg-bridge 4 — database access (history, outbox / dead letters, settings).
import { db } from 'sdk';
import { sql } from 'sdk/db';
import { parseConfig, formatRecent, ITEM_MAX_CHARS, PLACEHOLDER_UNSCOPED_MAX_AGE } from './core.js';

export const now = () => Math.floor(Date.now() / 1000);

export async function loadConfig() {
  const rows = await db.all(sql`SELECT k, v FROM kv`);
  return parseConfig(rows);
}

export async function getKv(k) {
  const r = await db.get(sql`SELECT v FROM kv WHERE k = ${k}`);
  return r ? r.v : null;
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

/**
 * Source conversation for /skills: the chat's conversation (topic) with the latest
 * non-command USER message — the current topic if it is the latest, else the most
 * recently active other one. Assistant replies are ignored when ranking so that the
 * reply to an earlier /usage or /skills does not make a service topic "the latest".
 * Returns {thread_id|null, topic_name|null, history[]} or null when there is no memory.
 */
export async function skillSource(chatId, limit, { maxChars = null, itemMaxChars = ITEM_MAX_CHARS } = {}) {
  const r = await db.get(sql`SELECT thread_id FROM messages
      WHERE chat_id = ${chatId} AND role = 'user' AND text NOT LIKE '/%'
      ORDER BY ts DESC, id DESC LIMIT 1`);
  if (!r) return null;
  const tid = Number(r.thread_id) || 0;
  const history = await recentHistory(chatId, tid, limit, { maxChars, itemMaxChars });
  return { thread_id: tid || null, topic_name: await topicName(chatId, tid), history };
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

// -- burst parts (v4.4.2) --------------------------------------------------
export async function addBurstPart(burstId, msg, updateId, threadId) {
  const r = await db.run(sql`INSERT OR IGNORE INTO burst_parts
      (burst_id, chat_id, thread_id, message_id, update_id, message, claimed, ts_ms, ts)
      VALUES (${burstId}, ${msg.chat.id}, ${threadId || 0}, ${msg.message_id}, ${updateId}, ${JSON.stringify(msg)}, 0,
              ${Date.now()}, ${now()})`);
  return r.rowsAffected > 0;
}

/** burst_id of the newest still-open burst in this chat/topic whose latest part is younger than `windowMs`. */
export async function openBurst(chatId, threadId, windowMs) {
  const r = await db.get(sql`SELECT burst_id FROM burst_parts WHERE chat_id = ${chatId}
      AND thread_id = ${threadId || 0} AND claimed = 0
      GROUP BY burst_id HAVING max(ts_ms) > ${Date.now() - windowMs}
      ORDER BY burst_id DESC LIMIT 1`);
  return r ? Number(r.burst_id) : null;
}

/** Age (ms) of the newest part of a burst. */
export async function burstIdleMs(chatId, burstId) {
  const r = await db.get(sql`SELECT max(ts_ms) AS t FROM burst_parts WHERE chat_id = ${chatId} AND burst_id = ${burstId}`);
  return r && r.t != null ? Date.now() - Number(r.t) : Infinity;
}

function burstRows(r) {
  const rows = (r.rows || []).slice().sort((a, b) => Number(a.message_id) - Number(b.message_id));
  return rows.map((x) => ({ updateId: x.update_id == null ? null : Number(x.update_id), msg: JSON.parse(x.message) }));
}

/** Atomically claim every unclaimed part of the burst (in message order). */
export async function claimBurst(chatId, burstId) {
  const r = await db.run(sql`UPDATE burst_parts SET claimed = 1 WHERE chat_id = ${chatId}
      AND burst_id = ${burstId} AND claimed = 0 RETURNING message_id, update_id, message`);
  return burstRows(r);
}

/** Claim a single part (a joiner that lost the race against its burst's claim). */
export async function claimBurstPart(chatId, messageId) {
  const r = await db.run(sql`UPDATE burst_parts SET claimed = 1 WHERE chat_id = ${chatId}
      AND message_id = ${messageId} AND claimed = 0 RETURNING message_id, update_id, message`);
  return burstRows(r);
}

/** Is the burst's opening message still unclaimed (i.e. the opener has not delivered yet)? */
export async function burstIsOpen(chatId, burstId) {
  const r = await db.get(sql`SELECT count(*) AS n FROM burst_parts WHERE chat_id = ${chatId}
      AND burst_id = ${burstId} AND message_id = ${burstId} AND claimed = 0`);
  return Number(r ? r.n : 0) > 0;
}

// -- placeholders (v4.3) ---------------------------------------------------
export async function addPlaceholder({ chatId, threadId, replyTo, messageId, stickerMessageId = null }) {
  const t = now();
  const stickerId = stickerMessageId || null;
  await db.run(sql`INSERT INTO placeholders (chat_id, thread_id, reply_to, message_id, sticker_message_id, state, created, updated)
      VALUES (${chatId}, ${threadId || 0}, ${replyTo}, ${messageId}, ${stickerId}, 'active', ${t}, ${t})
      ON CONFLICT(chat_id, reply_to) DO UPDATE SET thread_id = excluded.thread_id,
        message_id = excluded.message_id, sticker_message_id = excluded.sticker_message_id,
        state = 'active', created = excluded.created, updated = excluded.updated`);
}

function phRow(r) {
  if (!r) return null;
  const sticker = r.sticker_message_id != null ? Number(r.sticker_message_id) : null;
  return { message_id: Number(r.message_id), thread_id: Number(r.thread_id) || null,
    reply_to: Number(r.reply_to), created: Number(r.created),
    sticker_message_id: Number.isFinite(sticker) && sticker ? sticker : null };
}

/**
 * Atomically take the ACTIVE placeholder that replies to `replyTo` in `chatId`
 * and set it to `state` ('claimed' by tg-send, 'failed' by the bridge). Without
 * replyTo: only if exactly one active placeholder exists in that chat/topic and it
 * is younger than PLACEHOLDER_UNSCOPED_MAX_AGE. Returns the row or null.
 */
export async function takePlaceholder(chatId, replyTo, state, { threadId = 0 } = {}) {
  const t = now();
  if (replyTo) {
    const r = await db.run(sql`UPDATE placeholders SET state = ${state}, updated = ${t}
        WHERE chat_id = ${chatId} AND reply_to = ${replyTo} AND state = 'active'
        RETURNING message_id, thread_id, reply_to, created, sticker_message_id`);
    return phRow((r.rows || [])[0]);
  }
  const rows = await db.all(sql`SELECT id FROM placeholders WHERE chat_id = ${chatId}
      AND thread_id = ${threadId || 0} AND state = 'active' AND created > ${t - PLACEHOLDER_UNSCOPED_MAX_AGE}
      LIMIT 2`);
  if (rows.length !== 1) return null;
  const r = await db.run(sql`UPDATE placeholders SET state = ${state}, updated = ${t}
      WHERE id = ${rows[0].id} AND state = 'active' RETURNING message_id, thread_id, reply_to, created, sticker_message_id`);
  return phRow((r.rows || [])[0]);
}

/** Give a claimed placeholder back (tg-send could not send anything). */
export async function releasePlaceholder(chatId, messageId) {
  const r = await db.run(sql`UPDATE placeholders SET state = 'active', updated = ${now()}
      WHERE chat_id = ${chatId} AND message_id = ${messageId} AND state = 'claimed'`);
  return r.rowsAffected > 0;
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
 *  conversation), finished deliveries > 30 days, seen/album/burst/stops/placeholders > 2 days. */
export async function housekeep(cfg) {
  const t = now();
  const last = await db.get(sql`SELECT v FROM kv WHERE k = '_last_prune'`);
  if (last && t - Number(last.v) < 3600) return null;
  await db.run(sql`INSERT INTO kv (k, v) VALUES ('_last_prune', ${String(t)})
                   ON CONFLICT(k) DO UPDATE SET v = excluded.v`);
  const days = Math.max(1, Number(cfg.history_days) || 7);
  let n = 0;
  n += (await db.run(sql`DELETE FROM messages WHERE ts < ${t - days * 86400}`)).rowsAffected;
  n += (await db.run(sql`DELETE FROM messages WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
         PARTITION BY chat_id, thread_id ORDER BY id DESC) AS rn FROM messages) WHERE rn > 2000)`)).rowsAffected;
  await db.run(sql`DELETE FROM deliveries WHERE status IN ('delivered', 'redelivered', 'discarded') AND created < ${t - 30 * 86400}`);
  await db.run(sql`DELETE FROM seen WHERE ts < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM album_parts WHERE ts < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM burst_parts WHERE ts < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM stops WHERE ts < ${t - 2 * 86400}`);
  await db.run(sql`DELETE FROM placeholders WHERE created < ${t - 2 * 86400}`);
  return n;
}
