import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Briefcase, Clock, ExternalLink, EyeOff, Globe2, MapPin, Undo2 } from "lucide-react";
import { DEMO_LINK_TIP, enc, post, safeHttpUrl } from "../lib/api";
import { useDemo, useHotkeys, useOverlayOpen, useToast } from "../lib/app";
import { STATUS_LABEL, TIER_LABEL, ageText, sourceName, tzText, workplace, yoeText } from "../lib/format";
import { setParams } from "../lib/router";
import type { JobCard as Job } from "../lib/types";
import { Badge, Button, ScoreRing, cx } from "./ui";

// The ordered keys of the list on screen, so the review drawer's j/k can step through it.
let listKeys: string[] = [];
export const currentListKeys = () => listKeys;

export function openReview(key: string, tab?: string) {
  setParams({ job: key, tab: tab || null });
}

export function useStatusChange() {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: async (v: { key: string; status: string; prev?: string; note?: string; label?: string; silent?: boolean }) => {
      await post(`/api/jobs/${enc(v.key)}/status`, { status: v.status, source: "user", note: v.note });
      return v;
    },
    onSuccess: (v) => {
      qc.invalidateQueries();
      if (v.silent) return;
      const label = v.label || `Moved to ${STATUS_LABEL[v.status] || v.status}.`;
      toast.notify(label, v.prev ? {
        action: {
          label: "Undo",
          run: () => {
            post(`/api/jobs/${enc(v.key)}/status`, { status: v.prev, source: "user", note: "undo" })
              .then(() => { qc.invalidateQueries(); toast.notify("Undone."); })
              .catch(toast.error);
          },
        },
      } : {});
    },
    onError: (e) => toast.error(e),
  });
}

/** "Not interested": hide a job, with Undo in the toast. */
export function hide(job: { key: string; title: string | null; status: string }, change: ReturnType<typeof useStatusChange>) {
  change.mutate({ key: job.key, status: "not_interested", prev: job.status,
    label: `Hidden “${job.title || "this role"}”. Find it under Hidden roles › Hidden by you.` });
}

export function PostingLink({ job, size = "sm", label = "Posting" }: { job: Pick<Job, "apply_url" | "url">; size?: "sm" | "md"; label?: string }) {
  const demo = useDemo();
  const href = safeHttpUrl(job.apply_url || job.url);
  if (demo || !href) {
    return (
      <Button size={size} variant="secondary" disabled reason={demo ? DEMO_LINK_TIP : "No link on this posting"} icon={<ExternalLink className="size-3.5" />}>
        {label}
      </Button>
    );
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer"
      className={cx("inline-flex items-center gap-2 rounded-[10px] border border-line-2 bg-surface font-medium text-ink transition-colors hover:border-faint hover:bg-surface-2",
        size === "sm" ? "h-8 px-3 text-[13px]" : "h-9 px-3.5 text-sm")}>
      <ExternalLink aria-hidden className="size-3.5" />
      {label}
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  );
}

export function Via({ job }: { job: Pick<Job, "attribution_name" | "attribution_url" | "source"> }) {
  const name = sourceName(job);
  if (!name) return null;
  const href = safeHttpUrl(job.attribution_url);
  return href ? (
    <a href={href} target="_blank" rel="noopener noreferrer" className="underline decoration-line-2 underline-offset-[3px] hover:text-ink hover:decoration-faint">
      via {name}
    </a>
  ) : (
    <span>via {name}</span>
  );
}

export function JobMeta({ job, className }: { job: Job; className?: string }) {
  const yoe = yoeText(job.yoe);
  const tz = tzText(job);
  const place = [job.location, workplace(job.workplace_type)].filter(Boolean).join(" · ");
  const items = [
    place && { icon: <MapPin />, text: place, label: "Location" },
    tz && { icon: <Globe2 />, text: tz, label: "Time zone" },
    yoe && { icon: <Briefcase />, text: yoe, label: "Experience asked" },
    { icon: <Clock />, text: ageText(job.age_days), label: "Posting age" },
  ].filter(Boolean) as { icon: ReactNode; text: string; label: string }[];
  return (
    <ul className={cx("flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[13px] text-muted", className)}>
      {items.map((it) => (
        <li key={it.label} className="inline-flex items-center gap-1.5 [&_svg]:size-3.5 [&_svg]:text-faint">
          <span aria-hidden>{it.icon}</span>
          <span className="sr-only">{it.label}: </span>
          {it.text}
        </li>
      ))}
      <li className="inline-flex items-center"><Via job={job} /></li>
    </ul>
  );
}

export function TierBadge({ tier }: { tier: string | null }) {
  if (!tier) return null;
  const tone = tier === "brand" ? "accent" : tier === "funded" ? "good" : tier === "unknown" ? "outline" : "neutral";
  return <Badge tone={tone} title="Company tier">{TIER_LABEL[tier] || tier}</Badge>;
}

export function Skills({ matched, missing, max = 8 }: { matched: string[]; missing: string[]; max?: number }) {
  if (!matched.length && !missing.length) return null;
  const m = matched.slice(0, max), x = missing.slice(0, Math.max(0, max - m.length));
  return (
    <div className="flex flex-wrap gap-1.5" aria-label="Skills">
      {m.map((s) => (
        <span key={"m" + s} className="rounded-md bg-accent-soft px-2 py-0.5 text-[12px] font-medium text-accent-ink" title="You have this">{s}</span>
      ))}
      {x.map((s) => (
        <span key={"x" + s} className="rounded-md border border-dashed border-line-2 px-2 py-0.5 text-[12px] text-muted" title="Asked for; not in your confirmed facts">
          {s}
        </span>
      ))}
      {matched.length + missing.length > m.length + x.length && (
        <span className="px-1 text-[12px] text-faint">+{matched.length + missing.length - m.length - x.length}</span>
      )}
    </div>
  );
}

export function JobCard({ job, selected, onSelect, index, excluded = false, compact = false }:
  { job: Job; selected?: boolean; onSelect?: () => void; index?: number; excluded?: boolean; compact?: boolean }) {
  const change = useStatusChange();
  const qc = useQueryClient();
  const toast = useToast();
  const ref = useRef<HTMLElement>(null);
  const hidden = job.tracker_status === "not_interested";
  const open = () => openReview(job.key);
  const tracked = !["found", "not_interested"].includes(job.tracker_status);
  return (
    <article
      ref={ref}
      id={`job-${job.key}`}
      tabIndex={0}
      aria-label={`${job.title} at ${job.company}${job.fit_score != null ? `, fit ${Math.round(job.fit_score)}` : ""}`}
      aria-current={selected || undefined}
      onFocus={onSelect}
      onClick={(e) => {
        if ((e.target as HTMLElement).closest("a,button")) return;
        open();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" && e.target === e.currentTarget) { e.preventDefault(); open(); }
      }}
      style={index != null ? { animationDelay: `${Math.min(index, 10) * 30}ms` } : undefined}
      className={cx("card card-hover group relative cursor-pointer animate-rise outline-none",
        compact ? "p-4" : "p-5 sm:p-6", excluded && "opacity-95")}
    >
      <div className="flex gap-5">
        {!excluded && <ScoreRing score={job.fit_score} size={compact ? 44 : 56} />}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-semibold tracking-wide text-ink-2">{job.company || "Unknown company"}</span>
            <TierBadge tier={job.tier} />
            {job.fresher_friendly && <Badge tone="good">Fresher-friendly</Badge>}
            {job.posting_gone && <Badge tone="bad">No longer listed</Badge>}
            {tracked && <Badge tone="neutral">{STATUS_LABEL[job.tracker_status] || job.tracker_status}</Badge>}
          </div>
          <h3 className={cx("mt-1 font-display leading-snug text-ink", compact ? "text-[17px]" : "text-[21px]")}>
            {job.title || "Untitled role"}
          </h3>
          {!excluded && job.why && !compact && <p className="mt-1.5 text-[14px] leading-relaxed text-muted">{job.why}</p>}
          <JobMeta job={job} className="mt-3" />
          {!excluded && !compact && (
            <div className="mt-3.5"><Skills matched={job.matched_skills} missing={job.missing_skills} /></div>
          )}
          {excluded && job.reasons && (
            <ul className="mt-3.5 space-y-1.5">
              {job.reasons.map((r, i) => (
                <li key={i} className="flex flex-wrap items-baseline gap-2 text-[13px]">
                  <Badge tone="warn">{r.rule}</Badge>
                  <span className="text-ink-2">{r.detail}</span>
                  {r.excerpt && <q className="text-muted italic">{r.excerpt}</q>}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end justify-end gap-3">
          <div className="flex items-center gap-2">
            {!excluded && (hidden ? (
              <Button size="sm" variant="ghost" icon={<Undo2 className="size-3.5" />}
                onClick={() => post(`/api/jobs/${enc(job.key)}/unhide`).then(() => qc.invalidateQueries(), toast.error)}>
                Unhide
              </Button>
            ) : (
              <Button size="sm" variant="ghost" icon={<EyeOff className="size-3.5" />} aria-label={`Not interested: hide ${job.title}`}
                title="Not interested — hide this role (x)" onClick={() => hide(job, change)}>
                <span className="hidden sm:inline">Not interested</span>
              </Button>
            ))}
            <PostingLink job={job} />
            {!excluded && <Button size="sm" variant="primary" onClick={open}>Review</Button>}
          </div>
        </div>
      </div>
    </article>
  );
}

/** j/k selection over a list of jobs, Enter/o/x on the selected one. */
export function useJobListNav(jobs: Job[]) {
  const [sel, setSel] = useState(0);
  const overlay = useOverlayOpen();
  const change = useStatusChange();
  const demoKeys = jobs.map((j) => j.key).join("|");
  useEffect(() => {
    listKeys = jobs.map((j) => j.key);
    return () => { listKeys = []; };
  }, [demoKeys]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (sel >= jobs.length) setSel(Math.max(0, jobs.length - 1)); }, [jobs.length, sel]);
  const focus = (i: number) => {
    const j = jobs[i];
    if (!j) return;
    setSel(i);
    const el = document.getElementById(`job-${j.key}`);
    el?.focus({ preventScroll: true });
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  useHotkeys({
    j: (e) => { e.preventDefault(); focus(Math.min(jobs.length - 1, sel + (document.activeElement?.id === `job-${jobs[sel]?.key}` ? 1 : 0))); },
    ArrowDown: (e) => { if ((document.activeElement as HTMLElement)?.id?.startsWith("job-")) { e.preventDefault(); focus(Math.min(jobs.length - 1, sel + 1)); } },
    k: (e) => { e.preventDefault(); focus(Math.max(0, sel - 1)); },
    ArrowUp: (e) => { if ((document.activeElement as HTMLElement)?.id?.startsWith("job-")) { e.preventDefault(); focus(Math.max(0, sel - 1)); } },
    o: () => {
      const j = jobs[sel];
      const href = j && safeHttpUrl(j.apply_url || j.url);
      if (href && !document.querySelector(".demo-banner")) window.open(href, "_blank", "noopener,noreferrer");
    },
    x: () => {
      const j = jobs[sel];
      if (j && j.tracker_status !== "not_interested") hide(j, change);
    },
  }, !overlay && jobs.length > 0);
  return { sel, setSel };
}

export function JobList({ jobs, excluded, compact }: { jobs: Job[]; excluded?: boolean; compact?: boolean }) {
  const { sel, setSel } = useJobListNav(jobs);
  return (
    <div className="space-y-3" role="list">
      {jobs.map((j, i) => (
        <div role="listitem" key={j.key}>
          <JobCard job={j} index={i} selected={i === sel} onSelect={() => setSel(i)} excluded={excluded} compact={compact} />
        </div>
      ))}
    </div>
  );
}
