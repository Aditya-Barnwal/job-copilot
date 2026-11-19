import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CircleAlert, Hand, Lock, LockOpen, MousePointerClick, PartyPopper, Play, ShieldCheck } from "lucide-react";
import { ApiError, DEMO_ACTION_TIP, api, enc, post } from "../../lib/api";
import { useDemo, useToast } from "../../lib/app";
import { fmtDateTime, plural } from "../../lib/format";
import { Link, setParams } from "../../lib/router";
import type { AutofillSession, JobDetail } from "../../lib/types";
import { Badge, Button, cx } from "../../components/ui";
import { CLOSED_TIP, useArtifacts } from "./ReviewDrawer";
import { useFillPlan } from "./AnswersTab";

const POLL_WHILE = new Set(["starting", "filling", "ready_to_submit", "needs_you", "blocked"]);

const STATUS: Record<string, { title: string; body: string; tone: "accent" | "warn" | "bad" | "good" | "neutral" }> = {
  starting: { title: "Opening your browser…", body: "Chrome opens on your sign-in profile.", tone: "neutral" },
  filling: { title: "Filling the form…", body: "Typing answers from your bank and attaching your approved résumé. It stops before Submit.", tone: "neutral" },
  ready_to_submit: { title: "Ready for you", body: "Every field it could fill is filled. Check the form in the browser window, then unlock and press Submit yourself.", tone: "accent" },
  needs_you: { title: "Needs you", body: "It filled what it could. The questions left are highlighted in the form.", tone: "warn" },
  blocked: { title: "Blocked", body: "A sign-in wall, CAPTCHA or check stopped it. Filled what it could; finish the rest in the window.", tone: "warn" },
  submitted: { title: "Submitted", body: "The confirmation page was detected after your Submit. A screenshot is saved as proof.", tone: "good" },
  closed: { title: "Window closed", body: "The fill window was closed before a confirmation page appeared.", tone: "neutral" },
  timed_out: { title: "Stopped watching", body: "No confirmation page appeared in time. If you did submit, mark it below.", tone: "neutral" },
  abandoned: { title: "Ended", body: "The app restarted while this window was open.", tone: "neutral" },
  error: { title: "Something went wrong", body: "The fill didn't finish.", tone: "bad" },
};

function Step({ n, title, children, state, last }: { n: number; title: string; children?: ReactNode; state: "done" | "now" | "todo"; last?: boolean }) {
  return (
    <li className="relative flex gap-4 pb-6">
      {!last && <span aria-hidden className={cx("absolute left-[13px] top-8 h-[calc(100%-2rem)] w-px", state === "done" ? "bg-accent/50" : "bg-line")} />}
      <span aria-hidden className={cx("grid size-7 shrink-0 place-items-center rounded-full text-[12px] font-semibold transition-colors",
        state === "done" ? "bg-accent text-on-accent" : state === "now" ? "bg-accent-soft text-accent-ink ring-2 ring-accent/40" : "bg-paper-2 text-faint")}>
        {state === "done" ? <Check className="size-3.5" /> : n}
      </span>
      <div className="min-w-0 pt-0.5">
        <p className={cx("text-[15px] font-medium", state === "todo" ? "text-muted" : "text-ink")}>
          {title}<span className="sr-only"> ({state === "done" ? "done" : state === "now" ? "current step" : "to do"})</span>
        </p>
        {children && <div className="mt-1 text-[13px] leading-relaxed text-muted">{children}</div>}
      </div>
    </li>
  );
}

export function ApplyTab({ job, closed = null }: { job: JobDetail; closed?: string | null }) {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const arts = useArtifacts(job.key);
  const plan = useFillPlan(job.key);
  const sessions = useQuery({
    queryKey: ["sessions", job.key],
    queryFn: () => api<{ sessions: AutofillSession[] }>(`/api/jobs/${enc(job.key)}/autofill/sessions`),
  });
  const [sid, setSid] = useState<number | null>(null);
  const [unlocked, setUnlocked] = useState<number | null>(null);
  useEffect(() => { setSid(null); setUnlocked(null); }, [job.key]);
  const latest = sessions.data?.sessions[0];
  const activeId = sid ?? latest?.id ?? null;
  const session = useQuery({
    queryKey: ["session", activeId],
    enabled: activeId != null,
    queryFn: () => api<AutofillSession>(`/api/autofill/sessions/${activeId}`),
    refetchInterval: (q) => (q.state.data && POLL_WHILE.has(q.state.data.status) && !demo ? 1500 : false),
  });
  const s = session.data ?? (latest && latest.id === activeId ? latest : undefined);
  useEffect(() => {
    if (s?.status === "submitted") { qc.invalidateQueries({ queryKey: ["job", job.key] }); qc.invalidateQueries({ queryKey: ["tracker"] }); }
  }, [s?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = useMutation({
    mutationFn: () => post<{ session_id: number }>(`/api/jobs/${enc(job.key)}/autofill`, {}),
    onSuccess: (r) => { setSid(r.session_id); setUnlocked(null); qc.invalidateQueries({ queryKey: ["sessions", job.key] }); },
    onError: (e) => {
      const p = e instanceof ApiError ? (e.payload as { session_id?: number; link_out?: string; detail?: string } | null) : null;
      if (p?.session_id) setSid(p.session_id);
      toast.error(e);
    },
  });
  const handover = useMutation({
    mutationFn: (id: number) => post(`/api/autofill/sessions/${id}/handover`, {}),
    onSuccess: (_r, id) => { setUnlocked(id); toast.notify("Submit is unlocked. Press it in the browser window when you're ready.", { kind: "success" }); },
    onError: toast.error,
  });
  const mark = useMutation({
    mutationFn: () => post(`/api/jobs/${enc(job.key)}/status`, { status: "submitted", source: "user", note: "marked submitted in the review card" }),
    onSuccess: () => {
      toast.notify("Marked as submitted. It's on the tracker now.", { kind: "success", action: { label: "Open tracker", run: () => setParams({ job: null, tab: null }) } });
      qc.invalidateQueries();
    },
    onError: toast.error,
  });

  const approved = (arts.data?.artifacts || []).find((a) => a.status === "approved");
  const submitted = ["submitted", "oa", "interview", "offer", "rejected", "applied"].includes(job.status);
  const st = s ? STATUS[s.status] || { title: s.status, body: "", tone: "neutral" as const } : null;
  const open = !!s?.open;
  const isUnlocked = s != null && unlocked === s.id;
  const o = s?.outcome || {};
  const filledDone = !!s && !["starting", "filling"].includes(s.status);
  const shot = !!s && (s.has_screenshot ?? !!s.screenshot_path);

  const stepState = (i: number): "done" | "now" | "todo" => {
    if (submitted || s?.status === "submitted") return "done";
    const cur = !s || !open || !filledDone ? 0 : isUnlocked ? 3 : 1;
    return i < cur ? "done" : i === cur ? "now" : "todo";
  };

  return (
    <div className="mx-auto grid max-w-5xl gap-8 lg:grid-cols-[minmax(0,1fr)_340px]">
      <div className="space-y-6">
        {/* status card */}
        <section aria-live="polite" className={cx("card overflow-hidden",
          st?.tone === "good" && "border-good/30", st?.tone === "warn" && "border-warn/30", st?.tone === "bad" && "border-bad/30")}>
          <div className={cx("flex items-start gap-4 p-6",
            st?.tone === "good" ? "bg-good-soft/60" : st?.tone === "warn" ? "bg-warn-soft/50" : st?.tone === "bad" ? "bg-bad-soft/50" : st?.tone === "accent" ? "bg-accent-soft/50" : "")}>
            <span className={cx("grid size-11 shrink-0 place-items-center rounded-full [&_svg]:size-5",
              st?.tone === "good" ? "bg-good text-surface" : st?.tone === "warn" ? "bg-warn text-surface" : st?.tone === "bad" ? "bg-bad text-surface" : "bg-accent text-on-accent")}>
              {submitted || s?.status === "submitted" ? <PartyPopper /> : st?.tone === "warn" ? <Hand /> : st?.tone === "bad" ? <CircleAlert /> : s && POLL_WHILE.has(s.status) && !filledDone ? <Play /> : <MousePointerClick />}
            </span>
            <div className="min-w-0 flex-1">
              <h3 className="font-display text-[24px] leading-tight text-ink">
                {submitted ? "Submitted" : st ? st.title : "Open & Submit"}
              </h3>
              <p className="mt-1 text-[14px] leading-relaxed text-ink-2">
                {submitted
                  ? `You submitted this application${job.submitted_at ? ` on ${fmtDateTime(job.submitted_at)}` : ""}. Track replies on the tracker.`
                  : st ? (o.reason && s?.status === "error" ? `${st.body} ${o.reason}` : st.body)
                    : "Opens the application in your own Chrome, fills it from your answers bank and attaches your approved résumé — then stops. You press Submit."}
              </p>
              {s && filledDone && (o.filled != null || o.left_empty != null) && (
                <div className="mt-4 flex flex-wrap gap-6">
                  <div><div className="font-display text-[28px] leading-none tabular-nums text-ink">{o.filled ?? 0}</div><div className="mt-1 text-[12px] text-muted">fields filled</div></div>
                  <div><div className={cx("font-display text-[28px] leading-none tabular-nums", (o.left_empty || 0) > 0 ? "text-warn" : "text-ink")}>{o.left_empty ?? 0}</div><div className="mt-1 text-[12px] text-muted">left for you</div></div>
                  {o.step_reached && <div><div className="font-display text-[18px] leading-[28px] text-ink">{o.step_reached}</div><div className="mt-1 text-[12px] text-muted">page reached</div></div>}
                </div>
              )}
            </div>
          </div>
          {s && (o.highlighted?.length || 0) > 0 && (
            <div className="border-t border-line px-6 py-4">
              <p className="eyebrow mb-2">Left empty and highlighted in the form</p>
              <ul className="flex flex-wrap gap-1.5">
                {o.highlighted!.map((h) => <li key={h}><Badge tone="warn">{h}</Badge></li>)}
              </ul>
              <p className="mt-2 text-[13px] text-muted">Answer them in the window. Save your answers on <Link to="/needs-you" className="text-accent underline underline-offset-2">Needs you</Link> so next time they fill themselves.</p>
            </div>
          )}
          {s && shot && (
            <figure className="border-t border-line bg-paper-2/50 p-4">
              <img src={`/api/autofill/sessions/${s.id}/screenshot?v=${s.finished_at || s.status}`} alt={s.status === "submitted" ? "Screenshot of the confirmation page" : "Screenshot of the filled form"}
                className="max-h-[420px] w-full rounded-[10px] border border-line object-cover object-top" />
              <figcaption className="mt-2 text-[12px] text-muted">{s.status === "submitted" ? "Confirmation page, saved as proof." : "The form as it was left for you."}</figcaption>
            </figure>
          )}
          {submitted && !shot && job.submit_proof_path && (
            <figure className="border-t border-line bg-paper-2/50 p-4">
              <img src={`/api/jobs/${enc(job.key)}/proof`} alt="Screenshot of the confirmation page" className="max-h-[420px] w-full rounded-[10px] border border-line object-cover object-top" />
            </figure>
          )}
          <div className="flex flex-wrap items-center gap-2 border-t border-line px-6 py-4">
            {!submitted && (!s || !open) && (
              <Button variant="primary" size="lg" icon={<Play className="size-4" />} busy={start.isPending}
                disabled={demo || !approved || !!closed} reason={demo ? "Autofill is off in the public demo — it drives a real browser on your computer" : closed ? CLOSED_TIP : !approved ? "Approve a tailored résumé first" : undefined}
                onClick={() => start.mutate()}>
                {s ? "Open & fill again" : "Open & fill the form"}
              </Button>
            )}
            {!submitted && open && filledDone && (
              isUnlocked ? (
                <span className="inline-flex h-11 items-center gap-2 rounded-[10px] bg-good-soft px-4 text-[15px] font-medium text-good">
                  <LockOpen className="size-4" aria-hidden />Submit unlocked — press it in the browser
                </span>
              ) : (
                <Button variant="primary" size="lg" icon={<LockOpen className="size-4" />} busy={handover.isPending}
                  disabled={demo || !s?.live} reason={demo ? DEMO_ACTION_TIP : !s?.live ? "The fill window isn't open in this app session" : undefined}
                  onClick={() => s && handover.mutate(s.id)}>
                  Unlock Submit
                </Button>
              )
            )}
            {!submitted && (
              <Button variant="secondary" icon={<Check className="size-4" />} busy={mark.isPending} onClick={() => mark.mutate()}>
                Mark submitted
              </Button>
            )}
          </div>
          {!submitted && open && filledDone && !isUnlocked && (
            <p className="flex items-start gap-2 border-t border-line bg-surface-2 px-6 py-3 text-[13px] leading-relaxed text-muted">
              <Lock className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              Until you unlock, even your own click on Submit is blocked — that protects you from pages that try to submit themselves.
            </p>
          )}
        </section>
      </div>

      {/* steps + checklist */}
      <aside className="space-y-6">
        <section aria-label="How it works" className="card p-5">
          <h4 className="eyebrow mb-4">How it goes</h4>
          <ol>
            <Step n={1} title="Open & fill" state={stepState(0)}>The app opens the form in your Chrome and fills it. It never presses Submit.</Step>
            <Step n={2} title="Check the form" state={stepState(1)}>Read it over in the window; answer anything highlighted.</Step>
            <Step n={3} title="Unlock Submit" state={stepState(2)}>Lifts the page-level guard for this one window.</Step>
            <Step n={4} title="You press Submit" state={stepState(3)}>In the browser. The app only watches for the confirmation page.</Step>
            <Step n={5} title="Submitted" state={stepState(4)} last>Detected with a screenshot, or mark it yourself.</Step>
          </ol>
        </section>
        <section aria-label="Before you start" className="card p-5 text-[13px]">
          <h4 className="eyebrow mb-3">Before you start</h4>
          <ul className="space-y-2.5">
            <li className="flex items-start gap-2">
              {approved ? <ShieldCheck className="size-4 shrink-0 text-good" aria-hidden /> : <CircleAlert className="size-4 shrink-0 text-warn" aria-hidden />}
              <span className="text-ink-2">{approved ? `Résumé v${approved.version} approved` : <>No approved résumé yet — <button type="button" className="text-accent underline underline-offset-2" onClick={() => setParams({ tab: null })}>review it</button></>}</span>
            </li>
            <li className="flex items-start gap-2">
              {plan.data ? <ShieldCheck className="size-4 shrink-0 text-good" aria-hidden /> : <CircleAlert className="size-4 shrink-0 text-faint" aria-hidden />}
              <span className="text-ink-2">{plan.data ? `Fill plan ready${plan.data.unresolved_count ? ` · ${plural(plan.data.unresolved_count, "question")} for you` : ""}` : "No fill plan yet (it's built when you open the form)"}</span>
            </li>
            <li className="flex items-start gap-2">
              <CircleAlert className="size-4 shrink-0 text-faint" aria-hidden />
              <span className="text-ink-2">Portals like Workday need you signed in first: <Link to="/portals" className="text-accent underline underline-offset-2">Sign in to portals</Link></span>
            </li>
          </ul>
        </section>
      </aside>
    </div>
  );
}
