/**
 * Краткоживущий токен ответа для endpoints/box (op: send).
 * HMAC-SHA256(key, chat_id|thread|exp|nonce), срок ~30 минут.
 * Наша схема, не официальный API Grok Bot. Токен бота в рутину не попадает.
 */
const TTL_SECONDS = 1800;

export async function mintReplyToken(secret, chatId, threadId) {
  const exp = Math.floor(Date.now() / 1000) + TTL_SECONDS;
  const nonce = b64url(crypto.getRandomValues(new Uint8Array(16)));
  const thread = threadId == null ? '0' : String(threadId);
  const payload = String(chatId) + '|' + thread + '|' + exp + '|' + nonce;
  const sig = await hmacSha256(secret, payload);
  return {
    token: 'v1.' + b64url(utf8(payload)) + '.' + b64url(sig),
    expires_at: new Date(exp * 1000).toISOString(),
  };
}

export async function verifyReplyToken(secret, token) {
  if (!token || typeof token !== 'string') return null;
  const parts = token.split('.');
  if (parts.length !== 3 || parts[0] !== 'v1') return null;
  let payload;
  try { payload = utf8decode(fromB64url(parts[1])); } catch { return null; }
  const fields = payload.split('|');
  if (fields.length !== 4) return null;
  const [chatId, thread, expStr, nonce] = fields;
  if (!/^-?\d+$/.test(chatId) || !/^\d+$/.test(thread) || !nonce) return null;
  const exp = Number(expStr);
  if (!Number.isFinite(exp) || exp < Math.floor(Date.now() / 1000)) return null;
  let actual;
  try { actual = fromB64url(parts[2]); } catch { return null; }
  const expected = await hmacSha256(secret, payload);
  if (!safeEqual(expected, actual)) return null;
  return { chatId, threadId: thread === '0' ? null : Number(thread) };
}

/** Стабильный ключ для пометки «уже закрыт через final» — не сам токен. */
export async function tokenFingerprint(token) {
  const digest = await crypto.subtle.digest('SHA-256', utf8(String(token || '')));
  return hex(new Uint8Array(digest));
}

async function hmacSha256(secret, data) {
  const key = await crypto.subtle.importKey(
    'raw', utf8(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, utf8(data)));
}

function utf8(s) { return new TextEncoder().encode(s); }
function utf8decode(u8) { return new TextDecoder().decode(u8); }

function b64url(u8) {
  let bin = '';
  for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function fromB64url(s) {
  const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4));
  const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function hex(u8) {
  return Array.from(u8).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
  return d === 0;
}
