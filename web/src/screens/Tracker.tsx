import { useQuery } from "@tanstack/react-query";
import { BellRing, CalendarClock, Columns3, Download, Hourglass, Send } from "lucide-react";
import { api } from "../lib/api";
import { STATUS_LABEL, fmtDate, plural } from "../lib/format";
import { Link, useTitle } from "../lib/router";
import type { JobCard, TrackerResponse } from "../lib/types";
import { openReview, useStatusChange } from "../components/JobCard";
import { Badge, Button, EmptyState, ErrorState, LinkButton, PageHeader, Skeleton, cx } from "../components/ui";

const COLUMNS: [string, string][] = [
  ["found", "Found"], ["tailored", "Tailored"], ["ready_to_submit", "Ready"], ["submitted", "Submitted"],
  ["oa", "OA"], ["interview", "Interview"], ["offer", "Offer"], ["rejected", "Rejected"],
];
const NEXT: Record<string, [string, string]> = {
  found: ["tailored", "Tailored"], tailored: ["ready_to_submit", "Ready"], ready_to_submit: ["submitted", "Submitted"],
  submitted: ["oa", "Got an OA"], oa: ["interview", "Interview"], interview: ["offer", "Offer"],
};
const DOT: Record<string, string> = {
  found: "bg-faint", tailored: "bg-faint", ready_to_submit: "bg-accent/60", submitted: "bg-accent",
  oa: "bg-accent", interview: "bg-good", offer: "bg-good", rejected: "bg-bad/70",
};

function Trail({ dates }: { dates?: Record<string, number> }) {
  const steps = COLUMNS.map(([s]) => s).filter((s) => dates?.[s]);
  if (steps.length < 2) return null;
  return (
    <ol className="mt-2.5 flex flex-wrap items-center gap-x-1 gap-y-0.5 text-[11px] text-faint" aria-label="Status dates">
      {steps.map((s, i) => (
        <li key={s} className="inline-flex items-center gap-1">
          {i > 0 && <span aria-hidden>→</span>}
          <span>{STATUS_LABEL[s]} {fmtDate(dates![s])}</span>
        </li>
      ))}
    </ol>
  );
}

function TCard({ job, col }: { job: JobCard; col: string }) {
  const change = useStatusChange();
  const next = NEXT[col];
  const since = job.days_in_status == null ? null : job.days_in_status === 0 ? "today" : plural(job.days_in_status, "day");
  return (
    <article id={`job-${job.key}`} tabIndex={0} aria-label={`${job.title} at ${job.company}, ${STATUS_LABEL[col]}`}
      onKeyDown={(e) => { if (e.key === "Enter" && e.target === e.currentTarget) openReview(job.key, col === "ready_to_submit" ? "apply" : undefined); }}
      onClick={(e) => { if (!(e.target as HTMLElement).closest("a,button,select")) openReview(job.key, col === "ready_to_submit" ? "apply" : undefined); }}
      className="card card-hover cursor-pointer p-3.5 outline-none animate-rise">
      <div className="text-[12px] font-semibold tracking-wide text-ink-2">{job.company}</div>
      <h3 className="mt-0.5 font-display text-[15px] leading-snug text-ink">{job.title}</h3>
      <p className="mt-1.5 text-[12px] text-muted">
        {fmtDate(job.status_changed_at)}{since && <> · {since} here</>}
        {job.submitted_at && col !== "submitted" && <> · sent {fmtDate(job.submitted_at)}</>}
      </p>
      {job.submit_source === "detected" && col === "submitted" && <Badge tone="good" className="mt-2">Confirmation detected</Badge>}
      {job.warning && <p className="mt-1.5 text-[12px] text-warn">{job.warning}</p>}
      <Trail dates={job.dates} />
      <div className="mt-3 flex items-center gap-1.5">
        {next && (
          <Button size="sm" variant={next[0] === "submitted" ? "primary" : "secondary"} className="h-7 px-2.5 text-[12px]"
            onClick={() => change.mutate({ key: job.key, status: next[0], prev: job.status })}>
            {next[1]}
          </Button>
        )}
        <label className="sr-only" htmlFor={`mv-${job.key}`}>Move {job.title} to</label>
        <select id={`mv-${job.key}`} value="" onChange={(e) => e.target.value && change.mutate({ key: job.key, status: e.target.value, prev: job.status })}
          className="h-7 min-w-0 flex-1 rounded-[8px] border border-line bg-surface px-1.5 text-[12px] text-muted">
          <option value="">Move to…</option>
          {COLUMNS.filter(([s]) => s !== col).map(([s, l]) => <option key={s} value={s}>{l}</option>)}
          <option value="not_interested">Not interested (hide)</option>
        </select>
      </div>
    </article>
  );
}

function Reminders({ t }: { t: TrackerResponse }) {
  const ready = t.ready_to_submit.filter((j) => j.reminder_due || j.warning);
  const items = [
    ...ready.map((j) => ({ j, icon: <Send />, text: j.warning ? `ready, but the ${j.warning}` : `ready for ${plural(j.days_waiting || 0, "day")} — submit it or let it go`, tab: "apply" })),
    ...t.followups_due.map((j) => ({ j, icon: <BellRing />, text: `sent ${plural(j.days_since_submit || 0, "day")} ago — time for a polite follow-up`, tab: undefined })),
  ];
  if (!items.length && !t.cooldowns.length) return null;
  return (
    <div className="mb-8 grid gap-4 lg:grid-cols-2">
      {items.length > 0 && (
        <section className="card p-5" aria-label="Reminders">
          <h2 className="eyebrow mb-3 flex items-center gap-2"><CalendarClock className="size-3.5" />Reminders</h2>
          <ul className="space-y-2.5">
            {items.map(({ j, icon, text, tab }) => (
              <li key={j.key + text} className="flex items-start gap-3 text-sm">
                <span className="mt-0.5 text-warn [&_svg]:size-4" aria-hidden>{icon}</span>
                <button type="button" className="text-left" onClick={() => openReview(j.key, tab)}>
                  <span className="font-medium text-ink">{j.company}</span> <span className="text-ink-2">— {j.title}</span>
                  <span className="block text-[13px] text-muted">{text}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      {t.cooldowns.length > 0 && (
        <section className="card p-5" aria-label="Re-apply cooldowns">
          <h2 className="eyebrow mb-3 flex items-center gap-2"><Hourglass className="size-3.5" />Re-apply cooldowns</h2>
          <ul className="space-y-2.5">
            {t.cooldowns.map((c) => (
              <li key={c.company} className="flex items-baseline justify-between gap-3 text-sm">
                <span><span className="font-medium text-ink">{c.company}</span> <span className="text-[13px] text-muted">rejected {fmtDate(c.rejected_at)}</span></span>
                <span className="shrink-0 text-[13px] text-ink-2" title={`${c.cooldown_days}-day cooldown`}>
                  apply again after {fmtDate(c.reapply_after)} <span className="text-faint">({plural(c.days_left, "day")})</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export function Tracker() {
  useTitle("Tracker");
  const t = useQuery({ queryKey: ["tracker"], queryFn: () => api<TrackerResponse>("/api/tracker") });
  const by = Object.fromEntries((t.data?.columns || []).map((c) => [c.status, c]));
  const total = COLUMNS.reduce((n, [s]) => n + (by[s]?.count || 0), 0);
  const hidden = by.not_interested?.count || 0;
  return (
    <div className="lg:-mr-6">
      <PageHeader eyebrow="Daily" title="Tracker"
        actions={<LinkButton href="/api/tracker/export.csv" newTab={false} icon={<Download className="size-4" />}>Export CSV</LinkButton>}>
        Every application from found to offer, with the date of each step. Follow-ups are suggested a week after you submit.
      </PageHeader>
      {t.isLoading ? (
        <div className="flex gap-4 overflow-hidden">{COLUMNS.slice(0, 5).map(([s]) => <Skeleton key={s} className="h-80 w-64 shrink-0" />)}</div>
      ) : t.error ? (
        <ErrorState error={t.error} onRetry={() => t.refetch()} />
      ) : (
        <>
          <Reminders t={t.data!} />
          {total === 0 && (
            <EmptyState icon={<Columns3 />} title="Nothing tracked yet">
              Jobs land here when you tailor, approve or submit them. Open a role from Today to start.
            </EmptyState>
          )}
          <div className={cx("scroll-thin -mx-1 flex snap-x gap-4 overflow-x-auto px-1 pb-4", total === 0 && "opacity-60")} role="list" aria-label="Tracker columns">
            {COLUMNS.map(([s, label]) => {
              const c = by[s] || { count: 0, items: [] };
              return (
                <section key={s} role="listitem" aria-label={`${label}: ${c.count}`} className="flex w-[244px] shrink-0 snap-start flex-col">
                  <h2 className="mb-3 flex items-center gap-2 px-1 text-[13px] font-semibold text-ink-2">
                    <span className={cx("size-2 rounded-full", DOT[s])} aria-hidden />
                    {label}
                    <span className="ml-auto tabular-nums text-faint">{c.count}</span>
                  </h2>
                  <div className="flex min-h-[120px] flex-1 flex-col gap-2.5 rounded-[14px] bg-paper-2/60 p-2">
                    {c.items.length ? c.items.map((j) => <TCard key={j.key} job={j} col={s} />)
                      : <p className="py-6 text-center text-[12px] text-faint">—</p>}
                  </div>
                </section>
              );
            })}
          </div>
          {hidden > 0 && (
            <p className="mt-2 text-[13px] text-muted">
              {plural(hidden, "role")} marked not interested —{" "}
              <Link to="/hidden?view=mine" className="text-accent underline underline-offset-2">see or unhide them</Link>.
            </p>
          )}
        </>
      )}
    </div>
  );
}
