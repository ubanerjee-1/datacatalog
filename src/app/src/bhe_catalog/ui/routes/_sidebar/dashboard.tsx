import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchBranding, fetchCatalogStats, fetchCompanyProfile } from "@/lib/api-client";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Database, Layers, Table2, Sparkles } from "lucide-react";

export const Route = createFileRoute("/_sidebar/dashboard")({
  component: DashboardPage,
});

function StatCard({
  title,
  value,
  description,
  icon,
}: {
  title: string;
  value: number | string;
  description: string;
  icon: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        <div className="text-muted-foreground">{icon}</div>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value.toLocaleString()}</div>
        <p className="text-xs text-muted-foreground">{description}</p>
      </CardContent>
    </Card>
  );
}

function DistributionChart({
  title,
  data,
  colorFn,
}: {
  title: string;
  data: { name: string; value: number }[];
  colorFn: (i: number) => string;
}) {
  const total = data.reduce((s, d) => s + d.value, 0);
  if (!data.length) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.slice(0, 10).map((item, i) => {
          const pct = total > 0 ? (item.value / total) * 100 : 0;
          return (
            <div key={item.name} className="space-y-1">
              <div className="flex justify-between text-sm">
                <span className="truncate max-w-[180px]">{item.name}</span>
                <span className="text-muted-foreground">
                  {item.value.toLocaleString()} ({pct.toFixed(1)}%)
                </span>
              </div>
              <div className="h-2 bg-muted rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${pct}%`,
                    backgroundColor: colorFn(i),
                  }}
                />
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

const COLORS = [
  "#4ECDC4", "#FF6B6B", "#A29BFE", "#45B7D1", "#F9CA24",
  "#6C5CE7", "#FD79A8", "#00B894", "#E17055", "#74B9FF",
];

function DashboardPage() {
  const { data: stats, isLoading } = useQuery({
    queryKey: ["catalogStats"],
    queryFn: fetchCatalogStats,
  });
  const { data: branding } = useQuery({
    queryKey: ["branding"],
    queryFn: fetchBranding,
    staleTime: 60_000,
    retry: 1,
  });
  const { data: profile } = useQuery({
    queryKey: ["companyProfile"],
    queryFn: fetchCompanyProfile,
    staleTime: 60_000,
    retry: 1,
  });

  if (isLoading) {
    return (
      <div className="p-6 space-y-6">
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {[1, 2, 3, 4].map((i) => (
            <Card key={i}>
              <CardContent className="h-24 animate-pulse bg-muted rounded" />
            </Card>
          ))}
        </div>
      </div>
    );
  }

  const s = stats || {};
  // AI Coverage uses `enrichable_schemas` (PRODUCTION-only) as the denominator
  // because the enrichment job intentionally skips DEV/QA/SBX. Falling back
  // to total_schemas keeps the dashboard sane on older API responses that
  // don't yet include the field.
  const enrichableSchemas = s.enrichable_schemas ?? s.total_schemas ?? 0;
  const enrichmentPct =
    enrichableSchemas > 0
      ? ((s.enriched_schemas / enrichableSchemas) * 100).toFixed(1)
      : "0";

  const catalogName = (branding?.catalog_name || "").trim() || "Data Catalog";
  const companyName = (profile?.company_name || "").trim();
  const tagline = companyName
    ? `AI-powered metadata intelligence for ${companyName}`
    : "AI-powered metadata intelligence";

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{catalogName}</h1>
          <p className="text-muted-foreground">{tagline}</p>
        </div>
        <Badge variant="outline" className="text-sm">
          <Sparkles className="mr-1 h-3 w-3" />
          {enrichmentPct}% enriched
        </Badge>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          title="Catalogs"
          value={s.total_catalogs || 0}
          description="Unity Catalog namespaces"
          icon={<Database className="h-4 w-4" />}
        />
        <StatCard
          title="Schemas"
          value={s.total_schemas || 0}
          description={
            enrichableSchemas
              ? `${(s.enriched_schemas || 0).toLocaleString()} of ${enrichableSchemas.toLocaleString()} production enriched`
              : `${s.enriched_schemas || 0} AI-enriched`
          }
          icon={<Layers className="h-4 w-4" />}
        />
        <StatCard
          title="Tables"
          value={s.total_tables || 0}
          description={
            s.enrichable_tables
              ? `${(s.enriched_tables || 0).toLocaleString()} of ${s.enrichable_tables.toLocaleString()} production enriched`
              : `${s.enriched_tables || 0} AI-enriched`
          }
          icon={<Table2 className="h-4 w-4" />}
        />
        <StatCard
          title="AI Coverage"
          value={`${enrichmentPct}%`}
          description="Production schemas with AI definitions"
          icon={<Sparkles className="h-4 w-4" />}
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <DistributionChart
          title="By Environment"
          data={s.environments || []}
          colorFn={(i) => COLORS[i % COLORS.length]}
        />
        <DistributionChart
          title="By Business Domain"
          data={s.domains || []}
          colorFn={(i) => COLORS[i % COLORS.length]}
        />
        <DistributionChart
          title="By Table Type"
          data={s.table_types || []}
          colorFn={(i) => COLORS[i % COLORS.length]}
        />
        <DistributionChart
          title="By Department"
          data={s.departments || []}
          colorFn={(i) => COLORS[i % COLORS.length]}
        />
      </div>
    </div>
  );
}
