import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  fetchGapsMatrix,
  type GapsCanonical,
  type GapsAffiliate,
  type GapsCell,
  type GapsCellState,
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
import { Skeleton } from "@/components/ui/skeleton";
import {
  Search,
  AlertTriangle,
  CheckCircle2,
  Sparkles,
  Filter,
  X,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/gaps")({
  component: GapsPage,
});

// ---------------------------------------------------------------------------
// Gaps page
//
// A pivot of canonical source systems (rows) against BHE affiliates (columns)
// that exposes whitespace:
//
//   - red   "gap"      -> a use case applicable to this affiliate needs this
//                         canonical but it is missing from the lake
//   - green "covered"  -> required and present
//   - blue  "available"-> present but no active use case needs it for this
//                         affiliate (opportunity to design a new use case)
//   - blank            -> neither required nor present
//
// A row that is red across every affiliate it is required in is a
// "universal gap" - something no affiliate has today.
// ---------------------------------------------------------------------------

type Mode = "all" | "gaps-only" | "universal-gaps";

function formatUsdCompact(n: number): string {
  if (!n) return "$0";
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(0)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function GapsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["gaps-matrix"],
    queryFn: fetchGapsMatrix,
  });

  const [search, setSearch] = useState("");
  const [mode, setMode] = useState<Mode>("all");
  const [selectedCell, setSelectedCell] = useState<
    | (GapsCell & { canonicalMeta?: GapsCanonical; affiliateMeta?: GapsAffiliate })
    | null
  >(null);

  const { canonicals, affiliates, cellMap, visibleCanonicals } = useMemo(() => {
    if (!data) {
      return {
        canonicals: [] as GapsCanonical[],
        affiliates: [] as GapsAffiliate[],
        cellMap: new Map<string, GapsCell>(),
        visibleCanonicals: [] as GapsCanonical[],
      };
    }
    const cm = new Map<string, GapsCell>();
    for (const c of data.cells) {
      cm.set(`${c.canonical}|||${c.affiliate}`, c);
    }

    const q = search.trim().toLowerCase();
    const filtered = data.canonicals.filter((c) => {
      if (q) {
        const hay = `${c.name} ${c.category ?? ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (mode === "gaps-only") {
        return c.affiliates_gap > 0;
      }
      if (mode === "universal-gaps") {
        // required somewhere but present nowhere
        return c.affiliates_needing > 0 && c.affiliates_present === 0;
      }
      return true;
    });

    return {
      canonicals: data.canonicals,
      affiliates: data.affiliates,
      cellMap: cm,
      visibleCanonicals: filtered,
    };
  }, [data, search, mode]);

  return (
    <div className="flex flex-col h-full">
      {/* Sticky header */}
      <div className="sticky top-0 z-20 border-b bg-background">
        <div className="px-6 py-4 flex flex-col gap-3">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
                <AlertTriangle className="h-6 w-6 text-amber-500" />
                Gaps
              </h1>
              <p className="text-sm text-muted-foreground mt-1">
                Whitespace across the BHE data estate: which canonical source
                systems each affiliate needs versus what is actually in the
                lake. Red cells are the shortest path to unlocking more use
                cases.
              </p>
            </div>
          </div>

          {/* KPI strip */}
          {data?.summary && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <KpiTile
                label="Canonical sources tracked"
                value={data.summary.canonical_count.toString()}
                sub={`${data.summary.affiliate_count} affiliates`}
              />
              <KpiTile
                label="Gap cells (red)"
                value={data.summary.gap_count.toString()}
                sub={`${formatUsdCompact(data.summary.total_gap_value)} of use-case value at risk`}
                tone="gap"
              />
              <KpiTile
                label="Covered cells (green)"
                value={data.summary.covered_count.toString()}
                sub="required and present"
                tone="covered"
              />
              <KpiTile
                label="Available, unused (blue)"
                value={data.summary.available_count.toString()}
                sub="in the lake, no use case yet"
                tone="available"
              />
            </div>
          )}

          {/* Controls */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                type="search"
                placeholder="Search source or category..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8 h-9 w-64"
              />
            </div>
            <div className="flex items-center gap-1 ml-2">
              <Filter className="h-4 w-4 text-muted-foreground" />
              <ModeChip active={mode === "all"} onClick={() => setMode("all")}>
                All sources
              </ModeChip>
              <ModeChip
                active={mode === "gaps-only"}
                onClick={() => setMode("gaps-only")}
              >
                Gaps only
              </ModeChip>
              <ModeChip
                active={mode === "universal-gaps"}
                onClick={() => setMode("universal-gaps")}
              >
                Missing everywhere
              </ModeChip>
            </div>

            {/* Legend */}
            <div className="ml-auto flex items-center gap-3 text-xs text-muted-foreground">
              <LegendSwatch tone="gap" label="Gap" />
              <LegendSwatch tone="covered" label="Covered" />
              <LegendSwatch tone="available" label="Available, unused" />
              <span className="inline-flex items-center gap-1.5">
                <span className="inline-block w-3 h-3 rounded border border-muted-foreground/30" />
                Not required
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto">
        <div className="p-6">
          {error && (
            <Card className="border-destructive/50">
              <CardContent className="pt-6 text-sm text-destructive">
                Failed to load gaps matrix:{" "}
                {(error as Error)?.message ?? "unknown error"}
              </CardContent>
            </Card>
          )}

          {isLoading && (
            <div className="space-y-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          )}

          {data && visibleCanonicals.length === 0 && (
            <Card>
              <CardContent className="pt-6 text-sm text-muted-foreground">
                No sources match the current filters.
              </CardContent>
            </Card>
          )}

          {data && visibleCanonicals.length > 0 && (
            <GapMatrix
              canonicals={visibleCanonicals}
              affiliates={affiliates}
              cellMap={cellMap}
              onCellClick={(cell, canonicalMeta, affiliateMeta) =>
                setSelectedCell({ ...cell, canonicalMeta, affiliateMeta })
              }
            />
          )}

          {/* Universal-gap call-out below the table */}
          {data && mode !== "universal-gaps" && (
            <UniversalGapsPanel canonicals={canonicals} />
          )}
        </div>
      </div>

      {/* Cell detail drawer */}
      {selectedCell && (
        <CellDetailDrawer
          cell={selectedCell}
          onClose={() => setSelectedCell(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Matrix
// ---------------------------------------------------------------------------

function GapMatrix({
  canonicals,
  affiliates,
  cellMap,
  onCellClick,
}: {
  canonicals: GapsCanonical[];
  affiliates: GapsAffiliate[];
  cellMap: Map<string, GapsCell>;
  onCellClick: (
    cell: GapsCell,
    canonicalMeta: GapsCanonical,
    affiliateMeta: GapsAffiliate,
  ) => void;
}) {
  return (
    <div className="border rounded-lg overflow-auto bg-card">
      <table className="text-sm border-separate border-spacing-0 min-w-full">
        <thead>
          <tr>
            <th className="sticky left-0 top-0 z-30 bg-muted/70 border-b border-r px-3 py-2 text-left font-medium min-w-[260px]">
              Canonical source
            </th>
            <th className="sticky top-0 z-20 bg-muted/70 border-b border-r px-3 py-2 text-center font-medium min-w-[90px]">
              Gaps
            </th>
            <th className="sticky top-0 z-20 bg-muted/70 border-b border-r px-3 py-2 text-center font-medium min-w-[90px]">
              Use cases
            </th>
            <th className="sticky top-0 z-20 bg-muted/70 border-b border-r px-3 py-2 text-right font-medium min-w-[100px]">
              Value at risk
            </th>
            {affiliates.map((a) => (
              <th
                key={a.affiliate_name}
                className="sticky top-0 z-20 bg-muted/70 border-b border-r px-2 py-2 text-center font-medium"
                title={a.description ?? ""}
              >
                <div className="flex flex-col items-center gap-0.5">
                  <span className="whitespace-nowrap">
                    {a.affiliate_code ?? a.affiliate_name}
                  </span>
                  {a.business_type && (
                    <span className="text-[10px] font-normal text-muted-foreground whitespace-nowrap">
                      {a.business_type}
                    </span>
                  )}
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {canonicals.map((c) => (
            <tr key={c.name} className="group">
              <td className="sticky left-0 z-10 bg-background border-b border-r px-3 py-1.5 font-medium group-hover:bg-muted/40">
                <div className="flex flex-col">
                  <span>{c.name}</span>
                  {c.category && (
                    <span className="text-[10px] text-muted-foreground">
                      {c.category}
                    </span>
                  )}
                </div>
              </td>
              <td className="border-b border-r px-3 py-1.5 text-center">
                {c.affiliates_gap > 0 ? (
                  <Badge
                    variant="destructive"
                    className="font-mono text-[11px]"
                  >
                    {c.affiliates_gap}
                  </Badge>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              <td className="border-b border-r px-3 py-1.5 text-center tabular-nums">
                {c.total_use_cases}
              </td>
              <td className="border-b border-r px-3 py-1.5 text-right tabular-nums">
                {c.total_value_affected > 0
                  ? formatUsdCompact(c.total_value_affected)
                  : "—"}
              </td>
              {affiliates.map((a) => {
                const cell = cellMap.get(`${c.name}|||${a.affiliate_name}`);
                return (
                  <td
                    key={a.affiliate_name}
                    className="border-b border-r p-0"
                    style={{ width: 48, minWidth: 48 }}
                  >
                    <MatrixCell
                      cell={cell}
                      onClick={() => {
                        if (cell) onCellClick(cell, c, a);
                      }}
                    />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MatrixCell({
  cell,
  onClick,
}: {
  cell: GapsCell | undefined;
  onClick: () => void;
}) {
  if (!cell) {
    return <div className="h-9 w-full" aria-hidden />;
  }
  const toneClass =
    cell.state === "gap"
      ? "bg-rose-500/85 hover:bg-rose-500 text-white"
      : cell.state === "covered"
        ? "bg-emerald-500/85 hover:bg-emerald-500 text-white"
        : "bg-sky-400/70 hover:bg-sky-400 text-white";

  const label =
    cell.state === "gap"
      ? `GAP - ${cell.uc_count} use case${cell.uc_count === 1 ? "" : "s"} need this here, not in the lake`
      : cell.state === "covered"
        ? `COVERED - ${cell.uc_count} use case${cell.uc_count === 1 ? "" : "s"} use this`
        : `AVAILABLE - present in the lake, no use case tagged here yet`;

  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      className={`h-9 w-full flex items-center justify-center text-[11px] font-semibold tabular-nums ${toneClass} focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-primary`}
    >
      {cell.state === "gap" && (cell.must_count > 0 ? "!" : "•")}
      {cell.state === "covered" && cell.uc_count > 0 ? cell.uc_count : ""}
      {cell.state === "available" && "+"}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Detail drawer
// ---------------------------------------------------------------------------

function CellDetailDrawer({
  cell,
  onClose,
}: {
  cell: GapsCell & {
    canonicalMeta?: GapsCanonical;
    affiliateMeta?: GapsAffiliate;
  };
  onClose: () => void;
}) {
  const tone =
    cell.state === "gap"
      ? { label: "Gap", color: "bg-rose-500", icon: <AlertTriangle className="h-4 w-4" /> }
      : cell.state === "covered"
        ? { label: "Covered", color: "bg-emerald-500", icon: <CheckCircle2 className="h-4 w-4" /> }
        : { label: "Available, unused", color: "bg-sky-500", icon: <Sparkles className="h-4 w-4" /> };

  return (
    <div
      className="fixed inset-0 z-40 bg-black/30 flex justify-end"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md h-full bg-background shadow-xl border-l overflow-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-background border-b px-5 py-4 flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-wide text-muted-foreground">
              {cell.affiliateMeta?.affiliate_name ?? cell.affiliate}
            </div>
            <div className="text-lg font-semibold">{cell.canonical}</div>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>

        <div className="p-5 space-y-4">
          <Badge
            className={`${tone.color} text-white inline-flex items-center gap-1.5 px-2.5 py-1`}
          >
            {tone.icon}
            {tone.label}
          </Badge>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">This cell</CardTitle>
              <CardDescription>
                What the data says about this source + affiliate pair.
              </CardDescription>
            </CardHeader>
            <CardContent className="text-sm space-y-2">
              <Row label="Required by use cases">
                {cell.is_required
                  ? `${cell.uc_count} (${cell.must_count} must-have)`
                  : "No"}
              </Row>
              <Row label="Present in the lake">
                {cell.is_present ? "Yes" : "No"}
              </Row>
              {cell.is_required && (
                <Row label="Use-case value affected">
                  {formatUsdCompact(cell.total_value)}
                </Row>
              )}
            </CardContent>
          </Card>

          {cell.canonicalMeta && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  {cell.canonical} across BHE
                </CardTitle>
                <CardDescription>
                  How this canonical source is distributed across affiliates.
                </CardDescription>
              </CardHeader>
              <CardContent className="text-sm space-y-2">
                {cell.canonicalMeta.category && (
                  <Row label="Category">{cell.canonicalMeta.category}</Row>
                )}
                <Row label="Affiliates needing it">
                  {cell.canonicalMeta.affiliates_needing}
                </Row>
                <Row label="Affiliates that have it">
                  {cell.canonicalMeta.affiliates_present}
                </Row>
                <Row label="Affiliates with a gap">
                  <span
                    className={
                      cell.canonicalMeta.affiliates_gap > 0
                        ? "text-rose-600 font-semibold"
                        : ""
                    }
                  >
                    {cell.canonicalMeta.affiliates_gap}
                  </span>
                </Row>
                <Row label="Total use cases using it">
                  {cell.canonicalMeta.total_use_cases}
                </Row>
                <Row label="Value affected">
                  {formatUsdCompact(
                    cell.canonicalMeta.total_value_affected,
                  )}
                </Row>
              </CardContent>
            </Card>
          )}

          {cell.affiliateMeta && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  {cell.affiliateMeta.affiliate_name}
                </CardTitle>
                <CardDescription>
                  {cell.affiliateMeta.business_type}
                  {cell.affiliateMeta.region
                    ? ` · ${cell.affiliateMeta.region}`
                    : ""}
                </CardDescription>
              </CardHeader>
              <CardContent className="text-sm space-y-2">
                {cell.affiliateMeta.description && (
                  <p className="text-muted-foreground leading-relaxed">
                    {cell.affiliateMeta.description}
                  </p>
                )}
                <Row label="Sources required">
                  {cell.affiliateMeta.required_count}
                </Row>
                <Row label="Sources present">
                  {cell.affiliateMeta.present_count}
                </Row>
                <Row label="Gaps">
                  <span
                    className={
                      cell.affiliateMeta.gap_count > 0
                        ? "text-rose-600 font-semibold"
                        : ""
                    }
                  >
                    {cell.affiliateMeta.gap_count}
                  </span>
                </Row>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// "Missing everywhere" panel
// ---------------------------------------------------------------------------

function UniversalGapsPanel({ canonicals }: { canonicals: GapsCanonical[] }) {
  const universal = canonicals
    .filter(
      (c) => c.affiliates_needing > 0 && c.affiliates_present === 0,
    )
    .sort((a, b) => b.total_value_affected - a.total_value_affected);

  if (universal.length === 0) return null;

  return (
    <Card className="mt-6 border-rose-200 dark:border-rose-900">
      <CardHeader className="pb-2">
        <CardTitle className="text-base flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 text-rose-500" />
          Missing everywhere
        </CardTitle>
        <CardDescription>
          Canonical sources that at least one use case requires, but which no
          affiliate has in the lake today. Ingesting any of these lights up
          value across multiple affiliates at once.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
          {universal.slice(0, 12).map((c) => (
            <div
              key={c.name}
              className="border rounded-md px-3 py-2 bg-rose-50/40 dark:bg-rose-950/20"
            >
              <div className="font-medium text-sm">{c.name}</div>
              <div className="text-xs text-muted-foreground flex items-center gap-2 mt-0.5">
                <span>{c.affiliates_needing} affiliates</span>
                <span>·</span>
                <span>{c.total_use_cases} use cases</span>
                <span>·</span>
                <span className="font-semibold text-rose-600">
                  {formatUsdCompact(c.total_value_affected)}
                </span>
              </div>
            </div>
          ))}
        </div>
        {universal.length > 12 && (
          <div className="text-xs text-muted-foreground mt-2">
            …and {universal.length - 12} more. Switch the filter to "Missing
            everywhere" to see the full list.
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Small primitives
// ---------------------------------------------------------------------------

function KpiTile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: GapsCellState;
}) {
  const accent =
    tone === "gap"
      ? "border-rose-200 dark:border-rose-900"
      : tone === "covered"
        ? "border-emerald-200 dark:border-emerald-900"
        : tone === "available"
          ? "border-sky-200 dark:border-sky-900"
          : "";
  return (
    <div className={`border rounded-md px-3 py-2 ${accent}`}>
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="text-xl font-semibold mt-0.5">{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

function ModeChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`h-8 px-3 rounded-full text-xs border transition ${
        active
          ? "bg-primary text-primary-foreground border-primary"
          : "bg-background hover:bg-muted"
      }`}
    >
      {children}
    </button>
  );
}

function LegendSwatch({
  tone,
  label,
}: {
  tone: GapsCellState;
  label: string;
}) {
  const color =
    tone === "gap"
      ? "bg-rose-500"
      : tone === "covered"
        ? "bg-emerald-500"
        : "bg-sky-400";
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`inline-block w-3 h-3 rounded ${color}`} />
      {label}
    </span>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">{children}</span>
    </div>
  );
}
