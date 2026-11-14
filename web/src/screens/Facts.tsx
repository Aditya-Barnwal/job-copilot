import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, BookUser, Check, Clock4, Trash2 } from "lucide-react";
import { DEMO_ACTION_TIP, api, post } from "../lib/api";
import { useDemo, useToast } from "../lib/app";
import { setParams, useLocation, useTitle } from "../lib/router";
import type { AnswerRow, FactRow } from "../lib/types";
import { Badge, Button, CardSkeleton, EmptyState, ErrorState, PageHeader, Segmented, cx } from "../components/ui";

type Tab = "facts" | "answers" | "quick" | "profile";
const SECTION: Record<string, string> = { education: "Education", experience: "Experience", projects: "Projects", research: "Research", hackathons: "Hackathons", skills: "Skills" };
const inputCls = "h-9 w-full rounded-[10px] border border-line-2 bg-surface px-3 text-sm text-ink outline-none placeholder:text-faint focus:border-accent-line focus:ring-2 focus:ring-accent/20 disabled:opacity-60";
// Values whose key looks like a credential or ID number are never shown.
const SECRET_PATH = /(pass(word|code)?|secret|token|api[_-]?key|otp|\bpin\b|ssn|aadha+r|\bpan\b|pan_(number|no)|passport_(number|no)|account_(number|no)|iban|ifsc|cvv|card_number)/i;

function StatusBadge({ s }: { s: string }) {
  return s === "confirmed" ? <Badge tone="good"><BadgeCheck className="size-3" />confirmed</Badge>
    : s === "pending" ? <Badge tone="neutral"><Clock4 className="size-3" />pending</Badge>
      : <Badge tone="warn">needs review</Badge>;
}

function FactsTab() {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"open" | "all">("open");
  const data = useQuery({ queryKey: ["facts"], queryFn: () => api<{ available: boolean; detail?: string; facts: FactRow[]; errors: string[]; counts: Record<string, number>; usable?: number; file?: string }>("/api/facts") });
  const confirm = useMutation({
    mutationFn: (id: string) => post(`/api/facts/${encodeURIComponent(id)}/confirm`, {}),
    onSuccess: (_r, id) => { toast.notify(`Confirmed ${id}. Tailoring can use it now.`, { kind: "success" }); qc.invalidateQueries({ queryKey: ["facts"] }); },
    onError: toast.error,
  });
  if (data.isLoading) return <CardSkeleton rows={3} />;
  if (data.error) return <ErrorState error={data.error} onRetry={() => data.refetch()} />;
  const d = data.data!;
  if (!d.available) return <EmptyState icon={<BookUser />} title="No facts file yet">{d.detail || "Create profile/resume_facts.yaml (see profile.example/)."}</EmptyState>;
  const rows = d.facts.filter((f) => filter === "all" || f.status !== "confirmed" || f.claims.some((c) => c.status !== "confirmed"));
  const groups = Object.keys(SECTION).map((s) => [s, rows.filter((r) => r.section === s)] as const).filter(([, r]) => r.length);
  return (
    <>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted">
          {d.counts.confirmed || 0} confirmed · <span className="text-warn">{d.counts.unconfirmed || 0} to review</span> · {d.counts.pending || 0} pending · {d.usable ?? 0} usable for tailoring
        </p>
        <Segmented<"open" | "all"> label="Show" value={filter} onChange={setFilter} items={[{ value: "open", label: "To review" }, { value: "all", label: "All facts" }]} />
      </div>
      {d.errors.length > 0 && (
        <div className="card mb-5 border-warn/30 bg-warn-soft/40 p-4 text-[13px] text-ink-2">
          <p className="font-medium text-warn">The facts file has {d.errors.length} problem(s):</p>
          <ul className="mt-1 list-disc pl-5">{d.errors.slice(0, 6).map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
      {!groups.length ? (
        <EmptyState icon={<BadgeCheck />} title="Everything is confirmed">Every line tailoring can use has been checked by you.</EmptyState>
      ) : groups.map(([s, items]) => (
        <section key={s} className="mb-8">
          <h3 className="mb-3 font-display text-[20px] text-ink">{SECTION[s]}</h3>
          <ul className="card divide-y divide-line">
            {items.map((f) => (
              <li key={f.id} className={cx("flex flex-wrap items-start gap-4 px-5 py-4", f.kind === "bullet" && "pl-10")}>
                <div className="min-w-0 flex-1">
                  <p className={cx("text-[14px] leading-relaxed", f.kind === "bullet" ? "text-ink-2" : "font-medium text-ink")}>{f.text}</p>
                  <p className="mt-1 text-[12px] text-faint">
                    <code>{f.id}</code>{f.sources.length ? ` · from ${f.sources.join(", ")}` : ""}
                    {f.pending_until && ` · pending until ${f.pending_until}${f.pending_reason ? `: ${f.pending_reason}` : ""}`}
                  </p>
                  {f.claims.filter((c) => filter === "all" || c.status !== "confirmed").map((c) => (
                    <div key={c.id} className="mt-2 flex flex-wrap items-center gap-2 rounded-[8px] bg-paper-2/70 px-3 py-2 text-[13px]">
                      <span className="flex-1 text-ink-2">“{c.text || c.tech.join(", ")}”</span>
                      <StatusBadge s={c.status} />
                      {c.status === "unconfirmed" && (
                        <Button size="sm" variant="secondary" className="h-7" disabled={demo} reason={demo ? DEMO_ACTION_TIP : undefined}
                          busy={confirm.isPending && confirm.variables === c.id} onClick={() => confirm.mutate(c.id)}>Confirm</Button>
                      )}
                    </div>
                  ))}
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge s={f.status} />
                  {f.status === "unconfirmed" && (
                    <Button size="sm" variant="primary" icon={<Check className="size-3.5" />} disabled={demo} reason={demo ? DEMO_ACTION_TIP : undefined}
                      busy={confirm.isPending && confirm.variables === f.id} onClick={() => confirm.mutate(f.id)}>
                      It's true
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
      <p className="text-[13px] leading-relaxed text-muted">Confirming changes only that line's <code>status:</code> in {d.file || "resume_facts.yaml"}. Pending lines (things that aren't true yet) stay pending until you edit the file.</p>
    </>
  );
}

function fmtValue(path: string, v: unknown): { text: string; secret: boolean } {
  if (SECRET_PATH.test(path)) return { text: "hidden", secret: true };
  if (v == null || v === "") return { text: "—", secret: false };
  if (typeof v === "boolean") return { text: v ? "Yes" : "No", secret: false };
  if (Array.isArray(v)) return { text: v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(", "), secret: false };
  if (typeof v === "object") return { text: JSON.stringify(v), secret: false };
  return { text: String(v), secret: false };
}

function AnswersTab() {
  const [f, setF] = useState<"all" | "approved" | "proposed" | "empty">("all");
  const data = useQuery({ queryKey: ["answers-review"], queryFn: () => api<{ path: string | null; rows: AnswerRow[]; counts: Record<string, number> }>("/api/answers/review") });
  if (data.isLoading) return <CardSkeleton rows={3} />;
  if (data.error) return <ErrorState error={data.error} onRetry={() => data.refetch()} />;
  const d = data.data!;
  const rows = d.rows.filter((r) => f === "all" || (f === "empty" ? r.empty : !r.empty && r.status === f));
  const name = d.path ? d.path.split(/[\\/]/).pop() : "answers.yaml";
  return (
    <>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted">{d.counts.approved || 0} approved · <span className="text-warn">{d.counts.proposed || 0} need review</span> · {d.counts.empty || 0} empty · from {name}</p>
        <Segmented label="Filter answers" value={f} onChange={setF}
          items={[{ value: "all", label: "All" }, { value: "approved", label: "Approved" }, { value: "proposed", label: "Needs review" }, { value: "empty", label: "Empty" }]} />
      </div>
      <div className="card overflow-x-auto">
        <table className="w-full text-left text-[13px]">
          <caption className="sr-only">Answers autofill can use</caption>
          <thead className="border-b border-line text-[12px] uppercase tracking-wider text-faint">
            <tr><th scope="col" className="px-5 py-3 font-semibold">Field</th><th scope="col" className="px-5 py-3 font-semibold">Value</th><th scope="col" className="px-5 py-3 font-semibold">Status</th></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((r) => {
              const v = fmtValue(r.path, r.value);
              const st = r.empty ? "empty" : r.status;
              return (
                <tr key={r.path} className="align-top">
                  <td className="px-5 py-2.5 font-mono text-[12px] text-ink-2">{r.path}{r.derived && <span className="ml-1 font-sans text-faint">(derived)</span>}</td>
                  <td className={cx("max-w-[420px] break-words px-5 py-2.5", v.secret ? "italic text-faint" : "text-ink")}>{v.text}</td>
                  <td className="px-5 py-2.5"><Badge tone={st === "approved" ? "good" : st === "proposed" ? "warn" : "neutral"}>{st === "proposed" ? "needs review" : st || "—"}</Badge></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-4 text-[13px] text-muted">Approve a block by putting <code># approved</code> after its key in {name}. Autofill never uses a value that isn't approved.</p>
    </>
  );
}

function QuickAnswers() {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const data = useQuery({ queryKey: ["answer-pack"], queryFn: () => api<{ items: { q: string; a: string }[] }>("/api/answer-pack") });
  const [q, setQ] = useState(""), [a, setA] = useState("");
  const add = useMutation({ mutationFn: () => post("/api/answer-pack", { q, a }), onSuccess: () => { setQ(""); setA(""); qc.invalidateQueries({ queryKey: ["answer-pack"] }); }, onError: toast.error });
  const del = useMutation({ mutationFn: (key: string) => api(`/api/answer-pack?q=${encodeURIComponent(key)}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["answer-pack"] }), onError: toast.error });
  return (
    <>
      <p className="mb-5 text-sm text-muted">Snippets you copy by hand into forms (the answers bank is what autofill uses).</p>
      <form className="card mb-5 grid gap-3 p-5 sm:grid-cols-[1fr_1.4fr_auto]" onSubmit={(e) => { e.preventDefault(); if (q.trim() && a.trim()) add.mutate(); }}>
        <label className="sr-only" htmlFor="qa-q">Question</label>
        <input id="qa-q" className={inputCls} placeholder="Question" value={q} onChange={(e) => setQ(e.target.value)} disabled={demo} />
        <label className="sr-only" htmlFor="qa-a">Answer</label>
        <input id="qa-a" className={inputCls} placeholder="Answer" value={a} onChange={(e) => setA(e.target.value)} disabled={demo} />
        <Button type="submit" variant="primary" busy={add.isPending} disabled={demo || !q.trim() || !a.trim()} reason={demo ? DEMO_ACTION_TIP : undefined}>Add</Button>
      </form>
      {data.isLoading ? <CardSkeleton rows={2} /> : data.error ? <ErrorState error={data.error} /> : !data.data!.items.length ? (
        <p className="text-sm text-faint">No quick answers yet.</p>
      ) : (
        <ul className="card divide-y divide-line">
          {data.data!.items.map((it) => (
            <li key={it.q} className="flex items-start gap-4 px-5 py-3.5 text-sm">
              <div className="min-w-0 flex-1"><p className="font-medium text-ink">{it.q}</p><p className="mt-0.5 text-ink-2">{it.a}</p></div>
              {!demo && <Button size="sm" variant="danger" icon={<Trash2 className="size-3.5" />} onClick={() => del.mutate(it.q)} aria-label={`Delete ${it.q}`}>Delete</Button>}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

const PROFILE_FIELDS: [string, string][] = [["first_name", "First name"], ["last_name", "Last name"], ["email", "Email"], ["phone", "Phone"], ["linkedin_url", "LinkedIn"], ["portfolio_url", "Portfolio / GitHub"]];

function ProfileTab() {
  const demo = useDemo();
  const toast = useToast();
  const qc = useQueryClient();
  const prof = useQuery({ queryKey: ["profile"], queryFn: () => api<Record<string, string>>("/api/profile") });
  const yoe = useQuery({ queryKey: ["user-yoe"], queryFn: () => api<{ user_yoe: number | null }>("/api/user-yoe") });
  const style = useQuery({ queryKey: ["style"], queryFn: () => api<{ v: string }>("/api/meta-kv?k=user_email_style") });
  const [p, setP] = useState<Record<string, string>>({});
  const [y, setY] = useState("");
  const [s, setS] = useState("");
  useEffect(() => { if (prof.data) setP(prof.data); }, [prof.data]);
  useEffect(() => { if (yoe.data) setY(yoe.data.user_yoe == null ? "" : String(yoe.data.user_yoe)); }, [yoe.data]);
  useEffect(() => { if (style.data) setS(style.data.v || ""); }, [style.data]);
  const saveP = useMutation({ mutationFn: () => post("/api/profile", Object.fromEntries(PROFILE_FIELDS.map(([k]) => [k, p[k] || ""]))), onSuccess: () => { toast.notify("Profile saved."); qc.invalidateQueries({ queryKey: ["profile"] }); }, onError: toast.error });
  const saveY = useMutation({ mutationFn: () => post("/api/user-yoe", { user_yoe: y === "" ? null : Number(y) }), onSuccess: () => { toast.notify("Saved."); qc.invalidateQueries(); }, onError: toast.error });
  const saveS = useMutation({ mutationFn: () => post("/api/meta-kv", { k: "user_email_style", v: s }), onSuccess: () => toast.notify("Writing sample saved."), onError: toast.error });
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <form className="card space-y-3 p-5" onSubmit={(e) => { e.preventDefault(); saveP.mutate(); }}>
        <h3 className="font-display text-[19px] text-ink">Contact details</h3>
        <p className="text-[13px] text-muted">Shown in the copy-by-hand pack. Autofill reads your answers bank instead.</p>
        {PROFILE_FIELDS.map(([k, label]) => (
          <div key={k}>
            <label htmlFor={`p-${k}`} className="mb-1 block text-[12px] font-medium text-muted">{label}</label>
            <input id={`p-${k}`} className={inputCls} value={p[k] || ""} onChange={(e) => setP({ ...p, [k]: e.target.value })} disabled={demo} />
          </div>
        ))}
        <Button type="submit" variant="primary" busy={saveP.isPending} disabled={demo} reason={demo ? DEMO_ACTION_TIP : undefined}>Save details</Button>
      </form>
      <div className="space-y-6">
        <form className="card space-y-3 p-5" onSubmit={(e) => { e.preventDefault(); saveY.mutate(); }}>
          <h3 className="font-display text-[19px] text-ink">Years of experience</h3>
          <p className="text-[13px] text-muted">Used to flag roles asking for more than you have.</p>
          <label htmlFor="yoe" className="sr-only">Years of experience</label>
          <input id="yoe" type="number" min={0} max={50} className={inputCls + " max-w-[140px]"} value={y} onChange={(e) => setY(e.target.value)} />
          <Button type="submit" busy={saveY.isPending}>Save</Button>
        </form>
        <form className="card space-y-3 p-5" onSubmit={(e) => { e.preventDefault(); saveS.mutate(); }}>
          <h3 className="font-display text-[19px] text-ink">Your writing voice</h3>
          <p className="text-[13px] text-muted">A short sample of how you write, for drafted emails.</p>
          <label htmlFor="style" className="sr-only">Writing sample</label>
          <textarea id="style" rows={5} className={inputCls + " h-auto py-2"} value={s} onChange={(e) => setS(e.target.value)} />
          <Button type="submit" busy={saveS.isPending}>Save sample</Button>
        </form>
      </div>
    </div>
  );
}

export function Facts() {
  useTitle("Facts & answers");
  const { search } = useLocation();
  const tab = (["facts", "answers", "quick", "profile"].includes(search.get("section") || "") ? search.get("section") : "facts") as Tab;
  return (
    <>
      <PageHeader eyebrow="You" title="Facts & answers">
        Tailoring uses only résumé facts you've confirmed; autofill uses only answers you've approved. Review both here.
      </PageHeader>
      <Segmented<Tab> className="mb-7" label="Sections" value={tab} onChange={(t) => setParams({ section: t === "facts" ? null : t }, { replace: true })}
        items={[{ value: "facts", label: "Résumé facts" }, { value: "answers", label: "Answers bank" }, { value: "quick", label: "Quick answers" }, { value: "profile", label: "Profile" }]} />
      {tab === "facts" ? <FactsTab /> : tab === "answers" ? <AnswersTab /> : tab === "quick" ? <QuickAnswers /> : <ProfileTab />}
    </>
  );
}
