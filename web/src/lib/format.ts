export function fmtDate(ts: number | null | undefined, opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" }) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleDateString(undefined, opts);
}

export function fmtDateTime(ts: number | null | undefined) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function relTime(ts: number | null | undefined) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 0) {
    const d = Math.ceil(-s / 86400);
    return d <= 1 ? "tomorrow" : `in ${d} days`;
  }
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

export function ageText(d: number | null | undefined) {
  if (d == null) return "age unknown";
  if (d === 0) return "posted today";
  return d === 1 ? "1 day old" : `${d} days old`;
}

export function yoeText(y: { min: number | null; max: number | null; preferred: boolean } | null | undefined) {
  if (!y || y.min == null) return null;
  const t = y.max != null && y.max !== y.min ? `${y.min}–${y.max} yrs` : `${y.min}+ yrs`;
  return y.preferred ? `${t} (preferred)` : t;
}

export function plural(n: number, word: string, many?: string) {
  return `${n} ${n === 1 ? word : many || word + "s"}`;
}

export const STATUS_LABEL: Record<string, string> = {
  new: "New", found: "Found", interested: "Found", tailored: "Tailored", ready: "Ready",
  ready_to_submit: "Ready", applied: "Submitted", submitted: "Submitted", oa: "OA",
  interview: "Interview", offer: "Offer", rejected: "Rejected", not_interested: "Hidden",
};

export const TIER_LABEL: Record<string, string> = {
  brand: "Brand", funded: "Funded", watchlist: "Watchlist", unknown: "Unverified", excluded: "Excluded",
};

export const RULE_LABEL: Record<string, string> = {
  geo: "Location", role: "Role type", seniority: "Seniority", yoe: "Experience required",
  company: "Company", salary: "Salary", tier: "Company tier", stale: "Too old", duplicate: "Duplicate",
};

export function workplace(w: string | null | undefined) {
  if (!w || w === "unknown") return null;
  return w === "onsite" ? "On-site" : w.charAt(0).toUpperCase() + w.slice(1);
}

export function tzText(j: { tz_requirement?: string | null; timezone_restrictions?: string[]; region_restrictions?: string[]; country_bucket?: string | null; workplace_type?: string | null }) {
  if (j.tz_requirement) return j.tz_requirement;
  if (j.timezone_restrictions && j.timezone_restrictions.length) return j.timezone_restrictions.join(", ");
  if (j.region_restrictions && j.region_restrictions.length) return j.region_restrictions.join(", ");
  if (j.country_bucket === "IN") return "IST";
  if (j.workplace_type === "remote") return "Any time zone";
  return null;
}

export function sourceName(j: { attribution_name?: string | null; source?: string | null }) {
  const n = j.attribution_name || j.source || "";
  return n ? n.charAt(0).toUpperCase() + n.slice(1) : "";
}
