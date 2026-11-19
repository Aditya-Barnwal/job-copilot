import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, CircleSlash, EyeOff, Undo2, X } from "lucide-react";
import { api, enc, post } from "../../lib/api";
import { pushOverlay, useDemo, useToast } from "../../lib/app";
import { STATUS_LABEL, fmtDateTime, workplace } from "../../lib/format";
import { setParams, useLocation } from "../../lib/router";
import type { ArtifactSummary, JobCard, JobDetail, LiveCheck } from "../../lib/types";
import { currentListKeys, hide, PostingLink, TierBadge, useStatusChange, Via } from "../../components/JobCard";
import { trapTab } from "../../components/Shell";
import { Badge, Button, ErrorState, IconButton, Kbd, ScoreRing, Segmented, Skeleton } from "../../components/ui";
import { ResumeTab } from "./ResumeTab";
import { CoverLetterTab } from "./CoverLetterTab";
import { AnswersTab } from "./AnswersTab";
import { ApplyTab } from "./ApplyTab";

export type Tab = "resume" | "letter" | "answers" | "apply";
const TABS: Tab[] = ["resume", "letter", "answers", "apply"];

export interface FitInfo {
  fit_score?: number | null;
  components?: Record<string, number>;
  matched_skills?: string[];
  missing_skills?: string[];
  why?: string | null;
}

export function useJob(key: string) {
  return useQuery({ queryKey: ["job", key], queryFn: () => api<JobDetail>(`/api/jobs/${enc(key)}`) });
}

export function useArtifacts(key: string) {
  return useQuery({ queryKey: ["artifacts", key], queryFn: () => api<{ artifacts: ArtifactSummary[] }>(`/api/jobs/${enc(key)}/artifacts`) });
}

// Each job's posting is checked at most once per app session (POST check-live
// marks a dead posting gone on the server). Errors (endpoint missing, offline)
// count as "not known": nothing is shown and the check isn't retried.
const liveChecks = new Map<string, Promise<LiveCheck | null>>();

export function useLiveCheck(key: string, enabled: boolean, qc: QueryClient): LiveCheck | null {
  const [res, setRes] = useState<LiveCheck | null>(null);
  useEffect(() => {
    setRes(null);
    if (!enabled) return;
    let p = liveChecks.get(key);
    if (!p) {
      p = post<LiveCheck>(`/api/jobs/${enc(key)}/check-live`).then(
        (r) => {
          if (r && r.live === false) {
            // closed: the job leaves Today, the lanes and Earlier days; the tracker marks it
            for (const k of [["job", key], ["queue"], ["earlier"], ["matches"], ["tracker"], ["unanswered"]]) {
              qc.invalidateQueries({ queryKey: k });
            }
          }
          return r && typeof r.live === "boolean" ? r : null;
        },
        () => null,
      );
      liveChecks.set(key, p);
    }
    let alive = true;
    p.then((r) => { if (alive) setRes(r); });
    return () => { alive = false; };
  }, [key, enabled]); // eslint-disable-line react-hooks/exhaustive-deps
  return res;
}

/** Why the posting is closed, or null while it is (as far as we know) open. */
export function closedReason(job: JobDetail | undefined, live: LiveCheck | null): string | null {
  if (live?.live === false) return live.reason || "The posting is no longer accepting applications.";
  if (job?.gone_at) return "No job board lists this posting any more.";
  return null;
}

export const CLOSED_TIP = "This posting is closed";

function checkedAt(live: LiveCheck | null): string | null {
  const t = live?.checked_at;
  if (typeof t === "number") return fmtDateTime(t > 1e12 ? t / 1000 : t);
  if (typeof t === "string" && !Number.isNaN(Date.parse(t))) {
    return new Date(t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  return null;
}

export function ReviewDrawer() {
  const { search } = useLocation();
  const key = search.get("job");
  if (!key) return null;
  return <Drawer jobKey={key} tab={(TABS.includes(search.get("tab") as Tab) ? search.get("tab") : "resume") as Tab} />;
}

function Drawer({ jobKey, tab }: { jobKey: string; tab: Tab }) {
  const panel = useRef<HTMLDivElement>(null);
  const job = useJob(jobKey);
  const fit = useQuery({ queryKey: ["fit", jobKey], queryFn: () => api<FitInfo>(`/api/jobs/${enc(jobKey)}/fit`), retry: false });
  const qc = useQueryClient();
  const toast = useToast();
  const change = useStatusChange();
  const demo = useDemo();
  const live = useLiveCheck(jobKey, !demo, qc);
  const closed = closedReason(job.data, live);
  const close = () => setParams({ job: null, tab: null });
  const go = (d: number) => {
    const keys = currentListKeys();
    const i = keys.indexOf(jobKey);
    const n = keys[i + d];
    if (n) setParams({ job: n }, { replace: true });
  };

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const pop = pushOverlay();
    panel.current?.focus();
    document.body.style.overflow = "hidden";
    return () => {
      pop();
      document.body.style.overflow = "";
      const card = document.getElementById(`job-${jobKey}`);
      (card || prev)?.focus?.({ preventScroll: true });
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement)?.closest?.("input,textarea,select,[contenteditable]");
      if (e.key === "Escape") {
        if (typing) { (e.target as HTMLElement).blur(); return; }
        e.preventDefault(); close(); return;
      }
      if (e.key === "Tab" && panel.current) { trapTab(e, panel.current); return; }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "j") { e.preventDefault(); go(1); }
      else if (e.key === "k") { e.preventDefault(); go(-1); }
      else if (["1", "2", "3", "4"].includes(e.key)) { e.preventDefault(); setParams({ tab: TABS[Number(e.key) - 1] }, { replace: true }); }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  });

  const j = job.data;
  const card = j as unknown as JobCard | undefined;
  const keys = currentListKeys();
  const idx = keys.indexOf(jobKey);

  return (
    <div className="fixed inset-0 z-[65] flex justify-end">
      <div className="absolute inset-0 bg-ink/20 backdrop-blur-[1.5px] animate-fade" onClick={close} aria-hidden />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={j ? `Review: ${j.title} at ${j.company}` : "Review"}
        tabIndex={-1}
        className="relative flex h-full w-full max-w-[1240px] flex-col border-l border-line bg-paper shadow-[0_0_80px_-20px_rgb(var(--shadow)/0.45)] outline-none animate-drawer lg:w-[calc(100vw-96px)]"
      >
        <header className="border-b border-line bg-surface/80 px-6 pb-0 pt-5 backdrop-blur sm:px-8">
          <div className="flex items-start gap-5">
            <ScoreRing score={fit.data?.fit_score ?? null} size={60} />
            <div className="min-w-0 flex-1">
              {j ? (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-semibold tracking-wide text-ink-2">{j.company}</span>
                    {card?.tier && <TierBadge tier={card.tier} />}
                    <Badge>{STATUS_LABEL[j.status] || j.status}</Badge>
                  </div>
                  <h2 className="mt-1 font-display text-[26px] leading-tight text-ink sm:text-[30px]">{j.title}</h2>
                  <p className="mt-1.5 flex flex-wrap gap-x-3 text-[13px] text-muted">
                    {[j.location_text, workplace(j.workplace_type), j.comp_summary, j.department].filter(Boolean).map((t) => <span key={String(t)}>{t}</span>)}
                    <Via job={{ attribution_name: j.attribution_name as string | null, attribution_url: j.attribution_url as string | null, source: j.source as string | null }} />
                  </p>
                </>
              ) : (
                <div className="space-y-2 pt-1"><Skeleton className="h-3 w-32" /><Skeleton className="h-7 w-80" /></div>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {j && (j.status === "not_interested" ? (
                <Button size="sm" variant="ghost" icon={<Undo2 className="size-3.5" />}
                  onClick={() => post(`/api/jobs/${enc(j.key)}/unhide`).then(() => { qc.invalidateQueries(); toast.notify("Back on your list."); }, toast.error)}>
                  Unhide
                </Button>
              ) : (
                <Button size="sm" variant="ghost" icon={<EyeOff className="size-3.5" />} onClick={() => hide(j, change)}>
                  Not interested
                </Button>
              ))}
              {j && <PostingLink job={{ apply_url: j.apply_url, url: j.url }} label="Open posting" />}
              <IconButton label="Previous job (k)" disabled={idx <= 0} onClick={() => go(-1)}><ChevronUp className="size-4" /></IconButton>
              <IconButton label="Next job (j)" disabled={idx < 0 || idx >= keys.length - 1} onClick={() => go(1)}><ChevronDown className="size-4" /></IconButton>
              <IconButton label="Close (Esc)" onClick={close}><X className="size-4" /></IconButton>
            </div>
          </div>
          {closed && (
            <div role="status" className="mt-4 flex items-start gap-3 rounded-[12px] border border-bad/30 bg-bad-soft px-4 py-3 text-sm text-bad">
              <CircleSlash className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span className="flex-1">
                <span className="font-semibold">This posting is closed.</span> {closed}
                {" "}Approving a résumé and Open &amp; fill are off for it. It stays on the tracker, marked closed.
                {checkedAt(live) && <span className="text-bad/80"> Checked {checkedAt(live)}.</span>}
              </span>
            </div>
          )}
          <div className="mt-5 flex items-center justify-between gap-4">
            <Segmented<Tab>
              label="Review sections"
              className="-mb-px rounded-b-none border-b-0"
              value={tab}
              onChange={(t) => setParams({ tab: t === "resume" ? null : t }, { replace: true })}
              items={[
                { value: "resume", label: "Résumé" },
                { value: "letter", label: "Cover letter" },
                { value: "answers", label: "Answers" },
                { value: "apply", label: "Open & Submit" },
              ]}
            />
            <div className="hidden items-center gap-1.5 pb-2 text-[12px] text-faint md:flex">
              <Kbd>1</Kbd>–<Kbd>4</Kbd> sections · <Kbd>j</Kbd><Kbd>k</Kbd> next / previous · <Kbd>Esc</Kbd> close
            </div>
          </div>
        </header>
        <div className="scroll-thin flex-1 overflow-y-auto px-6 py-7 sm:px-8" role="tabpanel" aria-label={tab}>
          {job.error ? (
            <ErrorState error={job.error} onRetry={() => job.refetch()} />
          ) : !j ? (
            <div className="grid gap-6 lg:grid-cols-2"><Skeleton className="h-96" /><Skeleton className="h-96" /></div>
          ) : tab === "resume" ? (
            <ResumeTab job={j} fit={fit.data} closed={closed} />
          ) : tab === "letter" ? (
            <CoverLetterTab job={j} />
          ) : tab === "answers" ? (
            <AnswersTab job={j} />
          ) : (
            <ApplyTab job={j} closed={closed} />
          )}
        </div>
      </div>
    </div>
  );
}
