import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  fetchSourceSummary,
  fetchWorkspaceSummary,
  fetchEnvConsistency,
} from "@/lib/api-client";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  BarChart3,
  AlertTriangle,
  Server,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/analytics")({
  component: AnalyticsPage,
});

type Tab = "heatmap" | "consistency" | "workspaces";

function AnalyticsPage() {
  const [tab, setTab] = useState<Tab>("heatmap");

  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: "heatmap", label: "Source Heatmap", icon: <BarChart3 className="h-4 w-4" /> },
    { id: "consistency", label: "Env Consistency", icon: <AlertTriangle className="h-4 w-4" /> },
    { id: "workspaces", label: "Workspaces", icon: <Server className="h-4 w-4" /> },
  ];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <BarChart3 className="h-6 w-6" />
          Platform Analytics
        </h1>
        <p className="text-muted-foreground">
          Source inventory, environment consistency, and workspace overview
        </p>
      </div>

      <div className="flex gap-1 border-b">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t.id
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {tab === "heatmap" && <SourceHeatmapTab />}
      {tab === "consistency" && <EnvConsistencyTab />}
      {tab === "workspaces" && <WorkspacesTab />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Source Heatmap Tab
// ---------------------------------------------------------------------------

function SourceHeatmapTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["sourceSummary"],
    queryFn: fetchSourceSummary,
  });

  if (isLoading) return <LoadingCard />;

  const sources = data?.sources || [];
  if (!sources.length) return <EmptyCard message="No source data. Run 'Populate Gold' first." />;

  const maxTables = Math.max(...sources.map((s: any) => Math.max(s.dev_tables || 0, s.qa_tables || 0, s.prod_tables || 0)), 1);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-lg">Source x Environment Table Counts</CardTitle>
          <CardDescription>Color intensity reflects table count relative to maximum</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-2 px-3 font-medium">Program</th>
                  <th className="text-left py-2 px-3 font-medium">Affiliate</th>
                  <th className="text-right py-2 px-3 font-medium">Dev</th>
                  <th className="text-right py-2 px-3 font-medium">QA</th>
                  <th className="text-right py-2 px-3 font-medium">Prod</th>
                  <th className="text-right py-2 px-3 font-medium">Total</th>
                  <th className="text-right py-2 px-3 font-medium">Consistency</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((s: any) => (
                  <tr key={s.program} className="border-b border-border/50 hover:bg-muted/30">
                    <td className="py-2 px-3 font-mono text-xs">{s.program}</td>
                    <td className="py-2 px-3">
                      <Badge variant="outline" className="text-xs">{s.affiliate}</Badge>
                    </td>
                    <td className="py-2 px-3 text-right">
                      <HeatCell value={s.dev_tables} max={maxTables} />
                    </td>
                    <td className="py-2 px-3 text-right">
                      <HeatCell value={s.qa_tables} max={maxTables} />
                    </td>
                    <td className="py-2 px-3 text-right">
                      <HeatCell value={s.prod_tables} max={maxTables} />
                    </td>
                    <td className="py-2 px-3 text-right font-semibold">
                      {(s.total_tables || 0).toLocaleString()}
                    </td>
                    <td className="py-2 px-3 text-right">
                      <ConsistencyBadge score={s.consistency_score} />
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 font-semibold">
                  <td className="py-2 px-3" colSpan={2}>Grand Total</td>
                  <td className="py-2 px-3 text-right">
                    {sources.reduce((a: number, s: any) => a + (s.dev_tables || 0), 0).toLocaleString()}
                  </td>
                  <td className="py-2 px-3 text-right">
                    {sources.reduce((a: number, s: any) => a + (s.qa_tables || 0), 0).toLocaleString()}
                  </td>
                  <td className="py-2 px-3 text-right">
                    {sources.reduce((a: number, s: any) => a + (s.prod_tables || 0), 0).toLocaleString()}
                  </td>
                  <td className="py-2 px-3 text-right">
                    {sources.reduce((a: number, s: any) => a + (s.total_tables || 0), 0).toLocaleString()}
                  </td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-lg">Environment Gaps</CardTitle>
          <CardDescription>Programs with missing environments or zero tables</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {sources
              .filter((s: any) => s.consistency_score < 100 && s.total_tables > 10)
              .map((s: any) => (
                <div key={s.program} className="border rounded-lg p-3 space-y-2">
                  <div className="flex justify-between items-center">
                    <span className="font-mono text-sm font-medium">{s.program}</span>
                    <ConsistencyBadge score={s.consistency_score} />
                  </div>
                  <div className="text-xs text-muted-foreground space-y-1">
                    {(!s.prod_tables || s.prod_tables === 0) && (
                      <div className="text-red-500 font-medium">No production tables</div>
                    )}
                    {(!s.dev_tables || s.dev_tables === 0) && (
                      <div className="text-amber-500">No dev tables</div>
                    )}
                    {(!s.qa_tables || s.qa_tables === 0) && (
                      <div className="text-amber-500">No QA tables</div>
                    )}
                    <div>Dev: {s.dev_schemas} schemas / QA: {s.qa_schemas} / Prod: {s.prod_schemas}</div>
                  </div>
                </div>
              ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function HeatCell({ value, max }: { value: number; max: number }) {
  if (!value) return <span className="text-muted-foreground">0</span>;
  const intensity = Math.min(value / max, 1);
  const alpha = 0.15 + intensity * 0.7;
  return (
    <span
      className="inline-block px-2 py-0.5 rounded text-xs font-mono"
      style={{
        backgroundColor: `rgba(34, 197, 94, ${alpha})`,
        color: intensity > 0.5 ? "white" : "inherit",
      }}
    >
      {value.toLocaleString()}
    </span>
  );
}

function ConsistencyBadge({ score }: { score: number }) {
  const color = score >= 80 ? "text-green-500" : score >= 40 ? "text-amber-500" : "text-red-500";
  return <span className={`text-xs font-medium ${color}`}>{score}%</span>;
}

// ---------------------------------------------------------------------------
// Env Consistency Tab
// ---------------------------------------------------------------------------

function EnvConsistencyTab() {
  const [program, setProgram] = useState("");
  const [issueType, setIssueType] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["envConsistency", { program, issue_type: issueType }],
    queryFn: () => fetchEnvConsistency({ program: program || undefined, issue_type: issueType || undefined }),
  });

  if (isLoading) return <LoadingCard />;

  const records = data?.records || [];
  const programs = data?.programs || [];
  const issueTypes = data?.issue_types || [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-lg flex items-center gap-2">
          <AlertTriangle className="h-5 w-5 text-amber-500" />
          Schemas Missing Across Environments
        </CardTitle>
        <CardDescription>
          {records.length} schemas have gaps across dev/qa/prod
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex gap-3 flex-wrap">
          <select
            value={program}
            onChange={(e) => setProgram(e.target.value)}
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          >
            <option value="">All Programs</option>
            {programs.map((p: string) => <option key={p} value={p}>{p}</option>)}
          </select>
          <select
            value={issueType}
            onChange={(e) => setIssueType(e.target.value)}
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          >
            <option value="">All Issue Types</option>
            {issueTypes.map((t: string) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="text-left py-2 px-3 font-medium">Program</th>
                <th className="text-left py-2 px-3 font-medium">Schema</th>
                <th className="text-center py-2 px-2 font-medium">Dev</th>
                <th className="text-center py-2 px-2 font-medium">QA</th>
                <th className="text-center py-2 px-2 font-medium">Prod</th>
                <th className="text-right py-2 px-3 font-medium">Dev Tbl</th>
                <th className="text-right py-2 px-3 font-medium">QA Tbl</th>
                <th className="text-right py-2 px-3 font-medium">Prod Tbl</th>
                <th className="text-left py-2 px-3 font-medium">Issue</th>
              </tr>
            </thead>
            <tbody>
              {records.map((r: any, i: number) => (
                <tr key={i} className="border-b border-border/50 hover:bg-muted/30">
                  <td className="py-1.5 px-3 font-mono text-xs">{r.program}</td>
                  <td className="py-1.5 px-3 font-mono text-xs">{r.schema_name}</td>
                  <td className="py-1.5 px-2 text-center">
                    <EnvDot present={r.in_dev} />
                  </td>
                  <td className="py-1.5 px-2 text-center">
                    <EnvDot present={r.in_qa} />
                  </td>
                  <td className="py-1.5 px-2 text-center">
                    <EnvDot present={r.in_prod} />
                  </td>
                  <td className="py-1.5 px-3 text-right text-xs">{r.dev_tables || 0}</td>
                  <td className="py-1.5 px-3 text-right text-xs">{r.qa_tables || 0}</td>
                  <td className="py-1.5 px-3 text-right text-xs">{r.prod_tables || 0}</td>
                  <td className="py-1.5 px-3">
                    <IssueBadge type={r.issue_type} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!records.length && (
            <div className="text-center py-8 text-muted-foreground">
              No consistency issues found for these filters.
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function EnvDot({ present }: { present: boolean | string }) {
  const is = present === true || present === "true";
  return (
    <span className={`inline-block w-3 h-3 rounded-full ${is ? "bg-green-500" : "bg-red-400"}`} />
  );
}

function IssueBadge({ type }: { type: string }) {
  const colors: Record<string, string> = {
    missing_in_prod: "bg-red-500/15 text-red-600",
    dev_only: "bg-amber-500/15 text-amber-600",
    qa_only: "bg-amber-500/15 text-amber-600",
    prod_only: "bg-blue-500/15 text-blue-600",
    missing_in_qa: "bg-amber-500/15 text-amber-600",
    missing_in_dev: "bg-amber-500/15 text-amber-600",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${colors[type] || "bg-muted text-muted-foreground"}`}>
      {type.replace(/_/g, " ")}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Workspaces Tab
// ---------------------------------------------------------------------------

function WorkspacesTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["workspaceSummary"],
    queryFn: fetchWorkspaceSummary,
  });

  if (isLoading) return <LoadingCard />;
  const workspaces = data?.workspaces || [];
  if (!workspaces.length) return <EmptyCard message="No workspace data. Run 'Populate Gold' first." />;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {workspaces.map((ws: any) => (
        <Card key={ws.workspace_id}>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-mono">
              {ws.workspace_name || ws.workspace_id?.slice(0, 12) + "..."}
            </CardTitle>
            <CardDescription className="text-xs truncate">
              {ws.workspace_url}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="border rounded p-2">
                <div className="text-lg font-bold">{ws.catalog_count}</div>
                <div className="text-[10px] text-muted-foreground">Catalogs</div>
              </div>
              <div className="border rounded p-2">
                <div className="text-lg font-bold">{ws.schema_count}</div>
                <div className="text-[10px] text-muted-foreground">Schemas</div>
              </div>
              <div className="border rounded p-2">
                <div className="text-lg font-bold">{(ws.table_count || 0).toLocaleString()}</div>
                <div className="text-[10px] text-muted-foreground">Tables</div>
              </div>
            </div>
            <div className="flex flex-wrap gap-1">
              {(ws.programs || "").split(", ").filter(Boolean).map((p: string) => (
                <Badge key={p} variant="secondary" className="text-[10px]">{p}</Badge>
              ))}
            </div>
            <div className="flex flex-wrap gap-1">
              {(ws.environments || "").split(", ").filter(Boolean).map((e: string) => (
                <Badge key={e} variant="outline" className="text-[10px]">{e}</Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared components
// ---------------------------------------------------------------------------

function LoadingCard() {
  return (
    <Card>
      <CardContent className="flex items-center justify-center h-48">
        <div className="animate-pulse text-muted-foreground">Loading...</div>
      </CardContent>
    </Card>
  );
}

function EmptyCard({ message }: { message: string }) {
  return (
    <Card>
      <CardContent className="flex flex-col items-center justify-center h-48 text-muted-foreground">
        <BarChart3 className="h-10 w-10 mb-3 opacity-40" />
        <p>{message}</p>
      </CardContent>
    </Card>
  );
}
