import { forwardRef, useId, useRef, type ButtonHTMLAttributes, type KeyboardEvent, type ReactNode } from "react";
import { AlertTriangle, RotateCw } from "lucide-react";

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

// ---------------------------------------------------------------- buttons

type Variant = "primary" | "secondary" | "ghost" | "quiet" | "danger";
const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-accent text-on-accent hover:bg-accent-ink shadow-[0_1px_0_rgb(0_0_0/0.06),inset_0_1px_0_rgb(255_255_255/0.08)]",
  secondary: "bg-surface text-ink border border-line-2 hover:border-faint hover:bg-surface-2",
  ghost: "text-ink-2 hover:bg-paper-2 hover:text-ink",
  quiet: "text-muted hover:text-ink",
  danger: "text-bad hover:bg-bad-soft",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md" | "lg";
  icon?: ReactNode;
  /** Why the button is disabled (shown as a tooltip, read by screen readers). */
  reason?: string;
  busy?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", icon, reason, busy, disabled, className, children, ...rest }, ref) {
  const id = useId();
  const off = disabled || busy;
  const btn = (
    <button
      ref={ref}
      type="button"
      disabled={off}
      aria-disabled={off || undefined}
      aria-describedby={reason && off ? id : undefined}
      aria-busy={busy || undefined}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-[10px] font-medium whitespace-nowrap select-none",
        "transition-[background-color,border-color,color,box-shadow,transform] duration-150 active:translate-y-px",
        "disabled:opacity-50 disabled:active:translate-y-0",
        size === "sm" ? "h-8 px-3 text-[13px]" : size === "lg" ? "h-11 px-5 text-[15px]" : "h-9 px-3.5 text-sm",
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {busy ? <RotateCw aria-hidden className="size-4 animate-spin" /> : icon}
      {children}
    </button>
  );
  if (reason && off) {
    return (
      <span title={reason} className="inline-flex">
        {btn}
        <span id={id} className="sr-only">{reason}</span>
      </span>
    );
  }
  return btn;
});

export function IconButton({ label, children, className, ...rest }: ButtonProps & { label: string }) {
  return (
    <Button variant="ghost" size="sm" aria-label={label} title={rest.reason && rest.disabled ? undefined : label}
      className={cx("!px-0 w-8", className)} {...rest}>
      {children}
    </Button>
  );
}

/** A link styled as a button (only ever http(s) or same-origin hrefs). */
export function LinkButton({ href, children, variant = "secondary", size = "md", icon, className, newTab = true }:
  { href: string; children: ReactNode; variant?: Variant; size?: "sm" | "md"; icon?: ReactNode; className?: string; newTab?: boolean }) {
  return (
    <a href={href} {...(newTab ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      className={cx("inline-flex items-center justify-center gap-2 rounded-[10px] font-medium whitespace-nowrap transition-colors duration-150",
        size === "sm" ? "h-8 px-3 text-[13px]" : "h-9 px-3.5 text-sm", VARIANTS[variant], className)}>
      {icon}
      {children}
    </a>
  );
}

// ---------------------------------------------------------------- small pieces

type Tone = "neutral" | "accent" | "good" | "warn" | "bad" | "outline";
const TONES: Record<Tone, string> = {
  neutral: "bg-paper-2 text-ink-2",
  accent: "bg-accent-soft text-accent-ink",
  good: "bg-good-soft text-good",
  warn: "bg-warn-soft text-warn",
  bad: "bg-bad-soft text-bad",
  outline: "border border-dashed border-line-2 text-muted",
};

export function Badge({ tone = "neutral", children, className, title }: { tone?: Tone; children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[12px] font-medium leading-5", TONES[tone], className)}>
      {children}
    </span>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] border border-line-2 bg-surface px-1 text-[11px] font-medium text-muted shadow-[0_1px_0_var(--line-2)]">
      {children}
    </kbd>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cx("skeleton", className)} />;
}

export function CardSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading" className="space-y-3">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="card flex gap-5 p-5">
          <Skeleton className="size-14 shrink-0 rounded-full" />
          <div className="flex-1 space-y-3 pt-1">
            <Skeleton className="h-3 w-28" />
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ icon, title, children, action, className }:
  { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col items-center px-6 py-14 text-center animate-rise", className)}>
      {icon && (
        <div className="mb-5 grid size-14 place-items-center rounded-full bg-accent-soft text-accent [&_svg]:size-6">{icon}</div>
      )}
      <h3 className="font-display text-xl text-ink">{title}</h3>
      {children && <div className="mt-2 max-w-md text-[15px] leading-relaxed text-muted">{children}</div>}
      {action && <div className="mt-6">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, compact }: { error: unknown; onRetry?: () => void; compact?: boolean }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className={cx("card flex items-start gap-3 border-bad/30 bg-bad-soft/50 text-ink", compact ? "p-3" : "p-5")}>
      <AlertTriangle aria-hidden className="mt-0.5 size-5 shrink-0 text-bad" />
      <div className="flex-1">
        <p className="font-medium">That didn't work</p>
        <p className="mt-0.5 text-sm text-ink-2">{msg}</p>
      </div>
      {onRetry && <Button size="sm" onClick={onRetry} icon={<RotateCw className="size-3.5" />}>Retry</Button>}
    </div>
  );
}

export function PageHeader({ eyebrow, title, children, actions }:
  { eyebrow?: ReactNode; title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
      <div className="min-w-0 max-w-2xl">
        {eyebrow && <div className="eyebrow mb-2">{eyebrow}</div>}
        <h1 className="font-display text-[34px] leading-[1.1] font-[450] text-ink sm:text-[40px]">{title}</h1>
        {children && <div className="mt-3 text-[15px] leading-relaxed text-muted">{children}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function SectionTitle({ children, count, aside }: { children: ReactNode; count?: number; aside?: ReactNode }) {
  return (
    <div className="mb-4 mt-10 flex items-baseline justify-between gap-4 first:mt-0">
      <h2 className="font-display text-[22px] text-ink">
        {children}
        {count != null && <span className="ml-2 font-sans text-sm font-medium text-faint tabular-nums">{count}</span>}
      </h2>
      {aside}
    </div>
  );
}

// ---------------------------------------------------------------- segmented tabs (roving focus)

export interface TabItem<T extends string> {
  value: T;
  label: ReactNode;
  count?: number;
}

export function Segmented<T extends string>({ items, value, onChange, label, className }:
  { items: TabItem<T>[]; value: T; onChange: (v: T) => void; label: string; className?: string }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const n = (i + d + items.length) % items.length;
    refs.current[n]?.focus();
    onChange(items[n].value);
  };
  return (
    <div role="tablist" aria-label={label} className={cx("inline-flex rounded-[12px] border border-line bg-paper-2 p-1", className)}>
      {items.map((it, i) => {
        const on = it.value === value;
        return (
          <button
            key={it.value}
            ref={(el) => { refs.current[i] = el; }}
            role="tab"
            type="button"
            aria-selected={on}
            tabIndex={on ? 0 : -1}
            onKeyDown={(e) => onKey(e, i)}
            onClick={() => onChange(it.value)}
            className={cx(
              "inline-flex h-8 items-center gap-2 rounded-[9px] px-3 text-[13px] font-medium transition-all duration-200",
              on ? "bg-surface text-ink shadow-[0_1px_2px_rgb(var(--shadow)/0.08)]" : "text-muted hover:text-ink",
            )}
          >
            {it.label}
            {it.count != null && (
              <span className={cx("tabular-nums text-[12px]", on ? "text-accent" : "text-faint")}>{it.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- score ring

export function ScoreRing({ score, size = 56, label = "Fit score" }: { score: number | null | undefined; size?: number; label?: string }) {
  const r = size / 2 - 4;
  const c = 2 * Math.PI * r;
  const v = score == null ? 0 : Math.max(0, Math.min(100, score));
  const tone = score == null ? "var(--line-2)" : v >= 80 ? "var(--accent)" : v >= 65 ? "color-mix(in oklab, var(--accent) 65%, var(--warn))" : "var(--warn)";
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} role="img"
      aria-label={score == null ? `${label}: not scored` : `${label}: ${Math.round(v)} out of 100`}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line)" strokeWidth={3} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={tone} strokeWidth={3} strokeLinecap="round"
          strokeDasharray={`${(v / 100) * c} ${c}`} className="transition-[stroke-dasharray] duration-700 ease-out" />
      </svg>
      <span className="absolute inset-0 grid place-items-center font-display tabular-nums text-ink"
        style={{ fontSize: size * 0.34 }} aria-hidden>
        {score == null ? "–" : Math.round(v)}
      </span>
    </div>
  );
}

export function Meter({ value, label }: { value: number; label: string }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className="flex items-center gap-3 text-[13px]">
      <span className="w-24 shrink-0 capitalize text-muted">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-paper-2" role="meter" aria-label={label} aria-valuenow={Math.round(v)} aria-valuemin={0} aria-valuemax={100}>
        <div className="h-full rounded-full bg-accent/80" style={{ width: `${v}%` }} />
      </div>
      <span className="w-8 text-right tabular-nums text-ink-2">{Math.round(v)}</span>
    </div>
  );
}
