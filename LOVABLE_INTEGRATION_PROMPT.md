# Lovable Prompt — Wire `signal-convergence` to live backend via ngrok

> **Paste the section between the two horizontal rules below into Lovable.** Everything outside those rules is context for *you* (the human) on what changes Lovable will make and what to expect afterward.

---

## What this prompt does
- Replaces every read of `@/data/mockData` (which is hardcoded) with TanStack Query hooks that fetch from a FastAPI backend.
- Wires the Lovable preview to a stable ngrok URL so the live demo always points at the same backend regardless of where it runs.
- Adds "alive" UI: a connection status pill, last-refreshed timestamp, auto-refresh every 30 s, skeleton loading states, pulse animation on the top convergence pick, edge "flow" animation on the most recent signals, smooth fade-in on data changes.

## What stays the same
- Routing (`/`, `/explore`, `/founder/:id`, `/watchlist`, `/settings`).
- Visual design language — colors, type, shadcn components.
- Type definitions in `src/data/types.ts` (the backend response shape will match these so type imports do not change).
- Existing graph layout logic (tier-based clustering, polar placement).

## What you (the human) need to know about ngrok
The ngrok-free.dev domain shows a browser interstitial on first hit unless the request includes the header `ngrok-skip-browser-warning: true`. The prompt below configures the API client to send this header on every request.

---

# === BEGIN LOVABLE PROMPT ===

You are integrating this app with a live FastAPI backend instead of hardcoded mock data. The backend is reachable at a stable public ngrok URL: **`https://mispackaged-linn-prepoetic.ngrok-free.dev`**.

Do **not** change the visual design, the page routes, the file `src/data/types.ts`, or the existing graph layout math in `Explore.tsx`. The backend will return data in shapes that match the types in `src/data/types.ts` (Investor, Founder, Signal, ConvergenceAlert). Your job is to swap the data source and add a few "alive" UI touches.

## 1. Environment variable

Add a Vite env var so the API base URL can be overridden:

- Create `.env.example` with: `VITE_API_BASE_URL=https://mispackaged-linn-prepoetic.ngrok-free.dev`
- Document in the README that local users copy it to `.env.local`.
- Default fallback in code: `https://mispackaged-linn-prepoetic.ngrok-free.dev`.

## 2. API client — `src/lib/api.ts`

Create a typed fetch wrapper:

```ts
// src/lib/api.ts
import type {
  Investor,
  Founder,
  Signal,
  ConvergenceAlert,
} from "@/data/types";

const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
  "https://mispackaged-linn-prepoetic.ngrok-free.dev";

export interface GraphNodeDTO {
  id: string;
  kind: "investor" | "founder";
  // For investors: matches Investor type. For founders: matches Founder type.
  data: Investor | Founder;
}

export interface GraphEdgeDTO {
  id: string;
  sourceId: string;     // investor id
  targetId: string;     // founder id
  signal: Signal;       // the underlying signal that produced the edge
}

export interface GraphResponse {
  nodes: GraphNodeDTO[];
  edges: GraphEdgeDTO[];
  topPickFounderId?: string;
  generatedAt: string;
}

export interface HealthResponse {
  ok: boolean;
  neo4j: boolean;
  generatedAt: string;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      Accept: "application/json",
      // Required to bypass the ngrok-free.dev browser interstitial.
      "ngrok-skip-browser-warning": "true",
    },
  });
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  health:    () => getJSON<HealthResponse>("/api/health"),
  investors: () => getJSON<Investor[]>("/api/investors"),
  founders:  () => getJSON<Founder[]>("/api/founders"),
  alerts:    () => getJSON<ConvergenceAlert[]>("/api/alerts"),
  graph:     () => getJSON<GraphResponse>("/api/graph"),
  person:    (id: string) => getJSON<Investor | Founder>(`/api/person/${encodeURIComponent(id)}`),
  founder:   (id: string) => getJSON<Founder & { alerts: ConvergenceAlert[] }>(`/api/founder/${encodeURIComponent(id)}`),
};

export { API_BASE_URL };
```

## 3. React Query hooks — `src/hooks/useApi.ts`

```ts
// src/hooks/useApi.ts
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

const STALE_30S = 30_000;
const REFETCH_30S = 30_000;

export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: REFETCH_30S, staleTime: STALE_30S });

export const useInvestors = () =>
  useQuery({ queryKey: ["investors"], queryFn: api.investors, staleTime: STALE_30S });

export const useFounders = () =>
  useQuery({ queryKey: ["founders"], queryFn: api.founders, staleTime: STALE_30S });

export const useAlerts = () =>
  useQuery({ queryKey: ["alerts"], queryFn: api.alerts, refetchInterval: REFETCH_30S, staleTime: STALE_30S });

export const useGraph = () =>
  useQuery({ queryKey: ["graph"], queryFn: api.graph, refetchInterval: REFETCH_30S, staleTime: STALE_30S });

export const useFounder = (id: string | undefined) =>
  useQuery({
    queryKey: ["founder", id],
    queryFn: () => api.founder(id!),
    enabled: !!id,
    staleTime: STALE_30S,
  });
```

## 4. Ensure `QueryClientProvider` is mounted

In `src/main.tsx` (or `App.tsx` if it lives there), confirm there's a `QueryClientProvider` wrapping the router. If one already exists, leave it. If not, add:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});
// wrap <App /> with <QueryClientProvider client={queryClient}>...</QueryClientProvider>
```

## 5. Replace mock-data imports with hooks

In each page that imports from `@/data/mockData`, swap the imports for the hooks above and handle loading / error states. The `mockData.ts` file may stay in place as a dev fallback, but no page should import from it directly after this change.

Pages to update:
- `src/pages/Index.tsx` — uses `useAlerts()` for the dashboard cards.
- `src/pages/Explore.tsx` — uses `useInvestors()`, `useFounders()`, `useAlerts()` to build the graph. **Do not change the layout math** — only swap the data source. Pass the data into the existing `buildGraph(...)` function unchanged.
- `src/pages/FounderDetail.tsx` — uses `useFounder(id)` from the URL param.
- `src/pages/Watchlist.tsx` — uses `useInvestors()`.
- Keep `src/data/types.ts` and `investorById()` helper (rebuild `investorById` from the live `investors` array via `useMemo`).

For all pages, render:
- A `Skeleton` placeholder (use shadcn `<Skeleton />`) while `isLoading`.
- An error state with a retry button when `isError`.
- An empty state with helpful copy when the data is empty (e.g. "No convergence events yet — run the pipeline to ingest data.").

## 6. "Alive" UI — additions

These touches make the live data feel alive without changing the design language.

### 6a. Connection status pill (top-right of every page)

Create `src/components/converge/LiveStatus.tsx`:

```tsx
// Small pill: "Live", "Reconnecting…", "Offline"
import { useHealth } from "@/hooks/useApi";
import { Wifi, WifiOff, Loader2 } from "lucide-react";

export function LiveStatus() {
  const { data, isLoading, isError, dataUpdatedAt } = useHealth();
  const ago = dataUpdatedAt ? Math.max(0, Math.floor((Date.now() - dataUpdatedAt) / 1000)) : 0;
  const tone = isError ? "destructive" : data?.ok ? "live" : "loading";
  const label = isError ? "Offline" : data?.ok ? "Live" : "Connecting…";
  const Icon = isError ? WifiOff : isLoading ? Loader2 : Wifi;

  return (
    <div
      className={`flex items-center gap-1.5 text-[11px] font-mono uppercase tracking-wider px-2.5 py-1 rounded-full border transition-colors
        ${tone === "live"
          ? "bg-background border-border text-foreground"
          : tone === "loading"
            ? "bg-background border-border text-muted-foreground"
            : "bg-[hsl(var(--destructive))]/10 border-[hsl(var(--destructive))]/30 text-[hsl(var(--destructive))]"
        }`}
      title={`Last refreshed ${ago}s ago`}
    >
      <span
        className={`relative inline-flex h-1.5 w-1.5 rounded-full ${
          tone === "live"
            ? "bg-emerald-500"
            : tone === "destructive"
              ? "bg-[hsl(var(--destructive))]"
              : "bg-muted-foreground"
        }`}
      >
        {tone === "live" && (
          <span className="absolute inset-0 rounded-full bg-emerald-500 animate-ping opacity-75" />
        )}
      </span>
      <Icon className={`h-3 w-3 ${isLoading ? "animate-spin" : ""}`} />
      {label}
      {tone === "live" && <span className="text-muted-foreground ml-1">{ago}s</span>}
    </div>
  );
}
```

Mount it inside `AppLayout` next to the existing nav, so it appears on every page. Do not redesign the layout.

### 6b. Pulse on the top-pick founder node

In `Explore.tsx`, the top-pick founder already has a destructive-color treatment. Add a slow ambient pulse animation so it visually breathes:

In `tailwind.config.ts`, add a custom keyframe (only if not already present):

```ts
extend: {
  keyframes: {
    "pulse-ring": {
      "0%, 100%": { boxShadow: "0 0 0 0 hsl(var(--destructive) / 0.45)" },
      "50%":      { boxShadow: "0 0 0 14px hsl(var(--destructive) / 0)" },
    },
  },
  animation: {
    "pulse-ring": "pulse-ring 2.4s ease-out infinite",
  },
}
```

Then in `FounderNode` (inside `Explore.tsx`) — find the `isTopPick` branch and add `animate-pulse-ring` to its `className`. Do not change anything else about that node's styling.

### 6c. Animated edges for recent signals

The existing edge construction sets `animated: lit || isTopEdge`. Extend the rule so edges where `signal.occurredAt` is within the last 7 days are also animated. Find the `edgeMap.set(...)` call and compute `const isRecent = (Date.now() - +new Date(s.occurredAt)) < 7 * 24 * 3600 * 1000;` then `animated: lit || isTopEdge || isRecent`.

### 6d. Smooth data updates

Wrap the `ReactFlow` container in `Explore.tsx` with a CSS class that fades content on data change. Use the existing `animate-fade-in` utility (already in the codebase) on the panel and stats.

Add a small "data changed" toast via `useEffect` watching the `dataUpdatedAt` of `useGraph`. When it advances after the first mount, show a one-line toast: *"Graph refreshed · {N} new edges"*. Use shadcn `toast`.

### 6e. Skeleton states (for first-paint)

While `useInvestors() || useFounders() || useAlerts()` is loading, render the existing graph chrome (toolbar, contacts panel, stats footer) but show a centered shadcn `Skeleton` block where the ReactFlow canvas would be, with text *"Loading the convergence graph…"*. Once loaded, fade in.

## 7. Stop using mockData (but keep it as fallback)

Leave `src/data/mockData.ts` in place but un-export it from any barrel. Add a comment at the top: `// Phase 1 fallback only — production data flows through src/lib/api.ts.`

If the API call fails for >10 seconds, the error UI should offer a "Use offline data" button that loads `mockData` for that one session. This makes the demo robust if the backend is briefly down.

## 8. Acceptance criteria

After your changes:
- [ ] `pnpm dev` (or `npm run dev`) renders the app without TypeScript errors.
- [ ] The Explore page shows a graph populated from `https://mispackaged-linn-prepoetic.ngrok-free.dev/api/graph`. (If the backend is offline, the error state with a retry button appears.)
- [ ] The dashboard at `/` shows live convergence alerts.
- [ ] The connection status pill at the top right says "Live · Ns" when the backend is reachable, "Offline" when it isn't.
- [ ] Toggling platform / tier filters in Explore still works (filters are client-side, applied to live data).
- [ ] Clicking a node opens the existing side panel with that person's details (no visual changes).
- [ ] Auto-refresh every 30 seconds is visible (pill counter resets, occasional "graph refreshed" toast).
- [ ] No file in `src/pages/` directly imports from `@/data/mockData` anymore.

# === END LOVABLE PROMPT ===

---

## What I (the human) need to do *before* pasting this prompt

The prompt assumes the backend is already exposing the endpoints listed. It's not yet — that's the next step in our build. Order of operations:

1. **First**, build the FastAPI backend (`/backend/app.py`) that serves `/api/health`, `/api/investors`, `/api/founders`, `/api/alerts`, `/api/graph`, `/api/person/:id`, `/api/founder/:id` from Neo4j with response shapes matching `src/data/types.ts`.
2. **Second**, run `ngrok http 8000 --domain=mispackaged-linn-prepoetic.ngrok-free.dev` so the local backend is reachable at the stable URL.
3. **Third**, sanity-check the endpoints with `curl` (and the ngrok-skip-browser-warning header).
4. **Then** paste the prompt above into Lovable.

If you paste the prompt before the backend exists, Lovable's Explore page will render the empty/error state. That's still fine — the wiring will be ready and the moment the backend goes live, the graph populates.

## API contract reference (for backend implementer — me, in the next step)

For each endpoint, the response shape **must** match these contracts so Lovable's existing types work without rewrites.

### `GET /api/health`
```json
{ "ok": true, "neo4j": true, "generatedAt": "2026-05-01T12:00:00Z" }
```

### `GET /api/investors` → `Investor[]`
Maps to active watchlist members (`tier='active'` in our Neo4j). Source fields:
- `id`        ← `Person.canonical_id`
- `name`      ← `Person.display_name`
- `title`     ← from `WATCHED_BY.archetype` or `Person.investor_type`
- `firm`      ← derived (often null in our data)
- `tier`      ← map `Person.investor_type`: `Angel` → `'angel'`, `VC - Big fund` → `'vc'`, others → `'microvc'`
- `avatarColor` ← deterministic HSL from canonical_id
- `linkedinUrl` / `twitterHandle` / `githubUsername` ← from `PlatformIdentity` joins
- `group`     ← `Person.country` or `'unknown'`

### `GET /api/founders` → `Founder[]`
Maps to discovered founder candidates. In Phase 1 these come from:
- Anton Osika (from `data/identity_overrides.csv` — the M5 demo case), AND
- Persons that >= 2 active watchers have signals on (the convergence "preview"), AND
- Repository owners (when `OWNS_REPO` resolves to a Person with no `'investor'` tag).

### `GET /api/alerts` → `ConvergenceAlert[]`
One per `ConvergenceEvent` row. `signals[]` are the underlying STARRED_REPO + FOLLOWS_ON_GITHUB edges with their click-through URLs as evidence.

### `GET /api/graph` → `GraphResponse`
Pre-computed snapshot of (active investors) ∪ (founder candidates) with the edges between them. This is what the Explore page renders. ~200 nodes max.

### `GET /api/person/:id` and `GET /api/founder/:id`
Drill-in dossiers. The founder one includes the alerts that fired for that founder.

## ngrok setup quickstart (for the human)

```bash
# install ngrok if needed: https://ngrok.com/download
ngrok config add-authtoken YOUR_TOKEN

# permanent domain — replace if yours differs
ngrok http 8000 --domain=mispackaged-linn-prepoetic.ngrok-free.dev
```

Confirm by running:
```bash
curl -H "ngrok-skip-browser-warning: true" \
     https://mispackaged-linn-prepoetic.ngrok-free.dev/api/health
```
Expected: `{"ok": true, "neo4j": true, ...}`.

If you get HTML instead of JSON, the `ngrok-skip-browser-warning` header is missing. The Lovable code already sends it.

## Suggested Lovable iteration order

1. **First message**: paste the full prompt above. Let Lovable scaffold the full integration.
2. **Second message** (only if needed): "Add a small empty-state card on the dashboard when `/api/alerts` returns `[]`, with copy 'No convergence events yet. The pipeline ingests every 4 hours.' "
3. **Third message** (only if needed): "On the Explore page, when a node is hovered, fade all unrelated edges to 5% opacity for 200ms. Use a `transition` on the `opacity` style of each edge."

Keep iterations small. Lovable handles small focused asks better than mega-prompts.

---

# === FOLLOW-UP PROMPT: enriched alert details + editable rule ===

> Paste this AFTER the first prompt has been applied and the app is wired to the live backend. The backend now returns extra `meta` fields per alert and exposes endpoints to edit the alert rule.

## Background — what changed in the backend

Each alert in `GET /api/alerts` now includes a `meta` object alongside the existing fields:

```ts
interface ConvergenceMeta {
  score: number;                          // weighted total
  scoreBreakdown: {                       // per-component contribution
    distinct_members: number;
    recency: number;
    member_quality: number;
  };
  rank: number;                           // 1-based rank in the current ordering
  distinctMembers: number;                // N watchers that fired this convergence
  firstSignalAt: string | null;           // ISO — earliest evidence in window
  lastSignalAt:  string | null;           // ISO — latest evidence in window
  signalTypeCounts: { [signalType: string]: number };
                                          // e.g., { STARRED_REPO: 4 }
  windowStart: string | null;
  windowEnd:   string | null;
}
```

The backend also exposes a new alert-rule API:

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/alert-rule` | returns current `{ rule, allowed }` for the demo user |
| `PUT`  | `/api/alert-rule` | partial-update of the rule (validates, persists) |
| `POST` | `/api/alerts/recompute` | re-run convergence with the current rule |

The full rule shape:

```ts
interface AlertRule {
  min_distinct_watchers: number;          // default 2
  window_days: number;                    // default 365
  signal_types: string[];                 // ["FOLLOWS_ON_GITHUB", "STARRED_REPO"]
  weight_distinct_members: number;        // default 1.0
  weight_recency: number;                 // default 1.0
  weight_member_quality: number;          // default 0.0
  exclude_active_watchers: boolean;       // default true
  min_score: number;                      // default 0.0
  role_tag_filter: string[];              // default []
  sort_by: "score" | "watcher_count" | "recency";  // default "score"
  limit: number;                          // default 100
}
```

## What you should change

### 1. Extend the API client

In `src/lib/api.ts` add:

```ts
export interface AlertRule {
  min_distinct_watchers: number;
  window_days: number;
  signal_types: string[];
  weight_distinct_members: number;
  weight_recency: number;
  weight_member_quality: number;
  exclude_active_watchers: boolean;
  min_score: number;
  role_tag_filter: string[];
  sort_by: "score" | "watcher_count" | "recency";
  limit: number;
}

export interface AlertRuleResponse {
  userId: string;
  rule: AlertRule;
  allowed: { signal_types: string[]; sort_by: string[] };
}

api.alertRule       = () => getJSON<AlertRuleResponse>("/api/alert-rule");
api.updateAlertRule = (patch: Partial<AlertRule>) =>
  fetch(`${API_BASE_URL}/api/alert-rule`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "ngrok-skip-browser-warning": "true" },
    body: JSON.stringify(patch),
  }).then(r => r.json() as Promise<AlertRuleResponse>);
api.recomputeAlerts = () =>
  fetch(`${API_BASE_URL}/api/alerts/recompute`, {
    method: "POST",
    headers: { "ngrok-skip-browser-warning": "true" },
  }).then(r => r.json());
```

### 2. Add a `useAlertRule` hook

```ts
// src/hooks/useApi.ts
export const useAlertRule = () =>
  useQuery({ queryKey: ["alert-rule"], queryFn: api.alertRule, staleTime: 60_000 });

export const useRecomputeAlerts = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.recomputeAlerts,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });
};

export const useUpdateAlertRule = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<AlertRule>) => api.updateAlertRule(patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alert-rule"] }),
  });
};
```

### 3. Surface the `meta` fields in the alert detail / dossier panel

When the user clicks an alert and the side panel opens, show under a **"Why this fired"** heading:

- `meta.distinctMembers` — "**4 watchlist members** signaled within the window"
- A horizontal stacked bar showing `meta.scoreBreakdown` components: distinct_members (large), recency (small), member_quality (placeholder, faded)
- `meta.firstSignalAt` and `meta.lastSignalAt` — formatted as "First signal Jun 11 2023, latest Jun 20 2023 · 9 days span"
- `meta.signalTypeCounts` — small chips: `★ 4 stars` or `↪ 2 follows`
- `meta.rank` — small monospace pill: `#20 / 100`
- A subtle row at the bottom: "Window: Apr 27 2023 – May 1 2026 · score 4.05"

Use existing shadcn primitives (`Badge`, `Progress`, etc.) — no new design language.

### 4. Add a **Settings → Alert Rule** page

This is the editable surface. Path: `/settings/alerts`. Render a form bound to `useAlertRule()`:

- **Min distinct watchers**: slider 1..10 (default 2)
- **Time window (days)**: slider 7..1825 (default 365), with quick presets [30d, 90d, 1y, 3y]
- **Signal types**: checkbox group from `allowed.signal_types`
- **Weights**: three numeric inputs for distinct_members / recency / member_quality
- **Min score**: numeric input
- **Sort by**: radio group from `allowed.sort_by`
- **Limit**: numeric input 1..1000

Footer with two buttons:
- **Save rule** → `useUpdateAlertRule`
- **Save & recompute** → `useUpdateAlertRule` then `useRecomputeAlerts`

After save+recompute, show a toast: *"Rule saved · {fired} convergences re-evaluated"* using the response from `recompute` which returns `{ fired, topTargets, ... }`.

### 5. Optional polish — a "rule preview" diff

When the user has unsaved changes in the form, fetch a *preview* of how many alerts would fire by calling `/api/alerts?dry=true&...overrides...` if/when that endpoint exists. (Not implemented yet — Phase 2; just lay the UX scaffolding.)

## Acceptance criteria for this follow-up

- [ ] Clicking an alert opens the dossier and visibly shows the score breakdown, rank, signal-type counts, first/last signal dates.
- [ ] `/settings/alerts` renders the current rule, validates inputs (uses backend's 400 errors), and successfully PUTs partial updates.
- [ ] "Save & recompute" rebuilds the alert list within ~1s and the new ranking is visible.
- [ ] Setting `signal_types` to only `FOLLOWS_ON_GITHUB` and recomputing makes Anton Osika disappear from `/api/alerts`. Restoring both signal types brings him back at rank ~20. (This is the rule-mutability acceptance test.)

# === END FOLLOW-UP PROMPT ===

