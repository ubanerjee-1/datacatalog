import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  fetchValueAffiliates,
  fetchValueSummary,
  fetchValueUseCases,
  fetchValueUseCaseDetail,
  fetchValueSourceRollup,
  fetchValueSourceDetail,
  fetchValueSankey,
  fetchUseCaseProposalLink,
  generateUseCaseProposal,
  updateUseCaseStatus,
  USE_CASE_STATUS_LABEL,
  USE_CASE_STATUS_ORDER,
  type UseCaseStatus,
  type ValueFormula,
} from "@/lib/api-client";
import { toast } from "sonner";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import SankeyDiagram from "@/components/sankey-diagram";
import {
  Search,
  Building2,
  Filter,
  TrendingUp,
  Plug,
  GitFork,
  LayoutGrid,
  BarChart3,
  X,
  CheckCircle2,
  AlertTriangle,
  Trophy,
  PlayCircle,
  Lightbulb,
  PauseCircle,
  Activity,
  Users,
  ChevronDown,
  Database,
  MapPin,
  FileText,
  Sparkles,
  RotateCw,
  Loader2,
} from "lucide-react";

// Search-param schema. The chat assistant builds deeplinks like
// `/value-readiness?uc=<id>` for use-case citations; declaring it here
// lets us read it via `useSearch()` and clear it on close so a refresh
// doesn't re-pop the drawer the user just dismissed.
type ValueReadinessSearch = { uc?: string };

export const Route = createFileRoute("/_sidebar/value-readiness")({
  component: ValueReadinessPage,
  validateSearch: (search: Record<string, unknown>): ValueReadinessSearch => ({
    uc: typeof search.uc === "string" && search.uc ? search.uc : undefined,
  }),
});

// ---------------------------------------------------------------------------
// Shared types - kept loose since the API payloads are deliberately flat
// ---------------------------------------------------------------------------

type Affiliate = {
  affiliate_name: string;
  affiliate_code: string;
  business_type: string;
  region: string;
  description: string;
  is_active: boolean;
  use_case_count: number;
  primary_use_case_count: number;
};

type UseCaseRow = {
  id: string;
  use_case_name: string;
  description: string;
  department: string;
  priority: string;
  status: UseCaseStatus;
  estimated_value_usd: number;
  business_value: string;
  total_required: number;
  present_count: number;
  missing_count: number;
  must_total: number;
  must_present: number;
  unmapped_count: number;
  readiness_pct: number | null;
  applicable_affiliates: string[];
};

type SummaryBucket = { key: string; use_cases: number; value: number };

type StatusRollupEntry = { use_cases: number; value: number };

type SummaryResponse = {
  total_use_cases: number;
  total_value: number;
  ready_value: number;
  gap_value: number;
  ready_pct: number | null;
  buckets: SummaryBucket[];
  status_rollup?: Record<UseCaseStatus, StatusRollupEntry>;
};

type SourceRollupRow = {
  canonical: string;
  category: string;
  use_case_count: number;
  total_value: number;
  must_have_links: number;
  must_have_value: number;
  is_present: boolean;
};

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

const fmtMoney = (v: number) => {
  if (!Number.isFinite(v)) return "$0";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${Math.round(v)}`;
};

const fmtPct = (v: number | null | undefined) =>
  v == null ? "—" : `${v.toFixed(0)}%`;

const readinessColor = (pct: number | null | undefined): string => {
  if (pct == null) return "hsl(220, 10%, 70%)";
  if (pct >= 100) return "hsl(140, 60%, 45%)";
  if (pct >= 75) return "hsl(80, 60%, 50%)";
  if (pct >= 50) return "hsl(45, 80%, 55%)";
  if (pct >= 25) return "hsl(25, 80%, 55%)";
  return "hsl(0, 75%, 55%)";
};

const TABS = [
  { key: "treemap", label: "Value Treemap", icon: <LayoutGrid size={14} /> },
  { key: "bars", label: "Readiness", icon: <BarChart3 size={14} /> },
  { key: "pareto", label: "Source ROI", icon: <Plug size={14} /> },
  { key: "flow", label: "Data Flow", icon: <GitFork size={14} /> },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const PRIORITIES = ["High", "Medium", "Low"];

// ---------------------------------------------------------------------------
// Delivery status styling. Mirrors USE_CASE_STATUS_ORDER from the api client so
// KPI strip, chips, and the detail drawer all agree on color + icon.
// ---------------------------------------------------------------------------
const STATUS_STYLE: Record<
  UseCaseStatus,
  { label: string; color: string; bg: string; icon: React.ReactNode }
> = {
  delivered: {
    label: USE_CASE_STATUS_LABEL.delivered,
    color: "text-emerald-700",
    bg: "bg-emerald-50 border-emerald-200",
    icon: <Trophy size={14} className="text-emerald-600" />,
  },
  in_progress: {
    label: USE_CASE_STATUS_LABEL.in_progress,
    color: "text-sky-700",
    bg: "bg-sky-50 border-sky-200",
    icon: <PlayCircle size={14} className="text-sky-600" />,
  },
  not_started: {
    label: USE_CASE_STATUS_LABEL.not_started,
    color: "text-amber-700",
    bg: "bg-amber-50 border-amber-200",
    icon: <Lightbulb size={14} className="text-amber-600" />,
  },
  on_hold: {
    label: USE_CASE_STATUS_LABEL.on_hold,
    color: "text-slate-600",
    bg: "bg-slate-50 border-slate-200",
    icon: <PauseCircle size={14} className="text-slate-500" />,
  },
};

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function ValueReadinessPage() {
  const [affiliate, setAffiliate] = useState<string>("");
  const [priority, setPriority] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [department, setDepartment] = useState<string>("");
  const [formula, setFormula] = useState<ValueFormula>("simple");
  const [search, setSearch] = useState<string>("");
  const [tab, setTab] = useState<TabKey>("treemap");
  const [selectedUcId, setSelectedUcId] = useState<string | null>(null);
  const [selectedDept, setSelectedDept] = useState<string | null>(null);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);

  // Open the use-case drawer when the URL has `?uc=<id>` (e.g. from a
  // chat-assistant citation). We mirror the URL into local state on every
  // change so back/forward navigation works, and we strip the param when
  // the user closes the drawer so a refresh doesn't re-open it.
  const navigate = useNavigate({ from: Route.fullPath });
  const ucFromUrl = Route.useSearch({ select: (s) => s.uc });
  useEffect(() => {
    if (ucFromUrl && ucFromUrl !== selectedUcId) {
      setSelectedUcId(ucFromUrl);
    }
  }, [ucFromUrl, selectedUcId]);

  const filters = useMemo(
    () => ({
      affiliate: affiliate || undefined,
      priority: priority || undefined,
      status: (status as UseCaseStatus) || undefined,
      department: department || undefined,
      search: search || undefined,
      formula,
    }),
    [affiliate, priority, status, department, search, formula],
  );

  const { data: affiliatesData } = useQuery({
    queryKey: ["valueAffiliates"],
    queryFn: fetchValueAffiliates,
    staleTime: 5 * 60 * 1000,
  });
  const affiliates: Affiliate[] = affiliatesData?.affiliates ?? [];

  // Pull the unfiltered use-case catalog once to power the department + use
  // case slicers. Fast (single SQL call) and lets the user pick from the
  // full list even when other filters narrow what's actually displayed.
  const { data: catalogData } = useQuery({
    queryKey: ["valueUseCaseCatalog"],
    queryFn: () => fetchValueUseCases({ limit: 500 }),
    staleTime: 5 * 60 * 1000,
  });
  const allUseCases: { id: string; use_case_name: string; department: string }[] =
    catalogData?.use_cases ?? [];

  const departmentOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const uc of allUseCases) {
      const d = (uc.department || "").trim() || "Unassigned";
      counts.set(d, (counts.get(d) ?? 0) + 1);
    }
    return Array.from(counts.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([name, n]) => ({ value: name, label: `${name} (${n})` }));
  }, [allUseCases]);

  const useCaseOptions = useMemo(() => {
    const filtered = department
      ? allUseCases.filter(
          (u) => ((u.department || "").trim() || "Unassigned") === department,
        )
      : allUseCases;
    return filtered
      .map((u) => ({
        value: u.use_case_name,
        label: u.use_case_name,
        sub: (u.department || "").trim() || "Unassigned",
      }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [allUseCases, department]);

  const { data: summaryData, isLoading: summaryLoading } =
    useQuery<SummaryResponse>({
      queryKey: ["valueSummary", filters],
      queryFn: () => fetchValueSummary(filters),
    });

  const activeFilterCount =
    (affiliate ? 1 : 0) +
    (priority ? 1 : 0) +
    (status ? 1 : 0) +
    (department ? 1 : 0) +
    (search ? 1 : 0);

  const clearFilters = () => {
    setAffiliate("");
    setPriority("");
    setStatus("");
    setDepartment("");
    setSearch("");
  };

  return (
    <div className="flex flex-col min-h-full">
      {/* Title + KPI cards scroll naturally — they're context, not action.
          Pinning them was eating ~400px of every viewport and clipping the
          treemap below the fold. */}
      <div className="px-6 pt-5 pb-3 flex flex-col gap-3">
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              Value &amp; Readiness
            </h1>
            <p className="text-sm text-muted-foreground mt-1 max-w-3xl">
              What use cases can we deliver today, what&apos;s the dollar
              impact, and where are the gaps. Slice by affiliate to ground the
              conversation in a specific operating company.
            </p>
          </div>
        </div>

        <KpiStrip summary={summaryData} loading={summaryLoading} />
        <StatusBreakdownStrip
          summary={summaryData}
          loading={summaryLoading}
          activeStatus={(status as UseCaseStatus) || null}
          onSelect={(s) => setStatus(status === s ? "" : s)}
        />
      </div>

      {/* Sticky action bar: filters + tab nav stay pinned during scroll so
          users don't have to jump back to the top to switch views or
          reslice the data. Kept intentionally slim. */}
      <div className="border-b border-t bg-background sticky top-0 z-30 px-6 py-2 flex flex-col gap-2">
        {/* Filter bar */}
        <div className="flex items-center gap-2 flex-wrap">
          <FilterChip
            icon={<Building2 size={14} />}
            label="Affiliate"
            value={affiliate}
            options={affiliates.map((a) => ({
              value: a.affiliate_name,
              label: `${a.affiliate_name} (${a.use_case_count})`,
            }))}
            onChange={setAffiliate}
            placeholder="All affiliates"
          />
          <FilterChip
            icon={<Users size={14} />}
            label="Department"
            value={department}
            options={departmentOptions}
            onChange={(d) => {
              setDepartment(d);
              // If the currently selected use case isn't in the new dept,
              // reset it so the typeahead doesn't lie about what's filtered.
              if (d && search) {
                const stillVisible = allUseCases.some(
                  (u) =>
                    ((u.department || "").trim() || "Unassigned") === d &&
                    u.use_case_name === search,
                );
                if (!stillVisible) setSearch("");
              }
            }}
            placeholder="All departments"
          />
          <FilterChip
            icon={<Filter size={14} />}
            label="Priority"
            value={priority}
            options={PRIORITIES.map((p) => ({ value: p, label: p }))}
            onChange={setPriority}
            placeholder="Any priority"
          />
          <FilterChip
            icon={<Activity size={14} />}
            label="Status"
            value={status}
            options={USE_CASE_STATUS_ORDER.map((s) => ({
              value: s,
              label: USE_CASE_STATUS_LABEL[s],
            }))}
            onChange={setStatus}
            placeholder="Any status"
          />
          <FormulaToggle value={formula} onChange={setFormula} />
          <Combobox
            value={search}
            onChange={setSearch}
            options={useCaseOptions}
            placeholder={
              department
                ? `Search ${department} use cases...`
                : "Search use cases..."
            }
            icon={<Search size={14} />}
            className="flex-1 min-w-[220px] max-w-md"
            emptyLabel="No matching use cases"
          />
          {activeFilterCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={clearFilters}
              className="h-8 text-xs"
            >
              <X size={12} className="mr-1" />
              Clear ({activeFilterCount})
            </Button>
          )}
        </div>

        {/* Tab nav */}
        <div className="flex items-center gap-1 -mb-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={
                "flex items-center gap-1.5 text-sm px-3 py-2 border-b-2 transition-colors " +
                (tab === t.key
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground")
              }
            >
              {t.icon}
              <span>{t.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      <div className="px-6 py-5 flex-1">
        {tab === "treemap" && (
          <TreemapTab
            filters={filters}
            onSelect={(id) => {
              setSelectedUcId(id);
            }}
          />
        )}
        {tab === "bars" && (
          <BarsTab
            filters={filters}
            onSelect={(id) => setSelectedUcId(id)}
          />
        )}
        {tab === "pareto" && <ParetoTab filters={filters} />}
        {tab === "flow" && (
          <FlowTab
            filters={filters}
            onSelectUseCase={(id) => setSelectedUcId(id)}
            onSelectDepartment={(name) => setSelectedDept(name)}
            onSelectSource={(canonical) => setSelectedSource(canonical)}
          />
        )}
      </div>

      {selectedUcId && (
        <UseCaseDetailDrawer
          useCaseId={selectedUcId}
          affiliate={affiliate || undefined}
          onClose={() => {
            setSelectedUcId(null);
            // Strip ?uc=... from the URL so refresh / back doesn't re-open.
            // replace=true keeps the dismissal out of the history stack.
            if (ucFromUrl) {
              void navigate({
                search: (prev) => ({ ...prev, uc: undefined }),
                replace: true,
              });
            }
          }}
        />
      )}
      {selectedDept && (
        <DepartmentDetailDrawer
          department={selectedDept}
          affiliate={affiliate || undefined}
          onClose={() => setSelectedDept(null)}
          onSelectUseCase={(id) => {
            setSelectedDept(null);
            setSelectedUcId(id);
          }}
        />
      )}
      {selectedSource && (
        <SourceDetailDrawer
          canonical={selectedSource}
          affiliate={affiliate || undefined}
          onClose={() => setSelectedSource(null)}
          onSelectUseCase={(id) => {
            setSelectedSource(null);
            setSelectedUcId(id);
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Filter chip - compact native select styled like our other dropdowns
// ---------------------------------------------------------------------------

function FilterChip({
  icon,
  label,
  value,
  options,
  onChange,
  placeholder,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
  placeholder: string;
}) {
  return (
    <label className="flex items-center gap-1.5 text-sm border rounded-md px-2 h-8 bg-background hover:bg-muted/50">
      <span className="text-muted-foreground">{icon}</span>
      <span className="text-muted-foreground hidden md:inline">{label}:</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-transparent border-0 outline-none text-sm pr-1 max-w-[180px] truncate"
      >
        <option value="">{placeholder}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Lightweight typeahead combobox - controlled by string value, lets the user
// type to filter and pick from a known list. Used for the use-case slicer
// where the option set is too large for a native <select>.
// ---------------------------------------------------------------------------

type ComboOption = { value: string; label: string; sub?: string };

function Combobox({
  value,
  onChange,
  options,
  placeholder,
  icon,
  className,
  emptyLabel = "No matches",
}: {
  value: string;
  onChange: (v: string) => void;
  options: ComboOption[];
  placeholder: string;
  icon?: React.ReactNode;
  className?: string;
  emptyLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string>(value);
  const [activeIdx, setActiveIdx] = useState<number>(0);
  const wrapRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Keep the visible draft in sync with the upstream value when the parent
  // resets it (e.g. Clear filters or Department switch).
  useEffect(() => {
    if (!open) setDraft(value);
  }, [value, open]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) {
        setOpen(false);
        setDraft(value);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open, value]);

  const q = draft.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!q) return options.slice(0, 100);
    return options
      .filter(
        (o) =>
          o.label.toLowerCase().includes(q) ||
          (o.sub ?? "").toLowerCase().includes(q),
      )
      .slice(0, 100);
  }, [options, q]);

  useLayoutEffect(() => {
    setActiveIdx(0);
  }, [q, open]);

  const commit = (opt: ComboOption | null) => {
    if (opt) {
      onChange(opt.value);
      setDraft(opt.value);
    } else if (!draft.trim()) {
      onChange("");
    }
    setOpen(false);
  };

  return (
    <div ref={wrapRef} className={`relative ${className ?? ""}`}>
      <div className="relative">
        {icon && (
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground">
            {icon}
          </span>
        )}
        <Input
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setOpen(true);
              setActiveIdx((i) => Math.min(i + 1, filtered.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActiveIdx((i) => Math.max(i - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              commit(filtered[activeIdx] ?? null);
            } else if (e.key === "Escape") {
              setOpen(false);
              setDraft(value);
            }
          }}
          placeholder={placeholder}
          className={`h-8 ${icon ? "pl-8" : ""} ${value ? "pr-7" : "pr-8"}`}
        />
        {value ? (
          <button
            type="button"
            onMouseDown={(e) => {
              e.preventDefault();
              onChange("");
              setDraft("");
              setOpen(false);
            }}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 text-muted-foreground hover:text-foreground"
            aria-label="Clear"
          >
            <X size={12} />
          </button>
        ) : (
          <ChevronDown
            size={12}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"
          />
        )}
      </div>
      {open && (
        <div
          ref={listRef}
          className="absolute z-50 mt-1 w-full max-h-80 overflow-auto rounded-md border bg-popover text-popover-foreground shadow-lg text-sm"
        >
          {filtered.length === 0 ? (
            <div className="px-3 py-2 text-xs text-muted-foreground">
              {emptyLabel}
            </div>
          ) : (
            filtered.map((o, i) => (
              <button
                key={o.value}
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault();
                  commit(o);
                }}
                onMouseEnter={() => setActiveIdx(i)}
                className={
                  "block w-full text-left px-3 py-1.5 cursor-pointer " +
                  (i === activeIdx ? "bg-muted" : "hover:bg-muted/60")
                }
              >
                <div className="truncate">{o.label}</div>
                {o.sub && (
                  <div className="text-[11px] text-muted-foreground truncate">
                    {o.sub}
                  </div>
                )}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function FormulaToggle({
  value,
  onChange,
}: {
  value: ValueFormula;
  onChange: (v: ValueFormula) => void;
}) {
  return (
    <div
      className="flex items-center text-xs border rounded-md h-8 overflow-hidden"
      title="Simple = present sources / total required. Must = must-have present / must-have total."
    >
      <span className="px-2 text-muted-foreground hidden md:inline">
        Readiness:
      </span>
      {(["simple", "must"] as const).map((k) => (
        <button
          key={k}
          type="button"
          onClick={() => onChange(k)}
          className={
            "px-2 h-full text-xs " +
            (value === k
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-muted")
          }
        >
          {k === "simple" ? "Simple" : "Must-have"}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// KPI strip
// ---------------------------------------------------------------------------

function KpiStrip({
  summary,
  loading,
}: {
  summary?: SummaryResponse;
  loading: boolean;
}) {
  const items = [
    {
      label: "Use cases",
      value: loading ? "—" : String(summary?.total_use_cases ?? 0),
      sub: "in scope",
    },
    {
      label: "Total value",
      value: loading ? "—" : fmtMoney(summary?.total_value ?? 0),
      sub: "annual — realized + in-flight + opportunity",
    },
    {
      label: "Data-ready",
      value: loading ? "—" : fmtMoney(summary?.ready_value ?? 0),
      sub:
        summary?.ready_pct != null
          ? `${summary.ready_pct.toFixed(0)}% of total value`
          : "—",
      tone: "good" as const,
    },
    {
      label: "Data gap",
      value: loading ? "—" : fmtMoney(summary?.gap_value ?? 0),
      sub: "blocked by missing data",
      tone: "warn" as const,
    },
  ];
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {items.map((it) => (
        <Card key={it.label} className="border-muted">
          <CardContent className="py-3 px-4">
            <div className="text-xs text-muted-foreground uppercase tracking-wide">
              {it.label}
            </div>
            <div
              className={
                "text-2xl font-semibold mt-1 " +
                (it.tone === "good"
                  ? "text-emerald-600"
                  : it.tone === "warn"
                    ? "text-amber-600"
                    : "")
              }
            >
              {it.value}
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">{it.sub}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Delivery-status strip: Realized | In flight | Opportunity | On hold.
// Each tile doubles as a filter chip — click to scope the whole page to that
// slice of the portfolio.
// ---------------------------------------------------------------------------

function StatusBreakdownStrip({
  summary,
  loading,
  activeStatus,
  onSelect,
}: {
  summary?: SummaryResponse;
  loading: boolean;
  activeStatus: UseCaseStatus | null;
  onSelect: (status: UseCaseStatus) => void;
}) {
  const rollup = summary?.status_rollup;
  const total = summary?.total_value ?? 0;

  const tiles: {
    key: UseCaseStatus;
    label: string;
    hint: string;
    icon: React.ReactNode;
  }[] = [
    {
      key: "delivered",
      label: "Realized",
      hint: "live in production",
      icon: STATUS_STYLE.delivered.icon,
    },
    {
      key: "in_progress",
      label: "In flight",
      hint: "currently being delivered",
      icon: STATUS_STYLE.in_progress.icon,
    },
    {
      key: "not_started",
      label: "Opportunity",
      hint: "not yet started",
      icon: STATUS_STYLE.not_started.icon,
    },
    {
      key: "on_hold",
      label: "On hold",
      hint: "deferred",
      icon: STATUS_STYLE.on_hold.icon,
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {tiles.map((t) => {
        const entry = rollup?.[t.key];
        const value = entry?.value ?? 0;
        const count = entry?.use_cases ?? 0;
        const style = STATUS_STYLE[t.key];
        const pct = total > 0 ? (100 * value) / total : 0;
        const active = activeStatus === t.key;
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onSelect(t.key)}
            className={
              "text-left border rounded-md px-4 py-2 transition-colors " +
              (active
                ? `${style.bg} ring-2 ring-offset-1 ring-primary/40`
                : `${style.bg} hover:brightness-95`)
            }
            title={`Click to ${active ? "clear" : "filter by"} status: ${style.label}`}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                {t.icon}
                <span
                  className={
                    "text-xs uppercase tracking-wide font-medium " + style.color
                  }
                >
                  {t.label}
                </span>
              </div>
              <span className="text-[11px] text-muted-foreground">
                {loading
                  ? "…"
                  : `${count} UC · ${pct.toFixed(0)}%`}
              </span>
            </div>
            <div className={"text-xl font-semibold mt-1 " + style.color}>
              {loading ? "—" : fmtMoney(value)}
            </div>
            <div className="text-[11px] text-muted-foreground mt-0.5">
              {t.hint}
            </div>
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Treemap tab
// ---------------------------------------------------------------------------

type TreemapItem = {
  id: string;
  name: string;
  value: number;
  readiness: number | null;
  priority: string;
  status: UseCaseStatus;
  affiliates: string[];
  missing: number;
};

// Squarified treemap layout: classic Bruls/Huijing/van Wijk algorithm.
// Operates on a list of {value, ...} items and produces {x, y, w, h, item}.
type SquareNode<T> = { x: number; y: number; w: number; h: number; item: T };

function squarify<T extends { value: number }>(
  items: T[],
  width: number,
  height: number,
): SquareNode<T>[] {
  const total = items.reduce((s, it) => s + Math.max(it.value, 0), 0);
  if (total <= 0 || width <= 0 || height <= 0) return [];
  const scale = (width * height) / total;
  const scaled = items.map((it) => ({ ...it, area: Math.max(it.value, 0) * scale }));

  const out: SquareNode<T>[] = [];
  let x = 0,
    y = 0,
    w = width,
    h = height;
  let i = 0;
  while (i < scaled.length) {
    const row: typeof scaled = [];
    const shorter = Math.min(w, h);
    let rowSum = 0;
    let bestWorst = Infinity;
    while (i < scaled.length) {
      const next = scaled[i];
      const newSum = rowSum + next.area;
      const candidate = [...row, next];
      const worst = candidate.reduce((mx, r) => {
        const len = (shorter * r.area) / newSum;
        const wid = newSum / shorter;
        return Math.max(mx, Math.max(wid / len, len / wid));
      }, 0);
      if (worst > bestWorst && row.length > 0) break;
      row.push(next);
      rowSum = newSum;
      bestWorst = worst;
      i += 1;
    }
    if (w >= h) {
      const rw = rowSum / h;
      let ry = y;
      for (const r of row) {
        const rh = r.area / rw;
        out.push({ x, y: ry, w: rw, h: rh, item: r });
        ry += rh;
      }
      x += rw;
      w -= rw;
    } else {
      const rh = rowSum / w;
      let rx = x;
      for (const r of row) {
        const rw = r.area / rh;
        out.push({ x: rx, y, w: rw, h: rh, item: r });
        rx += rw;
      }
      y += rh;
      h -= rh;
    }
  }
  return out;
}

function TreemapTab({
  filters,
  onSelect,
}: {
  filters: ReturnType<typeof useMemo<unknown>> | any;
  onSelect: (id: string) => void;
}) {
  const { data, isLoading } = useQuery<{ use_cases: UseCaseRow[] }>({
    queryKey: ["valueUseCases", filters, 200],
    queryFn: () => fetchValueUseCases({ ...filters, limit: 200 }),
  });

  const items: TreemapItem[] = useMemo(() => {
    return (data?.use_cases ?? [])
      .filter((u) => (u.estimated_value_usd ?? 0) > 0)
      .map((u) => ({
        id: u.id,
        name: u.use_case_name,
        value: u.estimated_value_usd,
        readiness: u.readiness_pct,
        priority: u.priority,
        status: (u.status as UseCaseStatus) || "not_started",
        affiliates: u.applicable_affiliates,
        missing: u.missing_count,
      }))
      .sort((a, b) => b.value - a.value);
  }, [data]);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 1200, h: 600 });
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => {
      const r = el.getBoundingClientRect();
      const w = Math.max(Math.round(r.width), 600);
      setSize((prev) => (prev.w === w ? prev : { w, h: 600 }));
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layout = useMemo(
    () => squarify(items, size.w, size.h),
    [items, size.w, size.h],
  );

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Use Cases by Value &amp; Readiness
        </CardTitle>
        <CardDescription>
          Each rectangle is a use case. Area is dollar value; color is
          readiness. Solid borders = delivered (realized), dashed = in flight.
          Click any tile to see required data and set status.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[600px] w-full" />
        ) : items.length === 0 ? (
          <EmptyState message="No use cases match the current filters." />
        ) : (
          <div ref={containerRef}>
            <svg
              width="100%"
              height={size.h}
              viewBox={`0 0 ${size.w} ${size.h}`}
              preserveAspectRatio="none"
            >
              <defs>
                {/* Diagonal-stripe pattern used to mark delivered use cases. */}
                <pattern
                  id="deliveredStripes"
                  patternUnits="userSpaceOnUse"
                  width="8"
                  height="8"
                  patternTransform="rotate(45)"
                >
                  <rect width="8" height="8" fill="white" fillOpacity="0" />
                  <line
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="8"
                    stroke="white"
                    strokeOpacity="0.35"
                    strokeWidth="2"
                  />
                </pattern>
              </defs>
              {layout.map((n) => {
                const min = Math.min(n.w, n.h);
                const showLabel = n.w > 70 && n.h > 32;
                const showValue = min > 40;
                const isDelivered = n.item.status === "delivered";
                const isInProgress = n.item.status === "in_progress";
                const isOnHold = n.item.status === "on_hold";
                const statusMark = isDelivered
                  ? "✓"
                  : isInProgress
                    ? "▸"
                    : isOnHold
                      ? "‖"
                      : null;
                const strokeColor = isDelivered
                  ? "hsl(140, 60%, 30%)"
                  : isInProgress
                    ? "hsl(205, 70%, 40%)"
                    : "hsl(0,0%,100%)";
                return (
                  <g
                    key={n.item.id}
                    className="cursor-pointer"
                    onClick={() => onSelect(n.item.id)}
                  >
                    <rect
                      x={n.x}
                      y={n.y}
                      width={Math.max(n.w - 2, 0)}
                      height={Math.max(n.h - 2, 0)}
                      fill={readinessColor(n.item.readiness)}
                      fillOpacity={isOnHold ? 0.45 : 0.85}
                      stroke={strokeColor}
                      strokeWidth={isDelivered || isInProgress ? 2 : 1}
                      strokeDasharray={isInProgress ? "4 3" : undefined}
                    >
                      <title>
                        {n.item.name}
                        {"\n"}Value: {fmtMoney(n.item.value)}
                        {"\n"}Status: {USE_CASE_STATUS_LABEL[n.item.status]}
                        {"\n"}Readiness: {fmtPct(n.item.readiness)}
                        {"\n"}Missing sources: {n.item.missing}
                        {"\n"}Priority: {n.item.priority || "—"}
                      </title>
                    </rect>
                    {isDelivered && (
                      <rect
                        x={n.x}
                        y={n.y}
                        width={Math.max(n.w - 2, 0)}
                        height={Math.max(n.h - 2, 0)}
                        fill="url(#deliveredStripes)"
                        pointerEvents="none"
                      />
                    )}
                    {statusMark && min > 24 && (
                      <text
                        x={n.x + n.w - 8}
                        y={n.y + 14}
                        textAnchor="end"
                        fontSize={11}
                        fontWeight={700}
                        fill="white"
                        stroke="rgba(0,0,0,0.3)"
                        strokeWidth={0.4}
                        pointerEvents="none"
                      >
                        {statusMark}
                      </text>
                    )}
                    {showLabel && (
                      <foreignObject
                        x={n.x + 6}
                        y={n.y + 4}
                        width={Math.max(n.w - 12, 0)}
                        height={Math.max(n.h - 8, 0)}
                        style={{ pointerEvents: "none" }}
                      >
                        <div
                          style={{
                            color: "white",
                            fontSize:
                              min > 90 ? 13 : min > 60 ? 11 : 10,
                            lineHeight: 1.2,
                            overflow: "hidden",
                            textShadow: "0 1px 2px rgba(0,0,0,0.4)",
                          }}
                        >
                          <div className="font-medium" style={{
                            display: "-webkit-box",
                            WebkitLineClamp: Math.max(
                              1,
                              Math.floor((n.h - 24) / 14),
                            ),
                            WebkitBoxOrient: "vertical",
                            overflow: "hidden",
                          }}>
                            {n.item.name}
                          </div>
                          {showValue && (
                            <div
                              style={{
                                fontSize: 10,
                                opacity: 0.95,
                                marginTop: 2,
                              }}
                            >
                              {fmtMoney(n.item.value)} · {fmtPct(n.item.readiness)}
                            </div>
                          )}
                        </div>
                      </foreignObject>
                    )}
                  </g>
                );
              })}
            </svg>
            <ReadinessLegend />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReadinessLegend() {
  const stops: { label: string; color: string }[] = [
    { label: "100%", color: readinessColor(100) },
    { label: "75-99%", color: readinessColor(80) },
    { label: "50-74%", color: readinessColor(55) },
    { label: "25-49%", color: readinessColor(30) },
    { label: "<25%", color: readinessColor(10) },
  ];
  return (
    <div className="flex items-center gap-3 text-xs text-muted-foreground mt-3">
      <span>Readiness:</span>
      {stops.map((s) => (
        <span key={s.label} className="flex items-center gap-1">
          <span
            className="inline-block w-3 h-3 rounded-sm"
            style={{ backgroundColor: s.color }}
          />
          {s.label}
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bars tab
// ---------------------------------------------------------------------------

type SortKey = "value" | "readiness" | "gap" | "name";

function BarsTab({
  filters,
  onSelect,
}: {
  filters: any;
  onSelect: (id: string) => void;
}) {
  const { data, isLoading } = useQuery<{ use_cases: UseCaseRow[] }>({
    queryKey: ["valueUseCases", filters, 500],
    queryFn: () => fetchValueUseCases({ ...filters, limit: 500 }),
  });
  const [sortKey, setSortKey] = useState<SortKey>("value");

  const rows = useMemo(() => {
    const list = data?.use_cases ?? [];
    const sorted = [...list].sort((a, b) => {
      switch (sortKey) {
        case "readiness":
          return (b.readiness_pct ?? -1) - (a.readiness_pct ?? -1);
        case "gap":
          return (
            (b.estimated_value_usd ?? 0) * (1 - (b.readiness_pct ?? 0) / 100) -
            (a.estimated_value_usd ?? 0) * (1 - (a.readiness_pct ?? 0) / 100)
          );
        case "name":
          return a.use_case_name.localeCompare(b.use_case_name);
        case "value":
        default:
          return (b.estimated_value_usd ?? 0) - (a.estimated_value_usd ?? 0);
      }
    });
    return sorted;
  }, [data, sortKey]);

  const maxValue = rows.reduce(
    (mx, r) => Math.max(mx, r.estimated_value_usd ?? 0),
    1,
  );

  return (
    <Card>
      <CardHeader className="pb-3 flex flex-row items-start justify-between gap-3">
        <div>
          <CardTitle className="text-base">Use Case Readiness</CardTitle>
          <CardDescription>
            Per-use-case readiness with value bars. Sort by value, readiness, or
            gap to find the highest-ROI investments.
          </CardDescription>
        </div>
        <div className="flex items-center gap-1 text-xs">
          {(["value", "readiness", "gap", "name"] as SortKey[]).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setSortKey(k)}
              className={
                "px-2 h-7 rounded-md border " +
                (sortKey === k
                  ? "bg-primary text-primary-foreground border-primary"
                  : "text-muted-foreground hover:bg-muted")
              }
            >
              {k === "gap" ? "Gap $" : k.charAt(0).toUpperCase() + k.slice(1)}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[400px] w-full" />
        ) : rows.length === 0 ? (
          <EmptyState message="No use cases match the current filters." />
        ) : (
          <div className="space-y-1.5">
            {rows.map((u) => {
              const w = ((u.estimated_value_usd ?? 0) / maxValue) * 100;
              const ready = (w * (u.readiness_pct ?? 0)) / 100;
              return (
                <button
                  key={u.id}
                  type="button"
                  onClick={() => onSelect(u.id)}
                  className="w-full text-left grid grid-cols-[minmax(0,1fr)_60px_70px] gap-3 items-center px-3 py-2 rounded-md hover:bg-muted/50"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium truncate text-sm">
                        {u.use_case_name}
                      </span>
                      {u.priority === "High" && (
                        <Badge
                          variant="secondary"
                          className="h-4 px-1 text-[10px]"
                        >
                          High
                        </Badge>
                      )}
                    </div>
                    <div className="relative h-3 mt-1 bg-muted rounded-sm overflow-hidden">
                      <div
                        className="absolute inset-y-0 left-0"
                        style={{
                          width: `${w}%`,
                          backgroundColor: "hsl(220, 14%, 88%)",
                        }}
                      />
                      <div
                        className="absolute inset-y-0 left-0"
                        style={{
                          width: `${ready}%`,
                          backgroundColor: readinessColor(u.readiness_pct),
                        }}
                      />
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-0.5 truncate">
                      {u.applicable_affiliates.slice(0, 3).join(" · ")}
                      {u.applicable_affiliates.length > 3
                        ? ` +${u.applicable_affiliates.length - 3}`
                        : ""}
                    </div>
                  </div>
                  <div className="text-right text-sm tabular-nums">
                    {fmtMoney(u.estimated_value_usd)}
                  </div>
                  <div className="text-right">
                    <ReadinessChip pct={u.readiness_pct} />
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReadinessChip({ pct }: { pct: number | null }) {
  return (
    <span
      className="inline-flex items-center justify-center text-xs font-medium rounded-md px-2 py-0.5 tabular-nums"
      style={{
        color: "white",
        backgroundColor: readinessColor(pct),
        minWidth: 56,
      }}
    >
      {fmtPct(pct)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Pareto tab - Source ROI
// ---------------------------------------------------------------------------

function ParetoTab({ filters }: { filters: any }) {
  const [showOnlyMissing, setShowOnlyMissing] = useState(false);
  const rollupParams = {
    affiliate: filters.affiliate,
    priority: filters.priority,
    search: filters.search,
    only_missing: showOnlyMissing,
  };
  const { data, isLoading } = useQuery<{ sources: SourceRollupRow[] }>({
    queryKey: ["valueSourceRollup", rollupParams],
    queryFn: () => fetchValueSourceRollup(rollupParams),
  });

  const rows = data?.sources ?? [];
  const totalValue = rows.reduce((s, r) => s + (r.total_value || 0), 0);
  const maxValue = rows.reduce((m, r) => Math.max(m, r.total_value || 0), 1);

  let cumulative = 0;
  return (
    <Card>
      <CardHeader className="pb-3 flex flex-row items-start justify-between gap-3">
        <div>
          <CardTitle className="text-base">
            Source ROI &mdash; What unlocks the most value
          </CardTitle>
          <CardDescription>
            Each canonical source ranked by the combined value of use cases that
            need it. Green dot = present in lake; red dot = ingest-to-unlock.
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowOnlyMissing((v) => !v)}
            className={
              "text-xs h-7 px-2 rounded-md border flex items-center gap-1 " +
              (showOnlyMissing
                ? "bg-amber-50 border-amber-300 text-amber-800"
                : "text-muted-foreground hover:bg-muted")
            }
          >
            <AlertTriangle size={12} />
            {showOnlyMissing ? "Showing gaps only" : "Show gaps only"}
          </button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[500px] w-full" />
        ) : rows.length === 0 ? (
          <EmptyState message="No source systems match." />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-muted-foreground uppercase tracking-wide border-b">
                <th className="text-left py-2 pr-2">Canonical source</th>
                <th className="text-right pr-3">Use cases</th>
                <th className="text-right pr-3">Must-have</th>
                <th className="text-left pr-3 w-[40%]">$ unlocked</th>
                <th className="text-right pr-2">Cum %</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                cumulative += s.total_value || 0;
                const cumPct = totalValue > 0 ? (cumulative / totalValue) * 100 : 0;
                const w = ((s.total_value || 0) / maxValue) * 100;
                return (
                  <tr
                    key={s.canonical}
                    className="border-b last:border-b-0 hover:bg-muted/30"
                  >
                    <td className="py-2 pr-2">
                      <div className="flex items-center gap-2">
                        {s.is_present ? (
                          <CheckCircle2
                            size={14}
                            className="text-emerald-600 shrink-0"
                          />
                        ) : (
                          <AlertTriangle
                            size={14}
                            className="text-amber-600 shrink-0"
                          />
                        )}
                        <span className="font-medium">{s.canonical}</span>
                        {s.category && (
                          <Badge
                            variant="outline"
                            className="h-4 px-1 text-[10px] text-muted-foreground"
                          >
                            {s.category}
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="text-right pr-3 tabular-nums">
                      {s.use_case_count}
                    </td>
                    <td className="text-right pr-3 tabular-nums text-muted-foreground">
                      {s.must_have_links}
                    </td>
                    <td className="pr-3">
                      <div className="flex items-center gap-2">
                        <div className="flex-1 relative h-3 bg-muted rounded-sm overflow-hidden">
                          <div
                            className="absolute inset-y-0 left-0"
                            style={{
                              width: `${w}%`,
                              backgroundColor: s.is_present
                                ? "hsl(140, 60%, 45%)"
                                : "hsl(35, 80%, 55%)",
                            }}
                          />
                        </div>
                        <span className="text-xs tabular-nums w-14 text-right">
                          {fmtMoney(s.total_value)}
                        </span>
                      </div>
                    </td>
                    <td className="text-right pr-2 text-xs text-muted-foreground tabular-nums">
                      {cumPct.toFixed(0)}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Flow tab - reuse the existing SankeyDiagram
// ---------------------------------------------------------------------------

function FlowTab({
  filters,
  onSelectUseCase,
  onSelectDepartment,
  onSelectSource,
}: {
  filters: any;
  onSelectUseCase: (id: string) => void;
  onSelectDepartment: (name: string) => void;
  onSelectSource: (canonical: string) => void;
}) {
  const [topN, setTopN] = useState(25);
  const params = { ...filters, top_use_cases: topN };
  const { data, isLoading } = useQuery({
    queryKey: ["valueSankey", params],
    queryFn: () => fetchValueSankey(params),
  });

  const meta = data?.metadata ?? {};
  return (
    <Card>
      <CardHeader className="pb-3 flex flex-row items-start justify-between gap-3">
        <div>
          <CardTitle className="text-base">
            Data &rarr; Use Case &rarr; Department
          </CardTitle>
          <CardDescription>
            Value flowing from the data we have today through the use cases it
            unlocks to the departments that own them. Red source nodes are gaps
            &mdash; ingest these to unblock the use cases downstream. Hover any
            node to isolate the connected path.
          </CardDescription>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">Top use cases:</span>
          {[15, 25, 50, 100].map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setTopN(n)}
              className={
                "h-7 px-2 rounded-md border " +
                (topN === n
                  ? "bg-primary text-primary-foreground border-primary"
                  : "text-muted-foreground hover:bg-muted")
              }
            >
              {n}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[600px] w-full" />
        ) : !data?.nodes?.length ? (
          <EmptyState message="No flow to render for the current filters." />
        ) : (
          <>
            <div className="flex items-center gap-3 text-xs text-muted-foreground mb-3">
              <span>{meta.source_count ?? 0} sources</span>
              <span>·</span>
              <span>{meta.use_case_count ?? 0} use cases shown</span>
              <span>·</span>
              <span>{meta.department_count ?? 0} departments</span>
              {meta.missing_source_count > 0 && (
                <Badge variant="outline" className="text-amber-700 border-amber-300">
                  {meta.missing_source_count} gap source
                  {meta.missing_source_count === 1 ? "" : "s"}
                </Badge>
              )}
            </div>
            <SankeyDiagram
              data={data}
              columnLabels={["Data Sources", "Use Cases", "Departments"]}
              showFooter={false}
              onNodeClick={(node) => {
                // Node ids are prefixed by category in /value/sankey:
                //   src::<canonical>, uc::<id>, dept::<name>
                if (node.category === "use_case") {
                  const id = node.id.replace(/^uc::/, "");
                  onSelectUseCase(id);
                } else if (node.category === "department") {
                  onSelectDepartment(node.name);
                } else if (node.category === "source") {
                  onSelectSource(node.name);
                }
              }}
            />
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Use case detail drawer
// ---------------------------------------------------------------------------

function UseCaseDetailDrawer({
  useCaseId,
  affiliate,
  onClose,
}: {
  useCaseId: string;
  affiliate?: string;
  onClose: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["valueUseCaseDetail", useCaseId, affiliate],
    queryFn: () => fetchValueUseCaseDetail(useCaseId, { affiliate }),
  });

  return (
    <div
      className="fixed inset-0 z-50 bg-black/30 flex justify-end"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl h-full bg-background border-l shadow-xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-background border-b px-5 py-3 flex items-center justify-between">
          <div className="font-medium text-sm">Use case detail</div>
          <Button variant="ghost" size="sm" onClick={onClose}>
            <X size={14} />
          </Button>
        </div>
        {isLoading || !data ? (
          <div className="p-5 space-y-3">
            <Skeleton className="h-6 w-3/4" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-full" />
          </div>
        ) : data.error ? (
          <div className="p-5 text-sm text-muted-foreground">
            Error: {String(data.error)}
          </div>
        ) : (
          <UseCaseDetail data={data} />
        )}
      </div>
    </div>
  );
}

function UseCaseDetail({ data }: { data: any }) {
  const uc = data.use_case;
  const r = data.readiness;
  const qc = useQueryClient();
  const [proposalDialogOpen, setProposalDialogOpen] = useState(false);

  const statusMutation = useMutation({
    mutationFn: ({ status }: { status: UseCaseStatus }) =>
      updateUseCaseStatus(uc.id, { status }),
    onSuccess: () => {
      // Refresh the detail drawer plus every aggregate the status feeds into.
      qc.invalidateQueries({ queryKey: ["valueUseCaseDetail", uc.id] });
      qc.invalidateQueries({ queryKey: ["valueSummary"] });
      qc.invalidateQueries({ queryKey: ["valueUseCases"] });
      qc.invalidateQueries({ queryKey: ["valueSourceRollup"] });
      qc.invalidateQueries({ queryKey: ["valueSankey"] });
    },
  });

  // KB proposal lookup: drives the Generate vs. View+Regenerate UX. Polls on
  // mount only — the dialog refreshes this query after a successful generate.
  const proposalLinkQuery = useQuery({
    queryKey: ["useCaseProposalLink", uc.id],
    queryFn: () => fetchUseCaseProposalLink(uc.id),
    enabled: !!uc.id,
  });
  const proposalNodeId = proposalLinkQuery.data?.node_id || "";

  const currentStatus: UseCaseStatus =
    (uc.status as UseCaseStatus) || "not_started";
  const statusStyle = STATUS_STYLE[currentStatus];
  const deliveredValueNote =
    currentStatus === "delivered"
      ? "Realized value — this use case is live and producing its estimated return."
      : currentStatus === "in_progress"
        ? "In flight — value is not realized yet but delivery is underway."
        : currentStatus === "on_hold"
          ? "Deferred — value will not be captured until work resumes."
          : "Opportunity — captured if this use case is built and delivered.";

  return (
    <div className="p-5 space-y-5">
      <div>
        <div className="flex items-start justify-between gap-3">
          <h2 className="text-lg font-semibold">{uc.use_case_name}</h2>
          <ReadinessChip pct={r.readiness_pct_simple} />
        </div>
        <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
          <Badge variant="outline">{uc.department || "—"}</Badge>
          {uc.priority && <Badge variant="secondary">{uc.priority}</Badge>}
          <span className="text-emerald-600 font-medium">
            <TrendingUp size={12} className="inline mr-0.5" />
            {fmtMoney(uc.estimated_value_usd)} / yr
          </span>
        </div>
        <p className="text-sm mt-3 text-muted-foreground">{uc.description}</p>
      </div>

      {/* KB Proposal: one-click LLM generation of a structured proposal
         article (deliverables, design, timeline, risks). Becomes a
         "View / Regenerate" pair once an article is linked. */}
      <ProposalSection
        useCaseId={uc.id}
        useCaseName={uc.use_case_name}
        loading={proposalLinkQuery.isLoading}
        proposalNodeId={proposalNodeId}
        onGenerate={() => setProposalDialogOpen(true)}
      />

      {/* Delivery status control. Changing the selector drives the
         realized-vs-opportunity aggregates on the Value & Readiness page. */}
      <div className={"border rounded-md p-3 " + statusStyle.bg}>
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            {statusStyle.icon}
            <div>
              <div
                className={
                  "text-xs uppercase tracking-wide font-medium " + statusStyle.color
                }
              >
                Delivery status
              </div>
              <div className="text-sm mt-0.5">{statusStyle.label}</div>
            </div>
          </div>
          <select
            value={currentStatus}
            disabled={statusMutation.isPending}
            onChange={(e) =>
              statusMutation.mutate({ status: e.target.value as UseCaseStatus })
            }
            className="bg-background border rounded-md text-sm h-8 px-2"
          >
            {USE_CASE_STATUS_ORDER.map((s) => (
              <option key={s} value={s}>
                {USE_CASE_STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </div>
        <p className="text-[11px] text-muted-foreground mt-2">
          {deliveredValueNote}
          {uc.status_updated_at && (
            <>
              {" "}
              <span className="italic">
                · updated {String(uc.status_updated_at).slice(0, 10)}
              </span>
            </>
          )}
        </p>
        {uc.status_notes && (
          <p className="text-xs text-muted-foreground italic mt-1">
            {uc.status_notes}
          </p>
        )}
      </div>

      <div className="grid grid-cols-3 gap-3 text-center">
        <Stat label="Required" value={String(r.total_required)} />
        <Stat
          label="Present"
          value={String(r.present_count)}
          tone="good"
        />
        <Stat
          label="Missing"
          value={String(r.missing_count)}
          tone={r.missing_count > 0 ? "warn" : undefined}
        />
      </div>

      {data.applicable_affiliates?.length > 0 && (
        <Section title="Applies to">
          <div className="flex flex-wrap gap-1.5">
            {data.applicable_affiliates.map((a: any) => (
              <Badge
                key={a.affiliate_name}
                variant={a.applicability === "primary" ? "default" : "outline"}
                title={a.rationale || ""}
              >
                {a.affiliate_name}
              </Badge>
            ))}
          </div>
        </Section>
      )}

      {data.present_sources?.length > 0 && (
        <Section
          title={`Data we have (${data.present_sources.length})`}
          icon={<CheckCircle2 size={14} className="text-emerald-600" />}
        >
          <ul className="space-y-1 text-sm">
            {data.present_sources.map((s: any, i: number) => (
              <li key={`${s.required_canonical}-${i}`} className="flex items-start gap-2">
                <span className="text-emerald-600 mt-0.5">•</span>
                <div className="flex-1 min-w-0">
                  <div className="font-medium">
                    {s.required_canonical}
                    {s.necessity === "must_have" && (
                      <span className="ml-1 text-[10px] text-muted-foreground uppercase">
                        must
                      </span>
                    )}
                  </div>
                  {s.data_need_excerpt && (
                    <div className="text-xs text-muted-foreground italic">
                      {s.data_need_excerpt}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {data.missing_sources?.length > 0 && (
        <Section
          title={`Data we need (${data.missing_sources.length})`}
          icon={<AlertTriangle size={14} className="text-amber-600" />}
        >
          <ul className="space-y-1 text-sm">
            {data.missing_sources.map((s: any, i: number) => (
              <li key={`${s.required_canonical}-${i}`} className="flex items-start gap-2">
                <span className="text-amber-600 mt-0.5">•</span>
                <div className="flex-1 min-w-0">
                  <div className="font-medium">
                    {s.required_canonical}
                    {s.necessity === "must_have" && (
                      <span className="ml-1 text-[10px] text-muted-foreground uppercase">
                        must
                      </span>
                    )}
                  </div>
                  {s.data_need_excerpt && (
                    <div className="text-xs text-muted-foreground italic">
                      {s.data_need_excerpt}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {data.unmapped_needs?.length > 0 && (
        <Section
          title={`Needs not in our taxonomy (${data.unmapped_needs.length})`}
        >
          <ul className="space-y-1 text-sm text-muted-foreground">
            {data.unmapped_needs.map((s: any, i: number) => (
              <li key={i}>· {s.data_need_excerpt}</li>
            ))}
          </ul>
        </Section>
      )}

      {uc.business_value && (
        <Section title="Business value">
          <p className="text-sm text-muted-foreground">{uc.business_value}</p>
        </Section>
      )}

      {uc.value_rationale && (
        <Section title={`How we arrived at ${fmtMoney(uc.estimated_value_usd)}/yr`}>
          <p className="text-sm text-muted-foreground whitespace-pre-wrap">
            {uc.value_rationale}
          </p>
        </Section>
      )}

      {proposalDialogOpen && (
        <ProposalDialog
          useCaseId={uc.id}
          useCaseName={uc.use_case_name}
          regenerate={!!proposalNodeId}
          onClose={() => setProposalDialogOpen(false)}
          onGenerated={() => {
            qc.invalidateQueries({ queryKey: ["useCaseProposalLink", uc.id] });
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// KB Proposal generator — UI affordances for the use-case detail drawer.
//
// `ProposalSection` is the inline panel inside the detail (Generate or
// View+Regenerate). `ProposalDialog` is the modal that captures optional
// freeform context and calls the generator endpoint. Generation is
// synchronous (~10-30s); we show a spinner and a hint about the wait.
// ---------------------------------------------------------------------------

function ProposalSection({
  useCaseId,
  useCaseName,
  loading,
  proposalNodeId,
  onGenerate,
}: {
  useCaseId: string;
  useCaseName: string;
  loading: boolean;
  proposalNodeId: string;
  onGenerate: () => void;
}) {
  const navigate = useNavigate();

  if (loading) {
    return (
      <div className="border rounded-md p-3 flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 size={14} className="animate-spin" />
        Checking for existing proposal...
      </div>
    );
  }

  if (!proposalNodeId) {
    return (
      <div className="border rounded-md p-3 bg-violet-50/40 dark:bg-violet-950/20 flex items-start gap-3">
        <div className="mt-0.5 text-violet-600">
          <Sparkles size={16} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium">Next step: KB proposal</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Turn this use case into a structured KB article — deliverables,
            high-level design, timeline, assumptions and risks — generated
            from everything we know about <em>{useCaseName}</em>.
          </p>
          <Button
            size="sm"
            className="mt-2"
            onClick={onGenerate}
            data-use-case-id={useCaseId}
          >
            <Sparkles size={14} className="mr-1.5" />
            Generate Proposal
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="border rounded-md p-3 bg-violet-50/40 dark:bg-violet-950/20">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 text-violet-600">
          <FileText size={16} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium">KB Proposal generated</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            A proposal article is linked to this use case in the Knowledge
            Base. Regenerate to refresh it with the latest readiness data
            or new context.
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <Button
              size="sm"
              variant="default"
              onClick={() =>
                navigate({
                  to: "/knowledge",
                  search: { node: proposalNodeId } as any,
                })
              }
            >
              <FileText size={14} className="mr-1.5" />
              View Proposal
            </Button>
            <Button size="sm" variant="outline" onClick={onGenerate}>
              <RotateCw size={14} className="mr-1.5" />
              Regenerate
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

function ProposalDialog({
  useCaseId,
  useCaseName,
  regenerate,
  onClose,
  onGenerated,
}: {
  useCaseId: string;
  useCaseName: string;
  regenerate: boolean;
  onClose: () => void;
  onGenerated: () => void;
}) {
  const [additionalContext, setAdditionalContext] = useState("");
  const navigate = useNavigate();

  const generateMutation = useMutation({
    mutationFn: () =>
      generateUseCaseProposal(useCaseId, {
        additional_context: additionalContext,
        regenerate,
      }),
    onSuccess: (node) => {
      onGenerated();
      toast.success(
        regenerate ? "Proposal regenerated" : "Proposal generated",
        {
          description: `${node.title} is now in the Knowledge Base.`,
          action: {
            label: "Open",
            onClick: () =>
              navigate({
                to: "/knowledge",
                search: { node: node.node_id } as any,
              }),
          },
        },
      );
      onClose();
    },
    onError: (err: any) => {
      const detail =
        err?.response?.data?.detail ?? err?.message ?? "Generation failed";
      toast.error(
        typeof detail === "string" ? detail : "Generation failed",
      );
    },
  });

  const isPending = generateMutation.isPending;

  return (
    <div
      className="fixed inset-0 z-[60] bg-black/40 flex items-center justify-center p-4"
      onClick={() => !isPending && onClose()}
    >
      <div
        className="w-full max-w-lg bg-background border rounded-lg shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b px-5 py-3 flex items-center justify-between">
          <div>
            <div className="text-sm font-semibold flex items-center gap-1.5">
              <Sparkles size={14} className="text-violet-600" />
              {regenerate ? "Regenerate KB Proposal" : "Generate KB Proposal"}
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">
              {useCaseName}
            </div>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            disabled={isPending}
          >
            <X size={14} />
          </Button>
        </div>
        <div className="p-5 space-y-3">
          <p className="text-xs text-muted-foreground">
            We&apos;ll combine the use case description, business value,
            applicable affiliates and current data readiness (present /
            missing / unmapped sources) into a structured proposal article
            saved to the Knowledge Base.
          </p>
          <div>
            <label className="text-xs font-medium" htmlFor="proposal-context">
              Additional context for the AI{" "}
              <span className="text-muted-foreground font-normal">
                (optional)
              </span>
            </label>
            <textarea
              id="proposal-context"
              className="mt-1 w-full min-h-[120px] rounded-md border bg-background p-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
              placeholder="e.g. budget cap of $250K, must integrate with existing GIS, target go-live Q3, exclude affiliate X for now..."
              value={additionalContext}
              onChange={(e) => setAdditionalContext(e.target.value)}
              disabled={isPending}
              maxLength={4000}
            />
            <div className="text-[11px] text-muted-foreground mt-1 text-right">
              {additionalContext.length} / 4000
            </div>
          </div>
          {regenerate && (
            <div className="text-xs rounded-md border border-amber-200 bg-amber-50 dark:bg-amber-950/30 dark:border-amber-900 p-2 text-amber-900 dark:text-amber-200">
              Regenerating overwrites the existing proposal article and bumps
              its version. Existing links keep working.
            </div>
          )}
          <p className="text-[11px] text-muted-foreground italic">
            Generation typically takes 15-30 seconds.
          </p>
        </div>
        <div className="border-t px-5 py-3 flex items-center justify-end gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={() => generateMutation.mutate()}
            disabled={isPending}
          >
            {isPending ? (
              <>
                <Loader2 size={14} className="mr-1.5 animate-spin" />
                Generating...
              </>
            ) : (
              <>
                <Sparkles size={14} className="mr-1.5" />
                {regenerate ? "Regenerate" : "Generate"}
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared drawer shell - same layout/animation as UseCaseDetailDrawer so the
// three drawers feel like one component family.
// ---------------------------------------------------------------------------

function DrawerShell({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-50 bg-black/30 flex justify-end"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl h-full bg-background border-l shadow-xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-background border-b px-5 py-3 flex items-center justify-between">
          <div className="font-medium text-sm">{title}</div>
          <Button variant="ghost" size="sm" onClick={onClose}>
            <X size={14} />
          </Button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Department detail drawer - "what does this department own, and how ready
// are we to deliver it?" Reuses /value/summary + /value/use-cases under the
// hood so the numbers reconcile with the page-level KPI strip.
// ---------------------------------------------------------------------------

function DepartmentDetailDrawer({
  department,
  affiliate,
  onClose,
  onSelectUseCase,
}: {
  department: string;
  affiliate?: string;
  onClose: () => void;
  onSelectUseCase: (id: string) => void;
}) {
  const params = useMemo(
    () => ({
      department,
      affiliate: affiliate || undefined,
      formula: "simple" as ValueFormula,
    }),
    [department, affiliate],
  );

  const { data: summary, isLoading: sLoading } = useQuery<SummaryResponse>({
    queryKey: ["valueSummary", params],
    queryFn: () => fetchValueSummary(params),
  });

  const { data: ucData, isLoading: ucLoading } = useQuery({
    queryKey: ["valueUseCases", { ...params, limit: 200 }],
    queryFn: () => fetchValueUseCases({ ...params, limit: 200 }),
  });

  const useCases: UseCaseRow[] = ucData?.use_cases ?? [];
  const readyPct = summary?.ready_pct;

  return (
    <DrawerShell title="Department detail" onClose={onClose}>
      <div className="p-5 space-y-5">
        <div>
          <div className="flex items-start justify-between gap-3">
            <h2 className="text-lg font-semibold">{department}</h2>
            <ReadinessChip pct={readyPct ?? null} />
          </div>
          <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
            <Badge variant="outline" className="gap-1">
              <Users size={11} />
              Department
            </Badge>
            {affiliate && (
              <span className="text-muted-foreground">
                Scoped to <span className="font-medium">{affiliate}</span>
              </span>
            )}
          </div>
        </div>

        {sLoading ? (
          <Skeleton className="h-20 w-full" />
        ) : (
          <div className="grid grid-cols-3 gap-3 text-center">
            <Stat
              label="Use cases"
              value={String(summary?.total_use_cases ?? 0)}
            />
            <Stat
              label="Total value"
              value={fmtMoney(summary?.total_value ?? 0)}
            />
            <Stat
              label="Ready value"
              value={fmtMoney(summary?.ready_value ?? 0)}
              tone="good"
            />
          </div>
        )}

        <Section
          title={`Use cases (${useCases.length})`}
          icon={<Lightbulb size={14} className="text-amber-600" />}
        >
          {ucLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : useCases.length === 0 ? (
            <div className="text-sm text-muted-foreground">
              No use cases match the current filters.
            </div>
          ) : (
            <ul className="space-y-1.5">
              {useCases.map((u) => (
                <li key={u.id}>
                  <button
                    type="button"
                    onClick={() => onSelectUseCase(u.id)}
                    className="w-full text-left border rounded-md p-2.5 hover:bg-muted/60 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="text-sm font-medium truncate">
                        {u.use_case_name}
                      </div>
                      <ReadinessChip pct={u.readiness_pct} />
                    </div>
                    <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
                      <span className="text-emerald-600 font-medium">
                        {fmtMoney(u.estimated_value_usd)} / yr
                      </span>
                      {u.priority && (
                        <Badge variant="secondary" className="text-[10px]">
                          {u.priority}
                        </Badge>
                      )}
                      {u.missing_count > 0 && (
                        <span className="text-amber-600">
                          {u.missing_count} missing
                        </span>
                      )}
                      <Badge
                        variant="outline"
                        className={
                          "text-[10px] " + STATUS_STYLE[u.status].color
                        }
                      >
                        {STATUS_STYLE[u.status].label}
                      </Badge>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>
    </DrawerShell>
  );
}

// ---------------------------------------------------------------------------
// Source (canonical system) detail drawer - "what use cases need this, and
// where do we already have it?"
// ---------------------------------------------------------------------------

function SourceDetailDrawer({
  canonical,
  affiliate,
  onClose,
  onSelectUseCase,
}: {
  canonical: string;
  affiliate?: string;
  onClose: () => void;
  onSelectUseCase: (id: string) => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["valueSourceDetail", canonical, affiliate],
    queryFn: () => fetchValueSourceDetail(canonical, { affiliate }),
  });

  const meta = data?.meta ?? { canonical };
  const totals = data?.totals ?? {};
  const useCases: any[] = data?.use_cases ?? [];
  const locations: any[] = data?.locations ?? [];
  const isPresent = !!data?.is_present;

  return (
    <DrawerShell title="Source detail" onClose={onClose}>
      {isLoading || !data ? (
        <div className="p-5 space-y-3">
          <Skeleton className="h-6 w-3/4" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-full" />
        </div>
      ) : (
        <div className="p-5 space-y-5">
          <div>
            <div className="flex items-start justify-between gap-3">
              <h2 className="text-lg font-semibold">{meta.canonical}</h2>
              <Badge
                variant={isPresent ? "outline" : "destructive"}
                className={
                  isPresent
                    ? "border-emerald-300 text-emerald-700"
                    : "bg-red-100 text-red-700 border-red-200"
                }
              >
                {isPresent ? "In the lake" : "Gap — not ingested"}
              </Badge>
            </div>
            <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
              <Badge variant="outline" className="gap-1">
                <Database size={11} />
                {meta.category || "Source system"}
              </Badge>
              {affiliate && (
                <span className="text-muted-foreground">
                  Scoped to <span className="font-medium">{affiliate}</span>
                </span>
              )}
            </div>
            {meta.description && (
              <p className="text-sm mt-3 text-muted-foreground">
                {meta.description}
              </p>
            )}
          </div>

          <div className="grid grid-cols-3 gap-3 text-center">
            <Stat
              label="Use cases"
              value={String(totals.use_case_count ?? 0)}
            />
            <Stat
              label="Total value"
              value={fmtMoney(totals.total_value ?? 0)}
            />
            <Stat
              label="Must-have $"
              value={fmtMoney(totals.must_have_value ?? 0)}
              tone={totals.must_have_value > 0 ? "warn" : undefined}
            />
          </div>

          <Section
            title={`Use cases that need this (${useCases.length})`}
            icon={<Lightbulb size={14} className="text-amber-600" />}
          >
            {useCases.length === 0 ? (
              <div className="text-sm text-muted-foreground">
                No use cases currently require this source.
              </div>
            ) : (
              <ul className="space-y-1.5">
                {useCases.map((u) => (
                  <li key={u.id}>
                    <button
                      type="button"
                      onClick={() => onSelectUseCase(u.id)}
                      className="w-full text-left border rounded-md p-2.5 hover:bg-muted/60 transition-colors"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="text-sm font-medium truncate">
                          {u.use_case_name}
                        </div>
                        <ReadinessChip pct={u.readiness_pct} />
                      </div>
                      <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
                        <span className="text-emerald-600 font-medium">
                          {fmtMoney(u.value_usd)} / yr
                        </span>
                        {u.necessity === "must_have" && (
                          <Badge
                            variant="outline"
                            className="text-[10px] border-amber-300 text-amber-700"
                          >
                            must
                          </Badge>
                        )}
                        {u.department && (
                          <span className="text-muted-foreground truncate">
                            {u.department}
                          </span>
                        )}
                        <Badge
                          variant="outline"
                          className={
                            "text-[10px] " + STATUS_STYLE[u.status as UseCaseStatus].color
                          }
                        >
                          {STATUS_STYLE[u.status as UseCaseStatus].label}
                        </Badge>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section
            title={
              isPresent
                ? `Where it lives in the lake (${locations.length})`
                : "Lake locations"
            }
            icon={<MapPin size={14} className="text-sky-600" />}
          >
            {locations.length === 0 ? (
              <div className="text-sm text-muted-foreground">
                Not yet ingested. Bringing this source into the lake unblocks
                the use cases above.
              </div>
            ) : (
              <ul className="space-y-1 text-sm">
                {locations.map((l, i) => (
                  <li
                    key={`${l.catalog}.${l.schema}.${i}`}
                    className="flex items-start gap-2"
                  >
                    <span className="text-sky-600 mt-0.5">•</span>
                    <div className="flex-1 min-w-0">
                      <div className="font-medium truncate">
                        {l.catalog}.{l.schema}
                      </div>
                      <div className="text-xs text-muted-foreground flex items-center gap-2 flex-wrap">
                        {l.affiliate && <span>{l.affiliate}</span>}
                        {l.environment && (
                          <Badge variant="outline" className="text-[10px]">
                            {l.environment}
                          </Badge>
                        )}
                        <span>
                          {l.table_count} table{l.table_count === 1 ? "" : "s"}
                        </span>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </div>
      )}
    </DrawerShell>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "warn";
}) {
  return (
    <div className="border rounded-md py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div
        className={
          "text-xl font-semibold " +
          (tone === "good"
            ? "text-emerald-600"
            : tone === "warn"
              ? "text-amber-600"
              : "")
        }
      >
        {value}
      </div>
    </div>
  );
}

function Section({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2 flex items-center gap-1.5">
        {icon}
        {title}
      </div>
      {children}
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="text-sm text-muted-foreground py-12 text-center">
      {message}
    </div>
  );
}
