// The one fetch wrapper. Every state-changing request (POST/PUT/PATCH/DELETE,
// uploads included) carries X-JobCopilot: 1 — the server's CSRF guard refuses
// it otherwise (server/app.py local_api_guard). Errors become one plain
// sentence (ApiError.message); callers that need the payload read .payload.

export class ApiError extends Error {
  status: number;
  payload: unknown;
  path: string;
  constructor(message: string, status: number, payload: unknown, path: string) {
    super(message);
    this.status = status;
    this.payload = payload;
    this.path = path;
  }
  get demo(): boolean {
    return this.status === 403 && !!(this.payload as { demo?: boolean } | null)?.demo;
  }
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

// Same wording as the server's demo refusals, shortened for a toast.
export const DEMO_TAILOR_TIP = "Not available in the public demo — run Job-Copilot locally to tailor";
export const DEMO_ACTION_TIP = "Not available in the public demo — run Job-Copilot locally to use this";
export const DEMO_LINK_TIP = "Sample job — the link is not real in the demo";

export function friendlyError(status: number, payload: unknown, path = ""): string {
  if (status === 0) return "Couldn't reach the app — is it running?";
  const p = (payload ?? null) as { demo?: boolean; detail?: unknown } | null;
  if (status === 403 && p?.demo) {
    return /tailor|cover-letter|email|answers\/preview|resume-options|apply-pack/.test(path)
      ? DEMO_TAILOR_TIP
      : DEMO_ACTION_TIP;
  }
  const d = p?.detail;
  if (typeof d === "string" && d.trim()) return d.replace(/^demo: disabled — /, "");
  if (d && typeof d === "object" && !Array.isArray(d)) {
    const o = d as { error?: unknown; detail?: unknown };
    if (typeof o.error === "string") return o.error;
    if (typeof o.detail === "string") return o.detail;
  }
  if (Array.isArray(d) && d.length) {
    return "Invalid request: " + d.map((x) => (x && (x as { msg?: string }).msg) || "bad value").join("; ");
  }
  if (status === 404) return "Not found — it may have been removed. Try refreshing.";
  if (status === 409) return "That conflicts with the current state. Refresh and try again.";
  if (status >= 500) return `Something went wrong in the app (server error ${status}). Check the terminal running it.`;
  return `Request failed (${status}).`;
}

type Opts = Omit<RequestInit, "body"> & { body?: unknown; form?: FormData };

export async function api<T = unknown>(path: string, opts: Opts = {}): Promise<T> {
  const method = (opts.method || (opts.body !== undefined || opts.form ? "POST" : "GET")).toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (UNSAFE.has(method)) headers["X-JobCopilot"] = "1";
  let body: BodyInit | undefined;
  if (opts.form) {
    body = opts.form; // the browser sets the multipart boundary
  } else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  } else if (UNSAFE.has(method)) {
    headers["Content-Type"] = "application/json";
    body = "{}";
  }
  let res: Response;
  try {
    res = await fetch(path, {
      ...opts,
      method,
      body,
      credentials: "same-origin",
      headers: { ...headers, ...((opts.headers as Record<string, string>) || {}) },
    });
  } catch {
    throw new ApiError(friendlyError(0, null, path), 0, null, path);
  }
  const text = await res.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!res.ok) throw new ApiError(friendlyError(res.status, payload, path), res.status, payload, path);
  return payload as T;
}

export const enc = encodeURIComponent;

export function post<T = unknown>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, { method: "POST", body });
}

// Only http(s) URLs may become links; anything else (javascript:, data:, …) -> null.
export function safeHttpUrl(u: unknown): string | null {
  if (typeof u !== "string" || !u.trim()) return null;
  try {
    const url = new URL(u.trim(), window.location.href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}
