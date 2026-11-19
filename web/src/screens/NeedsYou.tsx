import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookmarkCheck, FileText, Hand, Paperclip, PartyPopper, Save } from "lucide-react";
import { DEMO_ACTION_TIP, api, post } from "../lib/api";
import { useDemo, useToast } from "../lib/app";
import { fmtDateTime, plural } from "../lib/format";
import { useTitle } from "../lib/router";
import type { AutofillSession, NeedsAction, UnansweredQuestion } from "../lib/types";
import { openReview } from "../components/JobCard";
import { Badge, Button, CardSkeleton, EmptyState, ErrorState, PageHeader, SectionTitle, cx } from "../components/ui";

function QuestionCard({ q, i }: { q: UnansweredQuestion; i: number }) {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const [value, setValue] = useState("");
  const id = `ans-${i}`;
  const save = useMutation({
    mutationFn: () => post<{ replaced: boolean; file: string }>("/api/answers/save", { question: q.question, answer: value }),
    onSuccess: (r) => {
      toast.notify(`${r.replaced ? "Updated" : "Saved"} in your answers bank (${r.file}). Autofill will use it from now on.`, { kind: "success" });
      qc.invalidateQueries({ queryKey: ["unanswered"] });
      qc.invalidateQueries({ queryKey: ["answers-review"] });
    },
    onError: toast.error,
  });
  const choice = q.options.length > 0 && ["select", "radio", "checkbox"].includes(q.field_type);
  return (
    <li className="card p-5 animate-rise" style={{ animationDelay: `${Math.min(i, 8) * 30}ms` }}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <label htmlFor={id} className="max-w-2xl font-display text-[18px] leading-snug text-ink">
          {q.question}
          {q.required && <span className="ml-2 align-middle font-sans text-[12px] font-medium text-bad">required</span>}
        </label>
        <span className="text-[12px] text-muted">asked by {plural(q.jobs.length, "job")}</span>
      </div>
      <p className="mt-1.5 flex flex-wrap gap-x-2 gap-y-1 text-[13px] text-muted">
        {q.jobs.slice(0, 4).map((j, k) => (
          <button key={j.key} type="button" onClick={() => openReview(j.key, "answers")} className="underline decoration-line-2 underline-offset-2 hover:text-ink">
            {j.company}{k < Math.min(3, q.jobs.length - 1) ? "," : ""}
          </button>
        ))}
        {q.jobs.length > 4 && <span>and {q.jobs.length - 4} more</span>}
        {q.reasons[0] && <span className="text-faint">· {q.reasons[0]}</span>}
      </p>
      <form className="mt-4 flex flex-wrap items-start gap-3" onSubmit={(e) => { e.preventDefault(); if (value.trim()) save.mutate(); }}>
        {choice ? (
          <select id={id} value={value} onChange={(e) => setValue(e.target.value)} disabled={demo}
            className="h-10 min-w-[240px] flex-1 rounded-[10px] border border-line-2 bg-surface px-3 text-sm text-ink outline-none focus:border-accent-line focus:ring-2 focus:ring-accent/20">
            <option value="">Choose your answer…</option>
            {q.options.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        ) : (
          <textarea id={id} value={value} onChange={(e) => setValue(e.target.value)} rows={q.field_type === "textarea" ? 4 : 2} disabled={demo}
            placeholder={demo ? "Saving answers is off in the public demo" : "Your answer, exactly as you'd type it in the form"}
            className="min-h-10 min-w-[240px] flex-1 resize-y rounded-[10px] border border-line-2 bg-surface px-3 py-2 text-sm leading-relaxed text-ink outline-none placeholder:text-faint focus:border-accent-line focus:ring-2 focus:ring-accent/20" />
        )}
        <Button type="submit" variant="primary" icon={<Save className="size-4" />} busy={save.isPending}
          disabled={demo || !value.trim()} reason={demo ? DEMO_ACTION_TIP : !value.trim() ? "Type an answer first" : undefined}>
          Save to answers bank
        </Button>
      </form>
    </li>
  );
}

const ACTION_BUTTON: Record<NeedsAction["kind"], string> = { resume: "Review", cover_letter: "Review", file: "Open" };

/** A file the form needs (résumé, cover letter, other upload): an action on the job, not a question. */
function ActionRow({ a }: { a: NeedsAction }) {
  const who = a.job.company || "This job";
  return (
    <li className="card flex flex-wrap items-center gap-4 p-4">
      <span className="grid size-9 place-items-center rounded-full bg-accent-soft text-accent-ink">
        {a.kind === "file" ? <Paperclip className="size-4" aria-hidden /> : <FileText className="size-4" aria-hidden />}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-ink">
          {who} {a.text}
          {a.kind === "file" && <span className="font-normal text-muted"> ({a.label})</span>}
        </p>
        <p className="mt-0.5 text-[13px] text-muted">
          {a.job.title}
          {a.kind === "resume" && " · approve a tailored résumé (or your base as is) and autofill attaches it"}
          {a.kind === "cover_letter" && " · write or approve a cover letter and autofill uses it"}
          {a.kind === "file" && " · autofill never attaches other files; add it in the form yourself"}
        </p>
      </div>
      <Button size="sm" variant="secondary" aria-label={`${ACTION_BUTTON[a.kind]}: ${who} ${a.text}`} onClick={() => openReview(a.job.key, a.tab)}>
        {ACTION_BUTTON[a.kind]}
      </Button>
    </li>
  );
}

function SessionRow({ s }: { s: AutofillSession }) {
  const o = s.outcome || {};
  return (
    <li className="card flex flex-wrap items-center gap-4 p-4">
      <span className={cx("grid size-9 place-items-center rounded-full", s.status === "blocked" ? "bg-bad-soft text-bad" : "bg-warn-soft text-warn")}>
        <Hand className="size-4" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-ink">{s.job_key.split(":").slice(1, 2).join("")} <Badge tone={s.status === "blocked" ? "bad" : "warn"} className="ml-1">{s.status === "blocked" ? "Blocked" : "Needs you"}</Badge></p>
        <p className="mt-0.5 text-[13px] text-muted">
          {o.reason || "Waiting for you in the browser window"} · {o.filled ?? 0} filled, {o.left_empty ?? 0} left · {fmtDateTime(s.started_at)}
        </p>
      </div>
      <Button size="sm" variant="secondary" onClick={() => openReview(s.job_key, "apply")}>Open</Button>
    </li>
  );
}

export function NeedsYou() {
  useTitle("Needs you");
  const sessions = useQuery({ queryKey: ["needs-you"], queryFn: () => api<{ sessions: AutofillSession[] }>("/api/autofill/needs-you"), refetchInterval: 10000 });
  const qs = useQuery({ queryKey: ["unanswered"], queryFn: () => api<{ questions: UnansweredQuestion[]; actions?: NeedsAction[]; bank_available: boolean }>("/api/answers/unanswered") });
  const loading = sessions.isLoading || qs.isLoading;
  const actions = qs.data?.actions || [];
  const nothing = !loading && !sessions.data?.sessions.length && !qs.data?.questions.length && !actions.length;
  return (
    <>
      <PageHeader eyebrow="Daily" title="Needs you">
        Questions only you can answer, files to approve, and fill windows waiting on you. Answer a question once and save it: the next form that asks it fills itself.
      </PageHeader>
      {loading ? <CardSkeleton rows={3} /> : sessions.error || qs.error ? (
        <ErrorState error={sessions.error || qs.error} onRetry={() => { sessions.refetch(); qs.refetch(); }} />
      ) : nothing ? (
        <EmptyState icon={<PartyPopper />} title="Nothing needs you">
          Every planned form can be filled from your answers bank, and no fill window is waiting.
        </EmptyState>
      ) : (
        <>
          {(sessions.data?.sessions.length || 0) > 0 && (
            <>
              <SectionTitle count={sessions.data!.sessions.length}>Waiting in the browser</SectionTitle>
              <ul className="space-y-3">{sessions.data!.sessions.map((s) => <SessionRow key={s.id} s={s} />)}</ul>
            </>
          )}
          {actions.length > 0 && (
            <>
              <SectionTitle count={actions.length}>Files to approve</SectionTitle>
              <ul className="space-y-3">{actions.map((a) => <ActionRow key={`${a.job.key}:${a.kind}:${a.label}`} a={a} />)}</ul>
            </>
          )}
          {(qs.data?.questions.length || 0) > 0 && (
            <>
              <SectionTitle count={qs.data!.questions.length} aside={<span className="hidden items-center gap-1.5 text-[13px] text-muted sm:inline-flex"><BookmarkCheck className="size-4" />Saved answers are marked approved</span>}>
                Unanswered questions
              </SectionTitle>
              <ul className="space-y-3">{qs.data!.questions.map((q, i) => <QuestionCard key={q.question} q={q} i={i} />)}</ul>
              <p className="mt-6 text-[13px] leading-relaxed text-muted">
                Answers go into the <code className="rounded bg-paper-2 px-1">custom_answers</code> block of your answers.yaml; the rest of the file is left as it is.
                Passwords, codes and ID or card numbers are never stored.
              </p>
            </>
          )}
        </>
      )}
    </>
  );
}
