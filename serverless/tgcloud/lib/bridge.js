// tg-bridge 4 — inbound flow for one Telegram message.
import { api, fetch } from 'sdk';
import {
  VERSION, classify, commandOf, topicThreadId, hasUserContent, buildEvent, deliver,
  historyEntries, historyAttachments, redact, RELAYED_COMMANDS, OWNER_ONLY_TEXT,
  SKILL_SOURCE_MESSAGES, SKILL_SOURCE_ITEM_MAX_CHARS, skillSourceMaxChars,
  sendThinkingSticker, STICKER_PACK_TTL,
} from './core.js';
import { USAGE_REFRESH_DATA, restoreUsageCard, parseStoredUsageCard, usageCardStoreKey } from './usage_card.js';
import { pause as realPause } from './pause.js';
import * as store from './store.js';

const ALBUM_WAIT_MS = 1500;
// v4.4.2: a comment + the forwarded messages right after it are ONE request.
const BURST_WAIT_MS = 1500;
const BURST_SETTLE_MS = 700;  // extra wait while parts are still arriving
const BURST_SETTLE_MAX = 2;

/**
 * Side-effect surface. Real Telegram updates always get the real one. `tgcloud
 * run … --ctx '{test:{…}}'` (only possible with the CLI token) can swap the Bot
 * API for a log and point delivery at a test URL with a dummy key; Telegram
 * never sends a `test` field, so production updates cannot reach this path.
 */
function makeIo(ctx, cfg) {
  const t = ctx && ctx.test;
  if (!t) return { cfg, api, fetch, pause: realPause, calls: null };
  const calls = [];
  const fakeApi = new Proxy({}, {
    get: (_, method) => async (params) => { calls.push({ method, params }); return { message_id: 1 }; },
  });
  const tcfg = { ...cfg };
  if (t.owner_id) tcfg.owner_id = t.owner_id;
  if (t.allowed) tcfg.allowed = new Set(t.allowed);
  tcfg.routine_url = t.routine_url || 'https://invalid.example/test';
  tcfg.routine_key = 'test-key-not-secret';
  tcfg.ready = true;
  return {
    cfg: tcfg, api: fakeApi, fetch, calls,
    pause: t.no_wait ? async () => {} : realPause,
  };
}

async function safe(io, method, params) {
  try {
    return await io.api[method](params);
  } catch (e) {
    console.warn('bot api call failed', method, String(e && (e.description || e.message) || e));
    return null;
  }
}

function notify(io, chatId, threadId, text) {
  const p = { chat_id: chatId, text };
  if (threadId) p.message_thread_id = threadId;
  return safe(io, 'sendMessage', p);
}

/**
 * v4.3: reply with the placeholder ("🗨️ Думаю...") and remember it, so tg-send
 * on the box can turn it into the answer. v4.4: right after it, send a random
 * thinking sticker (same chat/topic); its message_id is stored on the same
 * record and deleted when the first answer goes out. Returns the placeholder
 * message_id or null (disabled, or Telegram/DB refused — then nothing is left).
 */
async function sendPlaceholder(io, chatId, threadId, replyTo) {
  const text = io.cfg.placeholder;
  if (!text) return null;
  const p = { chat_id: chatId, text };
  // v4.3.1: plain message by default (no quote); kv reply_quote=on restores the reply.
  if (io.cfg.reply_quote) p.reply_parameters = { message_id: replyTo, allow_sending_without_reply: true };
  if (threadId) p.message_thread_id = threadId;
  const sent = await safe(io, 'sendMessage', p);
  const messageId = sent && sent.message_id ? Number(sent.message_id) : null;
  if (!messageId) return null;
  // Sticker comes after the placeholder. A failure must never block the rest.
  let stickerMessageId = null;
  const st = await sendThinkingSticker({
    pack: io.cfg.sticker_pack, ttl: STICKER_PACK_TTL, now: store.now(),
    chatId, threadId,
    getCache: (k) => store.getKv(k),
    setCache: (k, v) => store.setConfig({ [k]: v }),
    getStickerSet: (name) => io.api.getStickerSet({ name }),
    sendSticker: (params) => safe(io, 'sendSticker', params),
  });
  if (st && st.message_id) stickerMessageId = st.message_id;
  else if (st && st.error) console.warn('thinking sticker failed', st.error);
  try {
    await store.addPlaceholder({ chatId, threadId, replyTo, messageId, stickerMessageId });
  } catch (e) {
    // tg-send could never find it: do not leave a hanging "Думаю..." (or sticker) behind.
    console.warn('placeholder not stored, removing it', String(e));
    await safe(io, 'deleteMessage', { chat_id: chatId, message_id: messageId });
    if (stickerMessageId) await safe(io, 'deleteMessage', { chat_id: chatId, message_id: stickerMessageId });
    return null;
  }
  io.placeholder = { chatId, replyTo, stickerMessageId };
  return messageId;
}

/**
 * Failure notice: edit the still-active placeholder of (chatId, replyTo) into
 * `text` (atomic claim, so an answer tg-send already put there is never
 * overwritten); if there is none or the edit fails, send `text` as a new message.
 */
async function failNotice(io, chatId, threadId, replyTo, text) {
  if (replyTo) {
    let ph = null;
    try {
      ph = await store.takePlaceholder(chatId, replyTo, 'failed');
    } catch (e) {
      console.warn('placeholder lookup failed', String(e));
    }
    if (ph && await safe(io, 'editMessageText', { chat_id: chatId, message_id: ph.message_id, text })) return;
  }
  await notify(io, chatId, threadId, text);
}

/** Pending rows left by a killed invocation: mark dead and tell the sender. */
async function sweepStale(io) {
  for (const row of await store.stalePending(120)) {
    await store.updateDelivery(row.id, { status: 'dead', attempts: row.attempts, detail: 'handler stopped mid-delivery' });
    await failNotice(io, row.chat_id, row.thread_id || null, row.message_id,
      '⚠️ Сообщение могло не дойти до бота (сбой доставки). Оно сохранено; если ответа нет — отправьте его ещё раз.');
  }
}

async function statusText(cfg, chatId) {
  const base = 'Мост на связи ✅ Пишите задачу обычным сообщением.';
  if (chatId !== cfg.owner_id) return base;
  const s = await store.deliveryStats();
  const dead = s.deliveries.dead || 0;
  return `${base}\n\nПриём: Telegram Serverless, tg-bridge ${VERSION}. Недоставленных: ${dead}.`;
}

export async function handleMessage(message, ctx) {
  const baseCfg = await store.loadConfig();
  const io = makeIo(ctx, baseCfg);
  const cfg = io.cfg;
  if (!cfg.ready) {
    console.warn('tg-bridge is not configured yet (run tg-cloud setup on the box); ignoring update');
    return { skipped: 'not configured' };
  }
  if (classify(message, cfg) !== 'ok') return { skipped: 'not allowed' };

  const updateId = ctx && ctx.update && ctx.update.update_id != null ? Number(ctx.update.update_id) : null;
  if (updateId != null && !(await store.markSeen(updateId))) return { skipped: 'duplicate' };

  const chatId = message.chat.id;
  const threadId = topicThreadId(message);
  let result;
  try {
    result = await handleAllowed(io, message, updateId);
  } catch (e) {
    console.error('failed to handle message', redact(String(e && e.stack || e), [cfg.routine_key]));
    const ph = io.placeholder;
    await failNotice(io, chatId, threadId, ph ? ph.replyTo : null,
      '⚠️ Не получилось обработать это сообщение. Попробуйте ещё раз.');
    result = { error: 'exception' };
  }
  try {
    await store.housekeep(cfg);
  } catch (e) {
    console.warn('housekeeping failed', String(e));
  }
  if (io.calls) result = { ...result, api_calls: io.calls };
  return result;
}

async function handleAllowed(io, message, updateId) {
  const cfg = io.cfg;
  const chatId = message.chat.id;
  const tid = message.message_thread_id;
  const created = message.forum_topic_created || {};
  const edited = message.forum_topic_edited || {};
  if (tid && (created.name || edited.name)) await store.rememberTopic(chatId, Number(tid), created.name || edited.name);
  const threadId = topicThreadId(message);

  const cmd = commandOf(message.text);
  if (cmd === '/start' || cmd === '/ping' || cmd === '/status') {
    await notify(io, chatId, threadId, await statusText(cfg, chatId));
    return { command: cmd };
  }
  if (cmd === '/reset' || cmd === '/new') {
    const n = await store.resetConversation(chatId, threadId);
    await notify(io, chatId, threadId, '🧹 История этого чата очищена. Начинаем с чистого листа.');
    return { command: cmd, removed: n };
  }

  // Relayed menu commands (skills, usage, weather): owner only.
  const relayed = RELAYED_COMMANDS.includes(cmd) ? cmd.slice(1) : null;
  if (relayed && chatId !== cfg.owner_id) {
    await notify(io, chatId, threadId, OWNER_ONLY_TEXT);
    return { command: cmd, refused: 'owner only' };
  }

  await sweepStale(io);

  let group = [{ updateId, msg: message }];
  let burstId = null; // set when this invocation opened a burst (it delivers for everyone)
  if (message.forward_origin) {
    // A forward (alone, an album part, or the tail of "comment + forward"): join the burst
    // that is open for this chat/topic, else open one.
    if (!hasUserContent([message])) return { skipped: 'empty' };
    const open = await store.openBurst(chatId, threadId, BURST_WAIT_MS);
    if (open) {
      await store.addBurstPart(open, message, updateId, threadId);
      if (await store.burstIsOpen(chatId, open)) return { burst: 'joined', burst_id: open };
      // The opener claimed before we were stored: take our own part and deliver it alone.
      group = await store.claimBurstPart(chatId, message.message_id);
      if (!group.length) return { burst: 'joined late' }; // the opener claimed us after all
    } else {
      burstId = message.message_id;
    }
  } else if (message.media_group_id) {
    if (!hasUserContent([message])) return { skipped: 'empty' };
    await store.addAlbumPart(message, updateId);
    await io.pause(ALBUM_WAIT_MS);
    if (await store.newerAlbumPartExists(message)) return { album: 'handled by a later part' };
    group = await store.claimAlbum(message);
    if (!group.length) return { album: 'already claimed' };
  } else if (!relayed && message.text && hasUserContent([message])) {
    burstId = message.message_id; // a plain text message may be a comment on forwards that follow
  }
  if (burstId != null) {
    if (!(await store.addBurstPart(burstId, message, updateId, threadId))) return { skipped: 'duplicate' };
  }
  let msgs = group.map((g) => g.msg);
  let updateIds = group.map((g) => g.updateId).filter((x) => x != null);
  if (!hasUserContent(msgs)) return { skipped: 'empty' };

  if (await store.recentWakes(chatId) >= cfg.max_wakes_per_min) {
    if (burstId != null) await store.claimBurst(chatId, burstId); // nobody will deliver this burst
    await notify(io, chatId, threadId, '⏳ Слишком много сообщений за минуту. Подождите немного и отправьте ещё раз.');
    return { skipped: 'rate limited' };
  }

  const first = msgs[0];
  if (cfg.reaction) { // off by default since v4.3 (TG_REACTION on the box)
    await safe(io, 'setMessageReaction', { chat_id: chatId, message_id: first.message_id,
      reaction: [{ type: 'emoji', emoji: cfg.reaction }] });
  }
  // Placeholder only. "typing…" is sent by tg-send on the box right before the
  // answer — Serverless has no timers, so a chat action here would vanish in ~5 s.
  const placeholderId = await sendPlaceholder(io, chatId, threadId, first.message_id);
  if (burstId != null) {
    // Placeholder and sticker are already out; now let the rest of the burst arrive.
    await io.pause(BURST_WAIT_MS);
    for (let i = 0; i < BURST_SETTLE_MAX && await store.burstIdleMs(chatId, burstId) < BURST_SETTLE_MS; i++) {
      await io.pause(BURST_SETTLE_MS);
    }
    const parts = await store.claimBurst(chatId, burstId);
    if (parts.length) {
      // The opener stays first: the placeholder replies to it and tg-send --reply-to uses its id.
      const ordered = parts.filter((g) => g.msg.message_id === burstId)
        .concat(parts.filter((g) => g.msg.message_id !== burstId));
      msgs = ordered.map((g) => g.msg);
      updateIds = ordered.map((g) => g.updateId).filter((x) => x != null);
    }
  }

  let history = [];
  try {
    history = await store.recentHistory(chatId, threadId, cfg.history_messages, {
      maxChars: cfg.history_max_chars, excludeUserIds: msgs.map((m) => m.message_id) });
  } catch (e) {
    console.warn('history unavailable, sending without it', String(e));
  }
  let skillSource = null;
  if (relayed === 'skills') {
    try {
      skillSource = await store.skillSource(chatId, SKILL_SOURCE_MESSAGES, {
        maxChars: skillSourceMaxChars(cfg), itemMaxChars: SKILL_SOURCE_ITEM_MAX_CHARS });
    } catch (e) {
      console.warn('skill source unavailable, sending without it', String(e));
    }
  }
  const event = buildEvent({ updateIds, msgs, cfg, history, topicName: await store.topicName(chatId, threadId),
    command: relayed, skillSource, placeholderMessageId: placeholderId,
    stickerMessageId: (io.placeholder && io.placeholder.stickerMessageId) || null });

  const deliveryId = await store.createDelivery({
    updateId: updateIds.length ? updateIds[updateIds.length - 1] : null,
    chatId, threadId, messageId: first.message_id, event });
  // Service commands are not conversation: keep them out of memory (the routine's reply is still saved by tg-send).
  // A comment + forward is two rows: the sender's own text, then the forwarded content as data.
  const rowIds = [];
  if (!relayed) {
    for (const e of historyEntries(event)) {
      const id = await store.addMessage({
        chatId, threadId, role: e.role, text: e.text,
        attachments: historyAttachments(e.attachments),
        forwarded: e.forwarded, tgMessageId: e.tgMessageId, ts: e.ts });
      if (id) rowIds.push(id);
    }
  }

  const res = await deliver(event, {
    url: cfg.routine_url, key: cfg.routine_key, fetch: io.fetch, pause: io.pause,
    onAttempt: async (i) => {
      if (i > 1) {
        await store.noteAttempt(deliveryId, i);
      }
    },
  });
  const detail = redact(res.detail, [cfg.routine_key]).slice(0, 300) || null;
  if (res.ok) {
    await store.updateDelivery(deliveryId, { status: 'delivered', attempts: res.attempts, httpStatus: res.status });
    return { delivered: true, http: res.status, attempts: res.attempts, delivery_id: deliveryId,
      placeholder_message_id: placeholderId };
  }
  await store.updateDelivery(deliveryId, { status: 'dead', attempts: res.attempts, httpStatus: res.status, detail });
  if (rowIds.length) await store.removeMessages(rowIds); // unanswered; the sender is told to resend
  const code = res.status != null ? res.status : 'сеть';
  await failNotice(io, chatId, threadId, first.message_id, `⚠️ Не удалось передать сообщение боту (код ${code}). ` +
    'Оно сохранено, но не обработано: отправьте его ещё раз чуть позже.');
  console.warn('delivery failed', { http: res.status, attempts: res.attempts, permanent: res.permanent });
  return { delivered: false, http: res.status, attempts: res.attempts, delivery_id: deliveryId };
}

async function restoreSavedUsageCard(io, chatId, messageId, { failed = false } = {}) {
  if (!chatId || !messageId) return false;
  let raw = null;
  try { raw = await store.getKv(usageCardStoreKey(chatId, messageId)); }
  catch (e) { console.warn('usage card lookup failed', String(e)); return false; }
  const saved = parseStoredUsageCard(raw);
  if (!saved) return false;
  const res = await restoreUsageCard({ api: io.api, saved, chatId, messageId, failed });
  return !!(res && res.restored);
}

export async function handleCallbackQuery(cq, ctx) {
  const baseCfg = await store.loadConfig();
  const io = makeIo(ctx, baseCfg);
  const cfg = io.cfg;
  const cbId = cq && cq.id;
  let answered = false;
  const answer = async (text) => {
    if (!cbId || answered) return;
    answered = true;
    await safe(io, 'answerCallbackQuery', { callback_query_id: cbId, text });
  };

  if (!cfg.ready) {
    await answer('Мост ещё не настроен');
    return { skipped: 'not configured' };
  }
  const data = String((cq && cq.data) || '');
  if (data !== USAGE_REFRESH_DATA) {
    await answer('Недоступно');
    return { skipped: 'unknown callback' };
  }

  const from = (cq && cq.from) || {};
  const msg = (cq && cq.message) || {};
  const chat = msg.chat || {};
  const chatId = chat.id;
  const messageId = msg.message_id;
  const threadId = topicThreadId(msg) || (msg.message_thread_id ? Number(msg.message_thread_id) : null);

  try {
    if (chat.type !== 'private' || !chatId || !messageId) {
      await answer('Недоступно');
      return { skipped: 'bad callback' };
    }
    if (from.id !== cfg.owner_id) {
      await answer('Только владельцу');
      return { refused: 'owner only' };
    }

    const updateId = ctx && ctx.update && ctx.update.update_id != null ? Number(ctx.update.update_id) : null;
    if (updateId != null && !(await store.markSeen(updateId))) {
      await answer('Обновляю…');
      return { skipped: 'duplicate' };
    }

    if (await store.recentWakes(chatId) >= cfg.max_wakes_per_min) {
      await answer('Слишком много запросов, подождите');
      return { skipped: 'rate limited' };
    }

    // Toast only — do not replace the HTML card with «⏳ обновляю…».
    await answer('Обновляю…');

    await store.addPlaceholder({
      chatId, threadId: threadId || 0, replyTo: messageId, messageId,
    });

    const synthetic = {
      message_id: messageId,
      date: store.now(),
      text: '/usage',
      chat,
      from,
      ...(threadId ? { message_thread_id: threadId, is_topic_message: true } : {}),
    };
    const event = buildEvent({
      updateIds: updateId != null ? [updateId] : [],
      msgs: [synthetic],
      cfg,
      history: [],
      command: 'usage',
      placeholderMessageId: messageId,
    });

    const deliveryId = await store.createDelivery({
      updateId, chatId, threadId: threadId || 0, messageId, event,
    });
    const res = await deliver(event, {
      url: cfg.routine_url, key: cfg.routine_key, fetch: io.fetch, pause: io.pause,
      onAttempt: async (i) => { if (i > 1) await store.noteAttempt(deliveryId, i); },
    });
    const detail = redact(res.detail, [cfg.routine_key]).slice(0, 300) || null;
    if (res.ok) {
      await store.updateDelivery(deliveryId, { status: 'delivered', attempts: res.attempts, httpStatus: res.status });
      const out = { delivered: true, http: res.status, attempts: res.attempts, delivery_id: deliveryId,
        placeholder_message_id: messageId, command: 'usage', refreshed: true };
      if (io.calls) out.api_calls = io.calls;
      return out;
    }
    await store.updateDelivery(deliveryId, { status: 'dead', attempts: res.attempts, httpStatus: res.status, detail });
    const restored = await restoreSavedUsageCard(io, chatId, messageId, { failed: true });
    if (!restored) {
      const code = res.status != null ? res.status : 'сеть';
      await failNotice(io, chatId, threadId, messageId, `⚠️ Не удалось обновить лимиты (код ${code}). Попробуйте ещё раз чуть позже.`);
    }
    const out = { delivered: false, http: res.status, attempts: res.attempts, delivery_id: deliveryId, command: 'usage', restored };
    if (io.calls) out.api_calls = io.calls;
    return out;
  } catch (e) {
    console.error('failed to handle callback', redact(String(e && e.stack || e), [cfg.routine_key]));
    const restored = await restoreSavedUsageCard(io, chatId, messageId, { failed: true });
    if (!restored) {
      await failNotice(io, chatId, threadId, messageId,
        '⚠️ Не получилось обновить лимиты. Попробуйте ещё раз.');
    }
    // Do not answerCallbackQuery again — we already toasted «Обновляю…» (or an earlier error).
    if (!answered) await answer('Ошибка');
    const out = { error: 'exception', restored };
    if (io.calls) out.api_calls = io.calls;
    return out;
  } finally {
    try { await store.housekeep(cfg); } catch (e) { console.warn('housekeeping failed', String(e)); }
  }
}
