import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, EyeOff, Filter, Search, Telescope, Undo2 } from "lucide-react";
import { api, enc, post } from "../lib/api";
import { useHotkeys, useOverlayOpen, useToast } from "../lib/app";
import { RULE_LABEL, STATUS_LABEL, fmtDateTime, plural, relTime } from "../lib/format";
import { setParams, useLocation, useTitle } from "../lib/router";
import type { JobCard, Paged } from "../lib/types";
import { JobList, openReview } from "../components/JobCard";
import { Button, CardSkeleton, EmptyState, ErrorState, Kbd, PageHeader, Segmented, cx } from "../components/ui";

const PAGE = 20;

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[12px] font-medium text-muted">{label}</label>
      {children}
    </div>
  );
}

const inputCls = "h-9 rounded-[10px] border border-line-2 bg-surface px-3 text-sm text-ink placeholder:text-faint outline-none transition focus:border-accent-line focus:ring-2 focus:ring-accent/20";

/** All matches (lane=main|all) and the Watchlist (lane=extended) share this screen. */
export function Matches({ watchlist = false }: { watchlist?: boolean }) {
  useTitle(watchlist ? "Watchlist" : "All matches");
  const { search } = useLocation();
  const overlay = useOverlayOpen();
  const q = search.get("q") || "";
  const sort = (search.get("sort") || "fit") as "fit" | "age";
  const tier = search.get("tier") || "";
  const offset = Number(search.get("offset") || 0);
  const [text, setText] = useState(q);
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => setText(q), [q]);
  useHotkeys({ "/": (e) => { e.preventDefault(); box.current?.focus(); } }, !overlay);
  const lane = watchlist ? "extended" : "main";
  const params = new URLSearchParams({ lane, sort, limit: String(PAGE), offset: String(offset) });
  if (q) params.set("q", q);
  if (tier) params.set("tier", tier);
  const data = useQuery({
    queryKey: ["matches", params.toString()],
    queryFn: () => api<Paged<JobCard>>(`/api/matches?${params}`),
    placeholderData: keepPreviousData,
  });
  const d = data.data;
  const apply = () => setParams({ q: text.trim() || null, offset: null }, { replace: true });

  return (
    <>
      <PageHeader eyebrow={watchlist ? "Browse" : "Browse"} title={watchlist ? "Watchlist" : "All matches"}>
        {watchlist
          ? "Roles that pass your rules at companies we couldn't verify as a brand or funded startup yet. Good ones are worth a closer look."
          : "Every role that passed your rules, not just today's shortlist. Search, sort and open any of them."}
      </PageHeader>
      <form className="card mb-6 flex flex-wrap items-end gap-3 p-4" role="search" onSubmit={(e) => { e.preventDefault(); apply(); }}>
        <div className="relative min-w-[220px] flex-1">
          <Field id="m-q" label="Search">
            <div className="relative">
              <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-faint" />
              <input ref={box} id="m-q" value={text} onChange={(e) => setText(e.target.value)} placeholder="Title, company or skill   ( / )"
                className={cx(inputCls, "w-full pl-9")} />
            </div>
          </Field>
        </div>
        {!watchlist && (
          <Field id="m-tier" label="Company tier">
            <select id="m-tier" value={tier} onChange={(e) => setParams({ tier: e.target.value || null, offset: null }, { replace: true })} className={inputCls}>
              <option value="">Any</option>
              <option value="brand">Brand</option>
              <option value="funded">Funded</option>
            </select>
          </Field>
        )}
        <div className="flex flex-col gap-1">
          <span className="text-[12px] font-medium text-muted" id="sort-l">Sort</span>
          <Segmented<"fit" | "age"> label="Sort" value={sort} onChange={(v) => setParams({ sort: v === "fit" ? null : v, offset: null }, { replace: true })}
            items={[{ value: "fit", label: "Best fit" }, { value: "age", label: "Newest" }]} />
        </div>
        <Button type="submit" variant="secondary" icon={<Filter className="size-4" />}>Apply</Button>
      </form>
      {data.isLoading ? (
        <CardSkeleton rows={5} />
      ) : data.error ? (
        <ErrorState error={data.error} onRetry={() => data.refetch()} />
      ) : !d || !d.items.length ? (
        <EmptyState icon={<Telescope />} title={q || tier ? "Nothing matches that" : watchlist ? "The watchlist is empty" : "No matches yet"}>
          {q || tier ? "Try a broader search or clear the filters." : "The nightly run fills this in."}
        </EmptyState>
      ) : (
        <>
          <p className="mb-3 text-[13px] text-muted" aria-live="polite">{d.offset + 1}–{d.offset + d.items.length} of {d.total}</p>
          <JobList jobs={d.items} />
          <nav aria-label="Pages" className="mt-8 flex items-center justify-center gap-3">
            <Button variant="ghost" icon={<ChevronLeft className="size-4" />} disabled={d.offset <= 0}
              onClick={() => { setParams({ offset: String(Math.max(0, d.offset - PAGE)) }); window.scrollTo({ top: 0 }); }}>Previous</Button>
            <span className="text-[13px] tabular-nums text-muted">Page {Math.floor(d.offset / PAGE) + 1} of {Math.max(1, Math.ceil(d.total / PAGE))}</span>
            <Button variant="ghost" disabled={d.offset + d.items.length >= d.total}
              onClick={() => { setParams({ offset: String(d.offset + PAGE) }); window.scrollTo({ top: 0 }); }}>Next<ChevronRight className="size-4" /></Button>
          </nav>
        </>
      )}
    </>
  );
}

type HiddenView = "rules" | "mine";

export function Hidden() {
  useTitle("Hidden roles");
  const { search } = useLocation();
  const view: HiddenView = search.get("view") === "mine" ? "mine" : "rules";
  const mine = useQuery({ queryKey: ["hidden-mine"], queryFn: () => api<{ items: HiddenJob[]; total: number }>("/api/hidden") });
  const rulesTotal = useQuery({ queryKey: ["excluded", "", 1], queryFn: () => api<Paged<JobCard>>("/api/excluded?limit=1") });
  return (
    <>
      <PageHeader eyebrow="Browse" title="Hidden roles">
        Nothing disappears silently: roles your rules filtered out, with the reason and the words that triggered it, and the
        roles you hid yourself, which you can bring back.
      </PageHeader>
      <Segmented<HiddenView> className="mb-7" label="Hidden roles" value={view}
        onChange={(v) => setParams({ view: v === "rules" ? null : v, rule: null }, { replace: true })}
        items={[
          { value: "rules", label: "Filtered by your rules", count: rulesTotal.data?.total },
          { value: "mine", label: "Hidden by you", count: mine.data?.total },
        ]} />
      {view === "rules" ? <FilteredByRules /> : <HiddenByYou q={mine} />}
    </>
  );
}

interface HiddenJob extends JobCard { hidden_at: number | null; previous_status: string }

function HiddenByYou({ q }: { q: ReturnType<typeof useQuery<{ items: HiddenJob[]; total: number }>> }) {
  const qc = useQueryClient();
  const toast = useToast();
  const unhide = useMutation({
    mutationFn: (j: HiddenJob) => post<{ status: string }>(`/api/jobs/${enc(j.key)}/unhide`),
    onSuccess: (r, j) => {
      toast.notify(`“${j.title}” is back${r.status === "found" ? " on your list" : ` in ${STATUS_LABEL[r.status] || r.status}`}.`, { kind: "success" });
      qc.invalidateQueries();
    },
    onError: toast.error,
  });
  if (q.isLoading) return <CardSkeleton rows={3} />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const items = q.data?.items || [];
  if (!items.length) {
    return (
      <EmptyState icon={<EyeOff />} title="You haven't hidden anything">
        Use “Not interested” on a card (or press <Kbd>x</Kbd>) to clear a role off your lists. It shows up here, ready to unhide.
      </EmptyState>
    );
  }
  return (
    <ul className="card divide-y divide-line" aria-label="Hidden by you">
      {items.map((j) => (
        <li key={j.key} id={`hidden-${j.key}`} className="flex flex-wrap items-center gap-4 px-5 py-4">
          <div className="min-w-0 flex-1">
            <p className="text-[13px] font-semibold tracking-wide text-ink-2">{j.company}</p>
            <button type="button" onClick={() => openReview(j.key)}
              className="mt-0.5 text-left font-display text-[18px] leading-snug text-ink underline-offset-4 hover:text-accent hover:underline">
              {j.title}
            </button>
            <p className="mt-1 text-[13px] text-muted">
              Hidden {relTime(j.hidden_at)}<span className="sr-only"> ({fmtDateTime(j.hidden_at)})</span>
              {j.previous_status && j.previous_status !== "found" && <> · was {STATUS_LABEL[j.previous_status] || j.previous_status}</>}
              {j.location && <> · {j.location}</>}
            </p>
          </div>
          <Button size="sm" variant="ghost" onClick={() => openReview(j.key)}>Review</Button>
          <Button size="sm" variant="secondary" icon={<Undo2 className="size-3.5" />} busy={unhide.isPending && unhide.variables?.key === j.key}
            onClick={() => unhide.mutate(j)} aria-label={`Unhide ${j.title} at ${j.company}`}>
            Unhide
          </Button>
        </li>
      ))}
    </ul>
  );
}

function FilteredByRules() {
  const { search } = useLocation();
  const rule = search.get("rule") || "";
  const [limit, setLimit] = useState(50);
  const data = useQuery({
    queryKey: ["excluded", rule, limit],
    queryFn: () => api<Paged<JobCard>>(`/api/excluded?limit=${limit}${rule ? `&rule=${encodeURIComponent(rule)}` : ""}`),
    placeholderData: keepPreviousData,
  });
  const d = data.data;
  const byRule = d?.by_rule || {};
  const total = Object.values(byRule).reduce((a, b) => a + b, 0);
  return (
    <>
      <p className="mb-4 text-[14px] text-muted">Filtered out by config/filters.yaml. Each shows the rule, why, and the quoted words.</p>
      <div className="mb-6 flex flex-wrap gap-2" role="group" aria-label="Filter by rule">
        {[["", "All", total] as const, ...Object.entries(byRule).map(([r, n]) => [r, RULE_LABEL[r] || r, n] as const)].map(([r, label, n]) => (
          <button key={r || "all"} type="button" aria-pressed={rule === r}
            onClick={() => { setLimit(50); setParams({ rule: r || null }, { replace: true }); }}
            className={cx("inline-flex h-8 items-center gap-2 rounded-full border px-3.5 text-[13px] font-medium transition-colors",
              rule === r ? "border-accent-line bg-accent-soft text-accent-ink" : "border-line-2 bg-surface text-ink-2 hover:border-faint")}>
            {label}<span className="tabular-nums text-faint">{n}</span>
          </button>
        ))}
      </div>
      {data.isLoading ? <CardSkeleton rows={4} /> : data.error ? <ErrorState error={data.error} onRetry={() => data.refetch()} />
        : !d || !d.items.length ? (
          <EmptyState icon={<EyeOff />} title="Nothing filtered">{total ? "No hidden roles for this rule." : "Your rules haven't filtered anything out yet."}</EmptyState>
        ) : (
          <>
            <JobList jobs={d.items} excluded compact />
            {d.items.length < d.total && (
              <div className="mt-6 flex justify-center">
                <Button variant="secondary" busy={data.isFetching} onClick={() => setLimit((l) => l + 50)}>
                  Show more ({plural(d.total - d.items.length, "left", "left")})
                </Button>
              </div>
            )}
          </>
        )}
    </>
  );
}
