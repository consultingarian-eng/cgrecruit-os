// 12-hour helpers for raw HH:MM (24h) values coming from <input type="time">.
//
// Native time inputs render 12h or 24h depending on each user's OS locale, so
// a recruiter can type "2:00" and silently save 2:00 AM. Every screen that
// accepts a raw time spells the interpretation out in AM/PM next to the field
// and flags starts before business hours — that's how candidates once ended up
// booked (and reminded) for a 2:00 AM interview.

export const formatAmPm = (hhmm) => {
    const [h, m] = String(hhmm || "").split(":").map(Number);
    if (!Number.isFinite(h) || !Number.isFinite(m)) return "";
    const suffix = h >= 12 ? "PM" : "AM";
    const h12 = h % 12 || 12;
    return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
};

// Anything before 8:30 AM is almost certainly a 12h/24h mix-up, not a real
// session — the earliest real session either office runs is 9:00 AM.
export const isBeforeBusinessHours = (hhmm) => {
    const [h, m] = String(hhmm || "").split(":").map(Number);
    if (!Number.isFinite(h) || !Number.isFinite(m)) return false;
    return h < 8 || (h === 8 && m < 30);
};

// The PM reading of an accidental AM time ("02:00" -> "2:00 PM"), for the
// "did you mean…?" hint.
export const pmSuggestion = (hhmm) => {
    const [h, m] = String(hhmm || "").split(":").map(Number);
    if (!Number.isFinite(h) || !Number.isFinite(m) || h >= 12) return "";
    return formatAmPm(`${h + 12}:${String(m).padStart(2, "0")}`);
};
