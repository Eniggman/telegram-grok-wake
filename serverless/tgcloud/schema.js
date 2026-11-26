// tg-bridge 4 — database schema (Telegram Serverless, SQLite-backed).
// Deploying this file never changes the database: run `npx tgcloud migrate`.
import { table, integer, text, index, uniqueIndex } from 'sdk/db';

// Conversation memory: one row per inbound message (role=user) and per bot reply
// sent through tg-send on the box (role=assistant). A conversation is
// (chat_id, thread_id): private chat (= user id) + private-chat topic (0 = none).
export const messages = table('messages', {
  id:          integer('id').primaryKey({ autoIncrement: true }),
  chatId:      integer('chat_id').notNull(),
  threadId:    integer('thread_id').notNull().default(0),
  role:        text('role').notNull(),            // 'user' | 'assistant'
  text:        text('text').notNull().default(''),
  attachments: text('attachments'),               // JSON [{kind,file_name,mime_type,file_id}]
  forwarded:   integer('forwarded').notNull().default(0),
  tgMessageId: integer('tg_message_id'),
  ts:          integer('ts').notNull(),           // unix seconds
}, (t) => ({
  conv:   index('idx_messages_conv').on(t.chatId, t.threadId, t.id),
  dedupe: uniqueIndex('idx_messages_dedupe').on(t.chatId, t.role, t.tgMessageId),
  ts:     index('idx_messages_ts').on(t.ts),
}));

// Outbox for routine webhook deliveries. A row is written BEFORE the first POST
// ('pending'), so even a handler killed mid-delivery leaves a trace; it ends as
// 'delivered', 'dead' (dead letter: all attempts failed, sender was told) or
// 'redelivered' (sent later from the box with tg-redeliver).
export const deliveries = table('deliveries', {
  id:         integer('id').primaryKey({ autoIncrement: true }),
  updateId:   integer('update_id'),
  chatId:     integer('chat_id').notNull(),
  threadId:   integer('thread_id').notNull().default(0),
  messageId:  integer('message_id'),
  status:     text('status').notNull(),
  attempts:   integer('attempts').notNull().default(0),
  httpStatus: integer('http_status'),
  detail:     text('detail'),
  event:      text('event').notNull(),            // the JSON payload, for redelivery
  created:    integer('created').notNull(),
  updated:    integer('updated').notNull(),
}, (t) => ({
  upd:    uniqueIndex('idx_deliveries_update').on(t.updateId),
  status: index('idx_deliveries_status').on(t.status, t.created),
  chat:   index('idx_deliveries_chat').on(t.chatId, t.created),
}));

// Settings written from the box with `tg-cloud setup` (routine URL and key,
// allowlist, limits). Never readable from outside: there is no Mini App and the
// only endpoint refuses every call that carries Mini App init data.
export const kv = table('kv', {
  k: text('k').primaryKey(),
  v: text('v').notNull(),
});

// Telegram update_ids already handled (Telegram may re-send a webhook update).
export const seen = table('seen', {
  updateId: integer('update_id').primaryKey(),
  ts:       integer('ts').notNull(),
});

// Private-chat topic names (from forum_topic_created / _edited and tg-send topic).
export const topics = table('topics', {
  chatId:   integer('chat_id').notNull(),
  threadId: integer('thread_id').notNull(),
  name:     text('name').notNull(),
  ts:       integer('ts').notNull(),
}, (t) => ({
  uq: uniqueIndex('idx_topics_uq').on(t.chatId, t.threadId),
}));

// Album parts (media_group_id): collected so one album wakes the routine once.
export const albumParts = table('album_parts', {
  id:           integer('id').primaryKey({ autoIncrement: true }),
  mediaGroupId: text('media_group_id').notNull(),
  chatId:       integer('chat_id').notNull(),
  messageId:    integer('message_id').notNull(),
  updateId:     integer('update_id'),
  message:      text('message').notNull(),        // the Message JSON
  claimed:      integer('claimed').notNull().default(0),
  ts:           integer('ts').notNull(),
}, (t) => ({
  uq:  uniqueIndex('idx_album_uq').on(t.chatId, t.messageId),
  grp: index('idx_album_grp').on(t.chatId, t.mediaGroupId),
}));

// Burst parts (v4.4.2): a user's comment plus the forwarded messages that follow it
// within a short window are collected so they wake the routine once.
export const burstParts = table('burst_parts', {
  id:        integer('id').primaryKey({ autoIncrement: true }),
  burstId:   integer('burst_id').notNull(),       // message_id of the message that opened the burst
  chatId:    integer('chat_id').notNull(),
  threadId:  integer('thread_id').notNull().default(0),
  messageId: integer('message_id').notNull(),
  updateId:  integer('update_id'),
  message:   text('message').notNull(),           // the Message JSON
  claimed:   integer('claimed').notNull().default(0),
  tsMs:      integer('ts_ms').notNull(),
  ts:        integer('ts').notNull(),
}, (t) => ({
  uq:  uniqueIndex('idx_burst_uq').on(t.chatId, t.messageId),
  grp: index('idx_burst_grp').on(t.chatId, t.threadId, t.claimed),
}));

// "Stop" taps on a streaming draft (stopped_message_generation updates).
export const stops = table('stops', {
  chatId:  integer('chat_id').notNull(),
  draftId: integer('draft_id').notNull(),
  ts:      integer('ts').notNull(),
}, (t) => ({
  uq: uniqueIndex('idx_stops_uq').on(t.chatId, t.draftId),
}));

// v4.3: the "🗨️ Думаю..." placeholder the bridge sends as a reply to an incoming
// message, so tg-send on the box can find it (by chat + replied-to message id)
// and turn it into the answer. state: 'active' → 'claimed' (taken by tg-send)
// or 'failed' (turned into the delivery-error notice by the bridge). Claims are
// atomic UPDATE … WHERE state = 'active', so one placeholder is used once.
export const placeholders = table('placeholders', {
  id:        integer('id').primaryKey({ autoIncrement: true }),
  chatId:    integer('chat_id').notNull(),
  threadId:  integer('thread_id').notNull().default(0),
  replyTo:   integer('reply_to').notNull(),         // the user's message_id
  messageId: integer('message_id').notNull(),       // the placeholder's message_id
  stickerMessageId: integer('sticker_message_id'),  // v4.4: thinking sticker; deleted on first answer
  state:     text('state').notNull(),
  created:   integer('created').notNull(),
  updated:   integer('updated').notNull(),
}, (t) => ({
  uq:   uniqueIndex('idx_placeholders_uq').on(t.chatId, t.replyTo),
  conv: index('idx_placeholders_conv').on(t.chatId, t.threadId, t.state),
}));
