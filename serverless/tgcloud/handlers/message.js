// handlers/message.js — every incoming Telegram message lands here (the platform
// manages the webhook). Allowlisted private chats are relayed to the Grok Bot
// routine webhook; everything else is ignored. See lib/bridge.js.
import { handleMessage } from '../lib/bridge.js';

export default async function (message, ctx) {
  return handleMessage(message, ctx);
}
