import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useMemo, useEffect } from "react";
import {
  fetchTaxonomy,
  fetchTaxonomyPivot,
  fetchTaxonomyTables,
  updateTaxonomy,
  fetchTaxonomyHistory,
  fetchTaxonomyInspection,
  triggerTaxonomyReprocessing,
  fetchJobStatus,
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
import {
  Grid3X3,
  ChevronLeft,
  ChevronRight,
  Sparkles,
  PenLine,
  Save,
  X,
  History,
  Download,
  ShieldCheck,
  AlertTriangle,
  Loader2,
  RefreshCw,
  CheckCircle2,
  Filter,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/taxonomy")({
  component: TaxonomyPage,
});

const DIMENSIONS = [
  { key: "category", label: "Category" },
  { key: "department", label: "Department" },
  { key: "data_domain", label: "Data Domain" },
  { key: "integration_pattern", label: "Integration Pattern" },
  { key: "criticality", label: "Criticality" },
  { key: "vendor_type", label: "Vendor Type" },
  { key: "industry_vertical", label: "Industry Vertical" },
  { key: "use_case", label: "Use Case" },
];

const CRITICALITY_COLORS: Record<string, string> = {
  "T1 - Mission Critical": "bg-red-500 text-white",
  "T2 - Important": "bg-orange-400 text-white",
  "T3 - Supporting": "bg-green-500 text-white",
};

type PivotFilter = { rowDim: string; rowVal: string; colDim: string; colVal: string } | null;

function TaxonomyPage() {
  const [pivotFilter, setPivotFilter] = useState<PivotFilter>(null);
  const [metric, setMetric] = useState<"systems" | "tables">("systems");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Grid3X3 className="h-6 w-6" />
          Source Taxonomy
        </h1>
        <p className="text-muted-foreground">
          Classify source schemas across 8 taxonomy dimensions with AI generation and manual overrides
        </p>
      </div>
      <InspectionCard />
      <PivotTableSection pivotFilter={pivotFilter} onCellClick={setPivotFilter} metric={metric} onMetricChange={setMetric} />
      <DetailViewSection pivotFilter={pivotFilter} onClearPivotFilter={() => setPivotFilter(null)} metric={metric} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inspection & Reprocessing Card
// ---------------------------------------------------------------------------

function InspectionCard() {
  const queryClient = useQueryClient();
  const [reprocessRunId, setReprocessRunId] = useState<string | null>(null);

  const { data: inspection, isLoading } = useQuery({
    queryKey: ["taxonomyInspect"],
    queryFn: fetchTaxonomyInspection,
  });

  const { data: jobStatus } = useQuery({
    queryKey: ["reprocessStatus", reprocessRunId],
    queryFn: () => fetchJobStatus(reprocessRunId!),
    enabled: !!reprocessRunId,
    refetchInterval: (query) => {
      const st = query.state.data?.status;
      if (st === "TERMINATED" || st === "FAILED") return false;
      return 5000;
    },
  });

  useEffect(() => {
    if (jobStatus?.status === "TERMINATED") {
      queryClient.invalidateQueries({ queryKey: ["taxonomyInspect"] });
      queryClient.invalidateQueries({ queryKey: ["taxonomy"] });
      queryClient.invalidateQueries({ queryKey: ["taxonomyPivot"] });
      setReprocessRunId(null);
    }
    if (jobStatus?.status === "FAILED") {
      setReprocessRunId(null);
    }
  }, [jobStatus?.status]);

  const reprocessMutation = useMutation({
    mutationFn: triggerTaxonomyReprocessing,
    onSuccess: (data) => setReprocessRunId(data.run_id),
  });

  const isRunning = !!reprocessRunId;
  const s = inspection?.summary;
  const byDim = inspection?.invalid_by_dimension || {};

  if (isLoading) return null;

  const hasIssues = s && (s.missing_schemas > 0 || s.invalid_values > 0);
  const allGood = s && s.missing_schemas === 0 && s.invalid_values === 0 && s.classified_schemas > 0;

  return (
    <Card className={allGood ? "border-green-500/30" : hasIssues ? "border-amber-500/30" : ""}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            {allGood ? (
              <ShieldCheck className="h-5 w-5 text-green-500" />
            ) : (
              <AlertTriangle className="h-5 w-5 text-amber-500" />
            )}
            <CardTitle className="text-lg">Quality Inspection</CardTitle>
          </div>
          {hasIssues && (
            <Button
              onClick={() => reprocessMutation.mutate()}
              disabled={isRunning}
              size="sm"
            >
              {isRunning ? (
                <>
                  <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                  {jobStatus?.status || "Running"}...
                </>
              ) : (
                <>
                  <RefreshCw className="h-4 w-4 mr-1" />
                  Reprocess Invalid
                </>
              )}
            </Button>
          )}
          {allGood && (
            <span className="text-sm text-green-500 flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4" /> All values valid
            </span>
          )}
        </div>
      </CardHeader>
      {s && (
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
            <div className="border rounded-lg p-2.5 text-center">
              <div className="text-2xl font-bold">{s.total_schemas.toLocaleString()}</div>
              <div className="text-xs text-muted-foreground">Total Schemas</div>
            </div>
            <div className="border rounded-lg p-2.5 text-center">
              <div className="text-2xl font-bold">{s.classified_schemas.toLocaleString()}</div>
              <div className="text-xs text-muted-foreground">Classified</div>
            </div>
            <div className="border rounded-lg p-2.5 text-center">
              <div className="text-2xl font-bold text-amber-500">{s.missing_schemas.toLocaleString()}</div>
              <div className="text-xs text-muted-foreground">Missing</div>
            </div>
            <div className="border rounded-lg p-2.5 text-center">
              <div className="text-2xl font-bold text-green-500">{s.valid_values.toLocaleString()}</div>
              <div className="text-xs text-muted-foreground">Valid Values</div>
            </div>
            <div className="border rounded-lg p-2.5 text-center">
              <div className="text-2xl font-bold text-red-500">{s.invalid_values.toLocaleString()}</div>
              <div className="text-xs text-muted-foreground">Invalid Values</div>
            </div>
          </div>
          {Object.keys(byDim).length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              <span className="text-xs text-muted-foreground mr-1 self-center">Invalid by dimension:</span>
              {Object.entries(byDim).map(([dim, count]) => (
                <span key={dim} className="text-[10px] px-2 py-0.5 rounded bg-red-500/10 text-red-600 font-medium">
                  {dim}: {(count as number).toLocaleString()}
                </span>
              ))}
            </div>
          )}
        </CardContent>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Interactive Pivot Table
// ---------------------------------------------------------------------------

function PivotTableSection({
  pivotFilter,
  onCellClick,
  metric,
  onMetricChange,
}: {
  pivotFilter: PivotFilter;
  onCellClick: (f: PivotFilter) => void;
  metric: "systems" | "tables";
  onMetricChange: (m: "systems" | "tables") => void;
}) {
  const setMetric = onMetricChange;
  const [rowsDim, setRowsDim] = useState("category");
  const [colsDim, setColsDim] = useState("criticality");

  const handleDimChange = (setter: (v: string) => void, val: string) => {
    setter(val);
    if (pivotFilter) onCellClick(null);
  };

  const { data, isLoading } = useQuery({
    queryKey: ["taxonomyPivot", rowsDim, colsDim, metric],
    queryFn: () => fetchTaxonomyPivot({ rows_dim: rowsDim, cols_dim: colsDim, metric }),
  });

  const maxVal = useMemo(() => {
    if (!data?.cells) return 1;
    return Math.max(...data.cells.flat(), 1);
  }, [data]);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <CardTitle className="text-lg flex items-center gap-2">
              <Grid3X3 className="h-5 w-5" />
              Interactive Pivot Table
            </CardTitle>
            <CardDescription>
              Flexible cross-tabulation across all source systems
            </CardDescription>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1 text-sm">
              <span className="text-muted-foreground">Metric:</span>
              <div className="flex border rounded-md overflow-hidden">
                <button
                  onClick={() => setMetric("systems")}
                  className={`px-3 py-1 text-xs font-medium ${metric === "systems" ? "bg-primary text-primary-foreground" : "hover:bg-muted"}`}
                >
                  # Systems
                </button>
                <button
                  onClick={() => setMetric("tables")}
                  className={`px-3 py-1 text-xs font-medium ${metric === "tables" ? "bg-primary text-primary-foreground" : "hover:bg-muted"}`}
                >
                  # Tables
                </button>
              </div>
            </div>
            <div className="flex items-center gap-1 text-sm">
              <span className="text-muted-foreground">Columns:</span>
              <select
                value={colsDim}
                onChange={(e) => handleDimChange(setColsDim, e.target.value)}
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
              >
                {DIMENSIONS.filter((d) => d.key !== rowsDim).map((d) => (
                  <option key={d.key} value={d.key}>{d.label}</option>
                ))}
              </select>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 mt-2 flex-wrap">
          <span className="text-sm text-muted-foreground mr-1">Rows:</span>
          {DIMENSIONS.filter((d) => d.key !== colsDim).map((d) => (
            <button
              key={d.key}
              onClick={() => handleDimChange(setRowsDim, d.key)}
              className={`px-2.5 py-1 rounded text-xs font-medium border ${
                rowsDim === d.key
                  ? "bg-primary text-primary-foreground border-primary"
                  : "border-border hover:bg-muted"
              }`}
            >
              {d.label}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="h-32 flex items-center justify-center text-muted-foreground">Loading...</div>
        ) : !data?.row_labels?.length ? (
          <div className="h-32 flex items-center justify-center text-muted-foreground">
            No taxonomy data. Run "Generate Taxonomy" first.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-primary/10">
                  <th className="text-left py-2 px-3 font-semibold text-xs uppercase tracking-wide border-r">
                    {DIMENSIONS.find((d) => d.key === rowsDim)?.label}
                  </th>
                  {data.col_labels.map((cl: string) => (
                    <th key={cl} className="text-center py-2 px-3 font-semibold text-xs uppercase tracking-wide">
                      {cl}
                    </th>
                  ))}
                  <th className="text-center py-2 px-3 font-semibold text-xs uppercase tracking-wide bg-primary/20">
                    TOTAL
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.row_labels.map((rl: string, ri: number) => {
                  const isRowActive = pivotFilter?.rowDim === rowsDim && pivotFilter?.rowVal === rl
                    && pivotFilter?.colDim === colsDim && !pivotFilter?.colVal;
                  return (
                  <tr key={rl} className="border-b border-border/50 hover:bg-muted/20">
                    <td
                      className={`py-1.5 px-3 font-medium text-sm border-r cursor-pointer hover:underline ${isRowActive ? "bg-primary/20 ring-1 ring-primary" : ""}`}
                      onClick={() => {
                        const next: PivotFilter = { rowDim: rowsDim, rowVal: rl, colDim: colsDim, colVal: "" };
                        if (isRowActive) onCellClick(null);
                        else onCellClick(next);
                      }}
                      title={`Filter detail table by ${DIMENSIONS.find((d) => d.key === rowsDim)?.label}: ${rl}`}
                    >
                      {rl}
                    </td>
                    {data.cells[ri].map((val: number, ci: number) => {
                      const cl = data.col_labels[ci];
                      const isActive = pivotFilter?.rowDim === rowsDim && pivotFilter?.rowVal === rl
                        && pivotFilter?.colDim === colsDim && pivotFilter?.colVal === cl;
                      return (
                      <td key={ci} className="py-1.5 px-3 text-center">
                        {val > 0 ? (
                          <span
                            className={`inline-block min-w-[2rem] px-1.5 py-0.5 rounded text-xs font-mono cursor-pointer transition-all ${isActive ? "ring-2 ring-primary ring-offset-1 ring-offset-background scale-110" : "hover:scale-105"}`}
                            style={{
                              backgroundColor: `rgba(59, 130, 246, ${0.1 + (val / maxVal) * 0.7})`,
                              color: val / maxVal > 0.4 ? "white" : "inherit",
                            }}
                            onClick={() => {
                              if (isActive) onCellClick(null);
                              else onCellClick({ rowDim: rowsDim, rowVal: rl, colDim: colsDim, colVal: cl });
                            }}
                            title={`Filter: ${DIMENSIONS.find((d) => d.key === rowsDim)?.label}=${rl}, ${DIMENSIONS.find((d) => d.key === colsDim)?.label}=${cl}`}
                          >
                            {val.toLocaleString()}
                          </span>
                        ) : (
                          <span className="text-muted-foreground/30">&ndash;</span>
                        )}
                      </td>
                      );
                    })}
                    <td
                      className={`py-1.5 px-3 text-center font-semibold bg-primary/5 cursor-pointer hover:underline ${isRowActive ? "ring-1 ring-primary" : ""}`}
                      onClick={() => {
                        const next: PivotFilter = { rowDim: rowsDim, rowVal: rl, colDim: colsDim, colVal: "" };
                        if (isRowActive) onCellClick(null);
                        else onCellClick(next);
                      }}
                      title={`Filter detail table by ${DIMENSIONS.find((d) => d.key === rowsDim)?.label}: ${rl} (all columns)`}
                    >
                      {data.row_totals[ri].toLocaleString()}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="border-t-2 font-semibold bg-primary/5">
                  <td className="py-2 px-3 border-r">TOTAL</td>
                  {data.col_totals.map((ct: number, ci: number) => (
                    <td key={ci} className="py-2 px-3 text-center">{ct.toLocaleString()}</td>
                  ))}
                  <td className="py-2 px-3 text-center bg-primary/10">
                    {data.grand_total.toLocaleString()}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Detail View Table
// ---------------------------------------------------------------------------

function DetailViewSection({
  pivotFilter,
  onClearPivotFilter,
  metric,
}: {
  pivotFilter: PivotFilter;
  onClearPivotFilter: () => void;
  metric: "systems" | "tables";
}) {
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState({ program: "", affiliate: "", environment: "", search: "" });
  const [page, setPage] = useState(0);
  const pageSize = 50;

  useEffect(() => {
    setPage(0);
  }, [pivotFilter]);

  const dimFilterStr = useMemo(() => {
    if (!pivotFilter) return "";
    const parts = [`${pivotFilter.rowDim}:${pivotFilter.rowVal}`];
    if (pivotFilter.colVal) parts.push(`${pivotFilter.colDim}:${pivotFilter.colVal}`);
    return parts.join("|");
  }, [pivotFilter]);

  useEffect(() => {
    setPage(0);
  }, [metric]);

  const { data, isLoading } = useQuery({
    queryKey: ["taxonomy", filters, dimFilterStr, page],
    queryFn: () =>
      fetchTaxonomy({
        ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)),
        ...(dimFilterStr ? { dim_filters: dimFilterStr } : {}),
        limit: pageSize,
        offset: page * pageSize,
      }),
    enabled: metric === "systems",
  });

  const { data: tablesData, isLoading: tablesLoading } = useQuery({
    queryKey: ["taxonomyTables", filters.search, dimFilterStr, page],
    queryFn: () =>
      fetchTaxonomyTables({
        ...(filters.search ? { search: filters.search } : {}),
        ...(dimFilterStr ? { dim_filters: dimFilterStr } : {}),
        limit: pageSize,
        offset: page * pageSize,
      }),
    enabled: metric === "tables",
  });

  const schemas = data?.schemas || [];
  const tables = tablesData?.tables || [];
  const total = metric === "systems" ? (data?.total || 0) : (tablesData?.total || 0);
  const filterOpts = data?.filters || {};
  const loading = metric === "systems" ? isLoading : tablesLoading;

  const handleDownloadCSV = () => {
    let headers: string[];
    let csvRows: any[][];
    if (metric === "tables") {
      if (!tables.length) return;
      headers = ["#", "Table", "Schema", "Program", "Zone", "Type", "Format", "Envs", "Description"];
      csvRows = tables.map((t: any, i: number) => [
        i + 1 + page * pageSize, t.table_name, t.schema_name,
        t.program, t.zone || "", t.table_type || "", t.data_source_format || "",
        [t.in_dev && "DEV", t.in_qa && "QA", t.in_prod && "PRD"].filter(Boolean).join(" "),
        t.definition || t.comment || "",
      ]);
    } else {
      if (!schemas.length) return;
      headers = ["#", "Schema", "Name", "Program", "Zone",
        ...DIMENSIONS.map((d) => d.label), "Envs", "Dev", "QA", "Prd"];
      csvRows = schemas.map((s: any, i: number) => [
        i + 1 + page * pageSize, s.schema_name, s.business_name || "", s.program, s.zone || "",
        ...DIMENSIONS.map((d) => s[d.key] || ""),
        [s.in_dev && "DEV", s.in_qa && "QA", s.in_prod && "PRD"].filter(Boolean).join(" "),
        s.dev_tables || 0, s.qa_tables || 0, s.prod_tables || 0,
      ]);
    }
    const csv = [headers, ...csvRows].map((r) => r.map((c: any) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = metric === "tables" ? "taxonomy_tables_export.csv" : "taxonomy_export.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex justify-between items-center">
          <div>
            <CardTitle className="text-lg">
              Detail View: {metric === "systems" ? "All Systems" : "All Tables"}
              {total > 0 && <span className="text-muted-foreground font-normal ml-2">({total.toLocaleString()})</span>}
            </CardTitle>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={handleDownloadCSV} disabled={metric === "systems" ? !schemas.length : !tables.length}>
              <Download className="h-3 w-3 mr-1" /> Download CSV
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {pivotFilter && (
          <div className="flex items-center gap-2 flex-wrap">
            <Filter className="h-3.5 w-3.5 text-primary" />
            <span className="text-xs text-muted-foreground">Pivot filter:</span>
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-primary/15 text-primary text-xs font-medium">
              {DIMENSIONS.find((d) => d.key === pivotFilter.rowDim)?.label}: {pivotFilter.rowVal}
              {pivotFilter.colVal && (
                <> + {DIMENSIONS.find((d) => d.key === pivotFilter.colDim)?.label}: {pivotFilter.colVal}</>
              )}
              <button
                onClick={onClearPivotFilter}
                className="ml-0.5 hover:bg-primary/20 rounded-full p-0.5"
                title="Clear pivot filter"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          </div>
        )}
        <div className="flex gap-2 flex-wrap items-center">
          <Input
            placeholder={metric === "systems" ? "Search schemas..." : "Search tables..."}
            value={filters.search}
            onChange={(e) => { setFilters({ ...filters, search: e.target.value }); setPage(0); }}
            className="w-44 h-8 text-sm"
          />
          {metric === "systems" && ["program", "affiliate", "environment"].map((key) => (
            <select
              key={key}
              value={(filters as any)[key]}
              onChange={(e) => { setFilters({ ...filters, [key]: e.target.value }); setPage(0); }}
              className="h-8 rounded-md border border-input bg-background px-2 text-sm"
            >
              <option value="">All {key}s</option>
              {(filterOpts[key + "s"] || []).map((v: string) => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          ))}
        </div>

        {loading ? (
          <div className="h-32 flex items-center justify-center text-muted-foreground">Loading...</div>
        ) : metric === "systems" ? (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="bg-primary/10 border-b">
                    <th className="text-left py-1.5 px-2 font-semibold w-8">#</th>
                    <th className="text-left py-1.5 px-2 font-semibold">SCHEMA</th>
                    <th className="text-left py-1.5 px-2 font-semibold">NAME</th>
                    {DIMENSIONS.map((d) => (
                      <th key={d.key} className="text-left py-1.5 px-2 font-semibold uppercase">
                        {d.label.length > 12 ? d.label.slice(0, 10) + "..." : d.label}
                      </th>
                    ))}
                    <th className="text-center py-1.5 px-2 font-semibold">ENVS</th>
                    <th className="text-right py-1.5 px-1 font-semibold">DEV</th>
                    <th className="text-right py-1.5 px-1 font-semibold">QA</th>
                    <th className="text-right py-1.5 px-1 font-semibold">PRD</th>
                  </tr>
                </thead>
                <tbody>
                  {schemas.map((s: any, i: number) => (
                    <tr key={`${s.program}|${s.zone}|${s.schema_name}`} className="border-b border-border/30 hover:bg-muted/20">
                      <td className="py-1 px-2 text-muted-foreground">{i + 1 + page * pageSize}</td>
                      <td className="py-1 px-2 font-mono font-medium truncate max-w-[120px]" title={s.schema_name}>
                        {s.schema_name}
                      </td>
                      <td className="py-1 px-2 truncate max-w-[100px]" title={s.business_name}>
                        {s.business_name || <span className="text-muted-foreground/40 italic">—</span>}
                      </td>
                      {DIMENSIONS.map((d) => (
                        <td key={d.key} className="py-1 px-2">
                          <TaxonomyCell
                            schemaKey={s.schema_key}
                            dimension={d.key}
                            value={s[d.key] || ""}
                            source={s[`${d.key}_source`] || ""}
                            onUpdated={() => queryClient.invalidateQueries({ queryKey: ["taxonomy"] })}
                          />
                        </td>
                      ))}
                      <td className="py-1 px-2 text-center">
                        <EnvBadges inDev={s.in_dev} inQa={s.in_qa} inProd={s.in_prod} />
                      </td>
                      <td className="py-1 px-1 text-right font-mono">{s.dev_tables || 0}</td>
                      <td className="py-1 px-1 text-right font-mono">{s.qa_tables || 0}</td>
                      <td className="py-1 px-1 text-right font-mono">{s.prod_tables || 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!schemas.length && (
                <div className="text-center py-8 text-muted-foreground">
                  No taxonomy data. Run "Generate Taxonomy" from Company Setup.
                </div>
              )}
            </div>

            <Pagination page={page} pageSize={pageSize} total={total} onPageChange={setPage} />
          </>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="bg-primary/10 border-b">
                    <th className="text-left py-1.5 px-2 font-semibold w-8">#</th>
                    <th className="text-left py-1.5 px-2 font-semibold">TABLE</th>
                    <th className="text-left py-1.5 px-2 font-semibold">SCHEMA</th>
                    <th className="text-left py-1.5 px-2 font-semibold">PROGRAM</th>
                    <th className="text-left py-1.5 px-2 font-semibold">TYPE</th>
                    <th className="text-left py-1.5 px-2 font-semibold">FORMAT</th>
                    <th className="text-center py-1.5 px-2 font-semibold">ENVS</th>
                    <th className="text-left py-1.5 px-2 font-semibold max-w-[300px]">DESCRIPTION</th>
                  </tr>
                </thead>
                <tbody>
                  {tables.map((t: any, i: number) => (
                    <tr key={`${t.program}|${t.zone}|${t.schema_name}|${t.table_name}`} className="border-b border-border/30 hover:bg-muted/20">
                      <td className="py-1 px-2 text-muted-foreground">{i + 1 + page * pageSize}</td>
                      <td className="py-1 px-2 font-mono font-medium truncate max-w-[160px]" title={t.table_name}>
                        {t.table_name}
                      </td>
                      <td className="py-1 px-2 font-mono truncate max-w-[120px]" title={t.schema_name}>
                        {t.schema_name}
                      </td>
                      <td className="py-1 px-2 truncate max-w-[100px]">{t.program}</td>
                      <td className="py-1 px-2">
                        <span className="text-[9px] px-1 py-0.5 rounded bg-muted font-medium">{t.table_type || "—"}</span>
                      </td>
                      <td className="py-1 px-2 text-[10px] text-muted-foreground">{t.data_source_format || "—"}</td>
                      <td className="py-1 px-2 text-center">
                        <EnvBadges inDev={t.in_dev} inQa={t.in_qa} inProd={t.in_prod} />
                      </td>
                      <td className="py-1 px-2 truncate max-w-[300px] text-muted-foreground" title={t.definition || t.comment || ""}>
                        {t.definition || t.comment || <span className="text-muted-foreground/30 italic">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!tables.length && (
                <div className="text-center py-8 text-muted-foreground">
                  {dimFilterStr ? "No tables found for this filter." : "No table data available."}
                </div>
              )}
            </div>
            <Pagination page={page} pageSize={pageSize} total={total} onPageChange={setPage} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

function Pagination({ page, pageSize, total, onPageChange }: {
  page: number; pageSize: number; total: number; onPageChange: (p: number) => void;
}) {
  return (
    <div className="flex items-center justify-between px-1 pt-1">
      <span className="text-xs text-muted-foreground">
        {total > 0 ? `${page * pageSize + 1}-${Math.min((page + 1) * pageSize, total)} of ${total.toLocaleString()}` : "No results"}
      </span>
      <div className="flex gap-1">
        <Button variant="outline" size="sm" disabled={page === 0} onClick={() => onPageChange(page - 1)}>
          <ChevronLeft className="h-3 w-3" />
        </Button>
        <Button variant="outline" size="sm" disabled={(page + 1) * pageSize >= total} onClick={() => onPageChange(page + 1)}>
          <ChevronRight className="h-3 w-3" />
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Taxonomy Cell (inline editable)
// ---------------------------------------------------------------------------

function TaxonomyCell({
  schemaKey,
  dimension,
  value,
  source,
  onUpdated,
}: {
  schemaKey: string;
  dimension: string;
  value: string;
  source: string;
  onUpdated: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [editVal, setEditVal] = useState(value);
  const [showHistory, setShowHistory] = useState(false);

  const mutation = useMutation({
    mutationFn: () => updateTaxonomy(schemaKey, dimension, editVal),
    onSuccess: () => {
      setEditing(false);
      onUpdated();
    },
  });

  const { data: historyData } = useQuery({
    queryKey: ["taxonomyHistory", schemaKey],
    queryFn: () => fetchTaxonomyHistory(schemaKey),
    enabled: showHistory,
  });

  if (editing) {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex gap-0.5">
          <Input
            value={editVal}
            onChange={(e) => setEditVal(e.target.value)}
            className="h-6 text-[10px] w-24 px-1"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter") mutation.mutate();
              if (e.key === "Escape") setEditing(false);
            }}
          />
          <button onClick={() => mutation.mutate()} className="text-green-500 hover:text-green-600" title="Save">
            <Save className="h-3 w-3" />
          </button>
          <button onClick={() => setEditing(false)} className="text-muted-foreground hover:text-foreground" title="Cancel">
            <X className="h-3 w-3" />
          </button>
        </div>
        <button
          onClick={() => setShowHistory(!showHistory)}
          className="text-[9px] text-muted-foreground hover:text-foreground flex items-center gap-0.5"
        >
          <History className="h-2.5 w-2.5" /> History
        </button>
        {showHistory && historyData?.history && (
          <div className="text-[9px] space-y-0.5 max-h-20 overflow-y-auto border-t pt-1">
            {historyData.history
              .filter((h: any) => h.dimension === dimension)
              .map((h: any, i: number) => (
                <div key={i} className="flex justify-between gap-1">
                  <span className={h.effective_to ? "line-through text-muted-foreground" : "font-medium"}>
                    {h.value}
                  </span>
                  <span className="text-muted-foreground">
                    {h.source === "manual" ? "M" : "AI"}
                  </span>
                </div>
              ))}
          </div>
        )}
      </div>
    );
  }

  if (!value) {
    return (
      <button
        onClick={() => { setEditVal(""); setEditing(true); }}
        className="text-muted-foreground/30 hover:text-muted-foreground text-[10px] italic"
      >
        —
      </button>
    );
  }

  const isCriticality = dimension === "criticality";
  const critClass = isCriticality ? CRITICALITY_COLORS[value] : "";

  return (
    <button
      onClick={() => { setEditVal(value); setEditing(true); }}
      className="group flex items-center gap-0.5 text-left"
      title={`${value} (${source || "unknown"}) — click to edit`}
    >
      {isCriticality && critClass ? (
        <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${critClass}`}>
          {value.split(" - ")[0]}
        </span>
      ) : (
        <span className="text-[11px] truncate max-w-[100px]">{value}</span>
      )}
      <span className="opacity-0 group-hover:opacity-100 transition-opacity">
        {source === "ai_generated" ? (
          <Sparkles className="h-2.5 w-2.5 text-amber-400" />
        ) : source === "manual" ? (
          <PenLine className="h-2.5 w-2.5 text-blue-400" />
        ) : null}
      </span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Env Badges
// ---------------------------------------------------------------------------

function EnvBadges({ inDev, inQa, inProd }: { inDev: boolean; inQa: boolean; inProd: boolean }) {
  return (
    <div className="flex gap-0.5 justify-center">
      {inProd && <span className="text-[8px] px-1 py-0 rounded bg-green-500 text-white font-bold">PRD</span>}
      {inQa && <span className="text-[8px] px-1 py-0 rounded bg-orange-400 text-white font-bold">QA</span>}
      {inDev && <span className="text-[8px] px-1 py-0 rounded bg-blue-500 text-white font-bold">DEV</span>}
    </div>
  );
}
