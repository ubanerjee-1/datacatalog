import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect, useRef } from "react";
import {
  fetchSchemaExplorer,
  fetchSchemaTables,
  fetchSchemaTaxonomy,
  fetchCatalogTree,
  triggerTableEnrichment,
  fetchTableEnrichmentStatus,
  fetchSetupStatus,
} from "@/lib/api-client";
import { Link } from "@tanstack/react-router";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  ChevronRight,
  FolderOpen,
  Folder,
  Layers,
  Database,
  Table2,
  Info,
  Search,
  Tag,
  Play,
  Loader2,
  CheckCircle2,
  XCircle,
  ExternalLink,
  Building2,
  Server,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/data-catalog")({
  component: DataCatalogPage,
});

type ViewMode = "program" | "catalog";

function DataCatalogPage() {
  const [searchInput, setSearchInput] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [enrichedOnly, setEnrichedOnly] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("program");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [selectedSchema, setSelectedSchema] = useState<any>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    debounceRef.current = setTimeout(
      () => setDebouncedSearch(searchInput),
      400,
    );
    return () => clearTimeout(debounceRef.current);
  }, [searchInput]);

  const { data: programData, isLoading: programLoading } = useQuery({
    queryKey: ["schemaExplorer", debouncedSearch, enrichedOnly],
    queryFn: () =>
      fetchSchemaExplorer({
        ...(debouncedSearch ? { search: debouncedSearch } : {}),
        ...(enrichedOnly ? { enriched_only: true } : {}),
        limit: 5000,
        offset: 0,
      }),
    placeholderData: (prev) => prev,
    enabled: viewMode === "program",
  });

  const { data: catalogData, isLoading: catalogLoading } = useQuery({
    queryKey: ["catalogTree", debouncedSearch, enrichedOnly],
    queryFn: () =>
      fetchCatalogTree({
        ...(debouncedSearch ? { search: debouncedSearch } : {}),
        ...(enrichedOnly ? { enriched_only: true } : {}),
      }),
    placeholderData: (prev) => prev,
    enabled: viewMode === "catalog",
  });

  const isLoading =
    viewMode === "program"
      ? programLoading && !programData
      : catalogLoading && !catalogData;

  if (isLoading)
    return (
      <div className="space-y-4">
        <PageHeader />
        <LoadingCard />
      </div>
    );

  // Build tree: outer -> inner -> leaves. Both views share the rendering.
  const tree: Record<string, Record<string, any[]>> = {};
  let totalLeaves = 0;
  let outerLabel = "";
  let outerIcon: React.ReactNode;
  let innerIconClosed: React.ReactNode;
  let innerIconOpen: React.ReactNode;
  let outerSort: (keys: string[]) => string[];
  let innerSort: (keys: string[]) => string[];
  let formatOuter: (k: string) => string;

  if (viewMode === "program") {
    for (const s of programData?.schemas || []) {
      const outer = s.program || "Unknown";
      const inner = s.zone || "other";
      if (!tree[outer]) tree[outer] = {};
      if (!tree[outer][inner]) tree[outer][inner] = [];
      tree[outer][inner].push(s);
      totalLeaves += 1;
    }
    outerLabel = "programs";
    outerIcon = <Layers className="h-3.5 w-3.5 shrink-0 text-primary" />;
    innerIconClosed = (
      <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
    );
    innerIconOpen = (
      <FolderOpen className="h-3.5 w-3.5 shrink-0 text-amber-500" />
    );
    outerSort = (keys) => [...keys].sort();
    innerSort = (keys) => [...keys].sort();
    formatOuter = (k) => k;
  } else {
    for (const r of catalogData?.rows || []) {
      const outer = r.workspace || "unknown";
      const inner = r.catalog_name || "Unknown";
      if (!tree[outer]) tree[outer] = {};
      if (!tree[outer][inner]) tree[outer][inner] = [];
      tree[outer][inner].push(r);
      totalLeaves += 1;
    }
    outerLabel = "workspaces";
    outerIcon = <Server className="h-3.5 w-3.5 shrink-0 text-primary" />;
    innerIconClosed = (
      <Building2 className="h-3.5 w-3.5 shrink-0 text-sky-500" />
    );
    innerIconOpen = (
      <Building2 className="h-3.5 w-3.5 shrink-0 text-sky-500" />
    );
    outerSort = (keys) => [...keys].sort();
    innerSort = (keys) => [...keys].sort();
    formatOuter = (k) => k;
  }

  const sortedOuter = outerSort(Object.keys(tree));

  const toggleExpand = (key: string) =>
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const isSelected = (s: any) => {
    if (!selectedSchema) return false;
    if (selectedSchema.schema_name !== s.schema_name) return false;
    if (viewMode === "program") {
      return selectedSchema.program === s.program;
    }
    return (
      selectedSchema.workspace === s.workspace &&
      selectedSchema.catalog_name === s.catalog_name
    );
  };

  return (
    <div className="space-y-4">
      <PageHeader />

      <div className="flex gap-4 h-[calc(100vh-160px)]">
        {/* Tree panel */}
        <Card className="w-[420px] shrink-0 flex flex-col">
          <CardHeader className="pb-2 px-3 pt-3">
            <div className="flex flex-col gap-2">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
                <Input
                  placeholder={
                    viewMode === "program"
                      ? "Search schemas..."
                      : "Search workspaces, catalogs, or schemas..."
                  }
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  className="h-8 text-xs pl-8"
                />
              </div>
              <ViewModeToggle value={viewMode} onChange={setViewMode} />
              <div className="flex items-center justify-between">
                <span className="text-[10px] text-muted-foreground">
                  {totalLeaves.toLocaleString()}{" "}
                  {viewMode === "program" ? "schemas" : "schema entries"} across{" "}
                  {sortedOuter.length} {outerLabel}
                </span>
                <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={enrichedOnly}
                    onChange={(e) => setEnrichedOnly(e.target.checked)}
                    className="h-3 w-3"
                  />
                  Enriched only
                </label>
              </div>
            </div>
          </CardHeader>
          <CardContent className="p-0 overflow-y-auto flex-1">
            <div className="text-sm">
              {sortedOuter.map((outer) => {
                const outerKey = `o:${viewMode}:${outer}`;
                const isOuterOpen = expanded[outerKey];
                const innerMap = tree[outer];
                const sortedInner = innerSort(Object.keys(innerMap));
                const outerSchemaCount = sortedInner.reduce(
                  (sum, k) => sum + innerMap[k].length,
                  0,
                );

                return (
                  <div key={outer}>
                    <button
                      onClick={() => toggleExpand(outerKey)}
                      className="w-full flex items-center gap-1.5 px-3 py-1.5 hover:bg-muted/40 text-left group"
                    >
                      <ChevronRight
                        className={`h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform ${isOuterOpen ? "rotate-90" : ""}`}
                      />
                      {outerIcon}
                      <span className="font-medium text-xs truncate">
                        {formatOuter(outer)}
                      </span>
                      <span className="ml-auto text-[10px] text-muted-foreground shrink-0">
                        {outerSchemaCount.toLocaleString()}{" "}
                        {viewMode === "program" ? "schemas" : "schemas"}
                      </span>
                    </button>

                    {isOuterOpen &&
                      sortedInner.map((inner) => {
                        const innerKey = `i:${viewMode}:${outer}:${inner}`;
                        const isInnerOpen = expanded[innerKey];
                        const items = innerMap[inner];

                        return (
                          <div key={inner}>
                            <button
                              onClick={() => toggleExpand(innerKey)}
                              className="w-full flex items-center gap-1.5 pl-8 pr-3 py-1 hover:bg-muted/40 text-left"
                            >
                              <ChevronRight
                                className={`h-3 w-3 shrink-0 text-muted-foreground transition-transform ${isInnerOpen ? "rotate-90" : ""}`}
                              />
                              {isInnerOpen ? innerIconOpen : innerIconClosed}
                              <span className="text-xs truncate font-mono">
                                {inner}
                              </span>
                              <span className="ml-auto text-[10px] text-muted-foreground shrink-0">
                                {items.length}
                              </span>
                            </button>

                            {isInnerOpen &&
                              items.map((s: any, i: number) => (
                                <button
                                  key={i}
                                  onClick={() => setSelectedSchema(s)}
                                  className={`w-full flex items-center gap-1.5 pl-14 pr-3 py-0.5 hover:bg-muted/40 text-left ${
                                    isSelected(s) ? "bg-primary/10" : ""
                                  }`}
                                >
                                  <Database className="h-3 w-3 shrink-0 text-muted-foreground" />
                                  <span className="font-mono text-[11px] truncate">
                                    {s.schema_name}
                                  </span>
                                  <span className="ml-auto text-[10px] text-muted-foreground font-mono shrink-0">
                                    {s.total_tables}
                                  </span>
                                </button>
                              ))}
                          </div>
                        );
                      })}
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>

        {/* Detail panel */}
        <Card className="flex-1 flex flex-col">
          {selectedSchema ? (
            <SchemaDetailPanel schema={selectedSchema} />
          ) : (
            <CardContent className="flex flex-col items-center justify-center h-full text-muted-foreground">
              <Database className="h-10 w-10 mb-3 opacity-30" />
              <p className="text-sm">
                Select a schema from the tree to view details
              </p>
            </CardContent>
          )}
        </Card>
      </div>
    </div>
  );
}

function ViewModeToggle({
  value,
  onChange,
}: {
  value: ViewMode;
  onChange: (v: ViewMode) => void;
}) {
  const options: Array<{ id: ViewMode; label: string; icon: React.ReactNode }> =
    [
      {
        id: "program",
        label: "By Program",
        icon: <Layers className="h-3 w-3" />,
      },
      {
        id: "catalog",
        label: "By Catalog",
        icon: <Server className="h-3 w-3" />,
      },
    ];
  return (
    <div className="inline-flex rounded-md border bg-muted/30 p-0.5 w-full">
      {options.map((opt) => (
        <button
          key={opt.id}
          onClick={() => onChange(opt.id)}
          className={`flex-1 flex items-center justify-center gap-1.5 px-2 py-1 rounded text-[11px] font-medium transition-colors ${
            value === opt.id
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
          title={
            opt.id === "program"
              ? "Program → Zone → Schema"
              : "Workspace → Catalog → Schema"
          }
        >
          {opt.icon}
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function PageHeader() {
  const queryClient = useQueryClient();

  const { data: jobStatus, isLoading: statusLoading } = useQuery({
    queryKey: ["table-enrich-status"],
    queryFn: fetchTableEnrichmentStatus,
    refetchInterval: (query) => {
      const s = query.state.data?.life_cycle_state;
      return s === "RUNNING" || s === "PENDING" ? 10_000 : false;
    },
  });

  // Gate "Run Table Enrichment" until infra + data prerequisites are met.
  // Without silver_schemas/silver_tables the enrichment job has nothing to
  // do anyway, so kick the user back to the setup wizard with a tooltip.
  const { data: setupStatus } = useQuery({
    queryKey: ["setupStatus"],
    queryFn: fetchSetupStatus,
    staleTime: 30_000,
  });
  const setupReady = setupStatus?.is_data_ready ?? false;

  const triggerMutation = useMutation({
    mutationFn: () => triggerTableEnrichment({ batch_size: 200, max_batches: 500 }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["table-enrich-status"] });
    },
  });

  const isRunning =
    jobStatus?.life_cycle_state === "RUNNING" ||
    jobStatus?.life_cycle_state === "PENDING";

  return (
    <div className="flex items-start justify-between">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Database className="h-6 w-6" />
          Data Catalog
        </h1>
        <p className="text-muted-foreground">
          Browse schemas, explore tables, and view AI-enriched metadata
        </p>
      </div>
      <div className="flex items-center gap-3">
        {jobStatus && !statusLoading && (
          <EnrichmentStatusBadge status={jobStatus} />
        )}
        {!setupReady ? (
          <Link
            to="/company"
            className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-xs font-medium bg-amber-500/15 text-amber-300 border border-amber-500/30 hover:bg-amber-500/20"
            title="Complete the setup wizard before running enrichment"
          >
            <Play className="h-3.5 w-3.5" />
            Finish setup to enable
          </Link>
        ) : (
          <button
            onClick={() => triggerMutation.mutate()}
            disabled={isRunning || triggerMutation.isPending}
            className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-xs font-medium bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {isRunning || triggerMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            {isRunning ? "Enrichment Running" : "Run Table Enrichment"}
          </button>
        )}
      </div>
    </div>
  );
}

function EnrichmentStatusBadge({ status }: { status: any }) {
  const lifecycle = status.life_cycle_state || "";
  const result = status.result_state || "";

  let icon: React.ReactNode;
  let label: string;
  let color: string;

  if (lifecycle === "RUNNING" || lifecycle === "PENDING") {
    icon = <Loader2 className="h-3 w-3 animate-spin" />;
    label = lifecycle === "PENDING" ? "Starting..." : "Running";
    color = "bg-blue-500/15 text-blue-400 border-blue-500/30";
  } else if (result === "SUCCESS") {
    icon = <CheckCircle2 className="h-3 w-3" />;
    label = "Last run succeeded";
    color = "bg-emerald-500/15 text-emerald-400 border-emerald-500/30";
  } else if (result === "FAILED" || result === "TIMEDOUT") {
    icon = <XCircle className="h-3 w-3" />;
    label = `Last run: ${result.toLowerCase()}`;
    color = "bg-red-500/15 text-red-400 border-red-500/30";
  } else if (status.status === "NEVER_RUN") {
    icon = <Info className="h-3 w-3" />;
    label = "Never run";
    color = "bg-muted text-muted-foreground border-muted";
  } else {
    icon = <Info className="h-3 w-3" />;
    label = status.status || "Unknown";
    color = "bg-muted text-muted-foreground border-muted";
  }

  return (
    <a
      href={status.run_page_url || "#"}
      target="_blank"
      rel="noopener noreferrer"
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-medium border ${color} hover:opacity-80 transition-opacity`}
    >
      {icon}
      {label}
      {status.run_page_url && <ExternalLink className="h-2.5 w-2.5 ml-0.5" />}
    </a>
  );
}

// ---------------------------------------------------------------------------
// Schema Detail Panel with Overview / Tables tabs
// ---------------------------------------------------------------------------

const TAXONOMY_LABELS: Record<string, string> = {
  category: "Category",
  criticality: "Criticality",
  data_domain: "Data Domain",
  department: "Department",
  industry_vertical: "Industry",
  integration_pattern: "Integration",
  use_case: "Use Case",
  vendor_type: "Vendor Type",
};

const TAXONOMY_COLORS: Record<string, string> = {
  category: "bg-blue-500/15 text-blue-400",
  criticality: "bg-red-500/15 text-red-400",
  data_domain: "bg-emerald-500/15 text-emerald-400",
  department: "bg-violet-500/15 text-violet-400",
  industry_vertical: "bg-amber-500/15 text-amber-400",
  integration_pattern: "bg-cyan-500/15 text-cyan-400",
  use_case: "bg-pink-500/15 text-pink-400",
  vendor_type: "bg-orange-500/15 text-orange-400",
};

function SchemaDetailPanel({ schema: s }: { schema: any }) {
  const [tab, setTab] = useState<"overview" | "tables">("overview");
  const [tableSearch, setTableSearch] = useState("");
  const [debouncedTableSearch, setDebouncedTableSearch] = useState("");
  const tableSearchTimer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    clearTimeout(tableSearchTimer.current);
    tableSearchTimer.current = setTimeout(
      () => setDebouncedTableSearch(tableSearch),
      400,
    );
    return () => clearTimeout(tableSearchTimer.current);
  }, [tableSearch]);

  useEffect(() => {
    setTab("overview");
    setTableSearch("");
    setDebouncedTableSearch("");
  }, [s.schema_name]);

  const { data: tableCountData } = useQuery({
    queryKey: ["schema-tables-count", s.schema_name],
    queryFn: () => fetchSchemaTables({ schema_name: s.schema_name, limit: 0 }),
  });

  const { data: tablesData, isLoading: tablesLoading } = useQuery({
    queryKey: ["schema-tables", s.schema_name, debouncedTableSearch],
    queryFn: () =>
      fetchSchemaTables({
        schema_name: s.schema_name,
        search: debouncedTableSearch || undefined,
        limit: 500,
      }),
    enabled: tab === "tables",
    placeholderData: (prev: any) => prev,
  });

  const { data: taxonomyData } = useQuery({
    queryKey: ["schema-taxonomy", s.schema_name],
    queryFn: () => fetchSchemaTaxonomy(s.schema_name),
    enabled: tab === "overview",
  });

  const tables = tablesData?.tables ?? [];
  const taxonomy = taxonomyData?.taxonomy ?? {};
  const tableCount = tableCountData?.total ?? tablesData?.total ?? s.total_tables;

  return (
    <>
      <CardHeader className="pb-0">
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="text-base font-mono">
              {s.schema_name}
            </CardTitle>
            <CardDescription className="text-xs mt-0.5">
              {s.business_name || "No business name"}
            </CardDescription>
          </div>
          <div className="flex gap-1.5 items-center">
            <EnvBadge label="Dev" present={s.in_dev} />
            <EnvBadge label="QA" present={s.in_qa} />
            <EnvBadge label="Prd" present={s.in_prod} />
          </div>
        </div>
        <div className="flex gap-0 border-b mt-3">
          <button
            onClick={() => setTab("overview")}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border-b-2 transition-colors ${
              tab === "overview"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Info className="h-3.5 w-3.5" />
            Overview
          </button>
          <button
            onClick={() => setTab("tables")}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border-b-2 transition-colors ${
              tab === "tables"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Table2 className="h-3.5 w-3.5" />
            Tables
            {tableCount > 0 && (
              <span className="text-[10px] bg-muted px-1 rounded">
                {Number(tableCount).toLocaleString()}
              </span>
            )}
          </button>
        </div>
      </CardHeader>

      {tab === "overview" ? (
        <CardContent className="space-y-4 text-sm overflow-y-auto flex-1 pt-4">
          <div>
            <h4 className="text-xs font-medium text-muted-foreground mb-1">
              Definition
            </h4>
            <p className="text-xs leading-relaxed">
              {s.definition || (
                <span className="italic text-muted-foreground/50">
                  Not enriched
                </span>
              )}
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <DetailField label="Program" value={s.program} />
            <DetailField label="Zone" value={s.zone} />
            <DetailField label="Domain" value={s.data_domain} />
            <DetailField label="Department" value={s.department_owner} />
            <DetailField label="Source System" value={s.source_system} />
            <DetailField label="Sensitivity" value={s.sensitivity} />
            <DetailField
              label="Total Tables"
              value={s.total_tables?.toLocaleString?.()}
            />
          </div>

          {Object.keys(taxonomy).length > 0 && (
            <div>
              <h4 className="text-xs font-medium text-muted-foreground mb-2 flex items-center gap-1.5">
                <Tag className="h-3.5 w-3.5" />
                Taxonomy
              </h4>
              <div className="space-y-2">
                {Object.entries(TAXONOMY_LABELS).map(([dim, label]) => {
                  const values = taxonomy[dim];
                  if (!values || values.length === 0) return null;
                  return (
                    <div key={dim} className="flex items-start gap-2">
                      <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide w-20 shrink-0 pt-0.5">
                        {label}
                      </span>
                      <div className="flex flex-wrap gap-1">
                        {values.map((v: string) => (
                          <span
                            key={v}
                            className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${TAXONOMY_COLORS[dim] || "bg-muted text-muted-foreground"}`}
                          >
                            {v}
                          </span>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </CardContent>
      ) : (
        <CardContent className="flex flex-col overflow-hidden flex-1 pt-3 px-0">
          <div className="px-4 pb-2">
            <Input
              placeholder="Filter tables..."
              value={tableSearch}
              onChange={(e) => setTableSearch(e.target.value)}
              className="h-7 text-xs"
            />
          </div>
          <div className="overflow-y-auto flex-1">
            {tablesLoading && tables.length === 0 ? (
              <div className="flex items-center justify-center h-32 text-xs text-muted-foreground">
                Loading tables...
              </div>
            ) : tables.length === 0 ? (
              <div className="flex items-center justify-center h-32 text-xs text-muted-foreground">
                No tables found
              </div>
            ) : (
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-card z-10">
                  <tr className="border-b text-left">
                    <th className="px-4 py-1.5 font-medium text-muted-foreground">
                      Table Name
                    </th>
                    <th className="px-2 py-1.5 font-medium text-muted-foreground">
                      Business Name
                    </th>
                    <th className="px-2 py-1.5 font-medium text-muted-foreground">
                      Definition
                    </th>
                    <th className="px-2 py-1.5 font-medium text-muted-foreground">
                      Source System
                    </th>
                    <th
                      className="px-2 py-1.5 font-medium text-muted-foreground text-center"
                      title="Dev"
                    >
                      D
                    </th>
                    <th
                      className="px-2 py-1.5 font-medium text-muted-foreground text-center"
                      title="QA"
                    >
                      Q
                    </th>
                    <th
                      className="px-2 py-1.5 font-medium text-muted-foreground text-center"
                      title="Prod"
                    >
                      P
                    </th>
                    <th
                      className="px-2 py-1.5 font-medium text-muted-foreground text-center"
                      title="Sandbox"
                    >
                      S
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {tables.map((t: any, i: number) => (
                    <tr
                      key={i}
                      className="border-b border-border/40 hover:bg-muted/30"
                    >
                      <td
                        className="px-4 py-1.5 font-mono truncate max-w-[200px]"
                        title={t.table_name}
                      >
                        {t.table_name}
                      </td>
                      <td
                        className="px-2 py-1.5 truncate max-w-[160px] text-muted-foreground"
                        title={t.business_name || ""}
                      >
                        {t.business_name || (
                          <span className="text-muted-foreground/40">--</span>
                        )}
                      </td>
                      <td
                        className="px-2 py-1.5 truncate max-w-[220px] text-muted-foreground"
                        title={t.definition || ""}
                      >
                        {t.definition || (
                          <span className="text-muted-foreground/40">--</span>
                        )}
                      </td>
                      <td className="px-2 py-1.5 truncate max-w-[120px] text-muted-foreground" title={t.source_system || ""}>
                        {t.source_system || <span className="text-muted-foreground/40">--</span>}
                      </td>
                      <td className="px-2 py-1.5 text-center">
                        <EnvDot present={t.in_dev} />
                      </td>
                      <td className="px-2 py-1.5 text-center">
                        <EnvDot present={t.in_qa} />
                      </td>
                      <td className="px-2 py-1.5 text-center">
                        <EnvDot present={t.in_prod} />
                      </td>
                      <td className="px-2 py-1.5 text-center">
                        <EnvDot present={t.in_sbx} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {tablesData?.total != null && (
            <div className="px-4 py-1.5 text-[10px] text-muted-foreground border-t">
              {tables.length} of {tablesData.total.toLocaleString()} tables
            </div>
          )}
        </CardContent>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function DetailField({ label, value }: { label: string; value?: string }) {
  return (
    <div>
      <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      <p className="text-xs mt-0.5">
        {value || <span className="text-muted-foreground/50">--</span>}
      </p>
    </div>
  );
}

function EnvBadge({
  label,
  present,
}: {
  label: string;
  present: boolean | string;
}) {
  const is = present === true || present === "true";
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${is ? "bg-green-500/15 text-green-500" : "bg-muted text-muted-foreground/50"}`}
    >
      {label}
    </span>
  );
}

function EnvDot({ present }: { present: boolean | string }) {
  const is = present === true || present === "true";
  return (
    <span
      className={`inline-block h-2.5 w-2.5 rounded-full ${is ? "bg-green-500" : "bg-red-500/40"}`}
    />
  );
}

function LoadingCard() {
  return (
    <Card>
      <CardContent className="flex items-center justify-center h-48">
        <div className="animate-pulse text-muted-foreground">Loading...</div>
      </CardContent>
    </Card>
  );
}
