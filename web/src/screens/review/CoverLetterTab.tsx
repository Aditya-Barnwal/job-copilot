import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { CircleAlert, Copy, Mail, Sparkles } from "lucide-react";
import { DEMO_TAILOR_TIP, enc, post } from "../../lib/api";
import { useDemo, useToast } from "../../lib/app";
import type { Draft, JobDetail } from "../../lib/types";
import { Badge, Button, EmptyState } from "../../components/ui";

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

export function CoverLetterTab({ job }: { job: JobDetail }) {
  const demo = useDemo();
  const toast = useToast();
  const [text, setText] = useState(() => loadDraft(job.key));
  const [meta, setMeta] = useState<Draft | null>(null);
  useEffect(() => { setText(loadDraft(job.key)); setMeta(null); }, [job.key]);
  useEffect(() => { saveDraft(job.key, text); }, [job.key, text]);
  const gen = useMutation({
    mutationFn: () => post<Draft>(`/api/jobs/${enc(job.key)}/cover-letter`, {}),
    onSuccess: (d) => { setMeta(d); setText(d.text); },
    onError: toast.error,
  });
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  const flags = Object.entries(meta?.grounding?.flags || {}).filter(([, v]) => v && v.length);

  if (!text && !gen.isPending) {
    return (
      <div className="mx-auto max-w-2xl">
        <EmptyState icon={<Mail />} title="No cover letter"
          action={<Button variant="primary" icon={<Sparkles className="size-4" />} disabled={demo} reason={demo ? DEMO_TAILOR_TIP : undefined} onClick={() => gen.mutate()}>Draft a cover letter</Button>}>
          Most forms don't ask for one, so none is made by default. When this one does, draft it here: at most 180 words,
          from your confirmed facts only, with no clichés and no new numbers or names.
        </EmptyState>
      </div>
    );
  }
  return (
    <div className="mx-auto grid max-w-4xl gap-6 lg:grid-cols-[minmax(0,1fr)_260px]">
      <div className="card p-6">
        <label htmlFor="letter" className="eyebrow">Cover letter</label>
        <textarea id="letter" value={text} onChange={(e) => setText(e.target.value)} rows={16}
          placeholder={gen.isPending ? "Drafting from your confirmed facts…" : ""}
          className="mt-3 w-full resize-y rounded-[10px] border border-line bg-surface-2 p-4 font-display text-[16px] leading-[1.75] text-ink outline-none focus:border-accent-line focus:ring-2 focus:ring-accent/20" />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button variant="secondary" icon={<Copy className="size-4" />} onClick={() => navigator.clipboard.writeText(text).then(() => toast.notify("Copied."), () => toast.notify("Your browser blocked the clipboard.", { kind: "error" }))}>Copy</Button>
          <Button variant="ghost" icon={<Sparkles className="size-4" />} busy={gen.isPending} disabled={demo} reason={demo ? DEMO_TAILOR_TIP : undefined} onClick={() => gen.mutate()}>Redraft</Button>
          <span className="flex-1" />
          <span className={words > 180 ? "text-[13px] font-medium text-warn" : "text-[13px] text-muted"}>{words} / 180 words</span>
        </div>
      </div>
      <aside className="space-y-4 text-sm">
        <div className="card p-4">
          <div className="eyebrow mb-2">Checks</div>
          {meta ? (
            <ul className="space-y-2 text-ink-2">
              <li className="flex items-center justify-between">Grounded in facts <Badge tone={meta.grounding?.clean ? "good" : "warn"}>{meta.grounding?.clean ? "yes" : "review"}</Badge></li>
              <li className="flex items-center justify-between">Status <Badge tone={meta.status === "ok" ? "good" : "warn"}>{meta.status.replace("_", " ")}</Badge></li>
            </ul>
          ) : (
            <p className="text-muted">Checks run when the letter is drafted. Your own edits are kept on this device only.</p>
          )}
          {flags.map(([k, v]) => (
            <p key={k} className="mt-2 flex gap-1.5 text-[13px] text-warn"><CircleAlert className="mt-0.5 size-3.5 shrink-0" />{k.replace(/_/g, " ")}: {v.join(", ")}</p>
          ))}
          {meta?.warnings?.map((w) => <p key={w} className="mt-2 text-[13px] text-warn">{w}</p>)}
        </div>
        <p className="px-1 text-[13px] leading-relaxed text-muted">Paste it into the form's cover-letter box. Autofill only attaches a cover letter file you approved.</p>
      </aside>
    </div>
  );
}
