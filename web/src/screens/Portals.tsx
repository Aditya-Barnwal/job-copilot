import { useMutation, useQuery } from "@tanstack/react-query";
import { Copy, KeyRound, Lock, MonitorSmartphone } from "lucide-react";
import { DEMO_ACTION_TIP, api, post } from "../lib/api";
import { useDemo, useToast } from "../lib/app";
import { useTitle } from "../lib/router";
import { Badge, Button, CardSkeleton, ErrorState, PageHeader, SectionTitle } from "../components/ui";

export function Portals() {
  useTitle("Sign in to portals");
  const demo = useDemo();
  const toast = useToast();
  const data = useQuery({ queryKey: ["portals"], queryFn: () => api<{ links: { name: string; portal: string; url: string }[]; available: boolean; chrome: boolean }>("/api/portals") });
  const open = useMutation({
    mutationFn: () => post<{ profile: string }>("/api/portals/open", {}),
    onSuccess: (r) => toast.notify(`Chrome opened on ${r.profile}. Sign in there, then close the window.`, { kind: "success" }),
    onError: toast.error,
  });
  const copy = (u: string) => navigator.clipboard.writeText(u).then(() => toast.notify("Link copied — paste it into the sign-in window."), () => toast.notify("Your browser blocked the clipboard.", { kind: "error" }));
  return (
    <>
      <PageHeader eyebrow="You" title="Sign in to portals">
        Some application portals, Workday above all, need an account. Sign in once in the app's own Chrome profile; autofill reuses that session and never sees or stores a password.
      </PageHeader>
      <section className="card overflow-hidden">
        <div className="grid gap-6 p-6 sm:grid-cols-[1fr_auto] sm:items-center">
          <div className="flex gap-4">
            <span className="grid size-12 shrink-0 place-items-center rounded-[12px] bg-accent-soft text-accent"><MonitorSmartphone className="size-6" aria-hidden /></span>
            <div>
              <h2 className="font-display text-[22px] text-ink">Open the sign-in browser</h2>
              <p className="mt-1 max-w-xl text-[14px] leading-relaxed text-muted">
                Opens plain Google Chrome on <code className="rounded bg-paper-2 px-1">~/.jobcopilot/browser-profile</code> with one blank tab.
                It isn't automated: nothing is typed or opened for you. Sign in to each portal, then close the window.
              </p>
            </div>
          </div>
          <Button variant="primary" size="lg" icon={<KeyRound className="size-4" />} busy={open.isPending}
            disabled={demo || (data.data && !data.data.chrome)}
            reason={demo ? DEMO_ACTION_TIP : data.data && !data.data.chrome ? "Google Chrome isn't installed" : undefined}
            onClick={() => open.mutate()}>
            Open browser
          </Button>
        </div>
        <p className="flex items-center gap-2 border-t border-line bg-surface-2 px-6 py-3 text-[13px] text-muted">
          <Lock className="size-3.5" aria-hidden /> Close it before starting a fill: the profile can be open in one window at a time.
        </p>
      </section>
      <SectionTitle count={data.data?.links.length}>Portals from your companies</SectionTitle>
      {data.isLoading ? <CardSkeleton rows={2} /> : data.error ? <ErrorState error={data.error} /> : !data.data!.links.length ? (
        <p className="text-sm text-muted">None of your companies use a portal that needs an account.</p>
      ) : (
        <ul className="card divide-y divide-line">
          {data.data!.links.map((l) => (
            <li key={l.url} className="flex flex-wrap items-center gap-3 px-5 py-3.5">
              <div className="min-w-0 flex-1">
                <p className="text-[14px] font-medium text-ink">{l.name} <Badge className="ml-1">{l.portal}</Badge></p>
                <p className="mt-0.5 truncate font-mono text-[12px] text-muted">{l.url}</p>
              </div>
              <Button size="sm" icon={<Copy className="size-3.5" />} onClick={() => copy(l.url)}>Copy link</Button>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-4 text-[13px] text-muted">Links are copied, not opened here: open them in the sign-in window so the session lands in the right profile.</p>
    </>
  );
}
