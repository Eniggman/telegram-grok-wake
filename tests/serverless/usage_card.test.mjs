import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  escapeHtml, progressBar, formatPct, clampPct, renderUsageCard,
  USAGE_REFRESH_DATA, USAGE_REFRESH_LABEL, USAGE_BAR_WIDTH, USAGE_MAX_TOP,
  TELEGRAM_TEXT_MAX, API_NEEDS_KEY_NOTE, USAGE_UPDATING_MARK,
  formatDayDot, renderApiChart, renderApiBreakdown, API_BAR_WIDTH, API_BAR_BLOCK,
  isEmptyApiDays, hasApiBreakdown, withRefreshFailedNote, USAGE_REFRESH_FAIL_NOTE,
  publishUsageCard, restoreUsageCard, usageCardStoreKey, parseStoredUsageCard,
} from '../../serverless/tgcloud/lib/usage_card.js';

test('escapeHtml escapes &, <, > in that order', () => {
  assert.equal(escapeHtml('a & b <c>'), 'a &amp; b &lt;c&gt;');
  assert.equal(escapeHtml('Tom & Jerry'), 'Tom &amp; Jerry');
  assert.equal(escapeHtml(''), '');
  assert.equal(escapeHtml(null), '');
});

test('progressBar: 0, 50, 75.6, 100, and out-of-range', () => {
  assert.equal(progressBar(0), '░░░░░░░░░░ 0%');
  assert.equal(progressBar(50), '▓▓▓▓▓░░░░░ 50%');
  assert.equal(progressBar(75.6), '▓▓▓▓▓▓▓▓░░ 75.6%');
  assert.equal(progressBar(100), '▓▓▓▓▓▓▓▓▓▓ 100%');
  assert.equal(progressBar(-10).startsWith('░░░░░░░░░░'), true);
  assert.equal(progressBar(150).startsWith('▓▓▓▓▓▓▓▓▓▓'), true);
  assert.equal(progressBar(undefined), '░░░░░░░░░░ 0%');
  assert.equal(progressBar(75.6, USAGE_BAR_WIDTH).includes('75.6%'), true);
});

test('formatPct and clampPct', () => {
  assert.equal(formatPct(75.6), '75.6%');
  assert.equal(formatPct(37), '37%');
  assert.equal(formatPct(37.04), '37%');
  assert.equal(formatPct(37.05), '37.1%');
  assert.equal(clampPct(150), 100);
  assert.equal(clampPct(-5), 0);
  assert.equal(clampPct('x'), 0);
});

test('renderUsageCard: bars, HTML escape, keyboard, optional fields', () => {
  const card = renderUsageCard({
    plan: 'Super <Grok> & Co',
    weekly_used_pct: 75.6,
    grok_pct: 69.7,
    resets_at_text: 'среда, 25 ноября, 23:10 <Киев>',
    credits_text: '$0',
    top: [
      { name: 'Telegram <Grok>', pct: 37 },
      { name: 'Grok Bot', pct: 31 },
    ],
  });
  assert.equal(card.parse_mode, 'HTML');
  assert.match(card.text, /<b>Лимиты Grok<\/b>/);
  assert.match(card.text, /План: Super &lt;Grok&gt; &amp; Co/);
  assert.match(card.text, /▓▓▓▓▓▓▓▓░░ 75\.6%/);
  assert.match(card.text, /Доля Grok Bot: 69\.7%/);
  assert.match(card.text, /Telegram &lt;Grok&gt;/);
  assert.doesNotMatch(card.text, /Telegram <Grok>/);
  assert.match(card.text, /Сброс: среда, 25 ноября, 23:10 &lt;Киев&gt;/);
  assert.match(card.text, /Кредиты: \$0/);
  assert.doesNotMatch(card.text, /<b>API<\/b>/);
  assert.ok(card.text.length <= TELEGRAM_TEXT_MAX);
  const row = card.reply_markup.inline_keyboard[0][0];
  assert.equal(row.text, USAGE_REFRESH_LABEL);
  assert.equal(row.callback_data, USAGE_REFRESH_DATA);
  assert.ok(Buffer.byteLength(row.callback_data, 'utf8') <= 64);
  assert.ok(Buffer.byteLength(row.callback_data, 'utf8') >= 1);
});

test('renderUsageCard: API section only when provided; hide otherwise', () => {
  const hidden = renderUsageCard({ weekly_used_pct: 10, plan: 'P' });
  assert.doesNotMatch(hidden.text, /<b>API<\/b>/);
  assert.doesNotMatch(hidden.text, /нужен management-ключ/);

  const noteOnly = renderUsageCard({ weekly_used_pct: 10, api_note: API_NEEDS_KEY_NOTE });
  assert.match(noteOnly.text, /<b>API<\/b>/);
  assert.match(noteOnly.text, /нужен management-ключ/);

  const withSpend = renderUsageCard({
    weekly_used_pct: 10,
    api_spend_text: '$12.34',
    api_balance_text: '$50 & more',
  });
  assert.match(withSpend.text, /Потрачено: \$12\.34/);
  assert.doesNotMatch(withSpend.text, /Баланс/);
});

test('renderUsageCard: api_balance_text is not drawn', () => {
  const onlyBal = renderUsageCard({ weekly_used_pct: 10, api_balance_text: '$16.85' });
  assert.doesNotMatch(onlyBal.text, /<b>API<\/b>/);
  assert.doesNotMatch(onlyBal.text, /Баланс/);
  assert.doesNotMatch(onlyBal.text, /16\.85/);
});

test('renderUsageCard: top capped at 5; missing optionals omitted', () => {
  const top = Array.from({ length: 7 }, (_, i) => ({ name: `Bot ${i}`, pct: i * 10 }));
  const card = renderUsageCard({ weekly_used_pct: 1, top });
  assert.equal((card.text.match(/• Bot /g) || []).length, USAGE_MAX_TOP);
  assert.doesNotMatch(card.text, /Bot 5/);
  const slim = renderUsageCard({ weekly_used_pct: 0 });
  assert.doesNotMatch(slim.text, /План:/);
  assert.doesNotMatch(slim.text, /Доля Grok Bot/);
  assert.doesNotMatch(slim.text, /Больше всего/);
  assert.doesNotMatch(slim.text, /Сброс:/);
  assert.doesNotMatch(slim.text, /Кредиты:/);
});


test('formatDayDot and 10-day chart scaled to max=10 blocks', () => {
  assert.equal(formatDayDot('2026-11-22'), '22.11');
  assert.equal(formatDayDot('2026-01-05'), '05.01');
  const days = [
    { date: '2026-11-15', usd: 0 },
    { date: '2026-11-22', usd: 2.26 },
    { date: '2026-11-23', usd: 1.58 },
    { date: '2026-11-24', usd: 0 },
  ];
  const chart = renderApiChart(days);
  const rows = chart.split('\n');
  assert.equal(rows[0], '15.11 │            0.00');
  // 2.26 is the max → 10 blocks; 1.58/2.26*10 ≈ 7
  assert.equal(rows[1], `22.11 │ ${API_BAR_BLOCK.repeat(10)} 2.26`);
  assert.equal(rows[2], `23.11 │ ${API_BAR_BLOCK.repeat(7)}${' '.repeat(API_BAR_WIDTH - 7)} 1.58`);
  assert.equal(rows[3], '24.11 │            0.00');
});

test('renderApiBreakdown: last-3-days table and итого', () => {
  const text = renderApiBreakdown({
    input: { tokens_text: '1.3 млн', usd_text: '$1.37' },
    output: { tokens_text: '0.1 млн', usd_text: '$0.33' },
    cache: { tokens_text: '11.5 млн', usd_text: '$2.10' },
    other_usd_text: '$0.04',
    total_text: 'Итого за 3 дня: 12.9 млн токенов · $3.84',
  });
  assert.match(text, /Ввод/);
  assert.match(text, /1\.3 млн/);
  assert.match(text, /\$1\.37/);
  assert.match(text, /Выход/);
  assert.match(text, /Кэш/);
  assert.match(text, /11\.5 млн/);
  assert.match(text, /Прочее/);
  assert.match(text, /\$0\.04/);
  assert.match(text, /Итого за 3 дня: 12\.9 млн токенов · \$3\.84/);
});

test('renderUsageCard: API pre block, limit bars and refresh button kept', () => {
  const days = [];
  for (let i = 15; i <= 24; i++) {
    const dd = String(i).padStart(2, '0');
    let usd = 0;
    if (i === 22) usd = 2.26;
    if (i === 23) usd = 1.58;
    days.push({ date: `2026-11-${dd}`, usd });
  }
  const card = renderUsageCard({
    plan: 'SuperGrok',
    weekly_used_pct: 75.6,
    grok_pct: 69.7,
    resets_at_text: 'среда, 25 ноября',
    credits_text: '$0',
    top: [{ name: 'Telegram Grok', pct: 37 }],
    api_days: days,
    api_breakdown: {
      input: { tokens_text: '1.3 млн', usd_text: '$1.37' },
      output: { tokens_text: '0.1 млн', usd_text: '$0.33' },
      cache: { tokens_text: '11.5 млн', usd_text: '$2.10' },
      other_usd_text: '$0.04',
      total_text: 'Итого за 3 дня: 12.9 млн токенов · $3.84',
    },
    api_balance_text: '$0.00',
  });
  assert.equal(card.parse_mode, 'HTML');
  assert.match(card.text, /<b>Лимиты Grok<\/b>/);
  assert.match(card.text, /▓▓▓▓▓▓▓▓░░ 75\.6%/);
  assert.match(card.text, /<b>API<\/b>/);
  assert.match(card.text, /<pre>/);
  assert.match(card.text, /22\.11 │ █{10} 2\.26/);
  assert.match(card.text, /23\.11 │ █{7} {3} 1\.58/);
  assert.match(card.text, /Итого за 3 дня: 12\.9 млн токенов · \$3\.84/);
  assert.doesNotMatch(card.text, /Баланс/);
  assert.doesNotMatch(card.text, /Потрачено:/);
  assert.ok(card.text.length <= TELEGRAM_TEXT_MAX);
  const row = card.reply_markup.inline_keyboard[0][0];
  assert.equal(row.text, USAGE_REFRESH_LABEL);
  assert.equal(row.callback_data, USAGE_REFRESH_DATA);
});

test('renderUsageCard: API pre escapes HTML; old spend fields still work without chart', () => {
  const withHtml = renderUsageCard({
    weekly_used_pct: 1,
    api_days: [{ date: '2026-11-22', usd: 1 }],
    api_breakdown: {
      input: { tokens_text: '1 млн <oops>', usd_text: '$1 & up' },
      output: { tokens_text: '0 ток.', usd_text: '$0.00' },
      cache: { tokens_text: '0 ток.', usd_text: '$0.00' },
      other_usd_text: '$0.00',
      total_text: 'Итого за 3 дня: 1 млн токенов · $1 & up',
    },
  });
  assert.match(withHtml.text, /1 млн &lt;oops&gt;/);
  assert.match(withHtml.text, /\$1 &amp; up/);
  assert.doesNotMatch(withHtml.text, /<oops>/);

  const legacy = renderUsageCard({
    weekly_used_pct: 10,
    api_spend_text: '$12.34',
    api_tokens_text: '1 млн ток.',
    api_balance_text: '$50 & more',
  });
  assert.doesNotMatch(legacy.text, /<pre>/);
  assert.match(legacy.text, /Потрачено: \$12\.34 · 1 млн ток\./);
  assert.doesNotMatch(legacy.text, /Баланс/);
});

test('empty api_days and empty api_breakdown are omitted, not drawn as zeros', () => {
  assert.equal(isEmptyApiDays([]), true);
  assert.equal(isEmptyApiDays(null), true);
  assert.equal(isEmptyApiDays([{ date: '2026-11-22', usd: 0 }]), false);
  assert.equal(hasApiBreakdown({}), false);
  assert.equal(hasApiBreakdown(null), false);
  assert.equal(hasApiBreakdown({ input: null, output: null, cache: null }), false);
  assert.equal(hasApiBreakdown({ input: { tokens_text: '0 ток.', usd_text: '$0.00' } }), true);
  assert.equal(renderApiChart([]), '');
  assert.equal(renderApiBreakdown({}), '');
  assert.equal(renderApiBreakdown({ input: null }), '');

  const stub = renderUsageCard({
    weekly_used_pct: 10,
    api_days: [],
    api_breakdown: {},
  });
  assert.doesNotMatch(stub.text, /<pre>/);
  assert.doesNotMatch(stub.text, /<b>API<\/b>/);
  assert.doesNotMatch(stub.text, /0\.00/);
  assert.doesNotMatch(stub.text, /Ввод/);

  const realZero = renderApiChart([{ date: '2026-11-22', usd: 0 }]);
  assert.match(realZero, /22\.11 │            0\.00/);
});

test('withRefreshFailedNote appends once and strips the updating mark', () => {
  const once = withRefreshFailedNote('<b>API</b>\n\n' + USAGE_UPDATING_MARK);
  assert.match(once, /<b>API<\/b>/);
  assert.doesNotMatch(once, /обновляю/);
  assert.match(once, new RegExp(USAGE_REFRESH_FAIL_NOTE));
  assert.equal(withRefreshFailedNote(once), once);
});

test('publishUsageCard: edit, not-modified, and send fallback', async () => {
  const card = renderUsageCard({ weekly_used_pct: 10, api_note: 'ok' });
  const saved = [];
  const api = {
    async editMessageText(params) {
      this.edits = this.edits || [];
      this.edits.push(params);
      if (this.fail) {
        const err = new Error(this.fail);
        err.description = this.fail;
        throw err;
      }
      return {};
    },
    async sendMessage(params) {
      this.sends = this.sends || [];
      this.sends.push(params);
      return { message_id: 99 };
    },
  };

  const edited = await publishUsageCard({
    api, card, chatId: 7, targetId: 50,
    save: async (c, mid, ca) => saved.push({ c, mid, text: ca.text }),
  });
  assert.equal(edited.edited, true);
  assert.equal(edited.message_id, 50);
  assert.equal(api.edits.length, 1);
  assert.equal(api.edits[0].parse_mode, 'HTML');
  assert.equal(saved[0].mid, 50);

  api.fail = 'Bad Request: message is not modified';
  const unchanged = await publishUsageCard({ api, card, chatId: 7, targetId: 50 });
  assert.equal(unchanged.edited, true);
  assert.equal(unchanged.unchanged, true);
  assert.equal(unchanged.message_id, 50);
  assert.ok(!api.sends);

  api.fail = "Bad Request: message can't be edited";
  let released = 0;
  const fallback = await publishUsageCard({
    api, card, chatId: 7, targetId: 50, threadId: 3,
    onEditFail: async () => { released += 1; },
    save: async (c, mid) => saved.push({ c, mid }),
  });
  assert.equal(released, 1);
  assert.equal(fallback.edited, false);
  assert.equal(fallback.message_id, 99);
  assert.equal(api.sends.length, 1);
  assert.equal(api.sends[0].message_thread_id, 3);
  assert.equal(saved.at(-1).mid, 99);
});

test('restoreUsageCard writes the fail note and treats not-modified as success', async () => {
  const saved = {
    text: '📊 <b>Лимиты Grok</b>',
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: [[{ text: '🔄 Обновить', callback_data: 'usage_refresh' }]] },
  };
  const api = {
    async editMessageText(params) {
      this.last = params;
      if (this.fail) {
        const err = new Error(this.fail);
        err.description = this.fail;
        throw err;
      }
    },
  };
  const ok = await restoreUsageCard({ api, saved, chatId: 7, messageId: 50, failed: true });
  assert.equal(ok.restored, true);
  assert.match(api.last.text, /не удалось обновить/);
  assert.equal(api.last.parse_mode, 'HTML');
  assert.equal(api.last.message_id, 50);

  api.fail = 'Bad Request: message is not modified';
  const unchanged = await restoreUsageCard({ api, saved, chatId: 7, messageId: 50, failed: true });
  assert.equal(unchanged.restored, true);
  assert.equal(unchanged.unchanged, true);

  assert.equal(usageCardStoreKey(7, 50), '_usage_card:7:50');
  assert.equal(parseStoredUsageCard('{"text":"hi"}').text, 'hi');
  assert.equal(parseStoredUsageCard('nope'), null);
});
