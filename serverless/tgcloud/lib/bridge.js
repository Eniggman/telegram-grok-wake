// tg-bridge 4 — inbound flow for one Telegram message.
import { api, fetch } from 'sdk';
import {
  VERSION, classify, commandOf, topicThreadId, hasUserContent, buildEvent, deliver,
  inboundHistoryText, historyAttachments, redact,
} from './core.js';
import { pause as realPause } from './pause.js';
import * as store from './store.js';
import { mintReplyToken, tokenFingerprint } from './reply_token.js';

const ALBUM_WAIT_MS = 1500;

function makeIo(cfg) {
  return { cfg, api, fetch, pause: realPause };
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

/** Pending rows left by a killed invocation: mark dead and tell the sender. */
async function sweepStale(io) {
  for (const row of await store.stalePending(120)) {
    await store.updateDelivery(row.id, { status: 'dead', attempts: row.attempts, detail: 'handler stopped mid-delivery' });
    await notify(io, row.chat_id, row.thread_id || null,
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
  const io = makeIo(baseCfg);
  const cfg = io.cfg;
  if (!cfg.ready) {
    console.warn('tg-bridge is not configured yet (set owner_id, routine_url and routine_key with endpoints/box op: config_set); ignoring update');
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
    await notify(io, chatId, threadId, '⚠️ Не получилось обработать это сообщение. Попробуйте ещё раз.');
    result = { error: 'exception' };
  }
  try {
    await store.housekeep(cfg);
  } catch (e) {
    console.warn('housekeeping failed', String(e));
  }
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

  await sweepStale(io);

  let group = [{ updateId, msg: message }];
  if (message.media_group_id) {
    if (!hasUserContent([message])) return { skipped: 'empty' };
    await store.addAlbumPart(message, updateId);
    await io.pause(ALBUM_WAIT_MS);
    if (await store.newerAlbumPartExists(message)) return { album: 'handled by a later part' };
    group = await store.claimAlbum(message);
    if (!group.length) return { album: 'already claimed' };
  }
  const msgs = group.map((g) => g.msg);
  const updateIds = group.map((g) => g.updateId).filter((x) => x != null);
  if (!hasUserContent(msgs)) return { skipped: 'empty' };

  if (await store.recentWakes(chatId) >= cfg.max_wakes_per_min) {
    await notify(io, chatId, threadId, '⏳ Слишком много сообщений за минуту. Подождите немного и отправьте ещё раз.');
    return { skipped: 'rate limited' };
  }

  // Global cap for the whole bridge (all chats together), below the routine's own 200 runs/hour.
  if (await store.recentWakesGlobal() >= cfg.max_wakes_per_hour) {
    await notify(io, chatId, threadId, '⏳ Слишком много сообщений, попробуйте позже.');
    return { skipped: 'global rate limited' };
  }

  const first = msgs[0];
  if (cfg.reaction) {
    await safe(io, 'setMessageReaction', { chat_id: chatId, message_id: first.message_id,
      reaction: [{ type: 'emoji', emoji: cfg.reaction }] });
  }
  const typing = { chat_id: chatId, action: 'typing' };
  if (threadId) typing.message_thread_id = threadId;
  await safe(io, 'sendChatAction', typing);

  let history = [];
  try {
    history = await store.recentHistory(chatId, threadId, cfg.history_messages, {
      maxChars: cfg.history_max_chars, excludeUserIds: msgs.map((m) => m.message_id) });
  } catch (e) {
    console.warn('history unavailable, sending without it', String(e));
  }
  const event = buildEvent({ updateIds, msgs, cfg, history, topicName: await store.topicName(chatId, threadId) });
  try {
    const key = await store.getOrCreateReplySigningKey();
    const minted = await mintReplyToken(key, chatId, threadId);
    event.reply = {
      token: minted.token,
      expires_at: minted.expires_at,
      op: 'send',
      placeholder_message_id: null,
    };
    event.how_to_reply_via_box =
      'To send the answer back to this Telegram chat without the bot token, on the machine where this Serverless project is linked run: ' +
      'npx tgcloud run endpoints/box followed by JSON {"op":"send","token":<reply.token>,"text":<answer>,"final":true}. ' +
      'The token is bound to this chat (and topic) and lasts about 30 minutes. Non-final sends may repeat until expiry; final:true retires it. ' +
      'This is a bridge convention, not a documented Grok Bot API.';
  } catch (e) {
    console.warn('reply token not attached', String(e && (e.message || e)));
  }

  // Placeholder the first send will edit. Typing action above is reused from v4.
  const phParams = { chat_id: chatId, text: 'думаю…', reply_to_message_id: first.message_id };
  if (threadId) phParams.message_thread_id = threadId;
  const ph = await safe(io, 'sendMessage', phParams);
  const placeholderId = ph && ph.message_id != null ? Number(ph.message_id) : null;
  if (placeholderId && event.reply) {
    event.reply.placeholder_message_id = placeholderId;
    try {
      const fp = await tokenFingerprint(event.reply.token);
      await store.setReplyPlaceholder(fp, placeholderId);
    } catch (e) {
      console.warn('placeholder id not stored', String(e && (e.message || e)));
    }
  }

  const deliveryId = await store.createDelivery({
    updateId: updateIds.length ? updateIds[updateIds.length - 1] : null,
    chatId, threadId, messageId: first.message_id, event });
  const rowId = await store.addMessage({
    chatId, threadId, role: 'user', text: inboundHistoryText(event),
    attachments: historyAttachments(event.message.attachments),
    forwarded: Boolean(event.message.forward_origin), tgMessageId: first.message_id, ts: first.date });

  const res = await deliver(event, {
    url: cfg.routine_url, key: cfg.routine_key, fetch: io.fetch, pause: io.pause,
    onAttempt: async (i) => {
      if (i > 1) {
        await store.noteAttempt(deliveryId, i);
        await safe(io, 'sendChatAction', typing);
      }
    },
  });
  const detail = redact(res.detail, [cfg.routine_key]).slice(0, 300) || null;
  if (res.ok) {
    await store.updateDelivery(deliveryId, { status: 'delivered', attempts: res.attempts, httpStatus: res.status });
    await safe(io, 'sendChatAction', typing);
    return { delivered: true, http: res.status, attempts: res.attempts, delivery_id: deliveryId };
  }
  await store.updateDelivery(deliveryId, { status: 'dead', attempts: res.attempts, httpStatus: res.status, detail });
  if (rowId) await store.removeMessages([rowId]); // unanswered; the sender is told to resend
  const code = res.status != null ? res.status : 'сеть';
  const errText = `⚠️ Не удалось передать сообщение боту (код ${code}). Оно сохранено, но не обработано: отправьте его ещё раз чуть позже.`;
  let placeholderEdited = null;
  if (placeholderId) {
    placeholderEdited = await safe(io, 'editMessageText', { chat_id: chatId, message_id: placeholderId, text: errText });
  }
  if (!placeholderEdited) await notify(io, chatId, threadId, errText);
  console.warn('delivery failed', { http: res.status, attempts: res.attempts, permanent: res.permanent });
  return { delivered: false, http: res.status, attempts: res.attempts, delivery_id: deliveryId };
}
