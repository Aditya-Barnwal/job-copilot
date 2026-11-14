import { useState } from "react";
import { History, ChevronDown } from "lucide-react";
import { fmtDateTime, relTime } from "../lib/format";
import { useTitle } from "../lib/router";
import type { Run, RunStep } from "../lib/types";
import { Badge, CardSkeleton, EmptyState, ErrorState, PageHeader, cx } from "../components/ui";
import { RunNow, useRuns } from "./Today";

function duration(r: Run) {
  if (!r.finished_at) return "running";
  const s = Math.max(0, r.finished_at - r.started_at);
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

function summarize(step: RunStep): string {
  const r = step.result as Record<string, unknown> | unknown[] | undefined;
  if (!r || typeof r !== "object") return step.error || "";
  if (Array.isArray(r)) return r.join(", ");
  const keys = ["passed", "inserted", "updated", "excluded", "scored", "synced", "planned", "tailored", "main", "extended", "near_miss"];
  const parts = keys.filter((k) => typeof r[k] === "number").map((k) => `${r[k]} ${k.replace("_", " ")}`);
  if (r.counts && typeof r.counts === "object") {
    for (const [k, v] of Object.entries(r.counts as Record<string, number>)) parts.push(`${v} ${k.replace("_", " ")}`);
  }
  return parts.slice(0, 5).join(" · ");
}

function RunRow({ run, open, onToggle }: { run: Run; open: boolean; onToggle: () => void }) {
  const steps = run.summary?.steps || [];
  const tone = run.status === "ok" ? "good" : run.status === "running" ? "accent" : run.status === "partial" ? "warn" : "bad";
  return (
    <li className="card overflow-hidden">
      <button type="button" aria-expanded={open} onClick={onToggle}
        className="flex w-full items-center gap-4 px-5 py-4 text-left transition-colors hover:bg-surface-2">
        <span className={cx("size-2.5 shrink-0 rounded-full", run.status === "ok" ? "bg-good" : run.status === "running" ? "bg-accent animate-pulse" : run.status === "partial" ? "bg-warn" : "bg-bad")} aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="text-[15px] font-medium text-ink">{fmtDateTime(run.started_at)} <span className="font-normal text-muted">· {relTime(run.started_at)}</span></p>
          <p className="mt-0.5 text-[13px] text-muted">{run.kind} run{run.summary?.mode ? ` · ${run.summary.mode}` : ""} · {duration(run)} · {steps.length} steps</p>
        </div>
        <Badge tone={tone}>{run.status}</Badge>
        <ChevronDown className={cx("size-4 text-faint transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open && (
        <div className="border-t border-line px-5 py-4 animate-fade">
          {run.error && <p className="mb-3 text-sm text-bad">{run.error}</p>}
          <ol className="space-y-2">
            {steps.map((s, i) => (
              <li key={i} className="grid grid-cols-[16px_minmax(0,180px)_1fr_auto] items-baseline gap-3 text-[13px]">
                <span className={cx("size-1.5 translate-y-[-2px] rounded-full", s.status === "ok" ? "bg-good" : s.status === "skipped" ? "bg-faint" : "bg-bad")} aria-hidden />
                <span className="truncate font-medium text-ink-2">{s.name}</span>
                <span className="truncate text-muted">{s.status !== "ok" ? `${s.status}${s.error ? `: ${s.error}` : ""}` : summarize(s)}</span>
                <span className="tabular-nums text-faint">{s.seconds != null ? `${s.seconds}s` : ""}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </li>
  );
}

export function Runs() {
  useTitle("Runs");
  const runs = useRuns(30);
  const [open, setOpen] = useState<number | null>(null);
  return (
    <>
      <PageHeader eyebrow="Browse" title="Runs" actions={<RunNow />}>
        The nightly pipeline: discover companies, fetch postings, filter, score, build the queue and plan the forms. Each run keeps its log.
      </PageHeader>
      {runs.isLoading ? <CardSkeleton rows={3} /> : runs.error ? <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
        : !runs.data?.runs.length ? <EmptyState icon={<History />} title="No runs yet" action={<RunNow />}>The first nightly run builds your list. It runs at 19:00 by default, or start one now.</EmptyState>
          : (
            <ul className="space-y-3">
              {runs.data.runs.map((r, i) => (
                <RunRow key={r.id} run={r} open={open === r.id || (open === null && i === 0)} onToggle={() => setOpen(open === r.id ? -1 : r.id)} />
              ))}
            </ul>
          )}
    </>
  );
}
