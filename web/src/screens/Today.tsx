import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, ChevronDown, ChevronRight, Coffee, EyeOff, History, Play, Sunrise } from "lucide-react";
import { api, post } from "../lib/api";
import { useDemo, useToast } from "../lib/app";
import { fmtDateTime, plural, relTime } from "../lib/format";
import { Link, setParams, useLocation, useTitle } from "../lib/router";
import type { AutofillSession, EarlierResponse, NeedsAction, QueueResponse, Run, TrackerResponse, UnansweredQuestion } from "../lib/types";
import { JobCard, JobList } from "../components/JobCard";
import { Button, CardSkeleton, EmptyState, ErrorState, PageHeader, Segmented, cx } from "../components/ui";

type Lane = "main" | "extended" | "near_miss";

export function useRuns(limit = 1) {
  return useQuery({
    queryKey: ["runs", limit],
    queryFn: () => api<{ runs: Run[]; running: Run | null }>(`/api/runs?limit=${limit}`),
    refetchInterval: (q) => (q.state.data?.running ? 5000 : 60000),
  });
}

export function RunNow() {
  const demo = useDemo();
  const runs = useRuns();
  const qc = useQueryClient();
  const toast = useToast();
  const running = !!runs.data?.running;
  const [wasRunning, setWasRunning] = useState(false);
  useEffect(() => {
    if (running) setWasRunning(true);
    else if (wasRunning) {
      setWasRunning(false);
      const last = runs.data?.runs[0];
      qc.invalidateQueries();
      toast.notify(last?.status === "ok" ? "Nightly run finished — today's list is fresh." : `The run ended: ${last?.status || "unknown"}.`,
        { kind: last?.status === "ok" ? "success" : "error" });
    }
  }, [running]); // eslint-disable-line react-hooks/exhaustive-deps
  const start = useMutation({
    mutationFn: () => post("/api/runs/daily", {}),
    onSuccess: () => { toast.notify("Run started. The list refreshes when it finishes."); qc.invalidateQueries({ queryKey: ["runs"] }); },
    onError: toast.error,
  });
  return (
    <Button variant="secondary" icon={<Play className="size-3.5" />} busy={running || start.isPending}
      disabled={demo} reason={demo ? "Not available in the public demo — run Job-Copilot locally to use this" : undefined}
      onClick={() => start.mutate()}>
      {running ? "Running…" : "Run now"}
    </Button>
  );
}

function Stat({ label, value, to, tone }: { label: string; value: number | string; to: string; tone?: "warn" }) {
  return (
    <Link to={to} className="group rounded-[12px] px-4 py-3 transition-colors hover:bg-surface focus-visible:bg-surface">
      <div className={cx("font-display text-[30px] leading-none tabular-nums", tone === "warn" && Number(value) > 0 ? "text-warn" : "text-ink")}>{value}</div>
      <div className="mt-1.5 text-[13px] text-muted group-hover:text-ink-2">{label}</div>
    </Link>
  );
}

function dayLabel(day: string) {
  const d = new Date(day + "T00:00:00");
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

/** Previous queue days below today's list: newest first, the most recent open. */
function EarlierDays({ todayKeys }: { todayKeys: Set<string> }) {
  const [n, setN] = useState(7);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const q = useQuery({
    queryKey: ["earlier", n],
    queryFn: () => api<EarlierResponse>(`/api/queue/earlier?days=${n}`),
    placeholderData: (prev) => prev,
  });
  const days = (q.data?.days || [])
    .map((d) => ({ ...d, items: d.items.filter((j) => !j.posting_gone && !todayKeys.has(j.key)) }))
    .filter((d) => d.items.length > 0);
  if (q.isLoading || q.error || (!days.length && !q.data?.has_more)) return null;
  return (
    <section aria-labelledby="earlier-days" className="mt-12">
      <h2 id="earlier-days" className="mb-1 flex items-center gap-2 font-display text-[22px] text-ink">
        <History className="size-5 text-faint" aria-hidden />Earlier days
      </h2>
      <p className="mb-4 text-[13px] text-muted">Roles from previous lists that are still open and not on today's. Ones you acted on show their status.</p>
      <div className="space-y-3">
        {days.map((d, i) => {
          const isOpen = open[d.day] ?? i === 0;
          const id = `earlier-${d.day}`;
          return (
            <div key={d.day} className="rounded-[14px] border border-line">
              <button type="button" aria-expanded={isOpen} aria-controls={id}
                onClick={() => setOpen((o) => ({ ...o, [d.day]: !isOpen }))}
                className="flex w-full items-center gap-2 rounded-[14px] px-4 py-3 text-left text-[15px] font-medium text-ink hover:bg-surface-2">
                {isOpen ? <ChevronDown className="size-4 text-faint" aria-hidden /> : <ChevronRight className="size-4 text-faint" aria-hidden />}
                {dayLabel(d.day)} · {plural(d.items.length, "role")}
              </button>
              {isOpen && (
                <div id={id} role="list" aria-label={`Roles from ${dayLabel(d.day)}`} className="space-y-3 px-3 pb-3">
                  {d.items.map((j) => <div role="listitem" key={j.key}><JobCard job={j} compact /></div>)}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {q.data?.has_more && (
        <div className="mt-4 flex justify-center">
          <Button variant="ghost" busy={q.isFetching} onClick={() => setN((x) => x + 7)}>Show older</Button>
        </div>
      )}
    </section>
  );
}

export function Today() {
  useTitle("Today");
  const { search } = useLocation();
  const lane = (["main", "extended", "near_miss"].includes(search.get("lane") || "") ? search.get("lane") : "main") as Lane;
  const q = (l: Lane) => ({ queryKey: ["queue", l], queryFn: () => api<QueueResponse>(`/api/queue?lane=${l}`) });
  const main = useQuery(q("main"));
  const ext = useQuery(q("extended"));
  const near = useQuery(q("near_miss"));
  const runs = useRuns();
  const tracker = useQuery({ queryKey: ["tracker"], queryFn: () => api<TrackerResponse>("/api/tracker") });
  const needs = useQuery({ queryKey: ["needs-you"], queryFn: () => api<{ sessions: AutofillSession[] }>("/api/autofill/needs-you") });
  const unanswered = useQuery({ queryKey: ["unanswered"], queryFn: () => api<{ questions: UnansweredQuestion[]; actions?: NeedsAction[] }>("/api/answers/unanswered") });
  const hiddenMine = useQuery({ queryKey: ["hidden-mine"], queryFn: () => api<{ total: number }>("/api/hidden") });

  const current = lane === "main" ? main : lane === "extended" ? ext : near;
  // a posting found closed (e.g. when its review card opened) never shows on today's lists
  const currentItems = (current.data?.items || []).filter((j) => !j.posting_gone);
  const todayKeys = new Set([main, ext, near].flatMap((x) => (x.data?.items || []).map((j) => j.key)));
  const nHidden = hiddenMine.data?.total ?? 0;
  const day = main.data?.day ? new Date(main.data.day + "T00:00:00") : new Date();
  const last = runs.data?.runs[0];
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  return (
    <>
      <PageHeader
        eyebrow={day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}
        title={main.data?.day ? "Today's shortlist" : `${greeting}`}
        actions={<RunNow />}
      >
        {main.data?.day ? (
          <>
            {plural(main.data.count, "role")} picked for you
            {last && <> · last run {relTime(last.finished_at || last.started_at)}<span className="sr-only"> ({fmtDateTime(last.finished_at || last.started_at)})</span></>}.
            {" "}Review one, tailor the résumé, then open it and press Submit yourself.
          </>
        ) : (
          <>Your list is built by the nightly run. Start one now, or wait for this evening's.</>
        )}
      </PageHeader>

      <section aria-label="At a glance" className="-mx-4 mb-10 grid grid-cols-2 gap-1 sm:grid-cols-4">
        <Stat label="On today's list" value={main.data?.count ?? "–"} to="/today" />
        <Stat label="Needs you" value={(needs.data?.sessions.length ?? 0) + (unanswered.data?.questions.length ?? 0) + (unanswered.data?.actions?.length ?? 0)} to="/needs-you" tone="warn" />
        <Stat label="Ready to submit" value={tracker.data?.ready_to_submit.length ?? "–"} to="/tracker" />
        <Stat label="Follow-ups due" value={tracker.data?.followups_due.length ?? "–"} to="/tracker" tone="warn" />
      </section>

      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <Segmented<Lane>
          label="Lanes"
          value={lane}
          onChange={(v) => setParams({ lane: v === "main" ? null : v }, { replace: true })}
          items={[
            { value: "main", label: "Best matches", count: main.data?.count },
            { value: "extended", label: "Watchlist", count: ext.data?.count },
            { value: "near_miss", label: "Near misses", count: near.data?.count },
          ]}
        />
        <p className="text-[13px] text-faint">
          {lane === "main" && "Ranked by fit. At most three per company."}
          {lane === "extended" && "Good roles at companies we couldn't verify yet."}
          {lane === "near_miss" && "Just outside your rules (experience or salary). Worth a look."}
          {" "}
          <Link to={nHidden ? "/hidden?view=mine" : "/hidden"} className="inline-flex items-center gap-1 whitespace-nowrap text-muted underline decoration-line-2 underline-offset-2 hover:text-ink">
            <EyeOff className="size-3.5" aria-hidden />{nHidden ? `${nHidden} hidden · see Hidden roles` : "See Hidden roles"}
          </Link>
        </p>
      </div>

      {current.isLoading ? (
        <CardSkeleton rows={4} />
      ) : current.error ? (
        <ErrorState error={current.error} onRetry={() => current.refetch()} />
      ) : !main.data?.day && lane === "main" ? (
        <EmptyState icon={<Sunrise />} title="No list yet"
          action={<RunNow />}>
          The nightly run fetches new postings, filters them by your rules and ranks what's left. Run it now to build today's shortlist.
        </EmptyState>
      ) : current.data && currentItems.length ? (
        <JobList jobs={currentItems} />
      ) : (
        <EmptyState icon={lane === "main" ? <Coffee /> : <CalendarClock />}
          title={lane === "main" ? "All caught up" : lane === "extended" ? "Nothing on the watchlist today" : "No near misses today"}>
          {lane === "main"
            ? <>Everything on today's list is handled. <Link to="/matches" className="text-accent underline underline-offset-2">Browse all matches</Link> if you want more.</>
            : "Check back after the next run."}
        </EmptyState>
      )}

      {main.data?.day && <EarlierDays todayKeys={todayKeys} />}
    </>
  );
}
