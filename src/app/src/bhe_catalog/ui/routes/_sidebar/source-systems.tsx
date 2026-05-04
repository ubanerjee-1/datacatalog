import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState, useEffect, useMemo } from "react";
import {
  fetchSourceSystems,
  fetchSourceSystemDetail,
  fetchSourceSystemTables,
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
  Search,
  ChevronRight,
  ChevronLeft,
  Database,
  Building2,
  Layers,
  Tag,
  Table2,
  Boxes,
  AlertCircle,
  X,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/source-systems")({
  component: SourceSystemsPage,
});

// ---------------------------------------------------------------------------
// Types (kept loose to match the FastAPI payloads without over-typing)
// ---------------------------------------------------------------------------

type SystemSummary = {
  name: string;
  category: string | null;
  description: string | null;
  table_count: number;
  schema_count: number;
  affiliate_count: number;
  affiliates: string[];
  environments: string[];
  alias_count: number;
  is_unclassified?: boolean;
};

type SchemaRow = {
  catalog_name: string;
  schema_name: string;
  environment: string;
  affiliate: string;
  zone: string;
  classification: string;
  schema_friendly_name: string;
  schema_definition: string;
  table_count: number;
  raw_source_systems: string[];
};

type AliasRow = {
  raw: string;
  mapped_by: string;
  confidence: string | null;
  is_user_edited: boolean;
  mapped_at: string | null;
};

type TableRow = {
  table_catalog: string;
  table_schema: string;
  table_name: string;
  table_type: string;
  business_friendly_name: string;
  ai_definition: string;
  source_system: string;
  environment: string;
  affiliate: string;
};

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

function fmtNum(n: number | undefined | null) {
  return (n ?? 0).toLocaleString();
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function SourceSystemsPage() {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search, 250);
  const [category, setCategory] = useState<string>("");
  const [selected, setSelected] = useState<string | null>(null);
  const [onlyWithData, setOnlyWithData] = useState(true);

  const { data: listData, isLoading: listLoading } = useQuery({
    queryKey: [
      "source-systems",
      { search: debouncedSearch, category, onlyWithData },
    ],
    queryFn: () =>
      fetchSourceSystems({
        search: debouncedSearch || undefined,
        category: category || undefined,
        include_empty: !onlyWithData,
      }),
  });

  const systems: SystemSummary[] = listData?.systems || [];
  const categories: string[] = listData?.categories || [];

  // Default-select the top system on first load so the right pane isn't empty.
  useEffect(() => {
    if (!selected && systems.length > 0) {
      setSelected(systems[0].name);
    }
  }, [systems, selected]);

  // Group systems by category for the left rail.
  const grouped = useMemo(() => {
    const by = new Map<string, SystemSummary[]>();
    for (const s of systems) {
      const key = s.category || "Uncategorized";
      if (!by.has(key)) by.set(key, []);
      by.get(key)!.push(s);
    }
    return Array.from(by.entries()).sort((a, b) => {
      // Keep "Unclassified" last
      if (a[0] === "Unclassified") return 1;
      if (b[0] === "Unclassified") return -1;
      return a[0].localeCompare(b[0]);
    });
  }, [systems]);

  const totalTables = useMemo(
    () => systems.reduce((acc, s) => acc + (s.table_count || 0), 0),
    [systems],
  );

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Source Systems</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Browse the business source systems feeding the Lake. Answer
            &quot;Do we have data from X? Where? Which tables?&quot; without
            knowing catalog/schema names.
          </p>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div>
            <span className="font-semibold text-foreground">
              {fmtNum(systems.length)}
            </span>{" "}
            systems
          </div>
          <div>
            <span className="font-semibold text-foreground">
              {fmtNum(totalTables)}
            </span>{" "}
            tables
          </div>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[260px] max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search systems (SAP, Maximo, PI, ...)"
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
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
        >
          <option value="">All categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-muted-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={onlyWithData}
            onChange={(e) => setOnlyWithData(e.target.checked)}
            className="rounded border-input"
          />
          Only systems with data
        </label>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[360px_1fr] gap-4">
        {/* Left rail: grouped list */}
        <div className="space-y-4 lg:max-h-[calc(100vh-220px)] lg:overflow-y-auto pr-1">
          {listLoading ? (
            Array.from({ length: 6 }).map((_, i) => (
              <Card key={i}>
                <CardContent className="h-16 animate-pulse bg-muted rounded" />
              </Card>
            ))
          ) : systems.length === 0 ? (
            <Card>
              <CardContent className="p-6 text-sm text-muted-foreground text-center">
                No systems match your filters.
              </CardContent>
            </Card>
          ) : (
            grouped.map(([cat, items]) => (
              <div key={cat} className="space-y-1.5">
                <div className="flex items-center justify-between px-1">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {cat}
                  </h3>
                  <span className="text-[10px] text-muted-foreground">
                    {items.length}
                  </span>
                </div>
                {items.map((s) => (
                  <SystemCard
                    key={s.name}
                    s={s}
                    isSelected={selected === s.name}
                    onSelect={() => setSelected(s.name)}
                  />
                ))}
              </div>
            ))
          )}
        </div>

        {/* Right pane: detail */}
        <div>
          {selected ? (
            <SystemDetail name={selected} />
          ) : (
            <Card>
              <CardContent className="flex items-center justify-center h-48 text-sm text-muted-foreground">
                Select a source system to see where its data lives.
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Left rail card
// ---------------------------------------------------------------------------

function SystemCard({
  s,
  isSelected,
  onSelect,
}: {
  s: SystemSummary;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const empty = s.table_count === 0;
  return (
    <Card
      className={`cursor-pointer transition-colors ${
        isSelected
          ? "border-primary ring-1 ring-primary/30"
          : "hover:border-muted-foreground/40"
      } ${empty ? "opacity-70" : ""}`}
      onClick={onSelect}
    >
      <CardContent className="p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5">
              <p className="text-sm font-semibold truncate">{s.name}</p>
              {s.is_unclassified && (
                <Badge
                  variant="outline"
                  className="text-[9px] border-orange-400 text-orange-500"
                >
                  unclassified
                </Badge>
              )}
            </div>
            {s.description && (
              <p className="text-[11px] text-muted-foreground line-clamp-2 mt-0.5">
                {s.description}
              </p>
            )}
          </div>
          <div className="text-right flex-shrink-0">
            <div className="text-sm font-semibold tabular-nums">
              {fmtNum(s.table_count)}
            </div>
            <div className="text-[10px] text-muted-foreground">tables</div>
          </div>
        </div>
        <div className="flex flex-wrap gap-1 mt-2">
          {(s.affiliates || []).slice(0, 3).map((a) => (
            <Badge key={a} variant="secondary" className="text-[9px] py-0">
              {a}
            </Badge>
          ))}
          {(s.affiliates || []).length > 3 && (
            <Badge variant="secondary" className="text-[9px] py-0">
              +{s.affiliates.length - 3}
            </Badge>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Right pane: detail
// ---------------------------------------------------------------------------

function SystemDetail({ name }: { name: string }) {
  const [tab, setTab] = useState<"schemas" | "tables" | "aliases">("schemas");
  const [tableSearch, setTableSearch] = useState("");
  const debouncedTableSearch = useDebounce(tableSearch, 300);
  const [schemaFilter, setSchemaFilter] = useState<{
    catalog: string;
    schema: string;
  } | null>(null);
  const [offset, setOffset] = useState(0);
  const limit = 50;

  // Reset local state when selected system changes.
  useEffect(() => {
    setTab("schemas");
    setTableSearch("");
    setSchemaFilter(null);
    setOffset(0);
  }, [name]);

  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ["source-system", name],
    queryFn: () => fetchSourceSystemDetail(name),
  });

  const { data: tablesData, isLoading: tablesLoading } = useQuery({
    queryKey: [
      "source-system-tables",
      name,
      debouncedTableSearch,
      schemaFilter,
      offset,
    ],
    queryFn: () =>
      fetchSourceSystemTables(name, {
        search: debouncedTableSearch || undefined,
        catalog: schemaFilter?.catalog,
        schema: schemaFilter?.schema,
        limit,
        offset,
      }),
    enabled: tab === "tables",
  });

  if (detailLoading) {
    return (
      <Card>
        <CardContent className="h-48 animate-pulse bg-muted rounded" />
      </Card>
    );
  }

  if (!detail) return null;

  const meta = detail.meta || {};
  const totals = detail.totals || {};
  const schemas: SchemaRow[] = detail.schemas || [];
  const aliases: AliasRow[] = detail.aliases || [];
  const isUnclassified = detail.is_unclassified;

  const openTablesFor = (s: SchemaRow) => {
    setSchemaFilter({ catalog: s.catalog_name, schema: s.schema_name });
    setTableSearch("");
    setOffset(0);
    setTab("tables");
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <CardTitle className="text-xl">{meta.name}</CardTitle>
                {meta.category && (
                  <Badge variant="outline" className="text-[10px]">
                    {meta.category}
                  </Badge>
                )}
                {isUnclassified && (
                  <Badge
                    variant="outline"
                    className="border-orange-400 text-orange-500 text-[10px]"
                  >
                    <AlertCircle className="h-3 w-3 mr-1" />
                    unmapped
                  </Badge>
                )}
              </div>
              {meta.description && (
                <CardDescription className="mt-1">
                  {meta.description}
                </CardDescription>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatTile
              icon={<Table2 className="h-3.5 w-3.5" />}
              label="Tables"
              value={fmtNum(totals.table_count)}
            />
            <StatTile
              icon={<Layers className="h-3.5 w-3.5" />}
              label="Schemas"
              value={fmtNum(totals.schema_count)}
            />
            <StatTile
              icon={<Building2 className="h-3.5 w-3.5" />}
              label="Affiliates"
              value={fmtNum(totals.affiliate_count)}
            />
            <StatTile
              icon={<Boxes className="h-3.5 w-3.5" />}
              label="Environments"
              value={fmtNum(totals.environment_count)}
            />
          </div>
          {(totals.affiliates || []).length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1">
              {(totals.affiliates as string[]).map((a) => (
                <Badge key={a} variant="secondary" className="text-[10px]">
                  {a}
                </Badge>
              ))}
              {(totals.environments as string[] | undefined)?.map((e) => (
                <Badge key={e} variant="outline" className="text-[10px]">
                  {e}
                </Badge>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b">
        <TabButton
          active={tab === "schemas"}
          onClick={() => setTab("schemas")}
          icon={<Layers className="h-3.5 w-3.5" />}
          label="Where it lives"
          count={schemas.length}
        />
        <TabButton
          active={tab === "tables"}
          onClick={() => setTab("tables")}
          icon={<Table2 className="h-3.5 w-3.5" />}
          label="Tables"
          count={totals.table_count}
        />
        {!isUnclassified && (
          <TabButton
            active={tab === "aliases"}
            onClick={() => setTab("aliases")}
            icon={<Tag className="h-3.5 w-3.5" />}
            label="Raw labels"
            count={aliases.length}
          />
        )}
      </div>

      {tab === "schemas" && (
        <Card>
          <CardContent className="p-3 space-y-1.5 max-h-[600px] overflow-y-auto">
            {schemas.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                No schemas. The alias mapping may not cover this system yet.
              </p>
            ) : (
              schemas.map((s) => (
                <div
                  key={`${s.catalog_name}.${s.schema_name}`}
                  className="border rounded-md p-2.5 hover:border-muted-foreground/40 transition-colors"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <code className="text-sm font-mono font-semibold truncate">
                          {s.catalog_name}.{s.schema_name}
                        </code>
                        <Badge variant="outline" className="text-[9px]">
                          {s.environment}
                        </Badge>
                        {s.zone && s.zone !== "OTHER" && (
                          <Badge variant="outline" className="text-[9px]">
                            {s.zone}
                          </Badge>
                        )}
                      </div>
                      <div className="flex items-center gap-2 mt-1 text-[11px] text-muted-foreground">
                        <Building2 className="h-3 w-3" />
                        <span>{s.affiliate}</span>
                        {s.schema_friendly_name && (
                          <>
                            <span>·</span>
                            <span className="truncate">
                              {s.schema_friendly_name}
                            </span>
                          </>
                        )}
                      </div>
                      {(s.raw_source_systems || []).length > 1 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {s.raw_source_systems
                            .filter(Boolean)
                            .slice(0, 4)
                            .map((r) => (
                              <Badge
                                key={r}
                                variant="secondary"
                                className="text-[9px] py-0"
                              >
                                {r}
                              </Badge>
                            ))}
                          {s.raw_source_systems.length > 4 && (
                            <Badge
                              variant="secondary"
                              className="text-[9px] py-0"
                            >
                              +{s.raw_source_systems.length - 4}
                            </Badge>
                          )}
                        </div>
                      )}
                    </div>
                    <div className="flex-shrink-0 text-right">
                      <div className="text-sm font-semibold tabular-nums">
                        {fmtNum(s.table_count)}
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 text-[10px] mt-0.5"
                        onClick={() => openTablesFor(s)}
                      >
                        view tables
                        <ChevronRight className="h-3 w-3 ml-0.5" />
                      </Button>
                    </div>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      )}

      {tab === "tables" && (
        <Card>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative flex-1 min-w-[200px]">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder="Search tables..."
                  value={tableSearch}
                  onChange={(e) => {
                    setTableSearch(e.target.value);
                    setOffset(0);
                  }}
                  className="pl-9 h-9"
                />
              </div>
              {schemaFilter && (
                <Badge
                  variant="outline"
                  className="gap-1 cursor-pointer hover:border-destructive"
                  onClick={() => {
                    setSchemaFilter(null);
                    setOffset(0);
                  }}
                >
                  <Database className="h-3 w-3" />
                  {schemaFilter.catalog}.{schemaFilter.schema}
                  <X className="h-3 w-3" />
                </Badge>
              )}
              <span className="text-xs text-muted-foreground ml-auto">
                {fmtNum(tablesData?.total || 0)} tables
              </span>
            </div>
          </CardHeader>
          <CardContent className="pt-0 space-y-1.5 max-h-[600px] overflow-y-auto">
            {tablesLoading ? (
              Array.from({ length: 6 }).map((_, i) => (
                <div
                  key={i}
                  className="h-12 bg-muted animate-pulse rounded"
                />
              ))
            ) : (tablesData?.tables || []).length === 0 ? (
              <p className="text-sm text-muted-foreground py-6 text-center">
                No tables match your filters.
              </p>
            ) : (
              (tablesData.tables as TableRow[]).map((t) => (
                <div
                  key={`${t.table_catalog}.${t.table_schema}.${t.table_name}`}
                  className="border rounded-md p-2"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <code className="text-sm font-mono font-medium truncate">
                          {t.table_name}
                        </code>
                        {t.business_friendly_name && (
                          <span className="text-xs text-muted-foreground">
                            — {t.business_friendly_name}
                          </span>
                        )}
                      </div>
                      <p className="text-[10px] text-muted-foreground truncate">
                        {t.table_catalog}.{t.table_schema}
                      </p>
                      {t.ai_definition && (
                        <p className="text-[11px] text-muted-foreground mt-1 line-clamp-2">
                          {t.ai_definition}
                        </p>
                      )}
                    </div>
                    <div className="flex gap-1 flex-shrink-0">
                      <Badge variant="outline" className="text-[9px]">
                        {t.environment}
                      </Badge>
                      <Badge variant="outline" className="text-[9px]">
                        {t.table_type}
                      </Badge>
                    </div>
                  </div>
                </div>
              ))
            )}

            {/* Pagination */}
            {(tablesData?.total || 0) > limit && (
              <div className="flex justify-between items-center pt-2 sticky bottom-0 bg-card">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                >
                  <ChevronLeft className="h-4 w-4 mr-1" /> Prev
                </Button>
                <span className="text-xs text-muted-foreground">
                  {offset + 1}-
                  {Math.min(offset + limit, tablesData?.total || 0)} of{" "}
                  {fmtNum(tablesData?.total)}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + limit >= (tablesData?.total || 0)}
                  onClick={() => setOffset(offset + limit)}
                >
                  Next <ChevronRight className="h-4 w-4 ml-1" />
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {tab === "aliases" && !isUnclassified && (
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>
              These are the raw{" "}
              <code className="text-[11px] bg-muted px-1 rounded">
                source_system
              </code>{" "}
              labels from your ingested tables that collapse into{" "}
              <strong>{meta.name}</strong>. Edit them in the normalization job&apos;s
              alias table to refine classification.
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0 space-y-1 max-h-[600px] overflow-y-auto">
            {aliases.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                No aliases mapped yet.
              </p>
            ) : (
              aliases.map((a) => (
                <div
                  key={a.raw}
                  className="flex items-center justify-between gap-2 border rounded-md px-2 py-1.5"
                >
                  <code className="text-xs font-mono truncate">{a.raw}</code>
                  <div className="flex gap-1 flex-shrink-0">
                    <Badge variant="outline" className="text-[9px]">
                      {a.mapped_by}
                    </Badge>
                    {a.confidence && (
                      <Badge variant="outline" className="text-[9px]">
                        {a.confidence}
                      </Badge>
                    )}
                    {a.is_user_edited && (
                      <Badge variant="secondary" className="text-[9px]">
                        edited
                      </Badge>
                    )}
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function StatTile({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-md border bg-background p-2.5">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className="text-lg font-semibold tabular-nums mt-0.5">{value}</div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  count?: number;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 px-3 py-2 text-sm border-b-2 -mb-px transition-colors ${
        active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      {icon}
      {label}
      {typeof count === "number" && (
        <span className="text-[10px] tabular-nums">({fmtNum(count)})</span>
      )}
    </button>
  );
}
