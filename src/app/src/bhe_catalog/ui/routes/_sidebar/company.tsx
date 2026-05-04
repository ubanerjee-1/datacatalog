import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect, useRef } from "react";
import {
  fetchCompanyProfile,
  fetchDepartments,
  fetchUseCases,
  fetchEntities,
  triggerCompanyResearch,
  triggerEnrichJob,
  triggerPopulateGold,
  triggerTaxonomyGeneration,
  fetchJobStatus,
  fetchJobProgress,
  fetchActiveCompanyResearch,
  fetchCompanyResearchStatus,
  uploadFile,
  ingestSchemas,
  ingestTables,
  listUploadedFiles,
  downloadSchemaExtractor,
  triggerTableEnrichment,
  fetchTableEnrichmentStatus,
  fetchPipelineStatus,
  fetchBranding,
  updateBranding,
  uploadBrandingLogo,
  deleteBrandingLogo,
  fetchSetupStatus,
  bootstrapSchemas,
  bootstrapTables,
  nukeSetup,
  type SetupStatus,
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
  Building2,
  Rocket,
  Loader2,
  CheckCircle2,
  XCircle,
  Sparkles,
  Upload,
  FileUp,
  Database,
  Layers,
  Grid3X3,
  Download,
  ArrowRight,
  DollarSign,
  Target,
  Boxes,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Clock,
  Play,
  Image as ImageIcon,
  Save,
  Trash2,
  RefreshCw,
  Lock,
  Settings2,
  ShieldCheck,
  Server,
  Copy,
  AlertTriangle,
  AlertCircle,
  Skull,
  HardDriveDownload,
} from "lucide-react";
import { toast } from "sonner";

export const Route = createFileRoute("/_sidebar/company")({
  component: CompanyPage,
});

function CompanyPage() {
  const queryClient = useQueryClient();
  const [companyName, setCompanyName] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [enrichRunId, setEnrichRunId] = useState<string | null>(null);
  const [goldRunId, setGoldRunId] = useState<string | null>(null);
  const [taxRunId, setTaxRunId] = useState<string | null>(null);
  const [tableEnrichRunId, setTableEnrichRunId] = useState<string | null>(null);

  const { data: profile } = useQuery({
    queryKey: ["companyProfile"],
    queryFn: fetchCompanyProfile,
  });

  const { data: departments } = useQuery({
    queryKey: ["departments"],
    queryFn: fetchDepartments,
  });

  const { data: useCases } = useQuery({
    queryKey: ["useCases"],
    queryFn: () => fetchUseCases(),
  });

  const { data: entities } = useQuery({
    queryKey: ["entities"],
    queryFn: () => fetchEntities(),
  });

  useEffect(() => {
    if (profile?.company_name && !companyName) {
      setCompanyName(profile.company_name);
    }
  }, [profile?.company_name]);

  const { data: activeResearch } = useQuery({
    queryKey: ["activeCompanyResearch"],
    queryFn: fetchActiveCompanyResearch,
    refetchInterval: activeRunId ? false : 5000,
    enabled: !activeRunId,
  });

  useEffect(() => {
    if (!activeRunId && activeResearch?.run_id) {
      setActiveRunId(activeResearch.run_id);
    }
  }, [activeResearch?.run_id, activeRunId]);

  const { data: jobStatus } = useQuery({
    queryKey: ["jobStatus", activeRunId],
    queryFn: () => fetchJobStatus(activeRunId!),
    enabled: !!activeRunId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "TERMINATED" || status === "FAILED" || status === "SKIPPED")
        return false;
      return 5000;
    },
  });

  const { data: enrichStatus } = useQuery({
    queryKey: ["enrichStatus", enrichRunId],
    queryFn: () => fetchJobStatus(enrichRunId!),
    enabled: !!enrichRunId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "TERMINATED" || status === "FAILED") return false;
      return 5000;
    },
  });

  const { data: goldStatus } = useQuery({
    queryKey: ["goldStatus", goldRunId],
    queryFn: () => fetchJobStatus(goldRunId!),
    enabled: !!goldRunId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "TERMINATED" || status === "FAILED") return false;
      return 5000;
    },
  });

  const { data: taxStatus } = useQuery({
    queryKey: ["taxStatus", taxRunId],
    queryFn: () => fetchJobStatus(taxRunId!),
    enabled: !!taxRunId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "TERMINATED" || status === "FAILED") return false;
      return 5000;
    },
  });

  const { data: tableEnrichStatus } = useQuery({
    queryKey: ["tableEnrichStatus", tableEnrichRunId],
    queryFn: () => fetchTableEnrichmentStatus(),
    enabled: !!tableEnrichRunId,
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      if (s === "SUCCESS" || s === "FAILED" || s === "TERMINATED") return false;
      return 10000;
    },
  });

  useEffect(() => {
    if (!tableEnrichRunId) return;
    const s = tableEnrichStatus?.status;
    if (s === "SUCCESS" || s === "TERMINATED") {
      toast.success("Table enrichment complete!");
      queryClient.invalidateQueries({ queryKey: ["catalogStats"] });
      refetchPipelineStatus();
      setTableEnrichRunId(null);
    }
    if (s === "FAILED") {
      toast.error("Table enrichment failed");
      setTableEnrichRunId(null);
    }
  }, [tableEnrichStatus?.status]);

  useEffect(() => {
    if (jobStatus?.status === "TERMINATED") {
      toast.success("Company intelligence complete!");
      queryClient.invalidateQueries({ queryKey: ["companyProfile"] });
      queryClient.invalidateQueries({ queryKey: ["departments"] });
      queryClient.invalidateQueries({ queryKey: ["useCases"] });
      queryClient.invalidateQueries({ queryKey: ["entities"] });
      queryClient.invalidateQueries({ queryKey: ["sankeyData"] });
      queryClient.invalidateQueries({ queryKey: ["sankeyFilters"] });
      queryClient.invalidateQueries({ queryKey: ["companyResearchStatus"] });
      setActiveRunId(null);
    }
    if (jobStatus?.status === "FAILED") {
      toast.error("Company research failed");
      queryClient.invalidateQueries({ queryKey: ["companyResearchStatus"] });
      setActiveRunId(null);
    }
  }, [jobStatus?.status]);

  useEffect(() => {
    if (enrichStatus?.status === "TERMINATED") {
      toast.success("AI enrichment complete!");
      queryClient.invalidateQueries({ queryKey: ["catalogStats"] });
      queryClient.invalidateQueries({ queryKey: ["schemas"] });
      queryClient.invalidateQueries({ queryKey: ["schemaInventory"] });
      refetchPipelineStatus();
      setEnrichRunId(null);
    }
    if (enrichStatus?.status === "FAILED") {
      toast.error("AI enrichment failed");
      setEnrichRunId(null);
    }
  }, [enrichStatus?.status]);

  useEffect(() => {
    if (goldStatus?.status === "TERMINATED") {
      toast.success("Gold layer populated!");
      queryClient.invalidateQueries({ queryKey: ["sourceSummary"] });
      queryClient.invalidateQueries({ queryKey: ["schemaInventory"] });
      refetchPipelineStatus();
      setGoldRunId(null);
    }
    if (goldStatus?.status === "FAILED") {
      toast.error("Gold population failed");
      setGoldRunId(null);
    }
  }, [goldStatus?.status]);

  useEffect(() => {
    if (taxStatus?.status === "TERMINATED") {
      toast.success("Taxonomy generation complete!");
      queryClient.invalidateQueries({ queryKey: ["taxonomy"] });
      refetchPipelineStatus();
      setTaxRunId(null);
    }
    if (taxStatus?.status === "FAILED") {
      toast.error("Taxonomy generation failed");
      setTaxRunId(null);
    }
  }, [taxStatus?.status]);

  const { data: researchStatus, refetch: refetchResearchStatus } = useQuery({
    queryKey: ["companyResearchStatus"],
    queryFn: fetchCompanyResearchStatus,
    refetchInterval: activeRunId ? 5000 : false,
  });

  const researchMutation = useMutation({
    mutationFn: (opts: { reset?: boolean; steps?: string[]; force?: boolean }) =>
      triggerCompanyResearch(companyName, opts),
    onSuccess: (data) => {
      setActiveRunId(data.run_id);
      toast.info("Company intelligence job started...");
      refetchResearchStatus();
    },
    onError: () => toast.error("Failed to start research job"),
  });

  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);

  const enrichMutation = useMutation({
    mutationFn: triggerEnrichJob,
    onSuccess: (data) => {
      setEnrichRunId(data.run_id);
      toast.info("AI enrichment job started...");
    },
    onError: () => toast.error("Failed to start enrichment job"),
  });

  const goldMutation = useMutation({
    mutationFn: triggerPopulateGold,
    onSuccess: (data) => {
      setGoldRunId(data.run_id);
      toast.info("Gold layer population started...");
    },
    onError: () => toast.error("Failed to start gold population"),
  });

  const taxMutation = useMutation({
    mutationFn: triggerTaxonomyGeneration,
    onSuccess: (data) => {
      setTaxRunId(data.run_id);
      toast.info("Taxonomy generation started...");
    },
    onError: () => toast.error("Failed to start taxonomy generation"),
  });

  const tableEnrichMutation = useMutation({
    mutationFn: () => triggerTableEnrichment(),
    onSuccess: (data) => {
      setTableEnrichRunId(data.run_id);
      toast.info("Table enrichment job submitted to Databricks...");
    },
    onError: () => toast.error("Failed to start table enrichment job"),
  });

  const { data: pipelineStatus, refetch: refetchPipelineStatus } = useQuery({
    queryKey: ["pipelineStatus"],
    queryFn: fetchPipelineStatus,
    refetchInterval: (enrichRunId || goldRunId || taxRunId || tableEnrichRunId) ? 10000 : false,
  });

  const [runAllPhase, setRunAllPhase] = useState<
    null | "gold" | "enrich" | "tables" | "taxonomy"
  >(null);

  useEffect(() => {
    if (!runAllPhase) return;
    if (runAllPhase === "gold" && goldStatus?.status === "TERMINATED") {
      setRunAllPhase("enrich");
      enrichMutation.mutate();
    }
    if (runAllPhase === "enrich" && enrichStatus?.status === "TERMINATED") {
      setRunAllPhase("tables");
      tableEnrichMutation.mutate();
    }
    if (runAllPhase === "tables") {
      const s = tableEnrichStatus?.status;
      if (s === "SUCCESS" || s === "TERMINATED") {
        setRunAllPhase("taxonomy");
        taxMutation.mutate();
      }
      if (s === "FAILED") {
        setRunAllPhase(null);
      }
    }
    if (runAllPhase === "taxonomy" && taxStatus?.status === "TERMINATED") {
      setRunAllPhase(null);
      refetchPipelineStatus();
      toast.success("All pipeline jobs complete!");
    }
    if (
      (runAllPhase === "gold" && goldStatus?.status === "FAILED") ||
      (runAllPhase === "enrich" && enrichStatus?.status === "FAILED") ||
      (runAllPhase === "taxonomy" && taxStatus?.status === "FAILED")
    ) {
      setRunAllPhase(null);
    }
  }, [runAllPhase, goldStatus?.status, enrichStatus?.status, tableEnrichStatus?.status, taxStatus?.status]);

  const handleRunAll = () => {
    setRunAllPhase("gold");
    goldMutation.mutate();
  };

  const isResearching = !!activeRunId;
  const isEnriching = !!enrichRunId;
  const isPopulatingGold = !!goldRunId;
  const isGeneratingTax = !!taxRunId;
  const isEnrichingTables = !!tableEnrichRunId;
  const isAnyPipelineRunning = isEnriching || isPopulatingGold || isGeneratingTax || isEnrichingTables;

  const hasProfile = !!profile?.company_name;
  const hasData = !!(departments?.length || useCases?.length);

  // ---------------------------------------------------------------------
  // Infrastructure setup status — drives the new prerequisite cards (Step
  // 0/1/2) at the top of this page and gates the content-level steps below.
  // ---------------------------------------------------------------------
  const {
    data: setupStatus,
    refetch: refetchSetup,
    isLoading: setupLoading,
  } = useQuery({
    queryKey: ["setupStatus"],
    queryFn: fetchSetupStatus,
    // While a long-running setup mutation is in flight we still re-poll so
    // the UI converges; otherwise leave it cached for 30s.
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const infraReady = setupStatus?.is_setup_ready ?? false;
  const dataReady = setupStatus?.is_data_ready ?? false;

  return (
    <div className="p-6 space-y-6 max-w-6xl">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Building2 className="h-6 w-6" />
          Company Setup
        </h1>
        <p className="text-muted-foreground mt-1">
          Configure your organization, load data sources, and run enrichment pipelines
        </p>
      </div>

      <SetupOverviewBanner status={setupStatus} loading={setupLoading} />

      {/* Step 1: Environment & Configuration */}
      <EnvironmentStepCard status={setupStatus} loading={setupLoading} />

      {/* Step 2: Catalog & Warehouse Access */}
      <AccessStepCard
        status={setupStatus}
        loading={setupLoading}
        onRecheck={() => refetchSetup()}
      />

      {/* Step 3: Database Bootstrap */}
      <BootstrapStepCard
        status={setupStatus}
        loading={setupLoading}
        onChanged={() => refetchSetup()}
      />

      {/* Step 4: Company Intelligence (gated until infra is ready) */}
      <StepCard
        step={4}
        title="Company Intelligence"
        description="Enter your company name to auto-generate departments, use cases with business value estimates, and required data entities"
        isComplete={hasProfile && hasData && !isResearching}
        locked={!infraReady}
        lockedReason="Complete steps 1–3 first so the database tables exist"
        headerRight={
          isResearching && activeRunId ? (
            <ProgressRing runId={activeRunId} />
          ) : undefined
        }
      >
        <div className="flex gap-3 flex-wrap items-center">
          <Input
            placeholder="Enter company name..."
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            className="max-w-md"
          />
          {(() => {
            const state = researchStatus?.state ?? "empty";
            const missing = researchStatus?.missing_steps ?? [];
            if (state === "partial" && !isResearching) {
              return (
                <>
                  <Button
                    onClick={() =>
                      researchMutation.mutate({ steps: missing, reset: false })
                    }
                    disabled={!companyName}
                    title={`Runs: ${missing.join(", ")}`}
                  >
                    <Rocket className="h-4 w-4 mr-2" />
                    Resume Research ({missing.length} step
                    {missing.length === 1 ? "" : "s"} left)
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => setResetConfirmOpen(true)}
                    disabled={!companyName}
                  >
                    Reset and re-run
                  </Button>
                </>
              );
            }
            if (state === "complete" && !isResearching) {
              return (
                <Button
                  variant="outline"
                  onClick={() => setResetConfirmOpen(true)}
                  disabled={!companyName}
                >
                  Reset and re-run
                </Button>
              );
            }
            return (
              <Button
                onClick={() => researchMutation.mutate({ reset: false })}
                disabled={!companyName || isResearching}
              >
                {isResearching ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Researching...
                  </>
                ) : (
                  <>
                    <Rocket className="h-4 w-4 mr-2" />
                    Research Company
                  </>
                )}
              </Button>
            );
          })()}
          {researchMutation.isError && (
            <span className="text-xs text-red-400 self-center">
              {(researchMutation.error as any)?.response?.status === 409
                ? "A research job is already running"
                : "Failed to start"}
            </span>
          )}
        </div>

        {resetConfirmOpen && (
          <div className="mt-3 rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
            <div className="font-medium text-red-300 mb-1">
              Wipe all company research data?
            </div>
            <div className="text-muted-foreground mb-3">
              This will permanently delete the existing profile, departments
              ({researchStatus?.counts?.departments ?? 0}), use cases
              ({researchStatus?.counts?.use_cases ?? 0}), entities
              ({researchStatus?.counts?.use_case_entities ?? 0}), and Sankey
              mappings ({researchStatus?.counts?.sankey_mappings ?? 0}) for
              "{companyName}" and run the full pipeline from scratch. This will
              burn LLM quota equivalent to a fresh run.
            </div>
            <div className="flex gap-2">
              <Button
                variant="destructive"
                size="sm"
                onClick={() => {
                  researchMutation.mutate({ reset: true });
                  setResetConfirmOpen(false);
                }}
              >
                Yes, wipe and re-run
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setResetConfirmOpen(false)}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {isResearching && activeRunId && (
          <ResearchProgressTree runId={activeRunId} companyName={companyName} />
        )}

        {!isResearching && hasProfile && <CompanyProfileSection profile={profile} />}

        {!isResearching && hasProfile && <BrandingSection profile={profile} />}

        {!isResearching && hasData && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
            <DepartmentsSection departments={departments} />
            <UseCasesSection useCases={useCases} entities={entities} />
          </div>
        )}
      </StepCard>

      {/* Step 5: Data Sources */}
      <StepCard
        step={5}
        title="Data Sources"
        description="Load your schema and table metadata — download the extractor utility to pull from multiple workspaces, or upload CSVs directly"
        isComplete={dataReady}
        locked={!infraReady}
        lockedReason="Database tables must exist before you can ingest data"
      >
        <DataSourceSection />
      </StepCard>

      {/* Step 6: Enrichment Pipeline */}
      <StepCard
        step={6}
        title="Enrichment Pipeline"
        description="Run the AI enrichment pipeline to classify, describe, and connect your data assets"
        isComplete={false}
        locked={!dataReady}
        lockedReason="Ingest schema + table CSVs first (Step 5)"
        headerRight={
          <Button
            onClick={handleRunAll}
            disabled={isAnyPipelineRunning || !!runAllPhase}
            size="sm"
          >
            {runAllPhase ? (
              <>
                <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                Running ({runAllPhase})...
              </>
            ) : (
              <>
                <Play className="h-4 w-4 mr-1" />
                Run All (Sequential)
              </>
            )}
          </Button>
        }
      >
        <p className="text-xs text-muted-foreground -mt-2 mb-3">
          Jobs run in order: Gold Layer → Enrich Schemas → Enrich Tables → Generate Taxonomy.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <PipelineJobCard
            step="1"
            title="Populate Gold Layer"
            description="Derive program, affiliate, environment, zone. Build source summaries."
            icon={<Database className="h-4 w-4" />}
            isRunning={isPopulatingGold}
            status={goldStatus?.status}
            onRun={() => goldMutation.mutate()}
            lastRun={pipelineStatus?.populate_gold?.last_run}
            subtitle={pipelineStatus?.populate_gold?.total_schemas
              ? `${pipelineStatus.populate_gold.total_schemas.toLocaleString()} schemas`
              : undefined}
            highlight={runAllPhase === "gold"}
          />
          <PipelineJobCard
            step="2"
            title="AI Enrich Schemas"
            description="Generate definitions, business names, source systems, and data domains."
            icon={<Sparkles className="h-4 w-4" />}
            isRunning={isEnriching}
            status={enrichStatus?.status}
            onRun={() => enrichMutation.mutate()}
            lastRun={pipelineStatus?.enrich_schemas?.last_run}
            subtitle={pipelineStatus?.enrich_schemas
              ? `${(pipelineStatus.enrich_schemas.enriched ?? 0).toLocaleString()} enriched · ${(pipelineStatus.enrich_schemas.remaining ?? 0).toLocaleString()} remaining`
              : undefined}
            highlight={runAllPhase === "enrich"}
          />
          <PipelineJobCard
            step="3"
            title="AI Enrich Tables"
            description="Generate per-table definitions, business names, and source systems via Databricks job."
            icon={<Layers className="h-4 w-4" />}
            isRunning={isEnrichingTables}
            status={tableEnrichStatus?.status}
            onRun={() => tableEnrichMutation.mutate()}
            lastRun={pipelineStatus?.enrich_tables?.last_run
              ? new Date(Number(pipelineStatus.enrich_tables.last_run)).toISOString()
              : undefined}
            subtitle={pipelineStatus?.enrich_tables?.status
              ? `Job: ${pipelineStatus.enrich_tables.status}`
              : undefined}
            highlight={runAllPhase === "tables"}
            externalUrl={pipelineStatus?.enrich_tables?.run_page_url || undefined}
          />
          <PipelineJobCard
            step="4"
            title="Generate Taxonomy"
            description="Classify schemas across 8 taxonomy dimensions using AI."
            icon={<Grid3X3 className="h-4 w-4" />}
            isRunning={isGeneratingTax}
            status={taxStatus?.status}
            onRun={() => taxMutation.mutate()}
            lastRun={pipelineStatus?.generate_taxonomy?.last_run}
            subtitle={pipelineStatus?.generate_taxonomy?.classified
              ? `${pipelineStatus.generate_taxonomy.classified.toLocaleString()} classified`
              : undefined}
            highlight={runAllPhase === "taxonomy"}
          />
        </div>
      </StepCard>

      {/* Danger Zone: Reset / Nuke */}
      <DangerZoneCard
        status={setupStatus}
        onChanged={() => {
          refetchSetup();
          queryClient.invalidateQueries({ queryKey: ["companyProfile"] });
          queryClient.invalidateQueries({ queryKey: ["departments"] });
          queryClient.invalidateQueries({ queryKey: ["useCases"] });
          queryClient.invalidateQueries({ queryKey: ["catalogStats"] });
          queryClient.invalidateQueries({ queryKey: ["pipelineStatus"] });
        }}
      />
    </div>
  );
}

function StepCard({
  step,
  title,
  description,
  isComplete,
  locked = false,
  lockedReason,
  headerRight,
  children,
}: {
  step: number;
  title: string;
  description: string;
  isComplete: boolean;
  /** When true, the body is dimmed and pointer events are disabled. */
  locked?: boolean;
  /** Short explanation shown in the header when `locked` is true. */
  lockedReason?: string;
  headerRight?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card className={locked ? "opacity-70" : undefined}>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="flex items-center gap-3">
              <span
                className={`flex items-center justify-center h-7 w-7 rounded-full text-sm font-bold ${
                  locked
                    ? "bg-muted text-muted-foreground"
                    : isComplete
                      ? "bg-emerald-500/20 text-emerald-400"
                      : "bg-primary/10 text-primary"
                }`}
              >
                {locked ? (
                  <Lock className="h-3.5 w-3.5" />
                ) : isComplete ? (
                  <CheckCircle2 className="h-4 w-4" />
                ) : (
                  step
                )}
              </span>
              {title}
            </CardTitle>
            <CardDescription className="mt-1">{description}</CardDescription>
            {locked && lockedReason && (
              <p className="text-[11px] text-amber-400 mt-1.5 flex items-center gap-1">
                <Lock className="h-3 w-3" />
                {lockedReason}
              </p>
            )}
          </div>
          {!locked && headerRight}
        </div>
      </CardHeader>
      <CardContent
        className={`space-y-4 ${locked ? "pointer-events-none select-none" : ""}`}
      >
        {children}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Setup Wizard cards (Steps 1–3 + Danger Zone)
//
// These are powered by GET /api/setup/status. Steps 1 + 2 are read-only
// reports; step 3 has buttons that call /setup/bootstrap-schemas and
// /setup/bootstrap-tables, after which the parent re-runs the status query
// to advance the wizard.
// ---------------------------------------------------------------------------

function CheckPill({
  ok,
  label,
}: {
  ok: boolean | undefined;
  label: string;
}) {
  if (ok === undefined) {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> {label}
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center gap-1 text-[11px] font-medium ${
        ok ? "text-emerald-400" : "text-red-400"
      }`}
    >
      {ok ? (
        <CheckCircle2 className="h-3 w-3" />
      ) : (
        <XCircle className="h-3 w-3" />
      )}
      {label}
    </span>
  );
}

function SetupOverviewBanner({
  status,
  loading,
}: {
  status: SetupStatus | undefined;
  loading: boolean;
}) {
  if (loading || !status) {
    return (
      <Card className="bg-muted/20">
        <CardContent className="py-3 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Checking setup status…
        </CardContent>
      </Card>
    );
  }
  const items = [
    { ok: status.config_check.ok, label: "Config" },
    { ok: status.warehouse_access.ok, label: "Warehouse" },
    { ok: status.catalog_access.ok, label: "Catalog" },
    { ok: status.llm_access.ok, label: "LLM" },
    { ok: status.schemas.ok, label: "Schemas" },
    { ok: status.tables.ok, label: "Tables" },
    { ok: status.data.ok, label: "Data" },
    { ok: status.company.present, label: "Company" },
  ];
  const greenCount = items.filter((i) => i.ok).length;
  const allGreen = greenCount === items.length;
  return (
    <Card
      className={`border ${
        allGreen
          ? "border-emerald-500/30 bg-emerald-500/5"
          : status.is_setup_ready
            ? "border-blue-500/30 bg-blue-500/5"
            : "border-amber-500/30 bg-amber-500/5"
      }`}
    >
      <CardContent className="py-3 flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          {allGreen ? (
            <CheckCircle2 className="h-5 w-5 text-emerald-400" />
          ) : status.is_setup_ready ? (
            <Sparkles className="h-5 w-5 text-blue-400" />
          ) : (
            <AlertTriangle className="h-5 w-5 text-amber-400" />
          )}
          <div>
            <p className="text-sm font-medium">
              {allGreen
                ? "Setup complete"
                : status.is_setup_ready
                  ? "Infrastructure ready — load data and run enrichment to finish"
                  : "Setup incomplete — finish the steps below"}
            </p>
            <p className="text-[11px] text-muted-foreground">
              {greenCount} of {items.length} checks passing
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 ml-auto">
          {items.map((i) => (
            <CheckPill key={i.label} ok={i.ok} label={i.label} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function EnvironmentStepCard({
  status,
  loading,
}: {
  status: SetupStatus | undefined;
  loading: boolean;
}) {
  const config = status?.config;
  const identity = status?.identity;
  const ok = status?.config_check.ok ?? false;
  return (
    <StepCard
      step={1}
      title="Environment & Identity"
      description="Verifies that the app is configured with a target catalog, schemas, warehouse, and that we know the service principal it runs as."
      isComplete={ok}
    >
      {loading || !status ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="border rounded-lg p-3 bg-muted/20">
            <p className="text-xs font-semibold flex items-center gap-1.5 mb-2">
              <Settings2 className="h-3.5 w-3.5" /> Configuration
            </p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px]">
              <ConfigRow label="Catalog" value={config?.catalog} required />
              <ConfigRow label="Raw schema" value={config?.raw_schema} />
              <ConfigRow label="Silver schema" value={config?.silver_schema} />
              <ConfigRow label="Gold schema" value={config?.gold_schema} />
              <ConfigRow
                label="Warehouse"
                value={config?.warehouse_id}
                required
              />
              <ConfigRow
                label="LLM endpoint"
                value={config?.llm_endpoint}
                muted={!config?.llm_endpoint}
              />
              <ConfigRow label="Host" value={config?.host} />
            </dl>
            <p className="text-[10px] text-muted-foreground mt-2">
              These come from <code>src/app/app.yml</code>. Re-run{" "}
              <code>scripts/deploy.py</code> to change them.
            </p>
          </div>
          <div className="border rounded-lg p-3 bg-muted/20">
            <p className="text-xs font-semibold flex items-center gap-1.5 mb-2">
              <ShieldCheck className="h-3.5 w-3.5" /> Service Principal
            </p>
            {identity?.type === "unknown" ? (
              <div className="text-[11px] text-amber-400">
                <p className="font-medium">Could not resolve identity</p>
                <p className="text-muted-foreground mt-1 break-words">
                  {identity.error ||
                    "The /current-user SCIM call failed. The app will still work but you won't see who to grant permissions to."}
                </p>
              </div>
            ) : (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px]">
                <ConfigRow
                  label="Type"
                  value={
                    identity?.type === "service_principal"
                      ? "Service Principal"
                      : "User"
                  }
                />
                <ConfigRow label="Identifier" value={identity?.user_name} />
                <ConfigRow label="Display" value={identity?.display_name} />
                {identity?.client_id && (
                  <ConfigRow label="Client ID" value={identity.client_id} />
                )}
              </dl>
            )}
            <p className="text-[10px] text-muted-foreground mt-2">
              When deployed, this will be the Databricks Apps managed service
              principal for <code>{config?.catalog ?? "your catalog"}</code>.
              Grant it the permissions shown in step 2 below.
            </p>
          </div>
        </div>
      )}
    </StepCard>
  );
}

function ConfigRow({
  label,
  value,
  required,
  muted,
}: {
  label: string;
  value: string | undefined;
  required?: boolean;
  muted?: boolean;
}) {
  const empty = !value;
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd
        className={`font-mono truncate ${
          empty
            ? required
              ? "text-red-400"
              : "text-muted-foreground/50"
            : muted
              ? "text-muted-foreground"
              : ""
        }`}
        title={value}
      >
        {empty ? (required ? "(missing)" : "—") : value}
      </dd>
    </>
  );
}

function AccessStepCard({
  status,
  loading,
  onRecheck,
}: {
  status: SetupStatus | undefined;
  loading: boolean;
  onRecheck: () => void;
}) {
  const wh = status?.warehouse_access;
  const cat = status?.catalog_access;
  const llm = status?.llm_access;
  const ok = !!(wh?.ok && cat?.ok && llm?.ok);
  const showGrants = !ok && !!status;
  return (
    <StepCard
      step={2}
      title="Catalog, Warehouse & LLM Access"
      description="Confirms the service principal can run SQL on the warehouse, read the configured catalog, and call ai_query() against the LLM endpoint. If any fail, run the GRANT statements below."
      isComplete={ok}
      headerRight={
        <Button variant="outline" size="sm" onClick={onRecheck} disabled={loading}>
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5 mr-1" />
          )}
          Re-check
        </Button>
      }
    >
      {!status ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <AccessProbeRow
              icon={<Server className="h-3.5 w-3.5" />}
              title="SQL Warehouse"
              detail={
                status.config.warehouse_id
                  ? `id: ${status.config.warehouse_id}`
                  : "(missing)"
              }
              probe={wh}
            />
            <AccessProbeRow
              icon={<Database className="h-3.5 w-3.5" />}
              title="Unity Catalog"
              detail={status.config.catalog || "(missing)"}
              probe={cat}
            />
            <AccessProbeRow
              icon={<Sparkles className="h-3.5 w-3.5" />}
              title="LLM Endpoint"
              detail={status.config.llm_endpoint || "(missing)"}
              probe={llm}
            />
          </div>
          {showGrants && <GrantsPanel grants={status.grants_sql} />}
        </>
      )}
    </StepCard>
  );
}

function AccessProbeRow({
  icon,
  title,
  detail,
  probe,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  probe: { ok: boolean; message: string } | undefined;
}) {
  return (
    <div
      className={`border rounded-lg p-3 ${
        probe?.ok
          ? "border-emerald-500/30 bg-emerald-500/5"
          : "border-red-500/30 bg-red-500/5"
      }`}
    >
      <div className="flex items-start gap-2">
        <span className="text-muted-foreground mt-0.5">{icon}</span>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium flex items-center gap-2">
            {title}
            {probe?.ok ? (
              <Badge
                variant="outline"
                className="text-[9px] text-emerald-400 border-emerald-500/30"
              >
                OK
              </Badge>
            ) : (
              <Badge
                variant="outline"
                className="text-[9px] text-red-400 border-red-500/30"
              >
                BLOCKED
              </Badge>
            )}
          </p>
          <p className="text-[11px] text-muted-foreground font-mono">{detail}</p>
          <p
            className={`text-[11px] mt-1 break-words ${
              probe?.ok ? "text-muted-foreground" : "text-red-400"
            }`}
          >
            {probe?.message ?? ""}
          </p>
        </div>
      </div>
    </div>
  );
}

function GrantsPanel({ grants }: { grants: string[] }) {
  const [copied, setCopied] = useState(false);
  const sql = grants.join("\n");
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(sql);
      setCopied(true);
      toast.success("Grants copied to clipboard");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Could not copy — select the text manually");
    }
  };
  return (
    <div className="border rounded-lg p-3 bg-muted/30 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-xs font-semibold flex items-center gap-1.5">
            <AlertCircle className="h-3.5 w-3.5 text-amber-400" />
            Suggested grants
          </p>
          <p className="text-[11px] text-muted-foreground mt-0.5">
            Run as a metastore admin or catalog owner in a Databricks SQL editor,
            then click "Re-check".
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={handleCopy}>
          {copied ? (
            <CheckCircle2 className="h-3.5 w-3.5 mr-1" />
          ) : (
            <Copy className="h-3.5 w-3.5 mr-1" />
          )}
          {copied ? "Copied" : "Copy SQL"}
        </Button>
      </div>
      <pre className="text-[11px] font-mono bg-background/60 border rounded p-2 overflow-x-auto whitespace-pre">
        {sql}
      </pre>
    </div>
  );
}

function BootstrapStepCard({
  status,
  loading,
  onChanged,
}: {
  status: SetupStatus | undefined;
  loading: boolean;
  onChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const accessOk =
    !!(status?.warehouse_access.ok && status?.catalog_access.ok);
  const schemasOk = status?.schemas.ok ?? false;
  const tablesOk = status?.tables.ok ?? false;

  const provisionMutation = useMutation({
    mutationFn: async () => {
      // Schemas first; if that succeeds, tables. The status response on the
      // next refetch tells us what actually got created.
      await bootstrapSchemas();
      await bootstrapTables();
    },
    onSuccess: () => {
      toast.success("Database objects provisioned");
      queryClient.invalidateQueries({ queryKey: ["setupStatus"] });
      onChanged();
    },
    onError: (err: any) => {
      const detail =
        err?.response?.data?.detail?.message ??
        err?.response?.data?.message ??
        err?.message ??
        "Provisioning failed";
      toast.error(detail);
      queryClient.invalidateQueries({ queryKey: ["setupStatus"] });
      onChanged();
    },
  });

  const isProvisioning = provisionMutation.isPending;

  return (
    <StepCard
      step={3}
      title="Database Bootstrap"
      description="Creates the bhe_raw / bhe_silver / bhe_gold schemas and all required Delta tables (idempotent — safe to re-run)."
      isComplete={schemasOk && tablesOk}
      locked={!accessOk}
      lockedReason="Grant catalog access first (step 2)"
      headerRight={
        <Button
          size="sm"
          onClick={() => provisionMutation.mutate()}
          disabled={isProvisioning || !accessOk || (schemasOk && tablesOk)}
        >
          {isProvisioning ? (
            <>
              <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
              Provisioning…
            </>
          ) : schemasOk && tablesOk ? (
            <>
              <CheckCircle2 className="h-3.5 w-3.5 mr-1" />
              Already provisioned
            </>
          ) : (
            <>
              <HardDriveDownload className="h-3.5 w-3.5 mr-1" />
              Create schemas + tables
            </>
          )}
        </Button>
      }
    >
      {!status ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {(Object.entries(status.schemas.state) as [string, boolean][]).map(
            ([name, exists]) => (
              <div
                key={name}
                className={`border rounded-lg p-3 ${
                  exists
                    ? "border-emerald-500/30 bg-emerald-500/5"
                    : "border-muted bg-muted/20"
                }`}
              >
                <p className="text-xs font-semibold flex items-center gap-1.5">
                  <Layers className="h-3 w-3" />
                  {name}
                  {exists && (
                    <CheckCircle2 className="h-3 w-3 text-emerald-400 ml-auto" />
                  )}
                </p>
                <BootstrapTablesList status={status} schemaName={name} />
              </div>
            ),
          )}
        </div>
      )}
      {provisionMutation.isError && (
        <p className="text-[11px] text-red-400 break-words">
          {String(
            (provisionMutation.error as any)?.response?.data?.detail?.message ??
              (provisionMutation.error as any)?.message ??
              "",
          )}
        </p>
      )}
    </StepCard>
  );
}

function BootstrapTablesList({
  status,
  schemaName,
}: {
  status: SetupStatus;
  schemaName: string;
}) {
  const isSilver = schemaName === status.config.silver_schema;
  const isGold = schemaName === status.config.gold_schema;
  const isRaw = schemaName === status.config.raw_schema;
  if (isRaw) {
    return (
      <p className="text-[11px] text-muted-foreground mt-1.5">
        Holds Volume uploads — no tables expected here.
      </p>
    );
  }
  const present = isSilver
    ? status.tables.silver_present
    : status.tables.gold_present;
  const missing = isSilver
    ? status.tables.silver_missing
    : isGold
      ? status.tables.gold_missing
      : [];
  return (
    <div className="text-[11px] mt-1.5 space-y-0.5">
      <p>
        <span className="text-emerald-400">{present.length}</span> present
        {missing.length > 0 && (
          <span className="text-amber-400 ml-2">
            {missing.length} missing
          </span>
        )}
      </p>
      {missing.length > 0 && (
        <p
          className="text-muted-foreground truncate"
          title={missing.join(", ")}
        >
          missing: {missing.join(", ")}
        </p>
      )}
    </div>
  );
}

function DangerZoneCard({
  status,
  onChanged,
}: {
  status: SetupStatus | undefined;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [typedConfirm, setTypedConfirm] = useState("");
  const queryClient = useQueryClient();
  const catalog = status?.config.catalog ?? "";
  const canFire = typedConfirm === catalog;

  const nukeMutation = useMutation({
    mutationFn: () => nukeSetup(catalog),
    onSuccess: (data) => {
      toast.success(
        `Dropped ${data?.dropped?.length ?? 0} schemas. Run "Create schemas + tables" to re-provision.`,
      );
      setOpen(false);
      setTypedConfirm("");
      queryClient.invalidateQueries({ queryKey: ["setupStatus"] });
      onChanged();
    },
    onError: (err: any) => {
      const detail =
        err?.response?.data?.detail ??
        err?.response?.data?.message ??
        err?.message ??
        "Nuke failed";
      toast.error(typeof detail === "string" ? detail : JSON.stringify(detail));
    },
  });

  return (
    <Card className="border-red-500/30">
      <CardHeader className="cursor-pointer" onClick={() => setOpen(!open)}>
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="flex items-center gap-2 text-red-400">
              <Skull className="h-4 w-4" />
              Danger zone
            </CardTitle>
            <CardDescription className="mt-1">
              Drop every BHE Catalog schema in <code>{catalog}</code> and start
              from scratch. All ingested metadata, AI enrichments, knowledge
              articles, chat history, and uploaded Volume files will be gone.
            </CardDescription>
          </div>
          <Button variant="ghost" size="sm">
            {open ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </Button>
        </div>
      </CardHeader>
      {open && (
        <CardContent className="space-y-3">
          <div className="border border-red-500/40 bg-red-500/10 rounded-md p-3 space-y-2">
            <p className="text-xs font-medium text-red-300">
              This drops <code>{status?.config.raw_schema}</code>,{" "}
              <code>{status?.config.silver_schema}</code>, and{" "}
              <code>{status?.config.gold_schema}</code> with{" "}
              <code>CASCADE</code>. The action cannot be undone.
            </p>
            <p className="text-[11px] text-muted-foreground">
              Type the catalog name <code>{catalog}</code> below to confirm:
            </p>
            <div className="flex gap-2">
              <Input
                value={typedConfirm}
                onChange={(e) => setTypedConfirm(e.target.value)}
                placeholder={catalog}
                className="font-mono text-sm"
                disabled={nukeMutation.isPending}
              />
              <Button
                variant="destructive"
                disabled={!canFire || nukeMutation.isPending}
                onClick={() => nukeMutation.mutate()}
              >
                {nukeMutation.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                    Nuking…
                  </>
                ) : (
                  <>
                    <Trash2 className="h-4 w-4 mr-1" />
                    Drop everything
                  </>
                )}
              </Button>
            </div>
          </div>
        </CardContent>
      )}
    </Card>
  );
}

function CompanyProfileSection({ profile }: { profile: any }) {
  if (!profile?.company_name) return null;
  return (
    <div className="mt-4 border rounded-lg p-4 bg-muted/30">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="font-semibold">{profile.company_name}</h3>
          <p className="text-sm text-muted-foreground">
            {profile.industry}
            {profile.sub_industry ? ` — ${profile.sub_industry}` : ""}
            {profile.headquarters ? ` | ${profile.headquarters}` : ""}
          </p>
        </div>
        <Badge variant="outline" className="text-xs">
          <CheckCircle2 className="h-3 w-3 mr-1" />
          Profiled
        </Badge>
      </div>
      <p className="text-sm mt-2">{profile.description}</p>
      {profile.key_business_units?.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {profile.key_business_units.map((u: string) => (
            <Badge key={u} variant="secondary" className="text-xs">
              {u}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

function DepartmentsSection({ departments }: { departments: any[] | undefined }) {
  if (!departments?.length) return null;
  return (
    <div className="border rounded-lg p-4">
      <h3 className="text-sm font-medium mb-2 flex items-center gap-2">
        <Building2 className="h-4 w-4" />
        Departments ({departments.length})
      </h3>
      <div className="space-y-1.5 max-h-[300px] overflow-y-auto">
        {departments.map((d: any) => (
          <div key={d.id} className="flex items-center justify-between py-1 px-2 rounded hover:bg-muted/40">
            <span className="text-xs font-medium">{d.department_name}</span>
            {d.is_user_edited && (
              <Badge variant="secondary" className="text-[9px]">edited</Badge>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function UseCasesSection({
  useCases,
  entities,
}: {
  useCases: any[] | undefined;
  entities: any[] | undefined;
}) {
  const [expandedDept, setExpandedDept] = useState<string | null>(null);

  if (!useCases?.length) return null;

  const byDept: Record<string, any[]> = {};
  for (const uc of useCases) {
    const dept = uc.department || "Unknown";
    if (!byDept[dept]) byDept[dept] = [];
    byDept[dept].push(uc);
  }

  const totalValue = useCases.reduce(
    (sum: number, uc: any) => sum + (uc.estimated_value_usd || 0),
    0,
  );

  const entityCount = entities?.length || 0;

  return (
    <div className="border rounded-lg p-4">
      <h3 className="text-sm font-medium mb-1 flex items-center gap-2">
        <Target className="h-4 w-4" />
        Use Cases ({useCases.length})
      </h3>
      <div className="flex gap-3 mb-2">
        {totalValue > 0 && (
          <span className="text-xs text-emerald-400 flex items-center gap-1">
            <DollarSign className="h-3 w-3" />
            {formatValue(totalValue)} total est. value
          </span>
        )}
        {entityCount > 0 && (
          <span className="text-xs text-blue-400 flex items-center gap-1">
            <Boxes className="h-3 w-3" />
            {entityCount} required entities
          </span>
        )}
      </div>
      <div className="space-y-1 max-h-[300px] overflow-y-auto">
        {Object.entries(byDept)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([dept, ucs]) => {
            const deptValue = ucs.reduce(
              (s: number, u: any) => s + (u.estimated_value_usd || 0),
              0,
            );
            const isOpen = expandedDept === dept;
            return (
              <div key={dept}>
                <button
                  onClick={() => setExpandedDept(isOpen ? null : dept)}
                  className="w-full flex items-center gap-1.5 py-1 px-2 rounded hover:bg-muted/40 text-left"
                >
                  {isOpen ? (
                    <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
                  )}
                  <span className="text-xs font-medium truncate">{dept}</span>
                  <span className="ml-auto flex items-center gap-2 shrink-0">
                    <Badge variant="outline" className="text-[9px]">
                      {ucs.length}
                    </Badge>
                    {deptValue > 0 && (
                      <span className="text-[10px] text-emerald-400">
                        {formatValue(deptValue)}
                      </span>
                    )}
                  </span>
                </button>
                {isOpen && (
                  <div className="ml-5 space-y-1 mt-1">
                    {ucs.map((uc: any) => (
                      <div
                        key={uc.id}
                        className="border rounded p-2 text-xs space-y-1"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <span className="font-medium">{uc.use_case_name}</span>
                          <div className="flex gap-1 shrink-0">
                            <Badge
                              variant={
                                uc.priority === "High"
                                  ? "destructive"
                                  : uc.priority === "Medium"
                                    ? "default"
                                    : "secondary"
                              }
                              className="text-[9px]"
                            >
                              {uc.priority}
                            </Badge>
                          </div>
                        </div>
                        <p className="text-muted-foreground">{uc.description}</p>
                        {uc.estimated_value_usd > 0 && (
                          <div className="flex items-center gap-2 text-emerald-400">
                            <DollarSign className="h-3 w-3" />
                            <span className="font-semibold">
                              {formatValue(uc.estimated_value_usd)}
                            </span>
                            {uc.value_rationale && (
                              <span className="text-muted-foreground truncate">
                                — {uc.value_rationale.slice(0, 80)}...
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
      </div>
    </div>
  );
}

function formatValue(usd: number): string {
  if (usd >= 1_000_000_000) return `$${(usd / 1_000_000_000).toFixed(1)}B`;
  if (usd >= 1_000_000) return `$${(usd / 1_000_000).toFixed(1)}M`;
  if (usd >= 1_000) return `$${(usd / 1_000).toFixed(0)}K`;
  return `$${usd.toFixed(0)}`;
}

function BrandingSection({ profile }: { profile: any }) {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Local edit state — initialize from the profile, fall back to a sensible
  // default so the field is never empty for a brand-new tenant.
  const [catalogName, setCatalogName] = useState<string>(
    profile?.catalog_name ||
      (profile?.company_name ? `${profile.company_name} Data Catalog` : ""),
  );
  const [logoUrl, setLogoUrl] = useState<string>(profile?.logo_url || "");
  const [imgError, setImgError] = useState(false);

  // Re-sync if the profile changes underneath us (e.g. research re-runs).
  useEffect(() => {
    setCatalogName(
      profile?.catalog_name ||
        (profile?.company_name ? `${profile.company_name} Data Catalog` : ""),
    );
    setLogoUrl(profile?.logo_url || "");
    setImgError(false);
  }, [profile?.catalog_name, profile?.logo_url, profile?.company_name]);

  const { data: branding, refetch: refetchBranding } = useQuery({
    queryKey: ["branding"],
    queryFn: fetchBranding,
  });

  const invalidateBranding = () => {
    queryClient.invalidateQueries({ queryKey: ["branding"] });
    queryClient.invalidateQueries({ queryKey: ["companyProfile"] });
    refetchBranding();
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      updateBranding({ catalog_name: catalogName, logo_url: logoUrl }),
    onSuccess: () => {
      toast.success("Branding saved");
      invalidateBranding();
    },
    onError: () => toast.error("Failed to save branding"),
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadBrandingLogo(file),
    onSuccess: (data) => {
      toast.success("Logo uploaded");
      setLogoUrl(data?.logo_url || "/api/branding/logo");
      setImgError(false);
      invalidateBranding();
    },
    onError: () => toast.error("Logo upload failed"),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteBrandingLogo(),
    onSuccess: () => {
      toast.success("Logo removed");
      setLogoUrl("");
      setImgError(false);
      invalidateBranding();
    },
    onError: () => toast.error("Could not remove logo"),
  });

  const handleFile = (file: File | null | undefined) => {
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      toast.error("Logo must be under 5 MB");
      return;
    }
    uploadMutation.mutate(file);
  };

  // Live preview source: prefer the in-flight URL the user is typing, but
  // when they have an uploaded logo the backend returns "/api/branding/logo"
  // and we should respect that (cache-bust on each branding fetch).
  const previewSrc = (() => {
    const candidate = logoUrl || branding?.logo_url || "";
    if (!candidate) return "";
    if (candidate.startsWith("/api/branding/logo")) {
      // bust the 60s cache when the user re-uploads
      return `${candidate}?t=${branding?.has_uploaded_logo ? Date.now() : 0}`;
    }
    return candidate;
  })();

  const isDirty =
    catalogName !== (profile?.catalog_name || "") ||
    logoUrl !== (profile?.logo_url || "");

  return (
    <div className="mt-4 border rounded-lg p-4 bg-muted/20">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-sm font-semibold flex items-center gap-2">
            <ImageIcon className="h-4 w-4" />
            Catalog Branding
          </h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            Override the AI-suggested catalog name and logo shown in the top-left banner.
            {profile?.branding_user_edited ? (
              <span className="ml-2 text-emerald-400">Manually set</span>
            ) : (
              <span className="ml-2 text-blue-400">Auto-suggested by AI</span>
            )}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-4 items-start">
        <div className="space-y-3">
          <div>
            <label className="text-xs font-medium text-muted-foreground mb-1 block">
              Catalog Name
            </label>
            <Input
              value={catalogName}
              onChange={(e) => setCatalogName(e.target.value)}
              placeholder="e.g. Acme Data Catalog"
              maxLength={120}
            />
          </div>

          <div>
            <label className="text-xs font-medium text-muted-foreground mb-1 block">
              Logo URL
            </label>
            <div className="flex gap-2">
              <Input
                value={logoUrl}
                onChange={(e) => {
                  setLogoUrl(e.target.value);
                  setImgError(false);
                }}
                placeholder="https://logo.clearbit.com/example.com or /api/branding/logo"
                className="font-mono text-xs"
              />
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/svg+xml,image/gif,image/webp"
                className="hidden"
                onChange={(e) => handleFile(e.target.files?.[0])}
              />
              <Button
                variant="outline"
                size="sm"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploadMutation.isPending}
                title="Upload an image to the Volume; it will be served from /api/branding/logo"
              >
                {uploadMutation.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="h-4 w-4" />
                )}
                <span className="ml-1 hidden sm:inline">Upload</span>
              </Button>
              {branding?.has_uploaded_logo && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => deleteMutation.mutate()}
                  disabled={deleteMutation.isPending}
                  title="Remove uploaded logo"
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              )}
            </div>
            <p className="text-[11px] text-muted-foreground/70 mt-1">
              Paste a public URL, or upload PNG/JPG/SVG (≤ 5 MB). Uploaded
              logos are stored in the raw Volume and served via the backend.
            </p>
          </div>

          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => saveMutation.mutate()}
              disabled={!isDirty || saveMutation.isPending}
            >
              {saveMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-1 animate-spin" />
              ) : (
                <Save className="h-4 w-4 mr-1" />
              )}
              Save Branding
            </Button>
            {profile?.branding_user_edited && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setCatalogName(
                    profile?.company_name
                      ? `${profile.company_name} Data Catalog`
                      : "",
                  );
                  setLogoUrl("");
                }}
                title="Reset to AI-suggested defaults (re-run Research Company afterwards to repopulate)"
              >
                <RefreshCw className="h-3.5 w-3.5 mr-1" />
                Reset
              </Button>
            )}
          </div>
        </div>

        <div className="border rounded-md p-3 bg-background min-w-[180px]">
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground mb-2">
            Preview
          </p>
          <div className="flex items-center gap-2">
            {previewSrc && !imgError ? (
              <img
                src={previewSrc}
                alt="logo preview"
                className="h-8 w-8 object-contain rounded-sm border"
                onError={() => setImgError(true)}
              />
            ) : (
              <div className="h-8 w-8 rounded-sm border border-dashed border-muted-foreground/30 flex items-center justify-center">
                <ImageIcon className="h-4 w-4 text-muted-foreground/50" />
              </div>
            )}
            <span className="font-semibold text-sm truncate max-w-[160px]">
              {catalogName || "Catalog Name"}
            </span>
          </div>
          {imgError && previewSrc && (
            <p className="text-[10px] text-amber-400 mt-2">
              Could not load image from that URL
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function DataSourceSection() {
  const queryClient = useQueryClient();
  const schemasInputRef = useRef<HTMLInputElement>(null);
  const tablesInputRef = useRef<HTMLInputElement>(null);
  const [schemasFile, setSchemasFile] = useState<File | null>(null);
  const [tablesFile, setTablesFile] = useState<File | null>(null);

  const { data: uploadedFiles, refetch: refetchFiles } = useQuery({
    queryKey: ["uploadedFiles"],
    queryFn: listUploadedFiles,
  });

  const uploadMutation = useMutation({
    mutationFn: async ({ file, type }: { file: File; type: string }) =>
      uploadFile(file, type),
    onSuccess: (_data, vars) => {
      toast.success(`${vars.file.name} uploaded`);
      refetchFiles();
    },
    onError: () => toast.error("Upload failed"),
  });

  const ingestSchemasMutation = useMutation({
    mutationFn: (filename?: string) => ingestSchemas(filename),
    onSuccess: (data) => {
      toast.success(`Schemas ingested: ${data.rows} rows`);
      queryClient.invalidateQueries({ queryKey: ["catalogStats"] });
      queryClient.invalidateQueries({ queryKey: ["schemas"] });
    },
    onError: () => toast.error("Schema ingest failed"),
  });

  const ingestTablesMutation = useMutation({
    mutationFn: (filename?: string) => ingestTables(filename),
    onSuccess: (data) => {
      toast.success(`Tables ingested: ${data.rows} rows`);
      queryClient.invalidateQueries({ queryKey: ["catalogStats"] });
    },
    onError: () => toast.error("Table ingest failed"),
  });

  const handleUploadAndIngest = async (
    file: File,
    type: "schemas" | "tables",
  ) => {
    await uploadMutation.mutateAsync({ file, type });
    if (type === "schemas") {
      ingestSchemasMutation.mutate(file.name);
    } else {
      ingestTablesMutation.mutate(file.name);
    }
  };

  return (
    <div className="space-y-4">
      {/* Schema Extractor Download */}
      <div className="border rounded-lg p-4 bg-blue-500/5 border-blue-500/20">
        <div className="flex items-start justify-between">
          <div>
            <h4 className="text-sm font-medium flex items-center gap-2">
              <Download className="h-4 w-4 text-blue-400" />
              Schema Extractor Utility
            </h4>
            <p className="text-xs text-muted-foreground mt-1 max-w-xl">
              Download this Python utility to extract schema and table metadata
              from multiple Databricks workspaces. Useful when workspaces cannot
              see all catalogs across environments. Run it locally, then upload
              the generated CSV files below.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={downloadSchemaExtractor}>
            <Download className="h-4 w-4 mr-1" />
            Download ZIP
          </Button>
        </div>
        <div className="mt-2 text-[11px] text-muted-foreground/70 flex items-center gap-4">
          <span>1. Unzip and edit <code>workspaces.txt</code></span>
          <ArrowRight className="h-3 w-3" />
          <span>2. Run <code>python extract_schemas.py</code></span>
          <ArrowRight className="h-3 w-3" />
          <span>3. Upload the <code>all_schemas.csv</code> and <code>all_tables.csv</code> below</span>
        </div>
      </div>

      {/* Upload Area */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <UploadCard
          label="Schemas CSV"
          icon={<Database className="h-4 w-4" />}
          file={schemasFile}
          inputRef={schemasInputRef}
          onFileChange={setSchemasFile}
          onUpload={() =>
            schemasFile && handleUploadAndIngest(schemasFile, "schemas")
          }
          isUploading={uploadMutation.isPending || ingestSchemasMutation.isPending}
          result={
            ingestSchemasMutation.isSuccess
              ? `${ingestSchemasMutation.data?.rows} schemas loaded`
              : undefined
          }
        />
        <UploadCard
          label="Tables CSV"
          icon={<Database className="h-4 w-4" />}
          file={tablesFile}
          inputRef={tablesInputRef}
          onFileChange={setTablesFile}
          onUpload={() =>
            tablesFile && handleUploadAndIngest(tablesFile, "tables")
          }
          isUploading={uploadMutation.isPending || ingestTablesMutation.isPending}
          result={
            ingestTablesMutation.isSuccess
              ? `${ingestTablesMutation.data?.rows} tables loaded`
              : undefined
          }
        />
      </div>

      {uploadedFiles?.files?.length > 0 && (
        <div>
          <p className="text-xs text-muted-foreground mb-1">
            Files in Volume ({uploadedFiles.files.length}):
          </p>
          <div className="flex flex-wrap gap-1">
            {uploadedFiles.files.map((f: { name: string; size: number }) => (
              <Badge key={f.name} variant="outline" className="text-xs">
                {f.name} ({(f.size / 1024 / 1024).toFixed(1)}MB)
              </Badge>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function UploadCard({
  label,
  icon,
  file,
  inputRef,
  onFileChange,
  onUpload,
  isUploading,
  result,
}: {
  label: string;
  icon: React.ReactNode;
  file: File | null;
  inputRef: React.RefObject<HTMLInputElement | null>;
  onFileChange: (f: File) => void;
  onUpload: () => void;
  isUploading: boolean;
  result?: string;
}) {
  return (
    <div className="border rounded-lg p-4 space-y-3">
      <p className="text-sm font-medium flex items-center gap-2">
        {icon} {label}
      </p>
      <input
        ref={inputRef}
        type="file"
        accept=".csv"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFileChange(f);
        }}
      />
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => inputRef.current?.click()}
        >
          <FileUp className="h-4 w-4 mr-1" />
          {file ? file.name : "Choose file..."}
        </Button>
        <Button
          size="sm"
          disabled={!file || isUploading}
          onClick={onUpload}
        >
          {isUploading ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            "Upload & Ingest"
          )}
        </Button>
      </div>
      {result && (
        <p className="text-xs text-green-500 flex items-center gap-1">
          <CheckCircle2 className="h-3 w-3" /> {result}
        </p>
      )}
    </div>
  );
}

function formatTimeAgo(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  const now = Date.now();
  const diff = now - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString();
}

function PipelineJobCard({
  step,
  title,
  description,
  icon,
  isRunning,
  status,
  onRun,
  lastRun,
  subtitle,
  highlight,
  externalUrl,
}: {
  step: string;
  title: string;
  description: string;
  icon: React.ReactNode;
  isRunning: boolean;
  status?: string;
  onRun: () => void;
  lastRun?: string | null;
  subtitle?: string;
  highlight?: boolean;
  externalUrl?: string;
}) {
  const isDone = (status === "TERMINATED" || status === "SUCCESS") && !isRunning;
  const isFailed = status === "FAILED" && !isRunning;
  const timeAgo = formatTimeAgo(lastRun);

  return (
    <div
      className={`border rounded-lg p-4 space-y-2 transition-colors ${
        highlight ? "border-blue-500/50 bg-blue-500/5" : ""
      }`}
    >
      <div className="flex items-start justify-between">
        <p className="text-sm font-medium">
          {step}. {title}
        </p>
        {isDone && (
          <Badge variant="outline" className="text-[10px] text-green-500 border-green-500/30">
            <CheckCircle2 className="h-3 w-3 mr-0.5" /> Done
          </Badge>
        )}
        {isFailed && (
          <Badge variant="outline" className="text-[10px] text-red-500 border-red-500/30">
            <XCircle className="h-3 w-3 mr-0.5" /> Failed
          </Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{description}</p>
      {subtitle && (
        <p className="text-xs text-muted-foreground/80 font-mono">{subtitle}</p>
      )}
      {timeAgo && (
        <p className="text-[11px] text-muted-foreground/60 flex items-center gap-1">
          <Clock className="h-3 w-3" /> Last run: {timeAgo}
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button
          onClick={onRun}
          disabled={isRunning}
          variant="outline"
          size="sm"
        >
          {isRunning ? (
            <>
              <Loader2 className="h-4 w-4 mr-1 animate-spin" />
              {status || "Running"}...
            </>
          ) : (
            <>
              {icon}
              <span className="ml-1">{title}</span>
            </>
          )}
        </Button>
        {externalUrl && (
          <a
            href={externalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[11px] text-blue-400 hover:underline flex items-center gap-0.5"
          >
            <ExternalLink className="h-3 w-3" /> View Run
          </a>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Progress Ring (SVG circular indicator)
// ---------------------------------------------------------------------------

function ProgressRing({ runId }: { runId: string }) {
  const { data } = useQuery({
    queryKey: ["jobProgress", runId],
    queryFn: () => fetchJobProgress(runId),
    refetchInterval: 3000,
  });

  const pct = data?.pct_complete ?? 0;
  const r = 22;
  const circumference = 2 * Math.PI * r;
  const offset = circumference - (pct / 100) * circumference;

  return (
    <div className="relative flex items-center justify-center w-14 h-14 shrink-0">
      <svg width="56" height="56" className="-rotate-90">
        <circle
          cx="28"
          cy="28"
          r={r}
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          className="opacity-10"
        />
        <circle
          cx="28"
          cy="28"
          r={r}
          fill="none"
          stroke="hsl(142, 70%, 45%)"
          strokeWidth="3"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="transition-all duration-700 ease-out"
        />
      </svg>
      <span className="absolute text-xs font-bold">{pct}%</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Research Progress Tree
// ---------------------------------------------------------------------------

interface ProgressStep {
  step: string;
  step_index: number;
  total_steps: number;
  item_name: string;
  parent_item: string;
  detail: string;
}

function ResearchProgressTree({
  runId,
  companyName,
}: {
  runId: string;
  companyName: string;
}) {
  const { data } = useQuery({
    queryKey: ["jobProgress", runId],
    queryFn: () => fetchJobProgress(runId),
    refetchInterval: 3000,
  });

  const steps: ProgressStep[] = data?.steps ?? [];

  const profileDone = steps.some((s) => s.step === "profile");
  const deptsDone = steps.some((s) => s.step === "departments");
  const deptItems = steps.filter((s) => s.step === "dept_item").map((s) => s.item_name);
  const usecaseSteps = steps.filter((s) => s.step.startsWith("usecase:"));
  const completedDepts = new Set(usecaseSteps.map((s) => s.item_name));
  const entitiesDone = steps.some((s) => s.step === "entities");
  const sankeyDone = steps.some((s) => s.step === "sankey");

  const totalSteps = data?.total_steps ?? 0;
  const currentStepIdx = steps.length > 0 ? Math.max(...steps.map((s) => s.step_index)) : 0;

  const statusLabel = sankeyDone
    ? "Complete"
    : entitiesDone
      ? "Generating mappings..."
      : usecaseSteps.length > 0
        ? `Use cases: ${completedDepts.size}/${deptItems.length} departments`
        : deptsDone
          ? "Discovering use cases..."
          : profileDone
            ? "Discovering departments..."
            : "Researching company...";

  const runPageUrl = data?.run_page_url;

  return (
    <div className="mt-4 border rounded-lg p-4 bg-muted/20 space-y-3 animate-in fade-in duration-500">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground font-medium">{statusLabel}</p>
        <div className="flex items-center gap-3">
          {runPageUrl && (
            <a
              href={runPageUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-blue-400 hover:underline flex items-center gap-1"
            >
              <ExternalLink className="h-3 w-3" />
              View Databricks run
            </a>
          )}
          {!sankeyDone && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-400" />
          )}
        </div>
      </div>

      {/* Tree */}
      <div className="pl-1 space-y-0">
        {/* Root: Company */}
        <TreeNode
          label={companyName}
          status={profileDone ? "done" : "active"}
          detail={steps.find((s) => s.step === "profile")?.detail}
          level={0}
        />

        {/* Department children */}
        {deptItems.map((dept, i) => {
          const ucStep = usecaseSteps.find((s) => s.item_name === dept);
          const isDeptDone = completedDepts.has(dept);
          const isActive =
            !isDeptDone &&
            deptsDone &&
            completedDepts.size === i &&
            !entitiesDone;

          return (
            <div key={dept}>
              <TreeNode
                label={dept}
                status={isDeptDone ? "done" : isActive ? "active" : "pending"}
                detail={ucStep?.detail}
                level={1}
              />
            </div>
          );
        })}

        {/* Entities node */}
        {usecaseSteps.length > 0 && usecaseSteps.length >= deptItems.length && (
          <TreeNode
            label="Data Entities"
            status={entitiesDone ? "done" : sankeyDone ? "done" : "active"}
            detail={steps.find((s) => s.step === "entities")?.detail}
            level={1}
          />
        )}

        {/* Sankey node */}
        {entitiesDone && (
          <TreeNode
            label="Sankey Mappings"
            status={sankeyDone ? "done" : "active"}
            detail={steps.find((s) => s.step === "sankey")?.detail}
            level={1}
          />
        )}
      </div>
    </div>
  );
}

function TreeNode({
  label,
  status,
  detail,
  level,
}: {
  label: string;
  status: "done" | "active" | "pending";
  detail?: string;
  level: number;
}) {
  return (
    <div
      className="flex items-center gap-2 py-0.5 animate-in fade-in slide-in-from-left-2 duration-300"
      style={{ paddingLeft: `${level * 20}px` }}
    >
      {level > 0 && (
        <span className="text-muted-foreground/30 text-xs select-none">
          {"├"}
        </span>
      )}
      <span
        className={`inline-block w-2 h-2 rounded-full shrink-0 ${
          status === "done"
            ? "bg-emerald-400"
            : status === "active"
              ? "bg-blue-400 animate-pulse"
              : "bg-muted-foreground/25"
        }`}
      />
      <span
        className={`text-xs ${
          status === "done"
            ? "text-foreground"
            : status === "active"
              ? "text-blue-400 font-medium"
              : "text-muted-foreground/50"
        }`}
      >
        {label}
      </span>
      {detail && status === "done" && (
        <span className="text-[10px] text-muted-foreground/60 ml-1">
          {detail}
        </span>
      )}
      {status === "active" && (
        <Loader2 className="h-2.5 w-2.5 animate-spin text-blue-400 ml-1" />
      )}
    </div>
  );
}
