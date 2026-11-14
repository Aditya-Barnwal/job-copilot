import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, FileUp, ListChecks, RefreshCw } from "lucide-react";
import { ApiError, DEMO_ACTION_TIP, api, enc, post } from "../../lib/api";
import { useDemo, useToast } from "../../lib/app";
import { fmtDateTime, plural } from "../../lib/format";
import { Link } from "../../lib/router";
import type { FillAction, FillPlanRow, JobDetail } from "../../lib/types";
import { Badge, Button, EmptyState, ErrorState, Skeleton, cx } from "../../components/ui";

export function sourceLabel(src: string) {
  if (!src || src === "none") return "—";
  if (src.startsWith("answers.yaml:")) return "Answers bank · " + src.slice(13).replace(/\[\d+\]/g, "").replace(/\./g, " › ").replace(/_/g, " ");
  if (src.startsWith("facts:")) return "Résumé facts · " + src.slice(6);
  if (src.startsWith("file:")) return src.slice(5) === "resume" ? "Approved résumé" : "Approved cover letter";
  if (src === "generated") return "Drafted from your facts";
  if (src === "user") return "You";
  return src;
}

export function useFillPlan(key: string) {
  return useQuery({
    queryKey: ["fill-plan", key],
    retry: false,
    queryFn: async () => {
      try {
        return await api<FillPlanRow>(`/api/jobs/${enc(key)}/fill-plan`);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
  });
}

function Row({ a }: { a: FillAction }) {
  const filled = a.approved || (!a.needs_user && a.source !== "none");
  const upload = a.kind === "upload";
  return (
    <li className="flex items-start gap-3 py-3">
      <span className={cx("mt-0.5 shrink-0", filled ? "text-accent" : "text-warn")} aria-hidden>
        {upload ? <FileUp className="size-4" /> : filled ? <CheckCircle2 className="size-4" /> : <CircleDashed className="size-4" />}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[14px] text-ink">{a.label}{a.required && <span className="ml-1 text-bad" aria-label="required">*</span>}</p>
        <p className="mt-0.5 text-[12px] text-muted">{filled ? sourceLabel(a.source) : a.note || "Left for you"}</p>
      </div>
      <Badge tone={filled ? "accent" : "warn"}>{filled ? (upload ? "Attached" : "Filled") : "You"}</Badge>
    </li>
  );
}

export function AnswersTab({ job }: { job: JobDetail }) {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const plan = useFillPlan(job.key);
  const rebuild = useMutation({
    mutationFn: () => post(`/api/jobs/${enc(job.key)}/fill-plan/rebuild`, {}),
    onSuccess: () => { toast.notify("Fill plan rebuilt."); qc.invalidateQueries({ queryKey: ["fill-plan", job.key] }); qc.invalidateQueries({ queryKey: ["unanswered"] }); },
    onError: toast.error,
  });
  const rebuildBtn = (
    <Button variant="secondary" icon={<RefreshCw className="size-4" />} busy={rebuild.isPending} disabled={demo}
      reason={demo ? DEMO_ACTION_TIP : undefined} onClick={() => rebuild.mutate()}>
      Rebuild plan
    </Button>
  );
  if (plan.isLoading) return <Skeleton className="mx-auto h-72 max-w-3xl" />;
  if (plan.error) return <ErrorState error={plan.error} onRetry={() => plan.refetch()} />;
  const p = plan.data;
  if (!p || !p.plan) {
    return (
      <EmptyState icon={<ListChecks />} title="No answers planned yet" action={rebuildBtn}>
        The nightly run reads this form (without typing anything) for jobs with an approved résumé, and plans an answer for each
        question from your answers bank. Approve the résumé first, or build the plan now.
      </EmptyState>
    );
  }
  const actions = p.plan.steps.flat();
  const filled = actions.filter((a) => a.approved || (!a.needs_user && a.source !== "none")).length;
  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h3 className="font-display text-2xl text-ink">Planned answers</h3>
          <p className="mt-1 text-sm text-muted">
            {p.ats ? `${p.ats.charAt(0).toUpperCase()}${p.ats.slice(1)} form` : "Form"} · {plural(filled, "field")} from your bank · {plural(actions.length - filled, "left for you", "left for you")} · planned {fmtDateTime(p.created_at)}
          </p>
        </div>
        {rebuildBtn}
      </div>
      <p className="mb-4 rounded-[12px] bg-paper-2 px-4 py-3 text-[13px] leading-relaxed text-ink-2">
        Values are filled in from your answers bank when you open the form, never stored with the plan. Only approved values are used;
        consent and attestation boxes are always left for you.
        {p.unresolved_count > 0 && <> Answer the open ones on <Link to="/needs-you" className="font-medium text-accent underline underline-offset-2">Needs you</Link> and they'll be used next time.</>}
      </p>
      {p.plan.steps.map((step, i) => (
        <section key={i} className="card mb-4 px-5">
          {p.plan!.steps.length > 1 && <h4 className="eyebrow pt-4">Page {i + 1}</h4>}
          <ul className="divide-y divide-line">{step.map((a, k) => <Row key={a.label + k} a={a} />)}</ul>
        </section>
      ))}
    </div>
  );
}
