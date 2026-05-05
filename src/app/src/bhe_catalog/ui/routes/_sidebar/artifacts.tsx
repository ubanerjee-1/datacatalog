import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect, useRef, useMemo } from "react";
import {
  fetchArtifacts,
  fetchArtifactFilters,
  fetchArtifactStats,
  fetchArtifactDetail,
  fetchArtifactVocabulary,
  updateArtifact,
  createArtifact,
  deleteArtifact,
  ingestArtifacts,
  uploadFile,
  triggerArtifactEnrichment,
  type Artifact,
  type ArtifactCreate,
  type ArtifactFilters,
} from "@/lib/api-client";
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
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import {
  Search,
  X,
  Filter,
  CheckCircle2,
  AlertTriangle,
  ExternalLink,
  Upload,
  Sparkles,
  BarChart3,
  FileBarChart,
  Brain,
  LayoutDashboard,
  MessagesSquare,
  Cpu,
  Notebook,
  Clock,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Loader2,
  Pencil,
  Save,
  Trash2,
  Shield,
  Plus,
} from "lucide-react";
import { toast } from "sonner";

export const Route = createFileRoute("/_sidebar/artifacts")({
  component: ArtifactsPage,
});

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

function formatDate(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function daysSince(s: string | null | undefined): number | null {
  if (!s) return null;
  const d = new Date(s);
  if (isNaN(d.getTime())) return null;
  return Math.floor((Date.now() - d.getTime()) / 86400000);
}

function typeIcon(type: string) {
  const props = { className: "h-4 w-4" };
  switch (type) {
    case "Dashboard":
      return <LayoutDashboard {...props} />;
    case "Genie Space":
      return <MessagesSquare {...props} />;
    case "AI Agent":
      return <Brain {...props} />;
    case "ML Model Endpoint":
      return <Cpu {...props} />;
    case "Notebook":
      return <Notebook {...props} />;
    default:
      return <FileBarChart {...props} />;
  }
}

function splitCsv(s: string | undefined | null): string[] {
  if (!s) return [];
  return s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

type SortKey =
  | "artifact_name"
  | "platform"
  | "business_team"
  | "business_owner"
  | "artifact_type"
  | "status"
  | "last_modified"
  | "certified";

function ArtifactsPage() {
  const queryClient = useQueryClient();

  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 250);
  const [platform, setPlatform] = useState("");
  const [artifactType, setArtifactType] = useState("");
  const [team, setTeam] = useState("");
  const [status, setStatus] = useState("");
  const [domain, setDomain] = useState("");
  const [certifiedOnly, setCertifiedOnly] = useState(false);
  const [sortBy, setSortBy] = useState<SortKey>("artifact_name");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [newOpen, setNewOpen] = useState(false);

  const limit = 50;

  const { data, isLoading, isFetching } = useQuery({
    queryKey: [
      "artifacts",
      {
        debouncedSearch,
        platform,
        artifactType,
        team,
        status,
        domain,
        certifiedOnly,
        sortBy,
        sortDir,
        offset,
      },
    ],
    queryFn: () =>
      fetchArtifacts({
        search: debouncedSearch || undefined,
        platform: platform || undefined,
        type: artifactType || undefined,
        team: team || undefined,
        status: status || undefined,
        domain: domain || undefined,
        certified: certifiedOnly ? true : undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
        limit,
        offset,
      }),
  });

  const { data: filters } = useQuery({
    queryKey: ["artifactFilters"],
    queryFn: fetchArtifactFilters,
  });

  const { data: stats } = useQuery({
    queryKey: ["artifactStats"],
    queryFn: fetchArtifactStats,
  });

  const artifacts = data?.artifacts || [];
  const total = data?.total || 0;

  const hasActiveFilters =
    platform || artifactType || team || status || domain || certifiedOnly || search;

  const clearFilters = () => {
    setPlatform("");
    setArtifactType("");
    setTeam("");
    setStatus("");
    setDomain("");
    setCertifiedOnly(false);
    setSearch("");
    setOffset(0);
  };

  const toggleSort = (key: SortKey) => {
    if (sortBy === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(key);
      setSortDir("asc");
    }
    setOffset(0);
  };

  const sortIcon = (key: SortKey) => {
    if (sortBy !== key)
      return <ArrowUpDown className="h-3 w-3 opacity-40" />;
    return sortDir === "asc" ? (
      <ArrowUp className="h-3 w-3" />
    ) : (
      <ArrowDown className="h-3 w-3" />
    );
  };

  // Reset offset when filters change
  useEffect(() => {
    setOffset(0);
  }, [debouncedSearch, platform, artifactType, team, status, domain, certifiedOnly]);

  const enrichMutation = useMutation({
    mutationFn: () => triggerArtifactEnrichment(500),
    onSuccess: () => {
      toast.success("AI enrichment started. This runs in the background.");
    },
    onError: (e: Error) => toast.error(`Enrichment failed: ${e.message}`),
  });

  return (
    <div className="p-6 space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">BI &amp; AI Artifacts</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Discover deployed reports, dashboards, Genie spaces, and AI agents
            across the organization. Filter by platform, team, or domain to
            find the artifact you need.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => enrichMutation.mutate()}
            disabled={enrichMutation.isPending}
          >
            {enrichMutation.isPending ? (
              <Loader2 className="h-4 w-4 mr-1 animate-spin" />
            ) : (
              <Sparkles className="h-4 w-4 mr-1" />
            )}
            Enrich with AI
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setNewOpen(true)}
          >
            <Plus className="h-4 w-4 mr-1" />
            New
          </Button>
          <Button size="sm" onClick={() => setUploadOpen(true)}>
            <Upload className="h-4 w-4 mr-1" />
            Upload CSV
          </Button>
        </div>
      </div>

      {/* Stats row */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard
            icon={<FileBarChart className="h-4 w-4" />}
            label="Total Artifacts"
            value={stats.total}
          />
          <StatCard
            icon={<Shield className="h-4 w-4" />}
            label="Certified"
            value={stats.certified}
            accent={
              stats.total > 0
                ? `${Math.round((stats.certified / stats.total) * 100)}%`
                : undefined
            }
          />
          <StatCard
            icon={<AlertTriangle className="h-4 w-4" />}
            label="Stale (30d+)"
            value={stats.stale}
            warn={stats.stale > 0}
          />
          <StatCard
            icon={<BarChart3 className="h-4 w-4" />}
            label="Platforms"
            value={stats.by_platform.length}
          />
        </div>
      )}

      {/* Filters */}
      <Card>
        <CardContent className="p-4 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative flex-1 min-w-[260px] max-w-md">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search name, description, topics, owner…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-9"
              />
              {search && (
                <button
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  onClick={() => setSearch("")}
                  aria-label="Clear search"
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
            <FilterSelect
              value={platform}
              onChange={setPlatform}
              options={filters?.platforms || []}
              placeholder="All platforms"
            />
            <FilterSelect
              value={artifactType}
              onChange={setArtifactType}
              options={filters?.types || []}
              placeholder="All types"
            />
            <FilterSelect
              value={team}
              onChange={setTeam}
              options={filters?.teams || []}
              placeholder="All teams"
            />
            <FilterSelect
              value={status}
              onChange={setStatus}
              options={filters?.statuses || []}
              placeholder="All statuses"
            />
            <FilterSelect
              value={domain}
              onChange={setDomain}
              options={filters?.domains || []}
              placeholder="All domains"
            />
            <label className="flex items-center gap-2 text-sm text-muted-foreground cursor-pointer">
              <input
                type="checkbox"
                checked={certifiedOnly}
                onChange={(e) => setCertifiedOnly(e.target.checked)}
              />
              Certified only
            </label>
            {hasActiveFilters && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X className="h-3 w-3 mr-1" />
                Clear
              </Button>
            )}
            <div className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
              <Filter className="h-3 w-3" />
              Showing{" "}
              <span className="font-semibold text-foreground">
                {artifacts.length}
              </span>{" "}
              of{" "}
              <span className="font-semibold text-foreground">
                {total.toLocaleString()}
              </span>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Table */}
      <Card>
        <CardContent className="p-0">
          <div className="overflow-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 sticky top-0 z-10">
                <tr className="border-b">
                  <SortHeader
                    label="Name"
                    onClick={() => toggleSort("artifact_name")}
                    icon={sortIcon("artifact_name")}
                  />
                  <SortHeader
                    label="Type"
                    onClick={() => toggleSort("artifact_type")}
                    icon={sortIcon("artifact_type")}
                  />
                  <SortHeader
                    label="Platform"
                    onClick={() => toggleSort("platform")}
                    icon={sortIcon("platform")}
                  />
                  <SortHeader
                    label="Business Team"
                    onClick={() => toggleSort("business_team")}
                    icon={sortIcon("business_team")}
                  />
                  <SortHeader
                    label="Owner"
                    onClick={() => toggleSort("business_owner")}
                    icon={sortIcon("business_owner")}
                  />
                  <SortHeader
                    label="Status"
                    onClick={() => toggleSort("status")}
                    icon={sortIcon("status")}
                  />
                  <SortHeader
                    label="Certified"
                    onClick={() => toggleSort("certified")}
                    icon={sortIcon("certified")}
                  />
                  <SortHeader
                    label="Last Modified"
                    onClick={() => toggleSort("last_modified")}
                    icon={sortIcon("last_modified")}
                  />
                </tr>
              </thead>
              <tbody>
                {isLoading && (
                  <tr>
                    <td colSpan={8} className="p-8 text-center text-muted-foreground">
                      <Loader2 className="h-5 w-5 animate-spin inline mr-2" />
                      Loading…
                    </td>
                  </tr>
                )}
                {!isLoading && artifacts.length === 0 && (
                  <tr>
                    <td colSpan={8} className="p-12 text-center">
                      <div className="text-muted-foreground space-y-2">
                        <FileBarChart className="h-10 w-10 mx-auto opacity-40" />
                        <div className="font-medium">No artifacts found</div>
                        <div className="text-xs">
                          {hasActiveFilters
                            ? "Try clearing some filters."
                            : "Upload a CSV to get started."}
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
                {artifacts.map((a) => {
                  const stale = (() => {
                    const d = daysSince(a.last_refreshed);
                    return d !== null && d > 30;
                  })();
                  return (
                    <tr
                      key={a.artifact_id}
                      onClick={() => setSelected(a)}
                      className="border-b hover:bg-muted/30 cursor-pointer"
                    >
                      <td className="p-3">
                        <div className="flex items-start gap-2">
                          <span className="text-muted-foreground mt-0.5">
                            {typeIcon(a.artifact_type)}
                          </span>
                          <div className="min-w-0">
                            <div className="font-medium truncate">
                              {a.artifact_name || "Untitled"}
                            </div>
                            {a.description && (
                              <div className="text-xs text-muted-foreground truncate max-w-md">
                                {a.description}
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className="p-3 text-muted-foreground">
                        {a.artifact_type || "—"}
                      </td>
                      <td className="p-3 text-muted-foreground">
                        {a.platform || "—"}
                      </td>
                      <td className="p-3 text-muted-foreground">
                        {a.business_team || "—"}
                      </td>
                      <td className="p-3 text-muted-foreground">
                        {a.business_owner || "—"}
                      </td>
                      <td className="p-3">
                        <StatusBadge status={a.status} />
                      </td>
                      <td className="p-3">
                        {a.certified ? (
                          <Badge
                            variant="outline"
                            className="text-green-500 border-green-500/30"
                          >
                            <CheckCircle2 className="h-3 w-3 mr-0.5" />
                            Yes
                          </Badge>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="p-3 text-xs text-muted-foreground whitespace-nowrap">
                        {formatDate(a.last_modified)}
                        {stale && (
                          <div className="text-[10px] text-orange-500">
                            stale
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {total > limit && (
            <div className="flex items-center justify-between p-3 border-t text-xs text-muted-foreground">
              <div>
                Showing {offset + 1}–{Math.min(offset + limit, total)} of{" "}
                {total.toLocaleString()}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + limit >= total}
                  onClick={() => setOffset(offset + limit)}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {isFetching && !isLoading && (
        <div className="text-xs text-muted-foreground">Refreshing…</div>
      )}

      {/* Detail Side Panel */}
      <Sheet
        open={!!selected}
        onOpenChange={(o) => {
          if (!o) setSelected(null);
        }}
      >
        <SheetContent
          side="right"
          className="w-full sm:max-w-xl overflow-y-auto"
        >
          {selected && (
            <ArtifactDetail
              artifactId={selected.artifact_id}
              onClose={() => setSelected(null)}
              onSaved={() => {
                queryClient.invalidateQueries({ queryKey: ["artifacts"] });
                queryClient.invalidateQueries({
                  queryKey: ["artifactFilters"],
                });
                queryClient.invalidateQueries({ queryKey: ["artifactStats"] });
              }}
            />
          )}
        </SheetContent>
      </Sheet>

      {/* Upload Modal */}
      <Sheet
        open={uploadOpen}
        onOpenChange={setUploadOpen}
      >
        <SheetContent side="right" className="w-full sm:max-w-md">
          <UploadPanel
            onClose={() => setUploadOpen(false)}
            onIngested={() => {
              queryClient.invalidateQueries({ queryKey: ["artifacts"] });
              queryClient.invalidateQueries({ queryKey: ["artifactFilters"] });
              queryClient.invalidateQueries({ queryKey: ["artifactStats"] });
            }}
          />
        </SheetContent>
      </Sheet>

      {/* New (manual entry) Modal */}
      <Sheet open={newOpen} onOpenChange={setNewOpen}>
        <SheetContent side="right" className="w-full sm:max-w-md">
          <NewArtifactPanel
            filters={filters}
            onClose={() => setNewOpen(false)}
            onCreated={() => {
              queryClient.invalidateQueries({ queryKey: ["artifacts"] });
              queryClient.invalidateQueries({ queryKey: ["artifactFilters"] });
              queryClient.invalidateQueries({ queryKey: ["artifactStats"] });
            }}
          />
        </SheetContent>
      </Sheet>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function StatCard({
  icon,
  label,
  value,
  accent,
  warn,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  accent?: string;
  warn?: boolean;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {icon}
          {label}
        </div>
        <div className="mt-1 flex items-baseline gap-2">
          <div
            className={
              "text-2xl font-bold " + (warn && value > 0 ? "text-orange-500" : "")
            }
          >
            {value.toLocaleString()}
          </div>
          {accent && (
            <div className="text-xs text-muted-foreground">{accent}</div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function FilterSelect({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  placeholder: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="h-9 rounded-md border border-input bg-background px-3 text-sm min-w-[140px]"
    >
      <option value="">{placeholder}</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function SortHeader({
  label,
  onClick,
  icon,
}: {
  label: string;
  onClick: () => void;
  icon: React.ReactNode;
}) {
  return (
    <th className="text-left p-3 font-medium text-xs uppercase tracking-wide text-muted-foreground">
      <button
        onClick={onClick}
        className="inline-flex items-center gap-1 hover:text-foreground"
      >
        {label}
        {icon}
      </button>
    </th>
  );
}

function StatusBadge({ status }: { status: string }) {
  if (!status) return <span className="text-xs text-muted-foreground">—</span>;
  const color = (() => {
    switch (status) {
      case "Active":
        return "text-green-500 border-green-500/30";
      case "Draft":
        return "text-blue-400 border-blue-400/30";
      case "Under Review":
        return "text-yellow-500 border-yellow-500/30";
      case "Deprecated":
        return "text-red-500 border-red-500/30";
      default:
        return "text-muted-foreground";
    }
  })();
  return (
    <Badge variant="outline" className={color}>
      {status}
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------

function ArtifactDetail({
  artifactId,
  onClose,
  onSaved,
}: {
  artifactId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { data: artifact, isLoading } = useQuery({
    queryKey: ["artifactDetail", artifactId],
    queryFn: () => fetchArtifactDetail(artifactId),
  });

  const { data: vocab } = useQuery({
    queryKey: ["artifactVocabulary"],
    queryFn: fetchArtifactVocabulary,
  });

  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<Partial<Artifact>>({});

  useEffect(() => {
    if (artifact) setForm(artifact);
  }, [artifact]);

  const saveMutation = useMutation({
    mutationFn: () => updateArtifact(artifactId, form),
    onSuccess: () => {
      toast.success("Saved");
      setEditing(false);
      onSaved();
    },
    onError: (e: Error) => toast.error(`Save failed: ${e.message}`),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteArtifact(artifactId),
    onSuccess: () => {
      toast.success("Deleted");
      onSaved();
      onClose();
    },
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  });

  if (isLoading || !artifact) {
    return (
      <div className="p-8 text-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin inline mr-2" />
        Loading artifact…
      </div>
    );
  }

  const topics = splitCsv(artifact.topics);
  const aiTags = splitCsv(artifact.ai_suggested_tags);
  const sourceSchemas = splitCsv(artifact.source_schemas);
  const sourceTables = splitCsv(artifact.source_tables);

  return (
    <div className="space-y-4">
      <SheetHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-2">
            <span className="text-muted-foreground mt-1">
              {typeIcon(artifact.artifact_type)}
            </span>
            <div>
              <SheetTitle>{artifact.artifact_name || "Untitled"}</SheetTitle>
              <SheetDescription>
                {artifact.artifact_type}
                {artifact.platform && ` · ${artifact.platform}`}
              </SheetDescription>
            </div>
          </div>
          <div className="flex items-center gap-1">
            {artifact.certified && (
              <Badge
                variant="outline"
                className="text-green-500 border-green-500/30"
              >
                <CheckCircle2 className="h-3 w-3 mr-0.5" />
                Certified
              </Badge>
            )}
          </div>
        </div>
      </SheetHeader>

      {/* Quick actions */}
      <div className="flex items-center gap-2">
        {artifact.location_url && (
          <a
            href={artifact.location_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline"
          >
            <ExternalLink className="h-3 w-3" />
            Open Artifact
          </a>
        )}
        <div className="ml-auto flex items-center gap-2">
          {editing ? (
            <>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setEditing(false);
                  setForm(artifact);
                }}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => saveMutation.mutate()}
                disabled={saveMutation.isPending}
              >
                {saveMutation.isPending ? (
                  <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                ) : (
                  <Save className="h-4 w-4 mr-1" />
                )}
                Save
              </Button>
            </>
          ) : (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setEditing(true)}
              >
                <Pencil className="h-4 w-4 mr-1" />
                Edit
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  if (confirm("Delete this artifact?")) {
                    deleteMutation.mutate();
                  }
                }}
                disabled={deleteMutation.isPending}
              >
                <Trash2 className="h-4 w-4 text-red-500" />
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Description + AI Summary */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Description</h3>
        {editing ? (
          <textarea
            className="w-full min-h-[80px] rounded-md border border-input bg-background p-2 text-sm"
            value={form.description || ""}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        ) : (
          <p className="text-sm text-muted-foreground whitespace-pre-wrap">
            {artifact.description || "No description provided."}
          </p>
        )}
        {artifact.ai_summary && (
          <div className="rounded-md border border-blue-500/20 bg-blue-500/5 p-3 space-y-1">
            <div className="flex items-center gap-1 text-xs font-medium text-blue-400">
              <Sparkles className="h-3 w-3" />
              AI Summary
            </div>
            <p className="text-sm text-muted-foreground">
              {artifact.ai_summary}
            </p>
          </div>
        )}
        {artifact.ai_data_quality_notes && (
          <div className="rounded-md border border-orange-500/20 bg-orange-500/5 p-3 space-y-1">
            <div className="flex items-center gap-1 text-xs font-medium text-orange-400">
              <AlertTriangle className="h-3 w-3" />
              Data Quality Notes (AI)
            </div>
            <p className="text-sm text-muted-foreground">
              {artifact.ai_data_quality_notes}
            </p>
          </div>
        )}
      </section>

      {/* Classification */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Classification</h3>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <EditableField
            label="Type"
            value={form.artifact_type}
            options={vocab?.types}
            editing={editing}
            onChange={(v) => setForm({ ...form, artifact_type: v })}
            display={artifact.artifact_type}
          />
          <EditableField
            label="Status"
            value={form.status}
            options={vocab?.statuses}
            editing={editing}
            onChange={(v) => setForm({ ...form, status: v })}
            display={artifact.status}
          />
          <EditableField
            label="Platform"
            value={form.platform}
            editing={editing}
            onChange={(v) => setForm({ ...form, platform: v })}
            display={artifact.platform}
          />
          <EditableField
            label="Data Domain"
            value={form.data_domain}
            editing={editing}
            onChange={(v) => setForm({ ...form, data_domain: v })}
            display={artifact.data_domain}
          />
          <EditableField
            label="Department"
            value={form.department}
            editing={editing}
            onChange={(v) => setForm({ ...form, department: v })}
            display={artifact.department}
          />
          <EditableField
            label="Affiliate"
            value={form.affiliate}
            editing={editing}
            onChange={(v) => setForm({ ...form, affiliate: v })}
            display={artifact.affiliate}
          />
          <EditableField
            label="Access Level"
            value={form.access_level}
            options={vocab?.access_levels}
            editing={editing}
            onChange={(v) => setForm({ ...form, access_level: v })}
            display={artifact.access_level}
          />
          {editing ? (
            <div className="col-span-1">
              <div className="text-xs text-muted-foreground mb-1">Certified</div>
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={!!form.certified}
                  onChange={(e) =>
                    setForm({ ...form, certified: e.target.checked })
                  }
                />
                Certified trusted source
              </label>
            </div>
          ) : (
            <DisplayField
              label="Certified"
              value={artifact.certified ? "Yes" : "No"}
            />
          )}
        </div>
      </section>

      {/* Topics */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Topics &amp; Tags</h3>
        {editing ? (
          <Input
            value={form.topics || ""}
            onChange={(e) => setForm({ ...form, topics: e.target.value })}
            placeholder="comma,separated,tags"
          />
        ) : topics.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {topics.map((t) => (
              <Badge key={t} variant="outline" className="text-xs">
                {t}
              </Badge>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">No topics</p>
        )}
        {!editing && aiTags.length > 0 && (
          <div className="pt-1">
            <div className="flex items-center gap-1 text-xs text-muted-foreground mb-1">
              <Sparkles className="h-3 w-3" />
              AI-suggested
            </div>
            <div className="flex flex-wrap gap-1">
              {aiTags.map((t) => (
                <Badge
                  key={t}
                  variant="outline"
                  className="text-xs border-blue-500/30 text-blue-400"
                >
                  {t}
                </Badge>
              ))}
            </div>
          </div>
        )}
      </section>

      {/* Ownership */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Ownership</h3>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <EditableField
            label="Business Owner"
            value={form.business_owner}
            editing={editing}
            onChange={(v) => setForm({ ...form, business_owner: v })}
            display={artifact.business_owner}
          />
          <EditableField
            label="Business Team"
            value={form.business_team}
            editing={editing}
            onChange={(v) => setForm({ ...form, business_team: v })}
            display={artifact.business_team}
          />
          <EditableField
            label="Technical Owner"
            value={form.technical_owner}
            editing={editing}
            onChange={(v) => setForm({ ...form, technical_owner: v })}
            display={artifact.technical_owner}
          />
        </div>
      </section>

      {/* Location */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Location</h3>
        <div className="grid grid-cols-1 gap-3 text-sm">
          <EditableField
            label="URL"
            value={form.location_url}
            editing={editing}
            onChange={(v) => setForm({ ...form, location_url: v })}
            display={artifact.location_url}
            mono
          />
          <EditableField
            label="Workspace / Server"
            value={form.workspace_name}
            editing={editing}
            onChange={(v) => setForm({ ...form, workspace_name: v })}
            display={artifact.workspace_name}
          />
          <EditableField
            label="Folder Path"
            value={form.folder_path}
            editing={editing}
            onChange={(v) => setForm({ ...form, folder_path: v })}
            display={artifact.folder_path}
            mono
          />
        </div>
      </section>

      {/* Lifecycle */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Lifecycle</h3>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <EditableField
            label="Refresh Frequency"
            value={form.refresh_frequency}
            options={vocab?.refresh_frequencies}
            editing={editing}
            onChange={(v) => setForm({ ...form, refresh_frequency: v })}
            display={artifact.refresh_frequency}
          />
          <EditableField
            label="Last Refreshed"
            value={form.last_refreshed}
            editing={editing}
            onChange={(v) => setForm({ ...form, last_refreshed: v })}
            display={formatDate(artifact.last_refreshed)}
          />
          <EditableField
            label="Created"
            value={form.created_date}
            editing={editing}
            onChange={(v) => setForm({ ...form, created_date: v })}
            display={formatDate(artifact.created_date)}
          />
          <EditableField
            label="Last Modified"
            value={form.last_modified}
            editing={editing}
            onChange={(v) => setForm({ ...form, last_modified: v })}
            display={formatDate(artifact.last_modified)}
          />
        </div>
      </section>

      {/* Source lineage */}
      {(sourceSchemas.length > 0 || sourceTables.length > 0) && (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Source Data</h3>
          {sourceSchemas.length > 0 && (
            <div>
              <div className="text-xs text-muted-foreground mb-1">Schemas</div>
              <div className="flex flex-wrap gap-1">
                {sourceSchemas.map((s) => (
                  <Badge key={s} variant="outline" className="text-xs font-mono">
                    {s}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          {sourceTables.length > 0 && (
            <div>
              <div className="text-xs text-muted-foreground mb-1">Tables</div>
              <div className="flex flex-wrap gap-1">
                {sourceTables.map((t) => (
                  <Badge key={t} variant="outline" className="text-xs font-mono">
                    {t}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {/* Meta footer */}
      <section className="text-[11px] text-muted-foreground pt-2 border-t space-y-0.5">
        <div className="flex items-center gap-1">
          <Clock className="h-3 w-3" />
          Ingested: {formatDate(artifact.ingested_at)}
          {artifact.ingested_by && ` by ${artifact.ingested_by}`}
        </div>
        {artifact.enriched_at && (
          <div className="flex items-center gap-1">
            <Sparkles className="h-3 w-3" />
            AI-enriched: {formatDate(artifact.enriched_at)}
          </div>
        )}
        {artifact.is_user_edited && (
          <div className="flex items-center gap-1">
            <Pencil className="h-3 w-3" />
            Manually edited — AI will not overwrite
          </div>
        )}
      </section>
    </div>
  );
}

function EditableField({
  label,
  value,
  display,
  options,
  editing,
  onChange,
  mono,
}: {
  label: string;
  value: string | undefined;
  display: string;
  options?: string[];
  editing: boolean;
  onChange: (v: string) => void;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      {editing ? (
        options && options.length > 0 ? (
          <select
            className="w-full h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={value || ""}
            onChange={(e) => onChange(e.target.value)}
          >
            <option value="">—</option>
            {options.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        ) : (
          <Input
            value={value || ""}
            onChange={(e) => onChange(e.target.value)}
          />
        )
      ) : (
        <div
          className={
            "text-sm " +
            (mono ? "font-mono break-all " : "") +
            (!display ? "text-muted-foreground italic" : "")
          }
        >
          {display || "—"}
        </div>
      )}
    </div>
  );
}

function DisplayField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="text-sm">{value || "—"}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Upload Panel
// ---------------------------------------------------------------------------

function UploadPanel({
  onClose,
  onIngested,
}: {
  onClose: () => void;
  onIngested: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [replace, setReplace] = useState(false);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const submit = async () => {
    if (!file) {
      toast.error("Pick a CSV file first");
      return;
    }
    setUploading(true);
    try {
      await uploadFile(file, "artifacts");
      const res = await ingestArtifacts(file.name, replace);
      toast.success(`Ingested ${res.rows} artifacts`);
      onIngested();
      onClose();
    } catch (e) {
      const err = e as Error;
      toast.error(`Upload failed: ${err.message}`);
    } finally {
      setUploading(false);
    }
  };

  const expectedCols = useMemo(
    () => [
      "report_name (or artifact_name)",
      "report_description (or description)",
      "platform",
      "location (or location_url)",
      "business_team",
      "business_owner",
      "sysadmin (or technical_owner)",
      "topics (or tags)",
      "artifact_type (optional: BI Report, Dashboard, Genie Space, ...)",
      "status, certified, refresh_frequency (optional)",
      "data_domain, department, affiliate (optional)",
      "source_schemas, source_tables (comma-separated, optional)",
    ],
    [],
  );

  return (
    <div className="space-y-4">
      <SheetHeader>
        <SheetTitle>Upload Artifacts CSV</SheetTitle>
        <SheetDescription>
          Upload your existing BI/AI artifact list. Column names are flexible:
          common aliases are auto-mapped.
        </SheetDescription>
      </SheetHeader>

      <div className="space-y-2">
        <input
          ref={inputRef}
          type="file"
          accept=".csv"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
          className="hidden"
        />
        <Button
          variant="outline"
          className="w-full"
          onClick={() => inputRef.current?.click()}
        >
          <Upload className="h-4 w-4 mr-2" />
          {file ? file.name : "Choose CSV file"}
        </Button>
      </div>

      <label className="flex items-center gap-2 text-sm cursor-pointer">
        <input
          type="checkbox"
          checked={replace}
          onChange={(e) => setReplace(e.target.checked)}
        />
        Replace existing artifacts (otherwise merge by artifact_id)
      </label>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Recognized columns</CardTitle>
          <CardDescription className="text-xs">
            Additional columns are ignored. All fields except{" "}
            <code>artifact_name</code> are optional.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="text-xs text-muted-foreground space-y-1 font-mono">
            {expectedCols.map((c) => (
              <li key={c}>· {c}</li>
            ))}
          </ul>
        </CardContent>
      </Card>

      <div className="flex items-center gap-2 justify-end">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={!file || uploading}>
          {uploading && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
          Upload &amp; Ingest
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// New Artifact Panel (manual entry — B-016)
// ---------------------------------------------------------------------------

function NewArtifactPanel({
  filters,
  onClose,
  onCreated,
}: {
  filters: ArtifactFilters | undefined;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { data: vocab } = useQuery({
    queryKey: ["artifactVocabulary"],
    queryFn: fetchArtifactVocabulary,
  });

  const [form, setForm] = useState<ArtifactCreate>({
    artifact_name: "",
    platform: "",
    artifact_type: "BI Report",
    status: "Active",
    certified: false,
  });
  const [submitting, setSubmitting] = useState(false);

  const platformOptions = filters?.platforms ?? [];
  const teamOptions = filters?.teams ?? [];
  const domainOptions = filters?.domains ?? [];
  const departmentOptions = filters?.departments ?? [];

  const set = <K extends keyof ArtifactCreate>(k: K, v: ArtifactCreate[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const submit = async () => {
    if (!form.artifact_name.trim()) {
      toast.error("Name is required");
      return;
    }
    if (!form.platform.trim()) {
      toast.error("Platform is required");
      return;
    }
    setSubmitting(true);
    try {
      await createArtifact(form);
      toast.success("Artifact created");
      onCreated();
      onClose();
    } catch (e) {
      const err = e as Error;
      toast.error(`Create failed: ${err.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-4 overflow-y-auto max-h-[calc(100vh-2rem)] pr-1">
      <SheetHeader>
        <SheetTitle>New Artifact</SheetTitle>
        <SheetDescription>
          Add a single BI report, dashboard, Genie space, or AI agent. The
          artifact_id is derived from name + platform + URL, so re-creating an
          existing artifact updates it in place. Manual entries are flagged
          <code className="mx-1">is_user_edited=true</code>so AI enrichment
          and CSV re-uploads won't overwrite them.
        </SheetDescription>
      </SheetHeader>

      <div className="space-y-3">
        <NewField label="Name *" required>
          <Input
            value={form.artifact_name}
            onChange={(e) => set("artifact_name", e.target.value)}
            placeholder="e.g. Energy Trading P&L Dashboard"
          />
        </NewField>

        <div className="grid grid-cols-2 gap-3">
          <NewField label="Platform *" required>
            <ComboInput
              value={form.platform || ""}
              options={platformOptions}
              placeholder="Tableau, Power BI, Genie…"
              onChange={(v) => set("platform", v)}
            />
          </NewField>
          <NewField label="Type">
            <SelectInput
              value={form.artifact_type || ""}
              options={vocab?.types ?? []}
              onChange={(v) => set("artifact_type", v)}
            />
          </NewField>
        </div>

        <NewField label="Description">
          <textarea
            className="w-full min-h-[70px] rounded-md border border-input bg-background p-2 text-sm"
            value={form.description || ""}
            onChange={(e) => set("description", e.target.value)}
          />
        </NewField>

        <NewField label="Location URL">
          <Input
            value={form.location_url || ""}
            onChange={(e) => set("location_url", e.target.value)}
            placeholder="https://…"
          />
        </NewField>

        <div className="grid grid-cols-2 gap-3">
          <NewField label="Business Owner">
            <Input
              value={form.business_owner || ""}
              onChange={(e) => set("business_owner", e.target.value)}
            />
          </NewField>
          <NewField label="Business Team">
            <ComboInput
              value={form.business_team || ""}
              options={teamOptions}
              onChange={(v) => set("business_team", v)}
            />
          </NewField>
          <NewField label="Technical Owner">
            <Input
              value={form.technical_owner || ""}
              onChange={(e) => set("technical_owner", e.target.value)}
            />
          </NewField>
          <NewField label="Status">
            <SelectInput
              value={form.status || ""}
              options={vocab?.statuses ?? []}
              onChange={(v) => set("status", v)}
            />
          </NewField>
          <NewField label="Data Domain">
            <ComboInput
              value={form.data_domain || ""}
              options={domainOptions}
              onChange={(v) => set("data_domain", v)}
            />
          </NewField>
          <NewField label="Department">
            <ComboInput
              value={form.department || ""}
              options={departmentOptions}
              onChange={(v) => set("department", v)}
            />
          </NewField>
          <NewField label="Affiliate">
            <Input
              value={form.affiliate || ""}
              onChange={(e) => set("affiliate", e.target.value)}
            />
          </NewField>
          <NewField label="Refresh Frequency">
            <SelectInput
              value={form.refresh_frequency || ""}
              options={vocab?.refresh_frequencies ?? []}
              onChange={(v) => set("refresh_frequency", v)}
            />
          </NewField>
        </div>

        <NewField label="Topics (comma-separated)">
          <Input
            value={form.topics || ""}
            onChange={(e) => set("topics", e.target.value)}
            placeholder="finance, trading, hourly"
          />
        </NewField>

        <NewField label="Source Schemas (comma-separated)">
          <Input
            value={form.source_schemas || ""}
            onChange={(e) => set("source_schemas", e.target.value)}
            placeholder="finance.gold, trading.silver"
          />
        </NewField>

        <NewField label="Source Tables (comma-separated)">
          <Input
            value={form.source_tables || ""}
            onChange={(e) => set("source_tables", e.target.value)}
          />
        </NewField>

        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input
            type="checkbox"
            checked={!!form.certified}
            onChange={(e) => set("certified", e.target.checked)}
          />
          Mark as certified
        </label>
      </div>

      <div className="flex items-center gap-2 justify-end pt-2 border-t">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={submitting}>
          {submitting && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
          Create
        </Button>
      </div>
    </div>
  );
}

function NewField({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs text-muted-foreground">
        {label}
        {required && <span className="text-red-500 ml-0.5">*</span>}
      </label>
      {children}
    </div>
  );
}

function SelectInput({
  value,
  options,
  onChange,
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <select
      className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">—</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function ComboInput({
  value,
  options,
  onChange,
  placeholder,
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  // Free-text input + datalist of known values, so users can either
  // pick an existing platform/team/domain or type a new one without
  // being constrained to the dropdown.
  const id = `combo-${Math.random().toString(36).slice(2, 8)}`;
  return (
    <>
      <Input
        list={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
      <datalist id={id}>
        {options.map((o) => (
          <option key={o} value={o} />
        ))}
      </datalist>
    </>
  );
}
