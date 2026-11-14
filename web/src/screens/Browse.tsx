import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, EyeOff, Filter, Search, Telescope } from "lucide-react";
import { api } from "../lib/api";
import { useHotkeys, useOverlayOpen } from "../lib/app";
import { RULE_LABEL, plural } from "../lib/format";
import { setParams, useLocation, useTitle } from "../lib/router";
import type { JobCard, Paged } from "../lib/types";
import { JobList } from "../components/JobCard";
import { Button, CardSkeleton, EmptyState, ErrorState, PageHeader, Segmented, cx } from "../components/ui";

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

export function Hidden() {
  useTitle("Hidden roles");
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
      <PageHeader eyebrow="Browse" title="Hidden roles">
        Nothing is dropped silently. These roles were filtered out by your rules (config/filters.yaml), each with the reason and the words that triggered it.
      </PageHeader>
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
          <EmptyState icon={<EyeOff />} title="Nothing hidden">{total ? "No hidden roles for this rule." : "Your rules haven't filtered anything out yet."}</EmptyState>
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
