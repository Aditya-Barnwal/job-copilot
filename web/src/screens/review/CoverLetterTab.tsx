import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Copy, FileText, Mail, Save, ShieldCheck, Sparkles } from "lucide-react";
import { ApiError, DEMO_ACTION_TIP, DEMO_TAILOR_TIP, api, enc, post } from "../../lib/api";
import { useDemo, useToast } from "../../lib/app";
import { fmtDateTime } from "../../lib/format";
import type { CoverLetterState, Draft, LetterChecks } from "../../lib/types";
import type { JobDetail } from "../../lib/types";
import { Badge, Button, EmptyState, LinkButton } from "../../components/ui";

// localStorage holds only an UNSAVED draft; the saved letter lives on the server
// (data/applications/<job>/v<N>/), and autofill uses it only once you approve it.
const storeKey = (k: string) => `jc-letter:${k}`;

function loadDraft(key: string): string {
  try { return localStorage.getItem(storeKey(key)) || ""; } catch { return ""; }
}
function saveDraft(key: string, text: string) {
  try {
    if (text) localStorage.setItem(storeKey(key), text);
    else localStorage.removeItem(storeKey(key));
  } catch { /* storage blocked: the draft just isn't remembered */ }
}

function StatusBadge({ st, dirty }: { st?: CoverLetterState; dirty: boolean }) {
  if (dirty) return <Badge tone="warn">Unsaved changes</Badge>;
  if (!st?.saved) return <Badge tone="outline">Not saved</Badge>;
  if (st.approved && st.usable) return <Badge tone="good">Approved</Badge>;
  if (st.approved) return <Badge tone="accent">Approved, waiting on the résumé</Badge>;
  return <Badge tone="warn">Saved, needs your approval</Badge>;
}

function Checks({ checks }: { checks: LetterChecks | Draft }) {
  const flags = Object.entries(checks.grounding?.flags || {}).filter(([, v]) => v && v.length);
  return (
    <>
      <ul className="space-y-2 text-ink-2">
        <li className="flex items-center justify-between">Grounded in facts <Badge tone={checks.grounding?.clean ? "good" : "warn"}>{checks.grounding?.clean ? "yes" : "review"}</Badge></li>
        <li className="flex items-center justify-between">Checks <Badge tone={checks.status === "ok" ? "good" : "warn"}>{checks.status === "ok" ? "passed" : "flagged"}</Badge></li>
      </ul>
      {flags.map(([k, v]) => (
        <p key={k} className="mt-2 flex gap-1.5 text-[13px] text-warn"><CircleAlert className="mt-0.5 size-3.5 shrink-0" />{k.replace(/_/g, " ")}: {v.join(", ")}</p>
      ))}
      {checks.warnings?.map((w) => <p key={w} className="mt-2 text-[13px] text-warn">{w}</p>)}
    </>
  );
}

export function CoverLetterTab({ job }: { job: JobDetail }) {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const qk = ["cover-letter", job.key];
  const saved = useQuery({
    queryKey: qk,
    queryFn: () => api<CoverLetterState>(`/api/jobs/${enc(job.key)}/cover-letter/saved`),
    enabled: !demo,
  });
  const st = saved.data;
  const [text, setText] = useState(() => loadDraft(job.key));
  const [meta, setMeta] = useState<Draft | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [writing, setWriting] = useState(false);
  useEffect(() => { setText(loadDraft(job.key)); setMeta(null); setHydrated(false); setConfirmed(false); setWriting(false); }, [job.key]);
  // show the saved letter once it arrives, unless an unsaved draft is waiting
  useEffect(() => {
    if (hydrated || !st) return;
    if (!loadDraft(job.key) && st.saved && st.text) setText(st.text);
    setHydrated(true);
  }, [st, hydrated, job.key]);
  const savedText = st?.saved ? st.text || "" : "";
  const dirty = text.trim() !== "" && text !== savedText;
  useEffect(() => { saveDraft(job.key, dirty ? text : ""); }, [job.key, text, dirty]);
  useEffect(() => setConfirmed(false), [st?.sha256]);

  const gen = useMutation({
    mutationFn: () => post<Draft>(`/api/jobs/${enc(job.key)}/cover-letter`, {}),
    onSuccess: (d) => { setMeta(d); setText(d.text); },
    onError: toast.error,
  });
  const save = useMutation({
    mutationFn: () => post<CoverLetterState>(`/api/jobs/${enc(job.key)}/cover-letter/save`, { text }),
    onSuccess: (s) => {
      qc.setQueryData(qk, s);
      setText(s.text || "");
      setMeta(null);
      toast.notify(s.checks?.status === "ok" ? "Saved. Approve it to let autofill use it." : "Saved with flags: review them before you approve.",
        { kind: s.checks?.status === "ok" ? "success" : "info" });
    },
    onError: toast.error,
  });
  const approve = useMutation({
    mutationFn: () => post<CoverLetterState>(`/api/jobs/${enc(job.key)}/cover-letter/approve`,
      { sha256: st?.sha256, confirm_flags: confirmed }),
    onSuccess: (s) => {
      qc.setQueryData(qk, s);
      toast.notify(s.usable ? "Cover letter approved: autofill will use it." : "Cover letter approved. Autofill uses it once this résumé version is approved.", { kind: "success" });
    },
    onError: (e) => {
      const d = e instanceof ApiError ? (e.payload as { detail?: unknown })?.detail : null;
      if (d && typeof d === "object" && (d as CoverLetterState).needs_confirmation) {
        qc.setQueryData(qk, { ...st, ...(d as CoverLetterState) });
        toast.notify("The checks flagged this letter: confirm you've reviewed each flag, then approve.", { kind: "error" });
      } else {
        toast.error(e);
        qc.invalidateQueries({ queryKey: qk });
      }
    },
  });

  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  const maxWords = st?.checks?.max_words || 180;
  const noArtifact = !!st && st.artifact_id == null;
  const saveReason = demo ? DEMO_ACTION_TIP
    : noArtifact ? "Tailor a résumé for this job first: the letter is saved with that version"
    : !dirty ? "Nothing new to save" : undefined;
  const flagged = !!st?.checks && st.checks.status !== "ok";
  const canApprove = !!st?.saved && !st.approved && !dirty;

  if (!text && !gen.isPending && !st?.earlier && !writing) {
    return (
      <div className="mx-auto max-w-2xl">
        <EmptyState icon={<Mail />} title="No cover letter"
          action={<div className="flex flex-wrap justify-center gap-2">
            <Button variant="primary" icon={<Sparkles className="size-4" />} disabled={demo} reason={demo ? DEMO_TAILOR_TIP : undefined} onClick={() => gen.mutate()}>Draft a cover letter</Button>
            <Button variant="secondary" onClick={() => setWriting(true)}>Write it yourself</Button>
          </div>}>
          Most forms don't ask for one, so none is made by default. When this one does, draft it here: short,
          from your confirmed facts only, with no clichés and no new numbers or names. Save it and approve it, and
          autofill attaches it (or pastes it into the form's cover-letter box).
        </EmptyState>
      </div>
    );
  }
  return (
    <div className="mx-auto grid max-w-4xl gap-6 lg:grid-cols-[minmax(0,1fr)_260px]">
      <div className="card p-6">
        <div className="flex items-center justify-between gap-2">
          <label htmlFor="letter" className="eyebrow">Cover letter</label>
          <StatusBadge st={st} dirty={dirty} />
        </div>
        {st?.earlier && !st.saved && !text && (
          <p className="mt-3 rounded-[10px] bg-paper-2 px-3 py-2 text-[13px] text-ink-2">
            You saved a letter with résumé version {st.earlier.artifact_version}.{" "}
            <button type="button" className="font-medium text-accent-ink underline" onClick={() => setText(st.earlier!.text)}>Use it here</button>, then save it with this version.
          </p>
        )}
        <textarea id="letter" value={text} onChange={(e) => setText(e.target.value)} rows={16}
          placeholder={gen.isPending ? "Drafting from your confirmed facts…" : ""}
          className="mt-3 w-full resize-y rounded-[10px] border border-line bg-surface-2 p-4 font-display text-[16px] leading-[1.75] text-ink outline-none focus:border-accent-line focus:ring-2 focus:ring-accent/20" />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button variant="primary" icon={<Save className="size-4" />} busy={save.isPending} disabled={!!saveReason} reason={saveReason} onClick={() => save.mutate()}>Save</Button>
          <Button variant="secondary" icon={<Copy className="size-4" />} onClick={() => navigator.clipboard.writeText(text).then(() => toast.notify("Copied."), () => toast.notify("Your browser blocked the clipboard.", { kind: "error" }))}>Copy</Button>
          <Button variant="ghost" icon={<Sparkles className="size-4" />} busy={gen.isPending} disabled={demo} reason={demo ? DEMO_TAILOR_TIP : undefined} onClick={() => gen.mutate()}>Redraft</Button>
          <span className="flex-1" />
          <span className={words > maxWords ? "text-[13px] font-medium text-warn" : "text-[13px] text-muted"}>{words} / {maxWords} words</span>
        </div>
      </div>
      <aside className="space-y-4 text-sm">
        <div className="card p-4">
          <div className="eyebrow mb-2">{dirty ? "Draft checks" : "Checks on the saved letter"}</div>
          {dirty ? (
            meta && meta.text === text ? <Checks checks={meta} />
              : <p className="text-muted">Your edits are checked when you save: the same grounding, length and cliché checks as a draft. Flagged text is never changed for you.</p>
          ) : st?.checks ? <Checks checks={st.checks} /> : <p className="text-muted">Save the letter to run the checks.</p>}
          {!dirty && st?.notes?.map((n) => <p key={n} className="mt-2 text-[13px] text-muted">{n}</p>)}
        </div>
        <div className="card space-y-3 p-4">
          <div className="eyebrow">Approval</div>
          {st?.approved ? (
            <p className="flex gap-2 text-[13px] text-good"><ShieldCheck className="size-4 shrink-0" aria-hidden />
              {st.usable ? `Approved ${fmtDateTime(st.approved_at)}. Autofill attaches this letter and pastes it into a cover-letter box.`
                : `Approved. Autofill uses it once résumé version ${st.artifact_version} is approved.`}
            </p>
          ) : (
            <p className="text-[13px] text-muted">Autofill never uses a letter you haven't approved. Saving new text always needs a fresh approval.</p>
          )}
          {canApprove && flagged && (
            <label className="flex items-start gap-2 text-[13px] text-ink-2">
              <input type="checkbox" className="mt-0.5" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
              I've reviewed every flag above and this letter is accurate.
            </label>
          )}
          {canApprove && (
            <Button variant="primary" icon={<ShieldCheck className="size-4" />} busy={approve.isPending}
              disabled={demo || (flagged && !confirmed)} reason={demo ? DEMO_ACTION_TIP : flagged && !confirmed ? "Confirm you've reviewed the flags first" : undefined}
              onClick={() => approve.mutate()}>Approve cover letter</Button>
          )}
          {st?.saved && dirty && <p className="text-[13px] text-warn">Save your changes first; approval covers the saved text only.</p>}
          {st?.pdf_url && st.artifact_id != null && !dirty && <LinkButton href={`/api/artifacts/${st.artifact_id}/cover-letter/pdf`} size="sm" icon={<FileText className="size-4" />}>Open the PDF</LinkButton>}
        </div>
      </aside>
    </div>
  );
}
