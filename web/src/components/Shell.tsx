import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BookUser, Columns3, EyeOff, FileText, Hand, History, KeyRound, ListChecks, Moon, Search, Sparkles, Sun, X,
} from "lucide-react";
import { api } from "../lib/api";
import { useHotkeys, useOverlayOpen, useTheme, useToast, pushOverlay } from "../lib/app";
import { Link, navigate, useLocation } from "../lib/router";
import type { AutofillSession, UnansweredQuestion } from "../lib/types";
import { Button, IconButton, Kbd, cx } from "./ui";

export interface NavItem {
  path: string;
  label: string;
  icon: ReactNode;
  key: string;          // g + key shortcut
  badge?: number;
}

function useNeedsYouCount() {
  const s = useQuery({ queryKey: ["needs-you"], queryFn: () => api<{ sessions: AutofillSession[] }>("/api/autofill/needs-you"), refetchInterval: 20000 });
  const q = useQuery({ queryKey: ["unanswered"], queryFn: () => api<{ questions: UnansweredQuestion[] }>("/api/answers/unanswered"), refetchInterval: 60000 });
  return (s.data?.sessions.length || 0) + (q.data?.questions.length || 0);
}

export const NAV_GROUPS: { title: string; items: Omit<NavItem, "badge">[] }[] = [
  {
    title: "Daily",
    items: [
      { path: "/today", label: "Today", icon: <Sparkles />, key: "t" },
      { path: "/needs-you", label: "Needs you", icon: <Hand />, key: "y" },
      { path: "/tracker", label: "Tracker", icon: <Columns3 />, key: "p" },
    ],
  },
  {
    title: "Browse",
    items: [
      { path: "/matches", label: "All matches", icon: <Search />, key: "m" },
      { path: "/watchlist", label: "Watchlist", icon: <ListChecks />, key: "w" },
      { path: "/hidden", label: "Hidden roles", icon: <EyeOff />, key: "h" },
      { path: "/runs", label: "Runs", icon: <History />, key: "u" },
    ],
  },
  {
    title: "You",
    items: [
      { path: "/resumes", label: "Résumés", icon: <FileText />, key: "r" },
      { path: "/facts", label: "Facts & answers", icon: <BookUser />, key: "f" },
      { path: "/portals", label: "Sign in to portals", icon: <KeyRound />, key: "s" },
    ],
  },
];

function Wordmark() {
  return (
    <Link to="/today" className="group flex items-center gap-2.5 rounded-lg px-2 py-1" aria-label="Job-Copilot, go to Today">
      <span className="grid size-8 place-items-center rounded-[9px] bg-accent text-on-accent shadow-sm">
        <svg viewBox="0 0 32 32" className="size-5" aria-hidden><path d="M11 9h10M16 9v11a4 4 0 0 1-8 0" fill="none" stroke="currentColor" strokeWidth="2.8" strokeLinecap="round" /></svg>
      </span>
      <span className="hidden font-display text-[19px] leading-none tracking-tight text-ink lg:inline">
        Job<span className="text-accent">·</span>Copilot
      </span>
    </Link>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { path } = useLocation();
  const { theme, toggle } = useTheme();
  const needs = useNeedsYouCount();
  const [help, setHelp] = useState(false);
  const overlay = useOverlayOpen();
  const gPending = useRef<number | null>(null);

  // g + letter navigation, ? for help.
  useHotkeys({
    g: () => {
      gPending.current = window.setTimeout(() => { gPending.current = null; }, 1200);
    },
    "?": () => setHelp(true),
  }, !overlay);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (gPending.current == null || e.key === "g" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      window.clearTimeout(gPending.current);
      gPending.current = null;
      const item = NAV_GROUPS.flatMap((g) => g.items).find((i) => i.key === e.key);
      if (item) {
        e.preventDefault();
        e.stopImmediatePropagation();
        navigate(item.path);
      }
    };
    window.addEventListener("keydown", on, true);
    return () => window.removeEventListener("keydown", on, true);
  }, []);

  return (
    <div className="canvas min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[80] focus:rounded-lg focus:bg-surface focus:px-4 focus:py-2 focus:shadow-lg">
        Skip to content
      </a>
      <div className="mx-auto flex max-w-[1500px]">
        <aside className="sticky top-0 flex h-screen w-[68px] shrink-0 flex-col border-r border-line/70 px-2 py-5 lg:w-[248px] lg:px-4">
          <Wordmark />
          <nav aria-label="Main" className="scroll-thin mt-8 flex-1 space-y-6 overflow-y-auto">
            {NAV_GROUPS.map((g) => (
              <div key={g.title}>
                <div className="eyebrow mb-1.5 hidden px-3 lg:block">{g.title}</div>
                <ul className="space-y-0.5">
                  {g.items.map((it) => {
                    const on = path === it.path || (it.path === "/today" && path === "/");
                    const badge = it.path === "/needs-you" ? needs : 0;
                    return (
                      <li key={it.path}>
                        <Link
                          to={it.path}
                          aria-current={on ? "page" : undefined}
                          title={`${it.label} (g ${it.key})`}
                          className={cx(
                            "group relative flex h-9 items-center gap-3 rounded-[10px] px-3 text-[14px] transition-colors duration-150 [&_svg]:size-[18px] [&_svg]:shrink-0",
                            on ? "bg-surface text-ink shadow-[0_1px_2px_rgb(var(--shadow)/0.06)] ring-1 ring-line" : "text-muted hover:bg-paper-2 hover:text-ink",
                          )}
                        >
                          <span className={cx(on ? "text-accent" : "text-faint group-hover:text-ink-2")}>{it.icon}</span>
                          <span className="hidden flex-1 truncate lg:inline">{it.label}</span>
                          {badge > 0 && (
                            <span className="absolute right-1 top-1 grid h-4 min-w-4 place-items-center rounded-full bg-warn px-1 text-[10px] font-semibold text-surface lg:static lg:h-5 lg:min-w-5 lg:text-[11px]"
                              aria-label={`${badge} waiting`}>
                              {badge}
                            </span>
                          )}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </nav>
          <div className="mt-4 flex flex-col gap-1 border-t border-line/70 pt-4 lg:flex-row lg:items-center lg:justify-between">
            <IconButton label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"} onClick={toggle}>
              {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </IconButton>
            <button type="button" onClick={() => setHelp(true)}
              className="hidden items-center gap-1.5 rounded-lg px-2 py-1 text-[12px] text-muted hover:text-ink lg:inline-flex">
              Shortcuts <Kbd>?</Kbd>
            </button>
          </div>
        </aside>
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 px-5 pb-24 pt-8 outline-none sm:px-8 lg:px-12 lg:pt-12">
          <div className="mx-auto max-w-[1080px]">{children}</div>
        </main>
      </div>
      <Toasts />
      {help && <ShortcutsDialog onClose={() => setHelp(false)} />}
    </div>
  );
}

function Toasts() {
  const { toasts, dismiss } = useToast();
  return (
    <div aria-live="polite" className="pointer-events-none fixed bottom-5 left-1/2 z-[70] flex w-[min(92vw,460px)] -translate-x-1/2 flex-col gap-2">
      {toasts.map((t) => (
        <div key={t.id} role={t.kind === "error" ? "alert" : "status"}
          className={cx("pointer-events-auto flex items-start gap-3 rounded-[12px] border px-4 py-3 text-sm shadow-[0_12px_32px_-12px_rgb(var(--shadow)/0.35)] animate-rise",
            t.kind === "error" ? "border-bad/30 bg-surface text-ink" : "border-line bg-ink text-paper")}>
          {t.kind === "error" && <span className="mt-1.5 size-2 shrink-0 rounded-full bg-bad" aria-hidden />}
          <span className="flex-1 leading-relaxed">{t.text}</span>
          {t.action && (
            <button type="button" className={cx("font-semibold underline-offset-2 hover:underline", t.kind === "error" ? "text-accent" : "text-accent-soft")}
              onClick={() => { t.action!.run(); dismiss(t.id); }}>
              {t.action.label}
            </button>
          )}
          <button type="button" aria-label="Dismiss" onClick={() => dismiss(t.id)} className="opacity-60 hover:opacity-100">
            <X className="size-4" />
          </button>
        </div>
      ))}
    </div>
  );
}

const SHORTCUTS: [string[], string][] = [
  [["j", "k"], "Next / previous job in a list"],
  [["Enter"], "Open the review card"],
  [["o"], "Open the posting in a new tab"],
  [["x"], "Hide the selected job (undo in the toast)"],
  [["1", "2", "3", "4"], "Review card tabs"],
  [["Esc"], "Close the review card or a dialog"],
  [["/"], "Search (All matches)"],
  [["g", "t"], "Go to Today (also: y Needs you, p Tracker, m Matches, w Watchlist, h Hidden, u Runs, r Résumés, f Facts, s Sign in)"],
  [["?"], "This list"],
];

export function Dialog({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const pop = pushOverlay();
    ref.current?.focus();
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
      if (e.key === "Tab" && ref.current) trapTab(e, ref.current);
    };
    window.addEventListener("keydown", on);
    return () => { window.removeEventListener("keydown", on); pop(); prev?.focus?.(); };
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-[75] grid place-items-center bg-ink/25 p-4 backdrop-blur-[2px] animate-fade" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}
        className={cx("card max-h-[85vh] w-full overflow-y-auto p-6 outline-none animate-rise shadow-[0_24px_64px_-24px_rgb(var(--shadow)/0.45)]", wide ? "max-w-2xl" : "max-w-lg")}>
        <div className="mb-4 flex items-start justify-between gap-4">
          <h2 className="font-display text-2xl text-ink">{title}</h2>
          <IconButton label="Close" onClick={onClose}><X className="size-4" /></IconButton>
        </div>
        {children}
      </div>
    </div>
  );
}

export function trapTab(e: KeyboardEvent, root: HTMLElement) {
  const els = Array.from(root.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((el) => el.offsetParent !== null || el === document.activeElement);
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && (document.activeElement === first || document.activeElement === root)) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  return (
    <Dialog title="Keyboard shortcuts" onClose={onClose} wide>
      <ul className="divide-y divide-line">
        {SHORTCUTS.map(([keys, what]) => (
          <li key={what} className="flex items-start justify-between gap-6 py-2.5 text-sm">
            <span className="text-ink-2">{what}</span>
            <span className="flex shrink-0 gap-1">{keys.map((k) => <Kbd key={k}>{k}</Kbd>)}</span>
          </li>
        ))}
      </ul>
      <div className="mt-5 flex justify-end"><Button onClick={onClose}>Done</Button></div>
    </Dialog>
  );
}

