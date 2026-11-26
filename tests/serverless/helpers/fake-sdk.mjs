import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export const sqlite = new DatabaseSync(':memory:');
// Tables as declared in schema.js, hand-written for SQLite.
sqlite.exec(`
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE seen (update_id INTEGER PRIMARY KEY, ts INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, thread_id INTEGER DEFAULT 0, role TEXT,
  text TEXT DEFAULT '', attachments TEXT, forwarded INTEGER DEFAULT 0, tg_message_id INTEGER, ts INTEGER);
CREATE UNIQUE INDEX m_d ON messages (chat_id, role, tg_message_id);
CREATE TABLE topics (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, thread_id INTEGER, name TEXT, ts INTEGER, UNIQUE(chat_id, thread_id));
CREATE TABLE deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, update_id INTEGER UNIQUE, chat_id INTEGER, thread_id INTEGER,
  message_id INTEGER, status TEXT, attempts INTEGER, http_status INTEGER, detail TEXT, event TEXT, created INTEGER, updated INTEGER);
CREATE TABLE album_parts (id INTEGER PRIMARY KEY AUTOINCREMENT, media_group_id TEXT, chat_id INTEGER, message_id INTEGER,
  update_id INTEGER, message TEXT, claimed INTEGER DEFAULT 0, ts INTEGER, UNIQUE(chat_id, message_id));
CREATE TABLE burst_parts (id INTEGER PRIMARY KEY AUTOINCREMENT, burst_id INTEGER, chat_id INTEGER, thread_id INTEGER DEFAULT 0,
  message_id INTEGER, update_id INTEGER, message TEXT, claimed INTEGER DEFAULT 0, ts_ms INTEGER, ts INTEGER, UNIQUE(chat_id, message_id));
CREATE TABLE placeholders (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, thread_id INTEGER DEFAULT 0, reply_to INTEGER,
  message_id INTEGER, sticker_message_id INTEGER, state TEXT, created INTEGER, updated INTEGER, UNIQUE(chat_id, reply_to));
CREATE TABLE stops (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, draft_id TEXT, ts INTEGER, UNIQUE(chat_id, draft_id));
`);
const conv = (v) => (v === undefined ? null : v);
const run1 = (q) => sqlite.prepare(q.text).run(...q.values.map(conv));
export const db = {
  async run(q) {
    if (/RETURNING/i.test(q.text)) {
      const rows = sqlite.prepare(q.text).all(...q.values.map(conv));
      return { rows, rowsAffected: rows.length };
    }
    const r = run1(q);
    return { rowsAffected: Number(r.changes), lastInsertRowid: r.lastInsertRowid, rows: [] };
  },
  async all(q) { return sqlite.prepare(q.text).all(...q.values.map(conv)); },
  async get(q) { return sqlite.prepare(q.text).get(...q.values.map(conv)); },
};
export const calls = [];
export const api = new Proxy({}, {
  get: (_, method) => async (params) => {
    if (method === 'getMe') { await new Promise((r) => setTimeout(r, 5)); return {}; } // pause() polls this
    calls.push({ method, params });
    if (method === 'getStickerSet') return { stickers: [{ file_id: 'S1' }] };
    return { message_id: 1000 + calls.length };
  },
});
export const fetchCalls = [];
export const fetch = async (url, init) => { fetchCalls.push({ url, body: JSON.parse(init.body) }); return { ok: true, status: 200, text: async () => 'ok' }; };
