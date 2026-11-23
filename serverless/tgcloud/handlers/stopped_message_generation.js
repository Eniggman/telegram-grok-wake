// handlers/stopped_message_generation.js — the user tapped "Stop" on a streaming
// draft (streaming reply). Recorded so a later send can stop.
import { loadConfig, addStop } from '../lib/store.js';

export default async function (ev) {
  const cfg = await loadConfig();
  const chat = (ev && ev.chat) || {};
  if (chat.type !== 'private' || !cfg.allowed.has(chat.id)) return { skipped: 'not allowed' };
  const draftId = Number(ev.draft_id || 0);
  if (!draftId) return { skipped: 'no draft id' };
  await addStop(chat.id, draftId);
  return { stopped: draftId };
}
