// handlers/callback_query.js — taps on inline buttons (e.g. «Обновить» on /usage).
import { handleCallbackQuery } from '../lib/bridge.js';

export default async function (cq, ctx) {
  return handleCallbackQuery(cq, ctx);
}
