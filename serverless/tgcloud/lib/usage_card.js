// lib/usage_card.js — pure rendering of the /usage Telegram card (no SDK).
// Follows Bot API: HTML parse mode, 1–4096 chars after entity parsing,
// InlineKeyboardMarkup, callback_data ≤ 64 bytes.
// Docs: https://core.telegram.org/bots/api#formatting-options
//       https://core.telegram.org/bots/api#inlinekeyboardmarkup
//       https://core.telegram.org/bots/api#inlinekeyboardbutton
//       https://core.telegram.org/bots/api#editmessagetext
//       https://core.telegram.org/bots/api#answercallbackquery

export const USAGE_REFRESH_DATA = 'usage_refresh';
export const USAGE_REFRESH_LABEL = '🔄 Обновить';
export const USAGE_BAR_WIDTH = 10;
export const USAGE_MAX_TOP = 5;
export const TELEGRAM_TEXT_MAX = 4096;
export const API_NEEDS_KEY_NOTE = 'нужен management-ключ';
export const API_CHART_DAYS = 10;
export const API_BREAKDOWN_DAYS = 3;
export const API_BAR_WIDTH = 10;
export const API_BAR_BLOCK = '█';

/** Escape text for Telegram HTML parse_mode (&, <, >). */
export function escapeHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function clampPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

export function formatPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '0%';
  const p = Math.round(n * 10) / 10;
  return Number.isInteger(p) ? `${p}%` : `${p.toFixed(1)}%`;
}

/** Text bar like `▓▓▓▓▓▓▓░░░ 75.6%`. `pct` is shown as given (clamped only for fill). */
export function progressBar(pct, width = USAGE_BAR_WIDTH) {
  const w = Number.isInteger(width) && width > 0 ? width : USAGE_BAR_WIDTH;
  const fillPct = clampPct(pct);
  const filled = Math.min(w, Math.max(0, Math.round((fillPct / 100) * w)));
  const shown = Number.isFinite(Number(pct)) ? Number(pct) : 0;
  return `${'▓'.repeat(filled)}${'░'.repeat(w - filled)} ${formatPct(shown)}`;
}

function nonEmpty(v) {
  return v != null && String(v).trim() !== '';
}

function padEndVis(s, w) {
  const t = String(s);
  return t.length >= w ? t : t + ' '.repeat(w - t.length);
}

function padStartVis(s, w) {
  const t = String(s);
  return t.length >= w ? t : ' '.repeat(w - t.length) + t;
}

/** ISO date `YYYY-MM-DD` → `DD.MM`. */
export function formatDayDot(isoDate) {
  const m = String(isoDate || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return String(isoDate || '');
  return `${m[3]}.${m[2]}`;
}

/** True when there is no daily chart to draw (missing or empty, not a real all-zero series). */
export function isEmptyApiDays(days) {
  return !Array.isArray(days) || days.length === 0;
}

/**
 * True when api_breakdown has at least one real field.
 * `{}`, missing keys, or all-null stubs (as in the routine template) are empty.
 * A successful fetch of genuine zeros (tokens_text / usd_text present) is not empty.
 */
export function hasApiBreakdown(br) {
  if (!br || typeof br !== 'object' || Array.isArray(br)) return false;
  const rowHas = (row) => {
    if (row == null || typeof row !== 'object') return false;
    if (nonEmpty(row.tokens_text) || nonEmpty(row.usd_text)) return true;
    if (row.tokens != null && Number.isFinite(Number(row.tokens))) return true;
    if (row.usd != null && Number.isFinite(Number(row.usd))) return true;
    return false;
  };
  if (rowHas(br.input) || rowHas(br.output) || rowHas(br.cache)) return true;
  if (br.other_usd != null && Number.isFinite(Number(br.other_usd))) return true;
  if (nonEmpty(br.other_usd_text) || nonEmpty(br.total_text) || nonEmpty(br.total_usd_text) || nonEmpty(br.total_tokens_text)) return true;
  if (br.total_usd != null && Number.isFinite(Number(br.total_usd))) return true;
  if (br.total_tokens != null && Number.isFinite(Number(br.total_tokens))) return true;
  return false;
}

/**
 * 10-day daily $ chart, monospace rows:
 * `22.11 │ ██████████ 2.26`
 * Bar width is scaled so the max day is 10 blocks. Empty/missing days → no chart.
 */
export function renderApiChart(days) {
  if (isEmptyApiDays(days)) return '';
  const rows = days.filter((d) => d && nonEmpty(d.date));
  if (!rows.length) return '';
  const max = Math.max(0, ...rows.map((d) => {
    const n = Number(d.usd);
    return Number.isFinite(n) ? n : 0;
  }));
  return rows.map((d) => {
    const n = Number(d.usd);
    const usd = Number.isFinite(n) ? n : 0;
    const filled = max > 0
      ? Math.min(API_BAR_WIDTH, Math.max(0, Math.round((usd / max) * API_BAR_WIDTH)))
      : 0;
    const bar = API_BAR_BLOCK.repeat(filled).padEnd(API_BAR_WIDTH, ' ');
    return `${formatDayDot(d.date)} │ ${bar} ${usd.toFixed(2)}`;
  }).join('\n');
}

function rowUsd(row) {
  if (row && nonEmpty(row.usd_text)) return String(row.usd_text).trim();
  if (row && row.usd != null && Number.isFinite(Number(row.usd))) {
    return `$${Number(row.usd).toFixed(2)}`;
  }
  return '';
}

function rowTokens(row) {
  if (row && nonEmpty(row.tokens_text)) return String(row.tokens_text).trim();
  return '';
}

/**
 * Last-3-days table by token type, plus the итого line.
 * Empty/missing breakdown (`{}`, all-null) renders nothing — not a zero table.
 */
export function renderApiBreakdown(br) {
  if (!hasApiBreakdown(br)) return '';
  const lines = [];
  const mk = (label, tokens, usd) => {
    const lab = padEndVis(label, 6);
    const tok = padStartVis(tokens, 10);
    const money = padStartVis(usd, 7);
    return `${lab} ${tok} ${money}`.trimEnd();
  };
  lines.push(mk('Ввод', rowTokens(br.input), rowUsd(br.input)));
  lines.push(mk('Выход', rowTokens(br.output), rowUsd(br.output)));
  lines.push(mk('Кэш', rowTokens(br.cache), rowUsd(br.cache)));
  const other = nonEmpty(br.other_usd_text)
    ? String(br.other_usd_text).trim()
    : rowUsd({ usd: br.other_usd });
  lines.push(mk('Прочее', '', other));
  const total = nonEmpty(br.total_text)
    ? String(br.total_text).trim()
    : '';
  if (total) {
    lines.push('');
    lines.push(total);
  }
  return lines.join('\n');
}

function renderApiPre(d) {
  const chart = renderApiChart(d.api_days);
  const table = renderApiBreakdown(d.api_breakdown);
  const parts = [];
  if (chart) parts.push(chart);
  if (table) {
    if (parts.length) parts.push('');
    parts.push(table);
  }
  if (!parts.length) return '';
  return `<pre>${escapeHtml(parts.join('\n'))}</pre>`;
}

/**
 * Build the /usage card.
 * data: {plan, weekly_used_pct, grok_pct?, resets_at_text, credits_text?,
 *        top?: [{name, pct}],
 *        api_days?: [{date, usd}], api_breakdown?,
 *        api_spend_text?, api_tokens_text?, api_models_text?,
 *        api_note?}
 * API section is omitted unless chart, breakdown, spend, or api_note
 * is provided (the card never depends on API spend being available).
 * api_balance_text is ignored and is not drawn (Management prepaid is not
 * the grok.com credit balance).
 * Empty api_days ([]) and empty api_breakdown ({}) are omitted, not drawn as zeros.
 */
export function renderUsageCard(data) {
  const d = data && typeof data === 'object' ? data : {};
  const lines = [];
  lines.push('📊 <b>Лимиты Grok</b>');
  if (nonEmpty(d.plan)) lines.push(`План: ${escapeHtml(String(d.plan).trim())}`);
  lines.push('');
  lines.push('<b>Недельный лимит</b>');
  lines.push(`<code>${escapeHtml(progressBar(d.weekly_used_pct))}</code>`);
  if (d.grok_pct != null && String(d.grok_pct).trim() !== '') {
    lines.push(`Доля Grok Bot: ${escapeHtml(formatPct(d.grok_pct))}`);
  }

  const rawTop = Array.isArray(d.top) ? d.top.slice(0, USAGE_MAX_TOP) : [];
  const tops = rawTop.filter((row) => row && typeof row === 'object');
  if (tops.length) {
    lines.push('');
    lines.push('<b>Больше всего</b>');
    for (const row of tops) {
      const name = escapeHtml(String(row.name ?? '').trim() || '—');
      lines.push(`• ${name} — <code>${escapeHtml(progressBar(row.pct))}</code>`);
    }
  }

  const tail = [];
  if (nonEmpty(d.resets_at_text)) tail.push(`Сброс: ${escapeHtml(String(d.resets_at_text).trim())}`);
  if (nonEmpty(d.credits_text)) tail.push(`Кредиты: ${escapeHtml(String(d.credits_text).trim())}`);
  if (tail.length) {
    lines.push('');
    lines.push(...tail);
  }

  const pre = renderApiPre(d);
  const spend = nonEmpty(d.api_spend_text) ? String(d.api_spend_text).trim() : '';
  const tokens = nonEmpty(d.api_tokens_text) ? String(d.api_tokens_text).trim() : '';
  const models = nonEmpty(d.api_models_text) ? String(d.api_models_text).trim() : '';
  const note = nonEmpty(d.api_note) ? String(d.api_note).trim() : '';
  if (pre || spend || tokens || models || note) {
    lines.push('');
    lines.push('<b>API</b>');
    if (pre) {
      lines.push(pre);
    } else {
      if (spend && tokens) lines.push(`Потрачено: ${escapeHtml(spend)} · ${escapeHtml(tokens)}`);
      else if (spend) lines.push(`Потрачено: ${escapeHtml(spend)}`);
      else if (tokens) lines.push(`Токены: ${escapeHtml(tokens)}`);
      if (models) lines.push(escapeHtml(models));
    }
    if (note) lines.push(escapeHtml(note));
  }

  const text = lines.join('\n').trim();
  if (text.length > TELEGRAM_TEXT_MAX) {
    throw new Error(`usage card exceeds Telegram text limit (${text.length} > ${TELEGRAM_TEXT_MAX})`);
  }

  return {
    text,
    parse_mode: 'HTML',
    reply_markup: {
      inline_keyboard: [[{ text: USAGE_REFRESH_LABEL, callback_data: USAGE_REFRESH_DATA }]],
    },
  };
}

/** Legacy updating marker stripped by withRefreshFailedNote if present on older cards. */
export const USAGE_UPDATING_MARK = '⏳ обновляю…';
export const USAGE_REFRESH_FAIL_NOTE = 'не удалось обновить';

/** Strip a leftover updating mark and append (once) a short failure note. HTML is preserved. */
export function withRefreshFailedNote(html) {
  let t = String(html || '');
  if (t.includes(USAGE_UPDATING_MARK)) {
    t = t.split(USAGE_UPDATING_MARK).join('').replace(/\n{3,}/g, '\n\n').trim();
  }
  if (t.includes(USAGE_REFRESH_FAIL_NOTE)) return t;
  const note = escapeHtml(USAGE_REFRESH_FAIL_NOTE);
  const next = t ? `${t}\n\n${note}` : note;
  if (next.length > TELEGRAM_TEXT_MAX) return t || note;
  return next;
}

export function usageCardStoreKey(chatId, messageId) {
  return `_usage_card:${chatId}:${messageId}`;
}

function isNotModified(err) {
  return /message is not modified/i.test(String(err && (err.description || err.message) || err));
}

/**
 * Edit the existing message into the usage card, or send a new one.
 * "message is not modified" counts as a successful edit. Does not import the SDK.
 * `save(chatId, messageId, card)` is optional (store HTML so a failed refresh can restore it).
 */
export async function publishUsageCard({ api, card, chatId, targetId = null, threadId = 0, onEditFail, save } = {}) {
  if (!api || !card) throw new Error('api and card required');
  const params = {
    text: card.text, parse_mode: card.parse_mode, reply_markup: card.reply_markup,
    link_preview_options: { is_disabled: true },
  };
  if (targetId) {
    try {
      await api.editMessageText({ chat_id: chatId, message_id: targetId, ...params });
      if (save) await save(chatId, targetId, card);
      return { message_id: targetId, edited: true };
    } catch (e) {
      if (isNotModified(e)) {
        if (save) await save(chatId, targetId, card);
        return { message_id: targetId, edited: true, unchanged: true };
      }
      if (onEditFail) await onEditFail(e);
    }
  }
  const sent = await api.sendMessage({
    chat_id: chatId, ...params,
    ...(threadId ? { message_thread_id: threadId } : {}),
  });
  const mid = sent && sent.message_id ? Number(sent.message_id) : null;
  if (!mid) throw new Error('sendMessage did not return message_id');
  if (save) await save(chatId, mid, card);
  return { message_id: mid, edited: false };
}

/** Re-apply a previously published card, optionally with the «не удалось обновить» note. */
export async function restoreUsageCard({ api, saved, chatId, messageId, failed = false } = {}) {
  if (!api || !saved || !saved.text || !messageId) return { restored: false };
  const text = failed ? withRefreshFailedNote(saved.text) : saved.text;
  try {
    await api.editMessageText({
      chat_id: chatId, message_id: messageId, text,
      parse_mode: saved.parse_mode || 'HTML',
      reply_markup: saved.reply_markup,
      link_preview_options: { is_disabled: true },
    });
    return { restored: true, failed: !!failed };
  } catch (e) {
    if (isNotModified(e)) return { restored: true, unchanged: true, failed: !!failed };
    return { restored: false, error: String(e && (e.description || e.message) || e) };
  }
}

export function parseStoredUsageCard(raw) {
  if (!raw) return null;
  try {
    const o = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!o || !nonEmpty(o.text)) return null;
    return o;
  } catch {
    return null;
  }
}
