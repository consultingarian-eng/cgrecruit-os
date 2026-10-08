/**
 * Timezone-aware display helpers for CGRecruit.
 *
 * Pass `timeZone` inside opts to use the account's configured timezone
 * (from settings → Region & Language). Falls back to REACT_APP_TIMEZONE
 * (set it to the same value as the backend's APP_TIMEZONE), else America/New_York.
 *
 * Usage:
 *   const { timezone } = usePipeline();
 *   fmtET(iso, { timeZone: timezone, month: 'short', day: 'numeric', ... })
 *   etNaiveToUtcMs(next_call_at, timezone)
 */
export const DEFAULT_TZ = process.env.REACT_APP_TIMEZONE || 'America/New_York';

/**
 * Convert an ET-naive ISO string (e.g. "2026-06-06T09:00:00" as stored in
 * next_call_at) to a UTC millisecond timestamp suitable for countdown math.
 *
 * Uses iterative DST correction: start with a UTC-5 seed, nudge until the
 * wall clock in `tz` matches the requested time. Offset-aware values ("…Z",
 * "…-04:00") are taken at face value instead.
 */
export function etNaiveToUtcMs(etStr, tz = DEFAULT_TZ) {
  if (!etStr) return 0;
  // An offset-aware value already pins the instant. Walking it as a wall clock
  // would re-read it as local time in `tz` and shift the countdown by the offset.
  if (/(?:Z|[+-]\d{2}:?\d{2})$/.test(etStr)) {
    const ms = Date.parse(etStr);
    return Number.isNaN(ms) ? 0 : ms;
  }
  const m = etStr.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  if (!m) return 0;
  const [, y, mo, d, h, mi] = m.map(Number);
  let utcMs = Date.UTC(y, mo - 1, d, h + 5, mi, 0); // UTC-5 seed
  for (let i = 0; i < 3; i++) {
    const parts = {};
    for (const p of new Intl.DateTimeFormat('en-US', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).formatToParts(new Date(utcMs))) parts[p.type] = p.value;
    const diff = (h * 60 + mi) - (parseInt(parts.hour) * 60 + parseInt(parts.minute));
    if (diff === 0) break;
    utcMs += diff * 60_000;
  }
  return utcMs;
}

/**
 * Format any date value (ISO string, Date, or ms) in the account's timezone.
 * Pass `timeZone` in opts to override the default.
 */
export function fmtET(val, opts = {}) {
  if (!val && val !== 0) return '';
  try {
    const d = val instanceof Date ? val : new Date(val);
    const { timeZone = DEFAULT_TZ, ...rest } = opts;
    return d.toLocaleString('en-US', { timeZone, ...rest });
  } catch { return String(val); }
}
