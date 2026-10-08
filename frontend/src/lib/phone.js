// Phone input helpers for the candidate pages.
//
// The country comes from the backend (PHONE_DEFAULT_COUNTRY, sent as
// `phone_country` on the public pipeline and applicant responses): "US" or
// "GB". The backend does the real normalisation (backend/phone_util.py); these
// helpers only shape what the candidate sees while typing. A number that starts
// with + is always accepted as an international number.

export const phoneCountry = (c) => (String(c || "").toUpperCase() === "GB" ? "GB" : "US");

export const phonePlaceholder = (country) =>
    phoneCountry(country) === "GB" ? "07700 900123" : "(617) 555-0134";

// Progressive US formatting: digits in, "(617) 555-0134" out, capped at 10
// digits (a leading 1 is treated as the country code). Other countries and
// anything starting with + are left as typed, apart from stray characters.
export function formatPhoneInput(value, country) {
    const v = value || "";
    if (v.trim().startsWith("+") || phoneCountry(country) !== "US") {
        return v.replace(/[^\d+()\s-]/g, "").slice(0, 20);
    }
    let d = v.replace(/\D/g, "");
    if (d.length === 11 && d.startsWith("1")) d = d.slice(1);
    d = d.slice(0, 10);
    if (d.length > 6) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
    if (d.length > 3) return `(${d.slice(0, 3)}) ${d.slice(3)}`;
    return d;
}

// Stored numbers are E.164 — show them the way a local candidate reads their
// own number. Anything else shows as stored.
export function displayPhone(phone, country) {
    const p = phone || "";
    const us = /^\+1(\d{10})$/.exec(p);
    if (us && phoneCountry(country) === "US") return formatPhoneInput(us[1], "US");
    const uk = /^\+44(\d{10})$/.exec(p);
    if (uk && phoneCountry(country) === "GB") return `0${uk[1].slice(0, 4)} ${uk[1].slice(4)}`;
    return p;
}

// Good enough to send: the backend makes the final call.
export function looksLikeFullNumber(value, country) {
    const v = (value || "").trim();
    const d = v.replace(/\D/g, "");
    if (v.startsWith("+")) return d.length >= 10 && d.length <= 15;
    if (phoneCountry(country) === "GB") return (d.length === 11 && d.startsWith("0")) || (d.length === 12 && d.startsWith("44"));
    return d.length === 10 || (d.length === 11 && d.startsWith("1"));
}

export const phoneHint = (country) =>
    phoneCountry(country) === "GB"
        ? "Please enter a full UK number (11 digits starting with 0), or + and the country code."
        : "Please enter a full 10-digit US number, or + and the country code.";
