import { useEffect } from "react";
import { Shell } from "./components/Shell";
import { navigate, useLocation } from "./lib/router";
import { Today } from "./screens/Today";
import { NeedsYou } from "./screens/NeedsYou";
import { Tracker } from "./screens/Tracker";
import { Hidden, Matches } from "./screens/Browse";
import { Runs } from "./screens/Runs";
import { Resumes } from "./screens/Resumes";
import { Facts } from "./screens/Facts";
import { Portals } from "./screens/Portals";
import { ReviewDrawer } from "./screens/review/ReviewDrawer";
import { EmptyState, Button } from "./components/ui";

const ROUTES: Record<string, () => React.ReactNode> = {
  "/today": () => <Today />,
  "/needs-you": () => <NeedsYou />,
  "/tracker": () => <Tracker />,
  "/matches": () => <Matches />,
  "/watchlist": () => <Matches watchlist />,
  "/hidden": () => <Hidden />,
  "/runs": () => <Runs />,
  "/resumes": () => <Resumes />,
  "/facts": () => <Facts />,
  "/portals": () => <Portals />,
};

export function App() {
  const { path } = useLocation();
  useEffect(() => {
    if (path === "/" || path === "/index.html") navigate("/today" + window.location.search, { replace: true });
  }, [path]);
  useEffect(() => {
    document.getElementById("main")?.scrollTo?.(0, 0);
    window.scrollTo(0, 0);
  }, [path]);
  const route = ROUTES[path] || (path === "/" ? ROUTES["/today"] : null);
  return (
    <Shell>
      <div key={path} className="animate-fade">
        {route ? route() : (
          <EmptyState title="Page not found" action={<Button variant="primary" onClick={() => navigate("/today")}>Go to Today</Button>}>
            There's nothing at {path}.
          </EmptyState>
        )}
      </div>
      <ReviewDrawer />
    </Shell>
  );
}
