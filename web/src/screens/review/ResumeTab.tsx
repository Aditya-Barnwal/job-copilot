import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { diffWords } from "diff";
import { Check, CircleAlert, Copy, FileDown, Pencil, RotateCcw, RotateCw, ShieldCheck, Sparkles, Wand2, X } from "lucide-react";
import { ApiError, DEMO_TAILOR_TIP, api, enc, post } from "../../lib/api";
import { useDemo, useToast } from "../../lib/app";
import { fmtDateTime, plural } from "../../lib/format";
import type { ArtifactReview, ArtifactSummary, DiffRow, Grounding, JobDetail, Line, TailorState } from "../../lib/types";
import { Badge, Button, EmptyState, ErrorState, LinkButton, Meter, Skeleton, cx } from "../../components/ui";
import { CLOSED_TIP, useArtifacts, type FitInfo } from "./ReviewDrawer";

const VERSION_LABEL: Record<string, string> = { ok: "ready", needs_review: "needs review", approved: "approved", rejected: "rejected" };
const SAME_AS_BASE = "Same as your base résumé";
const SECTION_LABEL: Record<string, string> = {
  education: "Education", skills: "Skills", experience: "Experience", projects: "Projects",
  research: "Research", hackathons: "Hackathons",
};
const FLAG_LABEL: Record<string, string> = {
  unsupported_skills: "Skills your facts don't show",
  new_numbers: "Numbers not in your facts",
  inflated_claims: "Stronger ownership than your facts",
  new_proper_nouns: "Names not in your facts",
  unsupported_claims: "Claims your facts don't support",
  low_similarity: "Rewritten too far from the fact",
  unknown_facts: "Cites a fact that isn't confirmed (can't be approved)",
  over_one_page: "Longer than one page",
};

// ---------------------------------------------------------------- job description

function highlight(text: string, matched: string[], missing: string[]): ReactNode[] {
  const terms = [...matched.map((t) => [t, "have"] as const), ...missing.map((t) => [t, "miss"] as const)]
    .filter(([t]) => t && t.length > 1)
    .sort((a, b) => b[0].length - a[0].length);
  if (!terms.length) return [text];
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const rx = new RegExp(`(?<![A-Za-z0-9])(${terms.map(([t]) => esc(t)).join("|")})(?![A-Za-z0-9])`, "gi");
  const kind = new Map(terms.map(([t, k]) => [t.toLowerCase(), k]));
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(rx)) {
    const i = m.index ?? 0;
    if (i > last) out.push(text.slice(last, i));
    const k = kind.get(m[0].toLowerCase());
    out.push(
      <mark key={i} className={cx("rounded-[3px] px-0.5", k === "have" ? "bg-accent-soft text-accent-ink" : "bg-warn-soft text-warn")}
        title={k === "have" ? "In your confirmed facts" : "Asked for; not in your confirmed facts"}>
        {m[0]}
      </mark>,
    );
    last = i + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function JobDescription({ job, fit }: { job: JobDetail; fit?: FitInfo }) {
  const text = (job.description_text || "").trim();
  const parts = useMemo(() => highlight(text, fit?.matched_skills || [], fit?.missing_skills || []), [text, fit]);
  return (
    <section aria-label="Job description" className="card flex min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
        <h3 className="eyebrow">Job description</h3>
        <span className="flex items-center gap-3 text-[12px] text-muted">
          <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full bg-accent" aria-hidden />you have</span>
          <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full bg-warn" aria-hidden />missing</span>
        </span>
      </div>
      <div className="scroll-thin max-h-[68vh] overflow-y-auto px-5 py-4">
        {fit?.why && <p className="mb-4 font-display text-[17px] italic leading-snug text-ink-2">“{fit.why}”</p>}
        {fit?.components && Object.keys(fit.components).length > 0 && (
          <div className="mb-5 space-y-1.5 rounded-[10px] bg-paper-2/60 p-3.5">
            {Object.entries(fit.components).map(([k, v]) => <Meter key={k} label={k} value={Number(v)} />)}
          </div>
        )}
        {text ? <div className="prose-jd">{parts}</div> : <p className="text-sm text-muted">This posting came without a description. Open the posting to read it.</p>}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- diff rendering

function WordDiff({ before, after }: { before: string; after: string }) {
  const parts = diffWords(before, after);
  return (
    <>
      {parts.map((p, i) =>
        p.added ? <ins key={i} className="rounded-[3px] bg-accent-soft px-0.5 text-accent-ink no-underline">{p.value}</ins>
          : p.removed ? <del key={i} className="text-faint decoration-bad/50">{p.value}</del>
            : <span key={i}>{p.value}</span>,
      )}
    </>
  );
}

const OP_BADGE: Record<string, { label: string; tone: "accent" | "neutral" | "warn" | "bad" | "good" }> = {
  rephrased: { label: "Reworded", tone: "accent" },
  moved: { label: "Moved up", tone: "neutral" },
  added: { label: "Added", tone: "good" },
  removed: { label: "Left out", tone: "warn" },
};

interface RowProps {
  line?: Line;
  diff?: DiffRow;
  verdict?: { supported: boolean; offending: string[] };
  editable: boolean;
  draft?: string;
  onDraft: (text: string | undefined) => void;
  showChanges: boolean;
  userEdited: boolean;
  locked: boolean;
}

function ResumeRow({ line, diff, verdict, editable, draft, onDraft, showChanges, userEdited, locked }: RowProps) {
  const [editing, setEditing] = useState(false);
  const text = line?.text ?? diff?.after ?? diff?.before ?? "";
  const kind = line?.kind || (diff?.fact_ids[0]?.split(".").length === 3 ? "bullet" : "header");
  const bad = verdict && !verdict.supported;
  const removed = diff?.op === "removed";
  const changed = draft !== undefined;
  return (
    <li className={cx("group relative rounded-[8px] transition-colors",
      kind === "bullet" ? "ml-4 pl-4 before:absolute before:left-0 before:top-[0.7em] before:size-1 before:rounded-full before:bg-faint" : "mt-3 first:mt-0",
      bad && "bg-bad-soft/60 -mx-2 px-2 py-1", changed && "bg-accent-soft/50 -mx-2 px-2 py-1", removed && "opacity-60")}>
      {editing ? (
        <div className="py-1">
          <label className="sr-only" htmlFor={`edit-${line?.fact_ids[0]}`}>Edit this line</label>
          <textarea id={`edit-${line?.fact_ids[0]}`} autoFocus rows={3} defaultValue={draft ?? text}
            className="w-full resize-y rounded-[8px] border border-accent-line bg-surface p-2.5 text-[14px] leading-relaxed text-ink outline-none focus:ring-2 focus:ring-accent/30"
            onKeyDown={(e) => {
              if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setEditing(false); }
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                onDraft((e.target as HTMLTextAreaElement).value);
                setEditing(false);
              }
            }}
            onBlur={(e) => { if (e.target.value !== (draft ?? text)) onDraft(e.target.value); setEditing(false); }} />
          <p className="mt-1 text-[12px] text-faint">⌘/Ctrl + Enter to keep · Esc to cancel. Your wording is checked against the fact it cites.</p>
        </div>
      ) : (
        <div className="flex items-start gap-2">
          <p className={cx("flex-1 leading-relaxed", kind === "bullet" ? "text-[14px] text-ink-2" : kind === "skill" ? "text-[14px] text-ink-2" : "text-[14px] font-semibold text-ink")}>
            {line?.heading && <span className="font-semibold text-ink">{line.heading}: </span>}
            {changed ? draft : showChanges && diff?.op === "rephrased" && diff.before && diff.after
              ? <WordDiff before={diff.before} after={diff.after} />
              : removed ? <s className="decoration-faint">{text}</s> : text}
          </p>
          <span className="flex shrink-0 items-center gap-1 pt-0.5">
            {userEdited && !changed && <Badge tone="neutral">Your edit</Badge>}
            {changed && <Badge tone="accent">Unsaved</Badge>}
            {showChanges && diff && OP_BADGE[diff.op] && !changed && <Badge tone={OP_BADGE[diff.op].tone}>{OP_BADGE[diff.op].label}</Badge>}
            {editable && !locked && (
              <button type="button" aria-label="Edit this line" title="Edit this line"
                onClick={() => setEditing(true)}
                className="grid size-7 place-items-center rounded-md text-faint opacity-0 transition-opacity hover:bg-paper-2 hover:text-ink focus-visible:opacity-100 group-hover:opacity-100">
                <Pencil className="size-3.5" />
              </button>
            )}
            {changed && (
              <button type="button" aria-label="Discard this edit" title="Discard this edit" onClick={() => onDraft(undefined)}
                className="grid size-7 place-items-center rounded-md text-faint hover:bg-paper-2 hover:text-ink">
                <RotateCcw className="size-3.5" />
              </button>
            )}
          </span>
        </div>
      )}
      {bad && !editing && (
        <p className="mt-1 flex items-center gap-1.5 text-[12px] text-bad">
          <CircleAlert className="size-3.5" aria-hidden /> Not in the cited fact: {verdict!.offending.join(", ")}
        </p>
      )}
      {showChanges && diff?.note && <p className="mt-0.5 text-[12px] text-faint">{diff.note}</p>}
      {line?.kept_original && <p className="mt-0.5 text-[12px] text-faint">Kept your original wording: {line.kept_original}</p>}
    </li>
  );
}

// ---------------------------------------------------------------- tailoring in the background

interface Preview {
  status: string;
  base?: string;
  available_bases?: string[];
  keywords?: { supported: string[]; missing: string[]; missing_must: string[] };
  reasons?: string[];
}

function useTailorPreview(jobKey: string, enabled = true) {
  const demo = useDemo();
  return useQuery({
    queryKey: ["kw", jobKey],
    enabled: !demo && enabled,
    retry: false,
    queryFn: () => api<Preview>(`/api/jobs/${enc(jobKey)}/tailor/v2/keywords`),
  });
}

/** The job's background tailoring (reject -> new version, "Tailor again"); polled while it runs. */
function useTailorState(jobKey: string) {
  const demo = useDemo();
  return useQuery({
    queryKey: ["tailor-bg", jobKey],
    enabled: !demo,
    retry: false,
    queryFn: () => api<TailorState>(`/api/jobs/${enc(jobKey)}/tailor/v2/status`),
    refetchInterval: (q) => (q.state.data?.state === "running" ? 1500 : false),
  });
}

function TailoringBanner({ state }: { state: TailorState }) {
  return (
    <div role="status" aria-live="polite" className="mb-5 flex items-start gap-3 rounded-[12px] border border-accent-line bg-accent-soft/60 px-4 py-3 text-sm text-ink">
      <RotateCw className="mt-0.5 size-4 shrink-0 animate-spin text-accent" aria-hidden />
      <span>
        <span className="font-medium">Tailoring a new version…</span>
        <span className="text-ink-2"> It opens here when it's done (up to a minute).</span>
        {state.guidance && <span className="block text-[13px] text-muted">Your note for it: “{state.guidance}”. It steers which confirmed facts are picked; it is never added as a fact.</span>}
      </span>
    </div>
  );
}

type FormMode = "reject" | "again";

function GuidanceForm({ mode, jobKey, current, closed, onCancel, onStarted }:
  { mode: FormMode; jobKey: string; current: ArtifactSummary; closed: string | null; onCancel: () => void; onStarted: (t: TailorState | null) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const preview = useTailorPreview(jobKey);
  const [note, setNote] = useState("");
  const [base, setBase] = useState("");
  const bases = preview.data?.available_bases || [];
  const retailor = !closed;
  const submit = useMutation({
    mutationFn: () => mode === "reject"
      ? post<{ tailoring?: TailorState | null }>(`/api/jobs/${enc(jobKey)}/artifacts/${current.id}/reject`, {
        reason: note.trim() || null, base: base || null, retailor,
      })
      : post<{ tailoring?: TailorState | null }>(`/api/jobs/${enc(jobKey)}/tailor/v2/start`, {
        guidance: note.trim() || null, base: base || null, from_artifact_id: current.id,
      }),
    onSuccess: (r) => {
      if (mode === "reject") {
        toast.notify(retailor ? `Version ${current.version} rejected. Tailoring a new version…` : `Version ${current.version} rejected. It won't be used.`);
        qc.invalidateQueries({ queryKey: ["artifacts", jobKey] });
        qc.invalidateQueries({ queryKey: ["review", current.id] });
        qc.invalidateQueries({ queryKey: ["job", jobKey] });
      }
      onStarted(r.tailoring || null);
    },
    onError: toast.error,
  });
  const id = `guidance-${mode}`;
  return (
    <form className="space-y-3 rounded-[14px] border border-line bg-surface-2 p-4" aria-label={mode === "reject" ? "Reject this version" : "Tailor again"}
      onSubmit={(e) => { e.preventDefault(); submit.mutate(); }}>
      <div>
        <label htmlFor={id} className="text-sm font-medium text-ink">
          {mode === "reject" ? "What's wrong with it?" : "What should the new version do differently?"} <span className="font-normal text-muted">(optional)</span>
        </label>
        <input id={id} autoFocus value={note} maxLength={300} onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. doesn't emphasise CV work, too generic"
          className="mt-1.5 h-10 w-full rounded-[10px] border border-line-2 bg-surface px-3 text-sm text-ink outline-none placeholder:text-faint focus:border-accent-line focus:ring-2 focus:ring-accent/20" />
        <p className="mt-1 text-[12px] text-muted">
          {retailor
            ? "A new version is tailored with your note as guidance: it can change which confirmed facts are picked, emphasised or reordered, and the base. It is never added as a fact, and every line is still checked against your facts."
            : "This posting is closed, so no new version is tailored."}
        </p>
      </div>
      {retailor && bases.length > 1 && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <label htmlFor={`${id}-base`} className="text-ink-2">Base résumé</label>
          <select id={`${id}-base`} value={base} onChange={(e) => setBase(e.target.value)}
            className="h-8 rounded-[8px] border border-line-2 bg-surface px-2 text-[13px] text-ink">
            <option value="">Keep {current.base?.replace(/_/g, " ") || "the same"} (or the one your note names)</option>
            {bases.filter((b) => b !== current.base).map((b) => <option key={b} value={b}>{b.replace(/_/g, " ")}</option>)}
          </select>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant={mode === "reject" ? "danger" : "primary"} busy={submit.isPending}
          icon={mode === "reject" ? <X className="size-4" /> : <Sparkles className="size-4" />}>
          {mode === "reject" ? (retailor ? "Reject and tailor again" : "Reject") : "Tailor again"}
        </Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------- approve panel

function ApprovePanel({ review, jobKey, onApproved, onReject, onAgain, closed, busy }:
  { review: ArtifactReview; jobKey: string; onApproved: () => void; onReject: () => void; onAgain: () => void; closed: string | null; busy: boolean }) {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const g: Grounding | null = review.grounding;
  const flags = Object.entries(g?.flags || {}).filter(([, v]) => v && v.length);
  const [ok, setOk] = useState<Record<string, Set<string>>>({});
  useEffect(() => setOk({}), [review.artifact_id]);
  const confirmable = flags.filter(([k]) => k !== "unknown_facts");
  const blocked = flags.some(([k]) => k === "unknown_facts");
  const allChecked = confirmable.every(([k, items]) => items.every((t) => ok[k]?.has(t)));
  const same = !!review.unchanged;
  const approve = useMutation({
    mutationFn: () => post(`/api/jobs/${enc(jobKey)}/artifacts/${review.artifact_id}/approve`, {
      confirmed_flags: Object.fromEntries(Object.entries(ok).map(([k, s]) => [k, [...s]])),
    }),
    onSuccess: () => {
      toast.notify(same ? "Your base résumé is approved as is — the job is ready to submit." : `Version ${review.version} approved — the job is ready to submit.`, { kind: "success" });
      qc.invalidateQueries(); onApproved();
    },
    onError: (e) => toast.error(e instanceof ApiError && e.status === 409 && typeof (e.payload as { detail?: string })?.detail === "string" ? new Error((e.payload as { detail: string }).detail) : e),
  });
  if (review.status === "approved") {
    return (
      <div className="flex items-center gap-3 rounded-[12px] border border-good/25 bg-good-soft px-4 py-3 text-sm text-good">
        <ShieldCheck className="size-5 shrink-0" aria-hidden />
        <span className="flex-1">Approved {fmtDateTime(review.approved_at)}. This is the résumé autofill attaches.</span>
      </div>
    );
  }
  if (review.status === "rejected") {
    return (
      <div className="flex flex-wrap items-center gap-3 rounded-[12px] bg-paper-2 px-4 py-3 text-sm text-muted">
        <span className="flex-1">
          You rejected this version{review.rejection?.reason ? <> (“{review.rejection.reason}”)</> : null}. Pick another, edit it into a new version, or tailor again.
        </span>
        <Button size="sm" variant="secondary" icon={<Sparkles className="size-3.5" />} disabled={demo || !!closed || busy}
          reason={demo ? DEMO_TAILOR_TIP : closed ? CLOSED_TIP : busy ? "A new version is being tailored" : undefined} onClick={onAgain}>
          Tailor again
        </Button>
      </div>
    );
  }
  return (
    <div className="space-y-4 rounded-[14px] border border-line bg-surface-2 p-4">
      {g?.clean ? (
        <p className="flex items-center gap-2 text-sm text-good"><ShieldCheck className="size-4" aria-hidden />Every line is backed by a confirmed fact.</p>
      ) : (
        <div>
          <p className="flex items-center gap-2 text-sm font-medium text-ink"><CircleAlert className="size-4 text-bad" aria-hidden />
            The grounding check flagged {plural(flags.reduce((n, [, v]) => n + v.length, 0), "item")}.</p>
          <p className="mt-1 text-[13px] text-muted">Tick each one only if it is true of you, or edit the line. Nothing is approved until every flag is settled.</p>
          <fieldset className="mt-3 space-y-3">
            <legend className="sr-only">Flags to confirm</legend>
            {flags.map(([k, items]) => (
              <div key={k}>
                <div className="text-[12px] font-semibold uppercase tracking-wider text-faint">{FLAG_LABEL[k] || k}</div>
                <div className="mt-1.5 flex flex-wrap gap-2">
                  {items.map((t) => (
                    <label key={t} className={cx("inline-flex cursor-pointer items-center gap-2 rounded-full border px-2.5 py-1 text-[13px]",
                      k === "unknown_facts" ? "cursor-not-allowed border-bad/30 text-bad" : ok[k]?.has(t) ? "border-accent-line bg-accent-soft text-accent-ink" : "border-line-2 text-ink-2")}>
                      <input type="checkbox" className="accent-[var(--accent)]" disabled={k === "unknown_facts"}
                        checked={!!ok[k]?.has(t)}
                        onChange={(e) => setOk((o) => {
                          const s = new Set(o[k] || []);
                          if (e.target.checked) s.add(t); else s.delete(t);
                          return { ...o, [k]: s };
                        })} />
                      {t}
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </fieldset>
        </div>
      )}
      {review.stale && <p className="text-[13px] text-warn">Your confirmed facts changed since this version was made, so it can't be approved. Tailor again.</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" icon={same ? <Copy className="size-4" /> : <Check className="size-4" />} busy={approve.isPending}
          disabled={demo || !!closed || review.stale || blocked || (!g?.clean && !allChecked)}
          reason={demo ? DEMO_TAILOR_TIP : closed ? CLOSED_TIP : review.stale ? "Facts changed since tailoring" : blocked ? "A line cites an unconfirmed fact" : !g?.clean && !allChecked ? "Settle every flag first" : undefined}
          onClick={() => approve.mutate()}>
          {same ? "Use the base résumé as is" : `Approve version ${review.version}`}
        </Button>
        <Button variant="ghost" icon={<X className="size-4" />} disabled={demo || busy}
          reason={demo ? DEMO_TAILOR_TIP : busy ? "A new version is being tailored" : undefined} onClick={onReject}>
          Reject
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- tailor (no version yet)

function TailorStart({ job, closed }: { job: JobDetail; closed: string | null }) {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const preview = useTailorPreview(job.key);
  const [attest, setAttest] = useState<Set<string>>(new Set());
  const [strike, setStrike] = useState<Set<string>>(new Set());
  const tailor = useMutation({
    mutationFn: () => post<{ status: string; version: number; reasons: string[] }>(`/api/jobs/${enc(job.key)}/tailor/v2`, {
      approved_keywords: [...attest], struck_keywords: [...strike],
    }),
    onSuccess: (r) => { toast.notify(`Tailored version ${r.version}${r.status === "needs_review" ? " — it needs your review" : ""}.`); qc.invalidateQueries({ queryKey: ["artifacts", job.key] }); },
    onError: toast.error,
  });
  const kw = preview.data?.keywords;
  return (
    <div className="card p-6">
      <EmptyState className="!py-6" icon={<Wand2 />} title="No tailored résumé yet">
        Tailoring picks and reorders your confirmed facts for this role. It never adds a skill, number or title your facts don't have.
      </EmptyState>
      {demo ? (
        <p className="text-center text-sm text-muted">{DEMO_TAILOR_TIP}.</p>
      ) : preview.isLoading ? (
        <Skeleton className="mx-auto h-16 w-3/4" />
      ) : preview.error ? (
        <ErrorState error={preview.error} compact />
      ) : kw ? (
        <div className="space-y-4">
          {kw.supported.length > 0 && (
            <div>
              <div className="eyebrow mb-2">Backed by your facts</div>
              <div className="flex flex-wrap gap-1.5">{kw.supported.map((k) => <Badge key={k} tone="accent">{k}</Badge>)}</div>
            </div>
          )}
          {kw.missing.length > 0 && (
            <div>
              <div className="eyebrow mb-1">Missing — decide before tailoring</div>
              <p className="mb-2 text-[13px] text-muted">Tick “I have this” only if it's true; strike a keyword to keep it out entirely.</p>
              <ul className="divide-y divide-line rounded-[10px] border border-line">
                {kw.missing.map((k) => (
                  <li key={k} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                    <span className={cx(strike.has(k) && "text-faint line-through")}>{k}{kw.missing_must.includes(k) && <span className="ml-2 text-[12px] text-warn">must-have</span>}</span>
                    <span className="flex items-center gap-4 text-[13px]">
                      <label className="inline-flex items-center gap-1.5"><input type="checkbox" className="accent-[var(--accent)]" checked={attest.has(k)}
                        onChange={(e) => setAttest((s) => { const n = new Set(s); if (e.target.checked) { n.add(k); setStrike((x) => { const y = new Set(x); y.delete(k); return y; }); } else n.delete(k); return n; })} />I have this</label>
                      <label className="inline-flex items-center gap-1.5"><input type="checkbox" className="accent-[var(--accent)]" checked={strike.has(k)}
                        onChange={(e) => setStrike((s) => { const n = new Set(s); if (e.target.checked) { n.add(k); setAttest((x) => { const y = new Set(x); y.delete(k); return y; }); } else n.delete(k); return n; })} />Strike</label>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ) : preview.data?.reasons ? (
        <p className="text-center text-sm text-warn">{preview.data.reasons.join(" ")}</p>
      ) : null}
      <div className="mt-6 flex justify-center">
        <Button variant="primary" size="lg" icon={<Sparkles className="size-4" />} busy={tailor.isPending}
          disabled={demo || !!closed || preview.data?.status === "refused"} reason={demo ? DEMO_TAILOR_TIP : closed ? CLOSED_TIP : undefined} onClick={() => tailor.mutate()}>
          {tailor.isPending ? "Tailoring… (up to a minute)" : "Tailor résumé"}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- the tab

export function ResumeTab({ job, fit, closed = null }: { job: JobDetail; fit?: FitInfo; closed?: string | null }) {
  const arts = useArtifacts(job.key);
  const qc = useQueryClient();
  const toast = useToast();
  const bg = useTailorState(job.key);
  const list = arts.data?.artifacts || [];
  const usable = list.filter((a) => !["failed", "pending"].includes(a.status)).sort((a, b) => b.version - a.version);
  const preferred = usable.find((a) => a.id === job.artifact_id) || usable.find((a) => a.status !== "rejected") || usable[0];
  const [picked, setPicked] = useState<number | null>(null);
  useEffect(() => setPicked(null), [job.key]);
  const current: ArtifactSummary | undefined = usable.find((a) => a.id === picked) || preferred;

  // a background run that finishes switches the card to the new version
  const done = (t: TailorState) => {
    qc.invalidateQueries({ queryKey: ["job", job.key] });
    qc.invalidateQueries({ queryKey: ["artifacts", job.key] }).then(() => { if (t.artifact_id) setPicked(t.artifact_id); });
    toast.notify(t.unchanged
      ? `Version ${t.version} is ready. It is the same as your base résumé — the card says why.`
      : `Version ${t.version} is ready${t.status === "needs_review" ? " — it needs your review" : ""}.`, { kind: "success" });
  };
  const last = useRef<string | undefined>(undefined);
  useEffect(() => {
    const t = bg.data;
    if (last.current === "running" && t?.state === "done") done(t);
    if (last.current === "running" && t?.state === "failed") toast.error(new Error(`Tailoring the new version failed: ${t.detail || "unknown error"}`));
    last.current = t?.state;
  }, [bg.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const follow = (t: TailorState | null) => {
    if (!t) return;
    if (t.state === "running") { last.current = "running"; qc.setQueryData(["tailor-bg", job.key], t); }
    else if (t.state === "done") done(t);
    else if (t.detail) toast.error(new Error(t.detail));
  };
  const running = bg.data?.state === "running";

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <JobDescription job={job} fit={fit} />
      <div className="min-w-0">
        {running && <TailoringBanner state={bg.data!} />}
        {arts.isLoading ? (
          <Skeleton className="h-96" />
        ) : arts.error ? (
          <ErrorState error={arts.error} onRetry={() => arts.refetch()} />
        ) : !current ? (
          running ? null : <TailorStart job={job} closed={closed} />
        ) : (
          <VersionView jobKey={job.key} versions={usable} current={current} onPick={setPicked}
            closed={closed} running={running} lastRun={bg.data} onStarted={follow} />
        )}
      </div>
    </div>
  );
}

function VersionView({ jobKey, versions, current, onPick, closed, running, lastRun, onStarted }:
  { jobKey: string; versions: ArtifactSummary[]; current: ArtifactSummary; onPick: (id: number) => void;
    closed: string | null; running: boolean; lastRun?: TailorState; onStarted: (t: TailorState | null) => void }) {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const review = useQuery({ queryKey: ["review", current.id], queryFn: () => api<ArtifactReview>(`/api/artifacts/${current.id}/review`) });
  const [showChanges, setShowChanges] = useState(true);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [form, setForm] = useState<FormMode | null>(null);
  useEffect(() => { setDrafts({}); setForm(null); }, [current.id]);
  const save = useMutation({
    mutationFn: () => post<{ artifact_id: number; version: number; status: string }>(`/api/jobs/${enc(jobKey)}/artifacts/${current.id}/edit`, { rephrase: drafts }),
    onSuccess: (r) => {
      toast.notify(`Saved your edits as version ${r.version}${r.status === "needs_review" ? " — the check flagged something; see below" : ""}.`);
      setDrafts({});
      qc.invalidateQueries({ queryKey: ["artifacts", jobKey] }).then(() => onPick(r.artifact_id));
    },
    onError: toast.error,
  });
  const r = review.data;
  const editable = new Set(r?.editable || []);
  const verdictFor = (i: number) => r?.grounding?.lines?.[i];
  const removed = (r?.diff || []).filter((d) => d.op === "removed");
  const diffByKey = new Map((r?.diff || []).filter((d) => d.op !== "removed").map((d) => [d.fact_ids.join(","), d]));
  const sections: { name: string; rows: { line: Line; i: number }[] }[] = [];
  (r?.lines || []).forEach((line, i) => {
    const s = sections.find((x) => x.name === line.section) || (sections.push({ name: line.section, rows: [] }), sections[sections.length - 1]);
    s.rows.push({ line, i });
  });
  const changedCount = (r?.diff || []).filter((d) => d.op !== "same").length;
  const nDrafts = Object.keys(drafts).length;
  const againOff = demo || !!closed || running;
  const againWhy = demo ? DEMO_TAILOR_TIP : closed ? CLOSED_TIP : running ? "A new version is being tailored" : undefined;
  const started = (t: TailorState | null) => {
    if (form === "reject") onPick(current.id);        // stay on the rejected version until the new one is ready
    setForm(null);
    onStarted(t);
  };

  return (
    <section aria-label="Tailored résumé" className="card">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3.5">
        <h3 className="eyebrow">Tailored résumé</h3>
        <label className="sr-only" htmlFor="version-pick">Version</label>
        <select id="version-pick" value={current.id} onChange={(e) => onPick(Number(e.target.value))}
          className="h-8 rounded-[8px] border border-line-2 bg-surface px-2 text-[13px] text-ink">
          {versions.map((v) => (
            <option key={v.id} value={v.id}>Version {v.version} · {v.base?.replace(/_/g, " ")} · {VERSION_LABEL[v.status] || v.status}{v.unchanged ? " · same as base" : ""}</option>
          ))}
        </select>
        <Badge tone={current.status === "approved" ? "good" : current.status === "needs_review" ? "warn" : current.status === "rejected" ? "neutral" : "accent"}>
          {current.status === "needs_review" ? "Needs review" : current.status === "ok" ? "Ready to approve" : current.status}
        </Badge>
        {r?.unchanged && <Badge tone="warn">{SAME_AS_BASE}</Badge>}
        <span className="flex-1" />
        <label className="inline-flex items-center gap-2 text-[13px] text-muted">
          <input type="checkbox" className="accent-[var(--accent)]" checked={showChanges} onChange={(e) => setShowChanges(e.target.checked)} />
          Show changes vs base
        </label>
        <Button size="sm" variant="secondary" icon={<Sparkles className="size-3.5" />} disabled={againOff} reason={againWhy}
          onClick={() => setForm(form === "again" ? null : "again")}>
          Tailor again
        </Button>
        {current.pdf_url && <LinkButton size="sm" href={current.pdf_url} icon={<FileDown className="size-3.5" />}>PDF</LinkButton>}
      </div>
      <div className="px-5 py-5">
        {form === "again" && (
          <div className="mb-5">
            <GuidanceForm mode="again" jobKey={jobKey} current={current} closed={closed} onCancel={() => setForm(null)} onStarted={started} />
          </div>
        )}
        {!running && lastRun?.state === "failed" && lastRun.detail && (
          <p className="mb-4 flex items-start gap-2 rounded-[10px] bg-warn-soft px-3 py-2 text-[13px] text-warn">
            <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />The last “Tailor again” didn't finish: {lastRun.detail}
          </p>
        )}
        {review.isLoading ? (
          <div className="space-y-3"><Skeleton className="h-4 w-1/3" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-5/6" /><Skeleton className="h-4 w-2/3" /></div>
        ) : review.error ? (
          <ErrorState error={review.error} onRetry={() => review.refetch()} />
        ) : r?.problem ? (
          <p className="text-sm text-warn">{r.problem}</p>
        ) : r ? (
          <>
            {r.unchanged && (
              <div role="note" aria-label={SAME_AS_BASE} className="mb-5 rounded-[12px] border border-warn/30 bg-warn-soft/60 px-4 py-3.5">
                <p className="flex items-center gap-2 font-medium text-ink"><Copy className="size-4 text-warn" aria-hidden />{SAME_AS_BASE}</p>
                <p className="mt-1 text-[13px] leading-relaxed text-ink-2">{r.unchanged.reason}</p>
                <p className="mt-1 text-[13px] text-muted">
                  {current.status === "rejected" ? "Tailor again with a note on what to change." : "Tailor again with a note on what to change, or approve it below with “Use the base résumé as is”: nothing is added to your base."}
                </p>
                <div className="mt-3">
                  <Button size="sm" variant="secondary" icon={<Sparkles className="size-3.5" />} disabled={againOff} reason={againWhy} onClick={() => setForm("again")}>
                    Tailor again
                  </Button>
                </div>
              </div>
            )}
            <p className="mb-5 text-[13px] text-muted">
              Base <span className="font-medium text-ink-2">{r.base}</span> · {changedCount ? plural(changedCount, "change") : "no changes"} from it
              {r.pages ? ` · ${plural(r.pages, "page")}` : ""} · made {fmtDateTime(r.created_at)}
              {r.guidance && <> · tailored again{r.from_version ? ` after version ${r.from_version}` : ""} with your note “{r.guidance}”</>}
            </p>
            <div className="space-y-6">
              {sections.map((s) => (
                <div key={s.name}>
                  <h4 className="mb-2 border-b border-line pb-1 font-display text-[15px] text-ink">{SECTION_LABEL[s.name] || s.name}</h4>
                  <ul className="space-y-1.5">
                    {s.rows.map(({ line, i }) => {
                      const fid = line.fact_ids[0];
                      return (
                        <ResumeRow key={line.fact_ids.join(",") + i} line={line} diff={diffByKey.get(line.fact_ids.join(","))}
                          verdict={verdictFor(i)} editable={line.kind === "bullet" && editable.has(fid)} locked={demo}
                          userEdited={r.user_edited.includes(fid)} showChanges={showChanges}
                          draft={drafts[fid]}
                          onDraft={(t) => setDrafts((d) => {
                            const n = { ...d };
                            if (t === undefined || t === line.text) delete n[fid]; else n[fid] = t;
                            return n;
                          })} />
                      );
                    })}
                  </ul>
                </div>
              ))}
              {showChanges && removed.length > 0 && (
                <div>
                  <h4 className="mb-2 border-b border-line pb-1 font-display text-[15px] text-muted">Left out for this role</h4>
                  <ul className="space-y-1.5">
                    {removed.map((d) => (
                      <ResumeRow key={"r" + d.fact_ids.join(",")} diff={d} editable={false} locked onDraft={() => {}} showChanges userEdited={false} />
                    ))}
                  </ul>
                </div>
              )}
            </div>
            {nDrafts > 0 && (
              <div className="sticky bottom-0 mt-6 flex items-center justify-between gap-3 rounded-[12px] border border-accent-line bg-surface px-4 py-3 shadow-[0_8px_24px_-12px_rgb(var(--shadow)/0.3)]">
                <span className="text-sm text-ink-2">{plural(nDrafts, "edited line")} — saved as a new version and re-checked.</span>
                <span className="flex gap-2">
                  <Button size="sm" variant="ghost" onClick={() => setDrafts({})}>Discard</Button>
                  <Button size="sm" variant="primary" busy={save.isPending} onClick={() => save.mutate()}>Save as new version</Button>
                </span>
              </div>
            )}
            <div className="mt-6 space-y-4">
              {form === "reject" ? (
                <GuidanceForm mode="reject" jobKey={jobKey} current={current} closed={closed} onCancel={() => setForm(null)} onStarted={started} />
              ) : (
                <ApprovePanel review={r} jobKey={jobKey} onApproved={() => {}} closed={closed} busy={running}
                  onReject={() => setForm("reject")} onAgain={() => setForm("again")} />
              )}
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}
