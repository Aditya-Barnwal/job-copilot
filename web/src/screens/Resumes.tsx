import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Code2, FileDown, FileText, Layers, Upload } from "lucide-react";
import { DEMO_ACTION_TIP, api } from "../lib/api";
import { useDemo, useToast } from "../lib/app";
import { STATUS_LABEL, fmtDate, plural } from "../lib/format";
import { useTitle } from "../lib/router";
import { openReview } from "../components/JobCard";
import { Dialog } from "../components/Shell";
import { Badge, Button, CardSkeleton, EmptyState, ErrorState, LinkButton, PageHeader, SectionTitle, Skeleton } from "../components/ui";

interface Base { name: string; roles: string[]; sections: string[]; template: string | null }
interface Version { id: number; version: number; base: string | null; status: string; pages: number | null; created_at: number | null; approved_at: number | null; grounding_clean: boolean | null; pdf_url: string | null }
interface JobVersions { job_key: string; title: string | null; company: string | null; status: string | null; versions: Version[] }
interface Legacy { id: number; label: string; role_family: string | null; pdf_path: string | null; created_at?: number }
interface Meta { resume_loaded: boolean; resume_filename: string | null }

function SourceDialog({ name, onClose }: { name: string; onClose: () => void }) {
  const src = useQuery({ queryKey: ["base-src", name], queryFn: () => api<{ file: string; source: string }>(`/api/resumes/bases/${encodeURIComponent(name)}/source`) });
  return (
    <Dialog title={`${name} template`} onClose={onClose} wide>
      {src.isLoading ? <Skeleton className="h-64" /> : src.error ? <ErrorState error={src.error} compact /> : (
        <>
          <p className="mb-3 text-[13px] text-muted">{src.data!.file} · LaTeX (pdfLaTeX), filled from your confirmed facts by the tailoring step.</p>
          <pre className="scroll-thin max-h-[60vh] overflow-auto rounded-[10px] bg-paper-2 p-4 font-mono text-[12px] leading-relaxed text-ink-2">{src.data!.source}</pre>
        </>
      )}
    </Dialog>
  );
}

const STATUS_TONE: Record<string, "good" | "warn" | "neutral" | "accent" | "bad"> = {
  approved: "good", needs_review: "warn", ok: "accent", rejected: "neutral", failed: "bad", pending: "neutral",
};

export function Resumes() {
  useTitle("Résumés");
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const data = useQuery({ queryKey: ["resumes"], queryFn: () => api<{ bases: Base[]; jobs: JobVersions[]; legacy_versions: Legacy[] }>("/api/resumes") });
  const meta = useQuery({ queryKey: ["meta"], queryFn: () => api<Meta>("/api/meta") });
  const [view, setView] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: (f: File) => { const fd = new FormData(); fd.append("file", f); return api<{ filename: string; chars: number }>("/api/resume", { method: "POST", form: fd }); },
    onSuccess: (r) => { toast.notify(`Uploaded ${r.filename} (${r.chars.toLocaleString()} characters of text).`, { kind: "success" }); qc.invalidateQueries({ queryKey: ["meta"] }); },
    onError: toast.error,
  });
  const d = data.data;
  return (
    <>
      <PageHeader eyebrow="You" title="Résumés">
        Your LaTeX base résumés, and every version tailored from them, per job. Only an approved version is ever attached to an application.
      </PageHeader>
      {data.isLoading ? <CardSkeleton rows={3} /> : data.error ? <ErrorState error={data.error} onRetry={() => data.refetch()} /> : (
        <>
          <SectionTitle count={d!.bases.length}>Base résumés</SectionTitle>
          {d!.bases.length ? (
            <div className="grid gap-4 sm:grid-cols-2">
              {d!.bases.map((b) => (
                <article key={b.name} className="card p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h3 className="font-display text-[20px] text-ink">{b.name.replace(/_/g, " ")}</h3>
                      <p className="mt-1 text-[13px] text-muted">Picked for {b.roles.join(", ") || "any role"}</p>
                    </div>
                    <span className="grid size-10 place-items-center rounded-[10px] bg-accent-soft text-accent"><Layers className="size-5" aria-hidden /></span>
                  </div>
                  <p className="mt-3 text-[13px] text-ink-2">{b.sections.map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join(" · ")}</p>
                  <div className="mt-4">
                    <Button size="sm" icon={<Code2 className="size-3.5" />} disabled={!b.template} reason={!b.template ? "No template file found" : undefined} onClick={() => setView(b.name)}>
                      View template
                    </Button>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState icon={<Layers />} title="No base résumés">Add your templates to profile/base_resumes/ and list them in bases.yaml.</EmptyState>
          )}

          <SectionTitle count={d!.jobs.length}>Tailored for jobs</SectionTitle>
          {d!.jobs.length ? (
            <ul className="space-y-3">
              {d!.jobs.map((j) => (
                <li key={j.job_key} className="card p-5">
                  <div className="flex flex-wrap items-baseline justify-between gap-3">
                    <button type="button" className="text-left" onClick={() => openReview(j.job_key)}>
                      <span className="text-[13px] font-semibold text-ink-2">{j.company}</span>
                      <span className="mt-0.5 block font-display text-[18px] text-ink hover:text-accent">{j.title}</span>
                    </button>
                    {j.status && <Badge>{STATUS_LABEL[j.status] || j.status}</Badge>}
                  </div>
                  <ul className="mt-3 divide-y divide-line border-t border-line">
                    {j.versions.map((v) => (
                      <li key={v.id} className="flex flex-wrap items-center gap-3 py-2.5 text-[13px]">
                        <span className="w-10 font-display text-[15px] text-ink">v{v.version}</span>
                        <span className="text-muted">{v.base}</span>
                        <Badge tone={STATUS_TONE[v.status] || "neutral"}>{v.status.replace("_", " ")}</Badge>
                        {v.grounding_clean === false && v.status !== "approved" && <Badge tone="warn">flags</Badge>}
                        <span className="flex-1 text-faint">{fmtDate(v.created_at)}{v.pages ? ` · ${plural(v.pages, "page")}` : ""}</span>
                        {v.pdf_url ? <LinkButton size="sm" href={v.pdf_url} icon={<FileDown className="size-3.5" />}>PDF</LinkButton>
                          : <span className="text-faint">no PDF</span>}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState icon={<FileText />} title="Nothing tailored yet">Open a job from Today and tailor its résumé; each version shows up here.</EmptyState>
          )}

          {d!.legacy_versions.length > 0 && (
            <>
              <SectionTitle count={d!.legacy_versions.length}>Saved versions (all)</SectionTitle>
              <ul className="card divide-y divide-line px-5">
                {d!.legacy_versions.map((v) => (
                  <li key={v.id} className="flex items-center justify-between gap-3 py-3 text-sm">
                    <span className="text-ink-2">{v.label}</span>
                    {v.pdf_path && <LinkButton size="sm" href={`/api/resume-versions/${v.id}/download`} icon={<FileDown className="size-3.5" />}>PDF</LinkButton>}
                  </li>
                ))}
              </ul>
            </>
          )}

          <SectionTitle>Plain-text résumé</SectionTitle>
          <div className="card flex flex-wrap items-center gap-4 p-5">
            <div className="min-w-0 flex-1 text-sm">
              <p className="text-ink">{meta.data?.resume_loaded ? <>Loaded: <span className="font-medium">{meta.data.resume_filename}</span></> : "No résumé text uploaded."}</p>
              <p className="mt-1 text-[13px] text-muted">Used by the older keyword scoring and the v1 tailoring check. Tailoring v2 works from your facts file.</p>
            </div>
            <input ref={file} type="file" accept=".pdf,.txt" className="sr-only" id="resume-file" aria-label="Choose a résumé file"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
            <Button icon={<Upload className="size-4" />} busy={upload.isPending} disabled={demo} reason={demo ? DEMO_ACTION_TIP : undefined}
              onClick={() => file.current?.click()}>
              Upload PDF or text
            </Button>
          </div>
        </>
      )}
      {view && <SourceDialog name={view} onClose={() => setView(null)} />}
    </>
  );
}
