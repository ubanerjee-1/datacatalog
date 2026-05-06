import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import {
  fetchUseCases,
  fetchDepartments,
  fetchEditAffiliates,
  updateUseCaseStatus,
  deleteUseCase,
  generateUseCases,
  commitGeneratedUseCases,
  discoverPrograms,
  commitDiscoveredPrograms,
  USE_CASE_STATUS_LABEL,
  USE_CASE_STATUS_ORDER,
  type ProgramDiscoveryProposal,
  type ProgramsDiscoverOut,
  type UseCaseCandidate,
  type UseCaseGenerateOut,
  type UseCaseLens,
  type UseCaseStatus,
  type UseCaseTimeHorizon,
  type UseCaseValueType,
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
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Clock,
  DollarSign,
  Lightbulb,
  Loader2,
  Map as MapIcon,
  Search,
  ShieldAlert,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/use-cases")({
  component: UseCasesPage,
});

// ---------------------------------------------------------------------------
// Use Cases page (PR 1 of the UC redesign).
//
// Promoted out of `company.tsx` Step 5 (which used to inline a UC summary
// alongside Departments). UCs are the meat of the tool, so they live in a
// dedicated section now.
//
// PR 1 scope (this file): list + filter + inline status update + delete.
// PR 2 will add the "Generate" form (per (department, affiliate, count, lens)).
// PR 3 (optional) will add canonical-coverage panel.
// ---------------------------------------------------------------------------

type UseCase = {
  id: string;
  use_case_name: string;
  description: string;
  department: string;
  category: string;
  priority: string;
  business_value: string;
  estimated_value_usd: number | null;
  value_rationale: string;
  // The backend serializes `data_requirements` and `required_canonicals`
  // as JSON arrays of strings (already parsed); we only ever read them as
  // arrays. Tolerate `null`/`undefined` for back-compat with rows that
  // pre-date these fields.
  data_requirements: string[] | null;
  status: UseCaseStatus;
  status_notes: string;
  status_updated_at: string | null;
  is_user_edited: boolean;
  // PR 2 generation lens fields. Absent on rows created before the
  // backend ALTER ran or by the chat path; treat undefined `lens` as
  // "manual" so the badge stays informative without a backend migration.
  affiliate?: string | null;
  lens?: "ready" | "gap" | "manual" | null;
  time_horizon?: "quick_win" | "strategic" | null;
  value_type?: "cost" | "revenue" | "risk" | "mixed" | null;
  is_regulatory?: boolean | null;
  required_canonicals?: string[] | null;
};

const STATUS_BADGE: Record<
  UseCaseStatus,
  { label: string; className: string }
> = {
  proposed: { label: "Proposed", className: "bg-slate-700 text-slate-100" },
  in_progress: { label: "In progress", className: "bg-blue-700 text-blue-100" },
  realized: { label: "Realized", className: "bg-emerald-700 text-emerald-100" },
  on_hold: { label: "On hold", className: "bg-amber-700 text-amber-100" },
  rejected: { label: "Rejected", className: "bg-rose-900 text-rose-100" },
};

const LENS_BADGE: Record<
  NonNullable<UseCase["lens"]>,
  { label: string; className: string; tooltip: string }
> = {
  ready: {
    label: "Ready",
    className: "bg-emerald-900/40 text-emerald-300 border border-emerald-800",
    tooltip:
      "Generated with the actual table inventory available for this affiliate; can be implemented today",
  },
  gap: {
    label: "Gap",
    className: "bg-amber-900/40 text-amber-300 border border-amber-800",
    tooltip:
      "High-value use case requiring data sources the org has NOT ingested yet — drives ingest priorities",
  },
  manual: {
    label: "Manual",
    className: "bg-slate-800 text-slate-300 border border-slate-700",
    tooltip: "Created via chat or the Edit Center, not by the structured generator",
  },
};

function fmtUsd(n: number | null | undefined): string {
  if (!n) return "—";
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

/**
 * Coerce a "list of strings" field from the API into a string[].
 *
 * The backend already JSON-parses `data_requirements` and
 * `required_canonicals` before returning them, so the wire type is
 * `string[]` — but we still defensively handle the legacy shapes:
 *  - JSON-string blobs (older rows that survived a migration)
 *  - comma-separated strings (very early seeded rows)
 *  - null / undefined (column may not exist on stale rows)
 *
 * Returning `[]` on any malformed input keeps the UI from crashing —
 * which is what just bit us with `e.split is not a function`.
 */
function asStringArray(raw: unknown): string[] {
  if (!raw) return [];
  if (Array.isArray(raw)) return raw.map((x) => String(x));
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed) return [];
    if (trimmed.startsWith("[")) {
      try {
        const v = JSON.parse(trimmed);
        return Array.isArray(v) ? v.map((x) => String(x)) : [];
      } catch {
        return [];
      }
    }
    return trimmed
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return [];
}

function UseCasesPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [deptFilter, setDeptFilter] = useState<string>("__all");
  const [statusFilter, setStatusFilter] = useState<string>("__all");
  const [lensFilter, setLensFilter] = useState<string>("__all");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [generateOpen, setGenerateOpen] = useState(false);
  const [discoveryOpen, setDiscoveryOpen] = useState(false);

  const { data: useCases, isLoading } = useQuery({
    queryKey: ["useCases"],
    queryFn: () => fetchUseCases() as Promise<UseCase[]>,
  });

  const { data: departments } = useQuery({
    queryKey: ["departments"],
    queryFn: fetchDepartments,
  });

  const { data: affiliates } = useQuery({
    queryKey: ["editAffiliates"],
    queryFn: fetchEditAffiliates,
    staleTime: 60_000,
  });

  const statusMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: UseCaseStatus }) =>
      updateUseCaseStatus(id, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["useCases"] });
      toast.success("Status updated");
    },
    onError: (err: any) => {
      toast.error(`Update failed: ${err?.message || err}`);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteUseCase(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["useCases"] });
      toast.success("Use case deleted");
    },
    onError: (err: any) => {
      toast.error(`Delete failed: ${err?.message || err}`);
    },
  });

  // Departments come from `silver.departments`. The use cases table has a
  // free-form `department` string that may not 1:1 match (chat-created UCs
  // can name anything), so the filter dropdown unions both sources.
  const deptOptions = useMemo(() => {
    const set = new Set<string>();
    for (const d of departments || []) {
      const name = (d as any).department_name;
      if (name) set.add(name);
    }
    for (const uc of useCases || []) {
      if (uc.department) set.add(uc.department);
    }
    return Array.from(set).sort();
  }, [departments, useCases]);

  const filtered = useMemo(() => {
    let rows = useCases || [];
    if (deptFilter !== "__all") {
      rows = rows.filter((u) => u.department === deptFilter);
    }
    if (statusFilter !== "__all") {
      rows = rows.filter((u) => u.status === statusFilter);
    }
    if (lensFilter !== "__all") {
      rows = rows.filter((u) => (u.lens || "manual") === lensFilter);
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter(
        (u) =>
          u.use_case_name.toLowerCase().includes(q) ||
          (u.description || "").toLowerCase().includes(q) ||
          (u.business_value || "").toLowerCase().includes(q),
      );
    }
    return rows;
  }, [useCases, search, deptFilter, statusFilter, lensFilter]);

  const totalValue = useMemo(
    () =>
      (filtered || []).reduce(
        (acc, u) => acc + (u.estimated_value_usd || 0),
        0,
      ),
    [filtered],
  );

  const lensCounts = useMemo(() => {
    const counts = { ready: 0, gap: 0, manual: 0 };
    for (const u of useCases || []) {
      const l = (u.lens || "manual") as keyof typeof counts;
      counts[l] = (counts[l] || 0) + 1;
    }
    return counts;
  }, [useCases]);

  return (
    <div className="p-6 space-y-6 max-w-7xl">
      {/* Page header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Lightbulb className="h-6 w-6 text-amber-400" />
            Use Cases
          </h1>
          <p className="text-sm text-muted-foreground mt-1 max-w-2xl">
            Catalogue of high-value initiatives this catalog can power. Generate
            new ones grounded in your departments, affiliates, and canonical
            sources — separately for what's <span className="text-emerald-400">
              ready today
            </span>{" "}
            (data already in the catalog) and{" "}
            <span className="text-amber-400">data gaps</span> (high-value but
            not yet ingested).
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="lg"
            variant="outline"
            onClick={() => setDiscoveryOpen(true)}
            title="Map catalog prefixes to BHE programs and affiliates so 'ready' lens UC generation can surface real canonicals"
          >
            <MapIcon className="h-4 w-4 mr-2" />
            Map programs
          </Button>
          <Button size="lg" onClick={() => setGenerateOpen(true)}>
            <Sparkles className="h-4 w-4 mr-2" />
            Generate Use Cases
          </Button>
        </div>
      </div>

      {generateOpen && (
        <GenerateDialog
          affiliates={(affiliates || []).map((a: any) => a.affiliate_name)}
          departments={(departments || [])
            .map((d: any) => d.department_name)
            .filter(Boolean)}
          onClose={() => setGenerateOpen(false)}
          onCommitted={() => {
            queryClient.invalidateQueries({ queryKey: ["useCases"] });
            setGenerateOpen(false);
          }}
        />
      )}

      {discoveryOpen && (
        <ProgramDiscoveryDialog
          onClose={() => setDiscoveryOpen(false)}
          onCommitted={() => {
            // After committing rules + maps, the affiliate -> canonicals
            // resolution will improve once populate-gold finishes (it's
            // auto-fired by the commit endpoint). UC list isn't directly
            // affected, but invalidate caches that depend on the program
            // mapping so anything reactive picks up the new state.
            queryClient.invalidateQueries({ queryKey: ["editAffiliates"] });
            setDiscoveryOpen(false);
          }}
        />
      )}

      {/* Quick stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Total</CardDescription>
            <CardTitle className="text-2xl">
              {useCases?.length ?? "—"}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Est. value</CardDescription>
            <CardTitle className="text-2xl text-emerald-400">
              {fmtUsd(totalValue)}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="flex items-center gap-1">
              <span className="inline-block w-2 h-2 rounded-full bg-emerald-500" />
              Ready
            </CardDescription>
            <CardTitle className="text-2xl">{lensCounts.ready}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="flex items-center gap-1">
              <span className="inline-block w-2 h-2 rounded-full bg-amber-500" />
              Gap
            </CardDescription>
            <CardTitle className="text-2xl">{lensCounts.gap}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      {/* Filters */}
      <Card>
        <CardContent className="pt-6">
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search name, description, value..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-9"
              />
              {search && (
                <button
                  type="button"
                  onClick={() => setSearch("")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  aria-label="clear search"
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
            <select
              value={deptFilter}
              onChange={(e) => setDeptFilter(e.target.value)}
              className="border rounded-md bg-background h-9 px-3 text-sm"
            >
              <option value="__all">All departments</option>
              {deptOptions.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="border rounded-md bg-background h-9 px-3 text-sm"
            >
              <option value="__all">All statuses</option>
              {USE_CASE_STATUS_ORDER.map((s) => (
                <option key={s} value={s}>
                  {USE_CASE_STATUS_LABEL[s]}
                </option>
              ))}
            </select>
            <select
              value={lensFilter}
              onChange={(e) => setLensFilter(e.target.value)}
              className="border rounded-md bg-background h-9 px-3 text-sm"
            >
              <option value="__all">All lenses</option>
              <option value="ready">Ready</option>
              <option value="gap">Gap</option>
              <option value="manual">Manual</option>
            </select>
          </div>
        </CardContent>
      </Card>

      {/* List */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {isLoading
              ? "Loading..."
              : `${filtered.length} of ${useCases?.length ?? 0} use cases`}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState hasAny={(useCases?.length ?? 0) > 0} />
          ) : (
            <div className="divide-y divide-border">
              {filtered.map((uc) => (
                <UseCaseRow
                  key={uc.id}
                  uc={uc}
                  expanded={expandedId === uc.id}
                  onToggle={() =>
                    setExpandedId(expandedId === uc.id ? null : uc.id)
                  }
                  onStatusChange={(status) =>
                    statusMutation.mutate({ id: uc.id, status })
                  }
                  onDelete={() => {
                    if (
                      confirm(
                        `Delete use case "${uc.use_case_name}"? This cannot be undone.`,
                      )
                    ) {
                      deleteMutation.mutate(uc.id);
                    }
                  }}
                  isMutating={
                    (statusMutation.isPending &&
                      statusMutation.variables?.id === uc.id) ||
                    (deleteMutation.isPending &&
                      deleteMutation.variables === uc.id)
                  }
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function UseCaseRow({
  uc,
  expanded,
  onToggle,
  onStatusChange,
  onDelete,
  isMutating,
}: {
  uc: UseCase;
  expanded: boolean;
  onToggle: () => void;
  onStatusChange: (s: UseCaseStatus) => void;
  onDelete: () => void;
  isMutating: boolean;
}) {
  const lens = (uc.lens || "manual") as NonNullable<UseCase["lens"]>;
  const lensCfg = LENS_BADGE[lens];
  const statusCfg = STATUS_BADGE[uc.status] || {
    label: uc.status,
    className: "bg-slate-700 text-slate-100",
  };
  const dataReqs = asStringArray(uc.data_requirements);

  return (
    <div className={`py-3 ${isMutating ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-3">
        <button
          type="button"
          onClick={onToggle}
          className="mt-1 text-muted-foreground hover:text-foreground"
          aria-label={expanded ? "collapse" : "expand"}
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
        </button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3
              className="font-medium cursor-pointer hover:text-primary"
              onClick={onToggle}
            >
              {uc.use_case_name}
            </h3>
            <Badge className={lensCfg.className} title={lensCfg.tooltip}>
              {lensCfg.label}
            </Badge>
            {uc.priority && (
              <Badge variant="outline" className="text-xs">
                {uc.priority}
              </Badge>
            )}
            {uc.time_horizon && (
              <Badge
                variant="outline"
                className="text-xs flex items-center gap-1"
                title={
                  uc.time_horizon === "quick_win"
                    ? "Deliverable in ≤3 months"
                    : "Strategic 6-18 month initiative"
                }
              >
                <Clock className="h-3 w-3" />
                {uc.time_horizon === "quick_win" ? "Quick win" : "Strategic"}
              </Badge>
            )}
            {uc.value_type && (
              <Badge
                variant="outline"
                className="text-xs flex items-center gap-1"
                title={`Primary value driver: ${uc.value_type}`}
              >
                <DollarSign className="h-3 w-3" />
                {uc.value_type}
              </Badge>
            )}
            {uc.is_regulatory && (
              <Badge
                className="bg-orange-900/40 text-orange-300 border border-orange-800 text-xs flex items-center gap-1"
                title="Driven by a regulatory or compliance requirement"
              >
                <ShieldAlert className="h-3 w-3" />
                Regulatory
              </Badge>
            )}
            {uc.affiliate && (
              <span className="text-xs text-muted-foreground">
                · {uc.affiliate}
              </span>
            )}
            {uc.department && (
              <span className="text-xs text-muted-foreground">
                · {uc.department}
              </span>
            )}
          </div>
          {!expanded && uc.description && (
            <p className="text-sm text-muted-foreground line-clamp-1 mt-0.5">
              {uc.description}
            </p>
          )}
        </div>
        <div className="text-sm text-emerald-400 font-medium whitespace-nowrap">
          {fmtUsd(uc.estimated_value_usd)}
        </div>
        <select
          value={uc.status}
          onChange={(e) => onStatusChange(e.target.value as UseCaseStatus)}
          disabled={isMutating}
          className={`border rounded-md h-8 px-2 text-xs font-medium ${statusCfg.className}`}
          aria-label="status"
        >
          {USE_CASE_STATUS_ORDER.map((s) => (
            <option key={s} value={s} className="bg-background text-foreground">
              {USE_CASE_STATUS_LABEL[s]}
            </option>
          ))}
        </select>
        <Button
          size="icon"
          variant="ghost"
          onClick={onDelete}
          disabled={isMutating}
          aria-label="delete"
          title="Delete this use case"
        >
          <Trash2 className="h-4 w-4 text-rose-400" />
        </Button>
      </div>
      {expanded && (
        <div className="mt-3 pl-7 space-y-2 text-sm">
          {uc.description && (
            <p className="text-muted-foreground whitespace-pre-wrap">
              {uc.description}
            </p>
          )}
          {uc.business_value && (
            <div>
              <span className="text-xs text-muted-foreground uppercase tracking-wide">
                Business value
              </span>
              <p className="mt-0.5">{uc.business_value}</p>
            </div>
          )}
          {uc.value_rationale && (
            <div>
              <span className="text-xs text-muted-foreground uppercase tracking-wide">
                Rationale
              </span>
              <p className="mt-0.5 text-muted-foreground">
                {uc.value_rationale}
              </p>
            </div>
          )}
          {dataReqs.length > 0 && (
            <div>
              <span className="text-xs text-muted-foreground uppercase tracking-wide">
                Data requirements
              </span>
              <div className="flex flex-wrap gap-1 mt-1">
                {dataReqs.map((req, i) => (
                  <Badge key={i} variant="outline" className="text-xs">
                    {req}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          {(() => {
            const cans = asStringArray(uc.required_canonicals);
            if (cans.length === 0) return null;
            return (
              <div>
                <span className="text-xs text-muted-foreground uppercase tracking-wide">
                  {lens === "gap" ? "Required (to ingest)" : "Backed by sources"}
                </span>
                <div className="flex flex-wrap gap-1 mt-1">
                  {cans.map((c, i) => (
                    <Badge
                      key={i}
                      variant="outline"
                      className={`text-xs ${
                        lens === "gap"
                          ? "border-amber-700 text-amber-300"
                          : "border-emerald-700 text-emerald-300"
                      }`}
                    >
                      {c}
                    </Badge>
                  ))}
                </div>
              </div>
            );
          })()}
          {uc.status_notes && (
            <div className="text-xs text-muted-foreground">
              Status note: {uc.status_notes}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// GenerateDialog: structured (affiliate × department × lens × biases) UC
// generation. Two phases in one modal:
//   1. Form  — user picks scope + biases, hits "Preview"
//   2. Preview — server returns LLM candidates; user (de)selects, hits "Add"
//
// We deliberately keep both phases inside the same dialog to make the
// preview-then-commit flow obvious and to share the close handler.
// ---------------------------------------------------------------------------

function GenerateDialog({
  affiliates,
  departments,
  onClose,
  onCommitted,
}: {
  affiliates: string[];
  departments: string[];
  onClose: () => void;
  onCommitted: () => void;
}) {
  // Phase state. `preview` is set after a successful `generate` call.
  const [preview, setPreview] = useState<UseCaseGenerateOut | null>(null);

  // Form state
  const [affiliate, setAffiliate] = useState<string>(affiliates[0] || "");
  const [department, setDepartment] = useState<string>(departments[0] || "");
  const [count, setCount] = useState<number>(5);
  const [lens, setLens] = useState<UseCaseLens>("ready");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [timeHorizon, setTimeHorizon] = useState<UseCaseTimeHorizon>("any");
  const [valueType, setValueType] = useState<UseCaseValueType>("any");
  const [prioritizeRegulatory, setPrioritizeRegulatory] = useState(false);

  // Preview state (selection)
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    // Seed defaults the first time options arrive.
    if (!affiliate && affiliates[0]) setAffiliate(affiliates[0]);
    if (!department && departments[0]) setDepartment(departments[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [affiliates.length, departments.length]);

  const generateMut = useMutation({
    mutationFn: () =>
      generateUseCases({
        affiliate,
        department,
        count,
        lens,
        time_horizon: timeHorizon,
        value_type: valueType,
        prioritize_regulatory: prioritizeRegulatory,
      }),
    onSuccess: (data) => {
      setPreview(data);
      setSelected(new Set(data.candidates.map((c) => c.candidate_id)));
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail || err?.message || String(err);
      toast.error(`Generation failed: ${detail}`);
    },
  });

  const commitMut = useMutation({
    mutationFn: () =>
      commitGeneratedUseCases(preview!.preview_id, Array.from(selected)),
    onSuccess: (data) => {
      const skippedNote =
        data.skipped > 0 ? ` (${data.skipped} duplicate skipped)` : "";
      toast.success(
        `${data.inserted} use case${data.inserted === 1 ? "" : "s"} added${skippedNote}`,
      );
      onCommitted();
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail || err?.message || String(err);
      toast.error(`Commit failed: ${detail}`);
    },
  });

  const canSubmit =
    affiliate && department && count >= 1 && count <= 20 && !generateMut.isPending;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-background border rounded-lg shadow-xl max-w-3xl w-full max-h-[90vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-6 py-4 border-b flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <Sparkles className="h-5 w-5 text-amber-400" />
              {preview ? "Review generated use cases" : "Generate Use Cases"}
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {preview
                ? `${preview.candidates.length} candidate${
                    preview.candidates.length === 1 ? "" : "s"
                  } for ${preview.affiliate} · ${preview.department} · ${
                    preview.lens
                  } lens. Pick the ones to keep.`
                : "Scope generation to a single affiliate + department. The LLM grounds every use case in your canonical sources."}
            </p>
          </div>
          <Button size="icon" variant="ghost" onClick={onClose} aria-label="close">
            <X className="h-4 w-4" />
          </Button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-6">
          {!preview ? (
            <GenerateForm
              affiliates={affiliates}
              departments={departments}
              affiliate={affiliate}
              setAffiliate={setAffiliate}
              department={department}
              setDepartment={setDepartment}
              count={count}
              setCount={setCount}
              lens={lens}
              setLens={setLens}
              showAdvanced={showAdvanced}
              setShowAdvanced={setShowAdvanced}
              timeHorizon={timeHorizon}
              setTimeHorizon={setTimeHorizon}
              valueType={valueType}
              setValueType={setValueType}
              prioritizeRegulatory={prioritizeRegulatory}
              setPrioritizeRegulatory={setPrioritizeRegulatory}
            />
          ) : (
            <PreviewBody
              preview={preview}
              selected={selected}
              setSelected={setSelected}
            />
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t flex justify-between items-center gap-2 bg-muted/30">
          {!preview ? (
            <>
              <p className="text-xs text-muted-foreground">
                Preview is cached for 10 min — nothing is saved until you commit.
              </p>
              <div className="flex gap-2">
                <Button variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button
                  disabled={!canSubmit}
                  onClick={() => generateMut.mutate()}
                >
                  {generateMut.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      Generating...
                    </>
                  ) : (
                    <>
                      <Sparkles className="h-4 w-4 mr-2" />
                      Preview
                    </>
                  )}
                </Button>
              </div>
            </>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">
                {selected.size} of {preview.candidates.length} selected
              </p>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setPreview(null)}>
                  Back to form
                </Button>
                <Button
                  disabled={selected.size === 0 || commitMut.isPending}
                  onClick={() => commitMut.mutate()}
                >
                  {commitMut.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      Adding...
                    </>
                  ) : (
                    `Add ${selected.size} use case${selected.size === 1 ? "" : "s"}`
                  )}
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function GenerateForm(props: {
  affiliates: string[];
  departments: string[];
  affiliate: string;
  setAffiliate: (v: string) => void;
  department: string;
  setDepartment: (v: string) => void;
  count: number;
  setCount: (n: number) => void;
  lens: UseCaseLens;
  setLens: (v: UseCaseLens) => void;
  showAdvanced: boolean;
  setShowAdvanced: (b: boolean) => void;
  timeHorizon: UseCaseTimeHorizon;
  setTimeHorizon: (v: UseCaseTimeHorizon) => void;
  valueType: UseCaseValueType;
  setValueType: (v: UseCaseValueType) => void;
  prioritizeRegulatory: boolean;
  setPrioritizeRegulatory: (b: boolean) => void;
}) {
  const {
    affiliates,
    departments,
    affiliate,
    setAffiliate,
    department,
    setDepartment,
    count,
    setCount,
    lens,
    setLens,
    showAdvanced,
    setShowAdvanced,
    timeHorizon,
    setTimeHorizon,
    valueType,
    setValueType,
    prioritizeRegulatory,
    setPrioritizeRegulatory,
  } = props;

  const noAffiliates = affiliates.length === 0;
  const noDepartments = departments.length === 0;

  return (
    <div className="space-y-5">
      {(noAffiliates || noDepartments) && (
        <div className="rounded-md border border-amber-800 bg-amber-950/30 p-3 text-sm text-amber-300 flex gap-2">
          <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
          <div>
            {noAffiliates && (
              <p>
                No affiliates found. Run company research (Company Setup → Step 5)
                first.
              </p>
            )}
            {noDepartments && <p>No departments found.</p>}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Affiliate" required>
          <select
            value={affiliate}
            onChange={(e) => setAffiliate(e.target.value)}
            disabled={noAffiliates}
            className="border rounded-md bg-background h-9 px-3 text-sm w-full"
          >
            {affiliates.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Department" required>
          <select
            value={department}
            onChange={(e) => setDepartment(e.target.value)}
            disabled={noDepartments}
            className="border rounded-md bg-background h-9 px-3 text-sm w-full"
          >
            {departments.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field
        label="Number of use cases"
        hint="1–20. Smaller batches keep LLM cost down and let you iterate."
      >
        <Input
          type="number"
          min={1}
          max={20}
          value={count}
          onChange={(e) => {
            const n = Number.parseInt(e.target.value, 10);
            setCount(Number.isFinite(n) ? Math.max(1, Math.min(20, n)) : 5);
          }}
          className="max-w-[120px]"
        />
      </Field>

      <Field
        label="Lens"
        hint="Drives what context is fed into the prompt — actionable today vs. ingest targets."
      >
        <div className="flex gap-2 flex-wrap">
          <LensOption
            current={lens}
            value="ready"
            label="Ready"
            description="Only what we can build with today's data"
            colorClass="border-emerald-700 data-[active=true]:bg-emerald-900/30 data-[active=true]:text-emerald-200"
            onClick={() => setLens("ready")}
          />
          <LensOption
            current={lens}
            value="gap"
            label="Gap"
            description="High-value UCs needing data we haven't ingested"
            colorClass="border-amber-700 data-[active=true]:bg-amber-900/30 data-[active=true]:text-amber-200"
            onClick={() => setLens("gap")}
          />
          <LensOption
            current={lens}
            value="both"
            label="Both"
            description="Mixed batch, each tagged"
            colorClass="border-slate-600 data-[active=true]:bg-slate-700/40 data-[active=true]:text-slate-100"
            onClick={() => setLens("both")}
          />
        </div>
      </Field>

      <button
        type="button"
        onClick={() => setShowAdvanced(!showAdvanced)}
        className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1"
      >
        {showAdvanced ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        Advanced biases (optional)
      </button>

      {showAdvanced && (
        <div className="space-y-4 pl-5 border-l border-border">
          <Field
            label="Time horizon focus"
            hint="Soft preference — every UC still gets tagged based on its description."
          >
            <select
              value={timeHorizon}
              onChange={(e) =>
                setTimeHorizon(e.target.value as UseCaseTimeHorizon)
              }
              className="border rounded-md bg-background h-9 px-3 text-sm"
            >
              <option value="any">Any (no preference)</option>
              <option value="quick_win">Quick wins (≤3 months)</option>
              <option value="strategic">Strategic (6–18 months)</option>
            </select>
          </Field>
          <Field
            label="Value type focus"
            hint="Where should the value primarily come from."
          >
            <select
              value={valueType}
              onChange={(e) => setValueType(e.target.value as UseCaseValueType)}
              className="border rounded-md bg-background h-9 px-3 text-sm"
            >
              <option value="any">Any (no preference)</option>
              <option value="cost">Cost reduction</option>
              <option value="revenue">Revenue growth</option>
              <option value="risk">Risk &amp; compliance</option>
            </select>
          </Field>
          <Field label="Prioritize regulatory drivers">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={prioritizeRegulatory}
                onChange={(e) => setPrioritizeRegulatory(e.target.checked)}
              />
              <span>
                Force at least 30% of use cases to be regulatory-driven
              </span>
            </label>
          </Field>
        </div>
      )}
    </div>
  );
}

function LensOption({
  current,
  value,
  label,
  description,
  colorClass,
  onClick,
}: {
  current: UseCaseLens;
  value: UseCaseLens;
  label: string;
  description: string;
  colorClass: string;
  onClick: () => void;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      onClick={onClick}
      data-active={active}
      className={`flex-1 min-w-[140px] text-left border rounded-md p-3 transition-colors ${colorClass} ${
        active ? "" : "hover:bg-muted/50"
      }`}
    >
      <div className="font-medium text-sm">{label}</div>
      <div className="text-xs text-muted-foreground mt-0.5">{description}</div>
    </button>
  );
}

function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="text-sm font-medium block mb-1.5">
        {label}
        {required && <span className="text-rose-400 ml-0.5">*</span>}
      </label>
      {children}
      {hint && (
        <p className="text-xs text-muted-foreground mt-1.5">{hint}</p>
      )}
    </div>
  );
}

function PreviewBody({
  preview,
  selected,
  setSelected,
}: {
  preview: UseCaseGenerateOut;
  selected: Set<string>;
  setSelected: (s: Set<string>) => void;
}) {
  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }
  function selectAll() {
    setSelected(new Set(preview.candidates.map((c) => c.candidate_id)));
  }
  function selectNone() {
    setSelected(new Set());
  }
  const totalValue = preview.candidates
    .filter((c) => selected.has(c.candidate_id))
    .reduce((acc, c) => acc + (c.estimated_value_usd || 0), 0);

  return (
    <div className="space-y-4">
      {/* Provenance strip */}
      <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
          <div>
            <span className="uppercase tracking-wide">Sources used</span>
            <p className="text-foreground mt-0.5">
              {preview.canonicals_present.length} present ·{" "}
              {preview.table_sample_count} sample tables
            </p>
          </div>
          <div>
            <span className="uppercase tracking-wide">Gap candidates</span>
            <p className="text-foreground mt-0.5">
              {preview.canonicals_missing.length} missing canonicals
            </p>
          </div>
          <div>
            <span className="uppercase tracking-wide">Selected value</span>
            <p className="text-emerald-400 font-medium mt-0.5">
              {fmtUsd(totalValue)}
            </p>
          </div>
        </div>
      </div>

      {/* Bulk actions */}
      <div className="flex items-center justify-between text-xs">
        <div className="flex gap-3">
          <button
            type="button"
            onClick={selectAll}
            className="text-primary hover:underline"
          >
            Select all
          </button>
          <button
            type="button"
            onClick={selectNone}
            className="text-muted-foreground hover:text-foreground"
          >
            Select none
          </button>
        </div>
        <span className="text-muted-foreground">
          Uncheck the ones you don't want before committing.
        </span>
      </div>

      {/* Candidate list */}
      <div className="space-y-2">
        {preview.candidates.map((cand) => (
          <CandidateCard
            key={cand.candidate_id}
            cand={cand}
            checked={selected.has(cand.candidate_id)}
            onToggle={() => toggle(cand.candidate_id)}
          />
        ))}
      </div>
    </div>
  );
}

function CandidateCard({
  cand,
  checked,
  onToggle,
}: {
  cand: UseCaseCandidate;
  checked: boolean;
  onToggle: () => void;
}) {
  const lensCfg = LENS_BADGE[cand.lens] || LENS_BADGE.ready;
  return (
    <div
      className={`border rounded-md p-3 transition-colors cursor-pointer ${
        checked ? "border-primary/50 bg-primary/5" : "hover:bg-muted/30"
      }`}
      onClick={onToggle}
    >
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          onClick={(e) => e.stopPropagation()}
          className="mt-1"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h4 className="font-medium">{cand.use_case_name}</h4>
            <Badge className={lensCfg.className}>{lensCfg.label}</Badge>
            {cand.priority && (
              <Badge variant="outline" className="text-xs">
                {cand.priority}
              </Badge>
            )}
            {cand.time_horizon && (
              <Badge variant="outline" className="text-xs flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {cand.time_horizon === "quick_win" ? "Quick win" : "Strategic"}
              </Badge>
            )}
            {cand.value_type && (
              <Badge variant="outline" className="text-xs flex items-center gap-1">
                <DollarSign className="h-3 w-3" />
                {cand.value_type}
              </Badge>
            )}
            {cand.is_regulatory && (
              <Badge className="bg-orange-900/40 text-orange-300 border border-orange-800 text-xs flex items-center gap-1">
                <ShieldAlert className="h-3 w-3" />
                Regulatory
              </Badge>
            )}
          </div>
          {cand.description && (
            <p className="text-sm text-muted-foreground mt-1">
              {cand.description}
            </p>
          )}
          {cand.required_canonicals.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {cand.required_canonicals.map((c, i) => (
                <Badge
                  key={i}
                  variant="outline"
                  className={`text-xs ${
                    cand.lens === "gap"
                      ? "border-amber-700 text-amber-300"
                      : "border-emerald-700 text-emerald-300"
                  }`}
                >
                  {c}
                </Badge>
              ))}
            </div>
          )}
        </div>
        <div className="text-sm text-emerald-400 font-medium whitespace-nowrap">
          {fmtUsd(cand.estimated_value_usd)}
        </div>
      </div>
    </div>
  );
}

function EmptyState({ hasAny }: { hasAny: boolean }) {
  return (
    <div className="text-center py-12 text-muted-foreground">
      <Lightbulb className="h-10 w-10 mx-auto mb-3 opacity-30" />
      {hasAny ? (
        <p>No use cases match the current filters.</p>
      ) : (
        <>
          <p className="font-medium">No use cases yet</p>
          <p className="text-sm mt-1 max-w-md mx-auto">
            Use cases come from two sources: structured generation (coming next
            iteration) and free-form chat. Open the chat assistant and ask it
            to generate use cases for a department to get started.
          </p>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Program discovery dialog.
//
// One-shot LLM mapping of catalog prefixes -> programs -> affiliates. Unblocks
// `lens=ready` UC generation when the deploy starts with no `category=program`
// rules in classification_rules.
// ---------------------------------------------------------------------------

const CONFIDENCE_BADGE: Record<string, { label: string; cls: string }> = {
  high: { label: "high", cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" },
  medium: { label: "medium", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
  low: { label: "low", cls: "bg-rose-500/15 text-rose-300 border-rose-500/30" },
};

function ProgramDiscoveryDialog({
  onClose,
  onCommitted,
}: {
  onClose: () => void;
  onCommitted: () => void;
}) {
  const [preview, setPreview] = useState<ProgramsDiscoverOut | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [topN, setTopN] = useState(25);

  const discoverMut = useMutation({
    mutationFn: () => discoverPrograms({ top_n: topN, min_schema_count: 3 }),
    onSuccess: (data) => {
      setPreview(data);
      // Default-select everything HIGH confidence; let user opt-in for lower.
      setSelected(
        new Set(
          data.proposals
            .filter((p) => p.confidence === "high")
            .map((p) => p.proposal_id),
        ),
      );
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail || err?.message || String(err);
      toast.error(`Discovery failed: ${detail}`);
    },
  });

  const commitMut = useMutation({
    mutationFn: () =>
      commitDiscoveredPrograms({
        preview_id: preview!.preview_id,
        selected_ids: Array.from(selected),
        // Always echo the proposals back so the commit succeeds even if the
        // in-process preview cache missed (e.g. workers > 1 split discover
        // and commit across processes).
        proposals: preview!.proposals,
        run_populate_gold: true,
      }),
    onSuccess: (data) => {
      const parts: string[] = [];
      if (data.rules_inserted) parts.push(`${data.rules_inserted} rule${data.rules_inserted === 1 ? "" : "s"}`);
      if (data.maps_inserted) parts.push(`${data.maps_inserted} mapping${data.maps_inserted === 1 ? "" : "s"}`);
      const msg = parts.length ? `Added ${parts.join(" + ")}.` : "Nothing new added.";
      const tail = data.populate_gold_run_id
        ? " Re-running populate-gold in the background — refresh in ~1 minute."
        : "";
      toast.success(msg + tail);
      onCommitted();
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail || err?.message || String(err);
      toast.error(`Commit failed: ${detail}`);
    },
  });

  // Auto-fire discover when the dialog mounts so the user lands on results,
  // not on a blank "click here" form. Top_n is tunable via the chip below
  // if the first pass missed something.
  useEffect(() => {
    if (!preview && !discoverMut.isPending && !discoverMut.isError) {
      discoverMut.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleAll = (val: boolean) => {
    if (!preview) return;
    setSelected(val ? new Set(preview.proposals.map((p) => p.proposal_id)) : new Set());
  };

  const togglePid = (pid: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-background border rounded-lg shadow-xl max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 border-b flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <MapIcon className="h-5 w-5 text-sky-400" />
              Map catalog prefixes to programs &amp; affiliates
            </h2>
            <p className="text-xs text-muted-foreground mt-1">
              The LLM looks at the top catalog prefixes in <code>silver_schemas</code>{" "}
              and maps them to programs and operating affiliates. Selected rows
              are written to <code>classification_rules</code> and{" "}
              <code>program_affiliate_map</code>; populate-gold re-runs to
              backfill <code>silver_schemas.program</code> immediately.
            </p>
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4">
          {discoverMut.isPending && !preview && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="h-6 w-6 mr-2 animate-spin" />
              Asking the LLM to map your catalogs...
            </div>
          )}

          {discoverMut.isError && !preview && (
            <div className="text-center py-12">
              <AlertCircle className="h-8 w-8 mx-auto text-rose-400 mb-2" />
              <p className="text-sm text-muted-foreground mb-3">
                Discovery failed. Check that company research and AI enrichment
                have run.
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => discoverMut.mutate()}
              >
                Retry
              </Button>
            </div>
          )}

          {preview && (
            <>
              <div className="flex items-center justify-between mb-3">
                <div className="text-sm text-muted-foreground">
                  {preview.proposals.length} proposals against{" "}
                  {preview.affiliates_considered.length} affiliates
                  {preview.company_name ? ` for ${preview.company_name}` : ""}.
                </div>
                <div className="flex items-center gap-2">
                  <Button variant="ghost" size="sm" onClick={() => toggleAll(true)}>
                    Select all
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => toggleAll(false)}>
                    Clear
                  </Button>
                </div>
              </div>

              <div className="space-y-2">
                {preview.proposals.map((p) => (
                  <ProposalRow
                    key={p.proposal_id}
                    proposal={p}
                    checked={selected.has(p.proposal_id)}
                    onToggle={() => togglePid(p.proposal_id)}
                  />
                ))}
              </div>

              <div className="mt-4 flex items-center gap-3 text-xs text-muted-foreground">
                <span>Want more?</span>
                <select
                  value={topN}
                  onChange={(e) => setTopN(Number(e.target.value))}
                  className="border rounded-md bg-background h-7 px-2 text-xs"
                >
                  <option value={10}>top 10</option>
                  <option value={25}>top 25</option>
                  <option value={50}>top 50</option>
                </select>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setPreview(null);
                    setSelected(new Set());
                    discoverMut.mutate();
                  }}
                  disabled={discoverMut.isPending}
                >
                  {discoverMut.isPending ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    "Re-discover"
                  )}
                </Button>
              </div>
            </>
          )}
        </div>

        {preview && (
          <div className="border-t px-6 py-4 flex items-center justify-between">
            <div className="text-sm text-muted-foreground">
              {selected.size} selected
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button
                onClick={() => commitMut.mutate()}
                disabled={selected.size === 0 || commitMut.isPending}
              >
                {commitMut.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Committing...
                  </>
                ) : (
                  `Commit ${selected.size} mapping${selected.size === 1 ? "" : "s"}`
                )}
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ProposalRow({
  proposal,
  checked,
  onToggle,
}: {
  proposal: ProgramDiscoveryProposal;
  checked: boolean;
  onToggle: () => void;
}) {
  const conf = CONFIDENCE_BADGE[proposal.confidence] || CONFIDENCE_BADGE.medium;
  return (
    <div
      className={`border rounded-md p-3 cursor-pointer transition-colors ${
        checked ? "border-primary/50 bg-primary/5" : "hover:bg-muted/30"
      }`}
      onClick={onToggle}
    >
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          onClick={(e) => e.stopPropagation()}
          className="mt-1"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <code className="text-sm font-mono bg-muted/40 px-1.5 py-0.5 rounded">
              {proposal.catalog_pattern}_*
            </code>
            <span className="text-muted-foreground">→</span>
            <span className="font-medium">{proposal.program_name}</span>
            <span className="text-muted-foreground">→</span>
            <span className="text-sky-300">{proposal.affiliate_name}</span>
            <Badge variant="outline" className={`ml-1 ${conf.cls}`}>
              {conf.label}
            </Badge>
            <Badge variant="outline" className="text-xs">
              {proposal.schema_count} schemas
            </Badge>
          </div>
          {proposal.rationale && (
            <p className="text-xs text-muted-foreground mt-1.5 leading-snug">
              {proposal.rationale}
            </p>
          )}
          {proposal.sample_catalogs.length > 0 && (
            <div className="text-xs text-muted-foreground mt-1.5">
              <span className="opacity-70">Samples:</span>{" "}
              {proposal.sample_catalogs.slice(0, 3).map((c, i) => (
                <code key={c} className="font-mono">
                  {i > 0 ? ", " : ""}
                  {c}
                </code>
              ))}
              {proposal.sample_catalogs.length > 3 && (
                <span className="opacity-70">
                  {" "}+{proposal.sample_catalogs.length - 3} more
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
