import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { UiConfig } from "./types";

// ---------------------------------------------------------------- demo / config

const bannerInPage = () => typeof document !== "undefined" && !!document.querySelector(".demo-banner");

export function useConfig(): UiConfig {
  const q = useQuery({ queryKey: ["ui-config"], queryFn: () => api<UiConfig>("/api/ui/config"), staleTime: Infinity });
  const demo = bannerInPage() || !!q.data?.demo;
  return { demo, banner: q.data?.banner ?? null, autofill: q.data ? q.data.autofill && !demo : !demo };
}

export const useDemo = () => useConfig().demo;

// ---------------------------------------------------------------- toasts

export interface Toast {
  id: number;
  kind: "info" | "error" | "success";
  text: string;
  action?: { label: string; run: () => void };
}

interface ToastApi {
  toasts: Toast[];
  notify: (text: string, opts?: Partial<Omit<Toast, "id" | "text">> & { ms?: number }) => void;
  error: (e: unknown) => void;
  dismiss: (id: number) => void;
}

const ToastCtx = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const notify = useCallback<ToastApi["notify"]>((text, opts = {}) => {
    const id = ++seq.current;
    const kind = opts.kind || "info";
    setToasts((t) => [...t.slice(-3), { id, text, kind, action: opts.action }]);
    const ms = opts.ms ?? (opts.action ? 9000 : kind === "error" ? 9000 : 5000);
    window.setTimeout(() => dismiss(id), ms);
  }, [dismiss]);
  const error = useCallback((e: unknown) => {
    notify(e instanceof Error ? e.message : String(e), { kind: "error" });
  }, [notify]);
  const value = useMemo(() => ({ toasts, notify, error, dismiss }), [toasts, notify, error, dismiss]);
  return <ToastCtx.Provider value={value}>{children}</ToastCtx.Provider>;
}

export function useToast() {
  const c = useContext(ToastCtx);
  if (!c) throw new Error("ToastProvider missing");
  return c;
}

// ---------------------------------------------------------------- theme

export type Theme = "light" | "dark";

function storedTheme(): Theme | null {
  try {
    const t = localStorage.getItem("jc-theme");
    return t === "light" || t === "dark" ? t : null;
  } catch {
    return null;
  }
}

const systemTheme = (): Theme =>
  window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => (document.documentElement.getAttribute("data-theme") as Theme) || systemTheme());
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => { if (!storedTheme()) setTheme(systemTheme()); };
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const toggle = useCallback(() => {
    setTheme((t) => {
      const next: Theme = t === "dark" ? "light" : "dark";
      try { localStorage.setItem("jc-theme", next); } catch { /* storage blocked: still switch */ }
      return next;
    });
  }, []);
  return { theme, toggle };
}

// ---------------------------------------------------------------- keyboard

/** True when a key press belongs to a text field (shortcuts must not fire there). */
export function isTyping(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null;
  if (!t) return false;
  const tag = t.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable;
}

type Handler = (e: KeyboardEvent) => void;

/** Single-key shortcuts ({"j": fn, "Shift+?": fn}); ignored while typing or with Cmd/Ctrl/Alt. */
export function useHotkeys(map: Record<string, Handler>, enabled = true) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!enabled) return;
    const on = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e) || e.defaultPrevented) return;
      const k = (e.shiftKey && e.key.length === 1 && !/[a-z]/i.test(e.key) ? e.key : (e.shiftKey ? "Shift+" : "") + e.key);
      const fn = ref.current[k] || ref.current[e.key];
      if (fn) {
        fn(e);
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [enabled]);
}

// ---------------------------------------------------------------- overlay stack (drawer, dialogs)

let overlays = 0;
const overlayListeners = new Set<() => void>();
export function pushOverlay() {
  overlays += 1;
  overlayListeners.forEach((l) => l());
  return () => {
    overlays -= 1;
    overlayListeners.forEach((l) => l());
  };
}
export function useOverlayOpen() {
  const [n, setN] = useState(overlays);
  useEffect(() => {
    const l = () => setN(overlays);
    overlayListeners.add(l);
    return () => { overlayListeners.delete(l); };
  }, []);
  return n > 0;
}
