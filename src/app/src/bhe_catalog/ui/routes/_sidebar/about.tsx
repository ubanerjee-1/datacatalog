import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Info,
  Database,
  GitBranch,
  Layers,
  Boxes,
  TrendingUp,
  Building2,
  Sparkles,
  ArrowRight,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/about")({
  component: AboutPage,
});

const TABS = [
  { key: "overview", label: "Overview", icon: <Info size={14} /> },
  { key: "data-model", label: "Data Model", icon: <Database size={14} /> },
  { key: "pipelines", label: "Pipelines", icon: <GitBranch size={14} /> },
] as const;
type TabKey = (typeof TABS)[number]["key"];

function AboutPage() {
  const [tab, setTab] = useState<TabKey>("overview");
  return (
    <div className="flex flex-col min-h-full">
      <div className="border-b bg-background sticky top-0 z-30">
        <div className="px-6 pt-5 pb-0 flex flex-col gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              About this Application
            </h1>
            <p className="text-sm text-muted-foreground mt-1 max-w-3xl">
              How the catalog is structured, what tables back each page, and
              the jobs that keep the data fresh.
            </p>
          </div>
          <div className="flex items-center gap-1 -mb-px">
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
      </div>
      <div className="px-6 py-5 flex-1">
        {tab === "overview" && <OverviewTab />}
        {tab === "data-model" && <DataModelTab />}
        {tab === "pipelines" && <PipelinesTab />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Overview tab
// ---------------------------------------------------------------------------

function OverviewTab() {
  return (
    <div className="space-y-5 max-w-5xl">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">What this app answers</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          <Question
            num={1}
            text="Do we have data from <SourceSystem> in our Lake? If so, where? Which tables?"
            backedBy="Source Systems page"
            tables={[
              "bhe_gold.source_system_canonical",
              "bhe_gold.source_system_aliases",
              "bhe_silver.silver_tables",
            ]}
          />
          <Question
            num={2}
            text="Given the data we have today, which use cases can we deliver, for which affiliates, and what's the dollar impact?"
            backedBy="Value & Readiness page"
            tables={[
              "bhe_silver.use_cases",
              "bhe_gold.use_case_source_requirements",
              "bhe_gold.use_case_affiliates",
              "bhe_gold.affiliates",
            ]}
          />
          <Question
            num={3}
            text="What's the highest-ROI source system to ingest next? Which use cases would that unlock?"
            backedBy="Source ROI tab"
            tables={[
              "bhe_gold.use_case_source_requirements",
              "bhe_gold.source_system_canonical",
            ]}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Key concepts</CardTitle>
          <CardDescription>
            The vocabulary used across pages, dashboards, and APIs.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
          <Concept
            icon={<Boxes size={16} />}
            term="Canonical source system"
            def="A normalized name for a system of record (e.g. 'SAP', 'PI Historian'). Raw labels from the catalog scan get folded onto a closed canonical list to eliminate naming drift ('PI Historian' vs 'OSIsoft PI' vs 'AVEVA PI')."
          />
          <Concept
            icon={<Building2 size={16} />}
            term="Affiliate"
            def="A BHE operating company (PacifiCorp, MidAmerican, NV Energy, BHE Pipelines, etc.). Use cases are tagged with the affiliates they apply to so the conversation can be grounded in a specific business unit."
          />
          <Concept
            icon={<TrendingUp size={16} />}
            term="Use case"
            def="A discrete business analytics opportunity with an estimated annual dollar value, a priority, and a list of data requirements (free text). Use cases are the unit of value in the platform."
          />
          <Concept
            icon={<Layers size={16} />}
            term="Readiness"
            def="The fraction of a use case's required canonical sources that are present in the lake today. Two formulas are available: Simple (present / total required) and Must-have (present-must / total-must)."
          />
          <Concept
            icon={<Sparkles size={16} />}
            term="LLM-derived mapping"
            def="When the join from use-case data needs to source systems or affiliates can't be done by a deterministic rule, it's filled in with an ai_query() call against a closed vocabulary. Results have necessity, confidence, and an originating excerpt for traceability. Manual edits are never overwritten."
          />
          <Concept
            icon={<Database size={16} />}
            term="Silver / Gold layers"
            def="Silver = cleaned, per-row catalog records with light enrichment. Gold = aggregations and curated dimensions used by the UI and dashboards. Both live under bhe_silver / bhe_gold in Unity Catalog."
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Who is this for</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
          <Persona
            label="BHE IT team"
            text="Walks into a business stakeholder conversation already knowing which use cases the affiliate can deliver today, what's blocked, and what investment unlocks the next tier."
          />
          <Persona
            label="BHE business stakeholders"
            text="Sees the dollar value sitting in the lake right now and the projects already deliverable for their affiliate."
          />
        </CardContent>
      </Card>
    </div>
  );
}

function Question({
  num,
  text,
  backedBy,
  tables,
}: {
  num: number;
  text: string;
  backedBy: string;
  tables: string[];
}) {
  return (
    <div className="border rounded-md p-3">
      <div className="flex items-start gap-3">
        <div className="shrink-0 w-6 h-6 rounded-full bg-primary/10 text-primary text-xs font-semibold flex items-center justify-center">
          {num}
        </div>
        <div className="flex-1">
          <div className="font-medium">{text}</div>
          <div className="text-xs text-muted-foreground mt-1">
            Backed by{" "}
            <span className="font-medium text-foreground">{backedBy}</span>{" "}
            &middot;{" "}
            {tables.map((t, i) => (
              <span key={t}>
                {i > 0 && ", "}
                <code className="bg-muted px-1 rounded">{t}</code>
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Concept({
  icon,
  term,
  def,
}: {
  icon: React.ReactNode;
  term: string;
  def: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="shrink-0 mt-0.5 text-muted-foreground">{icon}</div>
      <div>
        <div className="font-medium">{term}</div>
        <div className="text-muted-foreground">{def}</div>
      </div>
    </div>
  );
}

function Persona({ label, text }: { label: string; text: string }) {
  return (
    <div className="border rounded-md p-3">
      <Badge variant="outline" className="mb-2">
        {label}
      </Badge>
      <div className="text-sm text-muted-foreground">{text}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Data Model tab
// ---------------------------------------------------------------------------

type TableSpec = {
  name: string;
  purpose: string;
  /** key columns to highlight - keep terse */
  columns: string[];
  /** which jobs write here */
  writtenBy: string[];
  /** which UI surfaces / endpoints read it */
  readBy: string[];
  /** flags */
  manualEdit?: boolean;
  llmDerived?: boolean;
};

const SILVER_TABLES: TableSpec[] = [
  {
    name: "silver_schemas",
    purpose:
      "One row per Unity Catalog schema discovered in the customer workspace. Adds business-friendly name, classification (PRODUCTION / dev / qa), suggested domain and department. Source of truth for the Source Systems and Data Catalog browsers.",
    columns: [
      "catalog_name",
      "schema_name",
      "classification",
      "environment",
      "affiliate",
      "zone",
      "schema_friendly_name",
      "suggested_domain",
      "suggested_department",
      "is_user_edited",
    ],
    writtenBy: ["ai_enrich_metadata.py"],
    readBy: ["Data Catalog", "Source Systems", "/api/catalog/schemas"],
    manualEdit: true,
    llmDerived: true,
  },
  {
    name: "silver_tables",
    purpose:
      "One row per Unity Catalog table. Adds business_friendly_name, ai_definition, raw source_system (free text from LLM), and source_system_canonical (the normalized canonical key after the normalize_source_systems run).",
    columns: [
      "table_catalog",
      "table_schema",
      "table_name",
      "business_friendly_name",
      "ai_definition",
      "source_system",
      "source_system_canonical",
      "is_user_edited",
    ],
    writtenBy: ["ai_enrich_tables.py", "normalize_source_systems.py"],
    readBy: [
      "Source Systems",
      "Data Catalog",
      "/api/source-systems/{name}/tables",
    ],
    manualEdit: true,
    llmDerived: true,
  },
  {
    name: "use_cases",
    purpose:
      "Curated business analytics opportunities. Each row has a description, a free-text data_requirements list, an estimated annual dollar value, a priority, and a department. The unit of value in the platform.",
    columns: [
      "id",
      "use_case_name",
      "description",
      "department",
      "priority",
      "estimated_value_usd",
      "value_rationale",
      "business_value",
      "data_requirements",
    ],
    writtenBy: ["company_research.py"],
    readBy: ["Value & Readiness", "/api/value/use-cases"],
    llmDerived: true,
  },
  {
    name: "use_case_entities",
    purpose:
      "Free-text data entities required by each use case (e.g. 'meter readings', 'outage tickets'). Pre-canonical: feeds the LLM prompt that produces use_case_source_requirements.",
    columns: ["use_case_id", "entity_name", "description"],
    writtenBy: ["company_research.py"],
    readBy: ["build_value_model.py"],
    llmDerived: true,
  },
  {
    name: "departments",
    purpose:
      "Curated list of business departments at the customer (e.g. 'Power Delivery', 'Grid Operations'). Drives the Department slicer and provides the parent for use cases.",
    columns: ["id", "name", "description"],
    writtenBy: ["company_research.py"],
    readBy: ["Value & Readiness", "Edit Center"],
    manualEdit: true,
  },
  {
    name: "company_profile",
    purpose:
      "Single-row table describing the company being analyzed (BHE), its industry, headquarters, and high-level business areas. Powers the Company Setup page.",
    columns: ["company_name", "industry", "description", "headquarters"],
    writtenBy: ["company_research.py"],
    readBy: ["Company Setup", "/api/company/profile"],
    manualEdit: true,
  },
  {
    name: "sankey_mappings",
    purpose:
      "Legacy edge table from the original Sankey: source -> entity -> use case -> department. Superseded by the gold use_case_* tables for the new Value & Readiness Sankey, but still backs Edit Center workflows.",
    columns: [
      "id",
      "source_system",
      "entity_name",
      "use_case",
      "department",
      "relevance",
    ],
    writtenBy: ["company_research.py", "Edit Center"],
    readBy: ["Edit Center", "/api/sankey/mappings"],
    manualEdit: true,
  },
  {
    name: "job_progress",
    purpose:
      "Append-only event log for long-running LLM jobs (company research, AI enrichment). Drives the live progress tree in the UI.",
    columns: ["job_id", "ts", "level", "phase", "message", "payload"],
    writtenBy: ["company_research.py", "ai_enrich_*.py"],
    readBy: ["/api/jobs/progress"],
  },
];

const GOLD_TABLES: TableSpec[] = [
  {
    name: "source_system_canonical",
    purpose:
      "The closed vocabulary of canonical source systems (e.g. 'SAP', 'PI Historian', 'OSIsoft Cloud'). Seeded from src/data/source_system_canonical_seed.csv and editable by the customer.",
    columns: ["canonical", "category", "description", "is_active"],
    writtenBy: ["normalize_source_systems.py (seed_canonical stage)"],
    readBy: ["Source Systems", "Source ROI", "Sankey"],
    manualEdit: true,
  },
  {
    name: "source_system_aliases",
    purpose:
      "Maps every raw silver_tables.source_system value to a canonical key. Built in three passes: deterministic (exact / normalized match), LLM fallback against the closed vocabulary, and manual override. mapped_by tracks provenance.",
    columns: [
      "raw",
      "canonical",
      "mapped_by",
      "confidence",
      "is_user_edited",
      "mapped_at",
    ],
    writtenBy: ["normalize_source_systems.py"],
    readBy: ["Source Systems detail", "/api/source-systems/{name}"],
    manualEdit: true,
    llmDerived: true,
  },
  {
    name: "affiliates",
    purpose:
      "Operating subsidiaries / business units of the catalog's company. Generated by the company-research wizard via ai_query (LLM training data). MERGE preserves rows with is_user_edited=true so manual entries survive re-runs. Edit via the Edit Center.",
    columns: [
      "affiliate_name",
      "affiliate_code",
      "business_type",
      "region",
      "description",
      "is_active",
    ],
    writtenBy: ["company-research wizard (Step 3, _ai_query_generate_affiliates)"],
    readBy: ["Value & Readiness slicer", "/api/value/affiliates"],
    manualEdit: true,
  },
  {
    name: "use_case_source_requirements",
    purpose:
      "Many-to-many: use cases <-> required canonical sources. Necessity is must_have | nice_to_have. The data_need_excerpt records which line of use_cases.data_requirements drove each link, for traceability.",
    columns: [
      "use_case_id",
      "required_canonical",
      "necessity",
      "data_need_excerpt",
      "confidence",
      "mapped_by",
      "is_user_edited",
    ],
    writtenBy: ["build_value_model.py (apply_llm_mapping stage)"],
    readBy: [
      "Value & Readiness (all tabs)",
      "/api/value/summary",
      "/api/value/source-rollup",
      "/api/value/sankey",
    ],
    manualEdit: true,
    llmDerived: true,
  },
  {
    name: "use_case_affiliates",
    purpose:
      "Many-to-many: use cases <-> applicable BHE affiliates. Applicability is primary | secondary. Enables affiliate-scoped readiness math and the affiliate slicer.",
    columns: [
      "use_case_id",
      "affiliate_name",
      "applicability",
      "rationale",
      "confidence",
      "mapped_by",
      "is_user_edited",
    ],
    writtenBy: ["build_value_model.py (apply_llm_mapping stage)"],
    readBy: ["Value & Readiness slicer", "/api/value/use-cases/{id}"],
    manualEdit: true,
    llmDerived: true,
  },
  {
    name: "program_affiliate_map",
    purpose:
      "Maps program names (catalog prefixes) to affiliates with an affiliation_strength. Used as an input to the LLM prompt so it can ground affiliate suggestions in observable catalog signals.",
    columns: [
      "program",
      "affiliate_name",
      "affiliation_strength",
      "notes",
      "is_user_edited",
    ],
    writtenBy: ["build_value_model.py (seed_program_affiliate_map stage)"],
    readBy: ["build_value_model.py (LLM prompt)"],
    manualEdit: true,
  },
  {
    name: "schema_taxonomy",
    purpose:
      "Pivot table: per silver_schema, the LLM-suggested taxonomy tags (Domain, SubDomain, Department, Function) with confidence scores. Backs the Source Taxonomy page.",
    columns: [
      "catalog_name",
      "schema_name",
      "tag_type",
      "tag_value",
      "confidence",
    ],
    writtenBy: ["ai_enrich_metadata.py"],
    readBy: ["Source Taxonomy", "/api/taxonomy"],
    llmDerived: true,
  },
  {
    name: "schema_inventory",
    purpose:
      "Pre-aggregated rollup of silver_schemas / silver_tables by classification and environment. Used for the Dashboard and to seed prompts for ai_enrich_tables.",
    columns: [
      "catalog_name",
      "schema_name",
      "table_count",
      "classification",
      "environment",
    ],
    writtenBy: ["ai_enrich_metadata.py (rebuild stage)"],
    readBy: ["Dashboard", "/api/inventory"],
  },
  {
    name: "source_summary",
    purpose:
      "Per-source-system rollup (table count, schema count, environments). Used by Platform Analytics.",
    columns: [
      "source_system",
      "table_count",
      "schema_count",
      "environments",
    ],
    writtenBy: ["ai_enrich_metadata.py"],
    readBy: ["Platform Analytics", "/api/source-summary"],
  },
  {
    name: "workspace_summary",
    purpose:
      "Single-row workspace-level rollup (total schemas, tables, source systems, last refresh time).",
    columns: ["total_schemas", "total_tables", "total_source_systems", "ts"],
    writtenBy: ["ai_enrich_metadata.py"],
    readBy: ["Dashboard", "/api/workspace-summary"],
  },
  {
    name: "env_consistency",
    purpose:
      "Per-schema-family table indicating whether dev/qa/prod siblings exist. Helps surface inconsistent environment coverage.",
    columns: [
      "schema_family",
      "has_dev",
      "has_qa",
      "has_prod",
      "table_count",
    ],
    writtenBy: ["ai_enrich_metadata.py"],
    readBy: ["Platform Analytics", "/api/env-consistency"],
  },
  {
    name: "classification_rules",
    purpose:
      "User-editable heuristics that classify silver_schemas (e.g. schema-name regex -> classification). The Classification Rules page edits this directly.",
    columns: [
      "id",
      "category",
      "name",
      "rule_type",
      "rule_value",
      "result",
      "is_active",
    ],
    writtenBy: ["Classification Rules page"],
    readBy: ["ai_enrich_metadata.py", "/api/rules"],
    manualEdit: true,
  },
];

function DataModelTab() {
  return (
    <div className="space-y-5 max-w-6xl">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Layered architecture</CardTitle>
          <CardDescription>
            Data flows left-to-right. Bronze is the raw catalog scan, silver
            is per-row enrichment, gold is aggregation + curated dimensions
            consumed by the UI and dashboards.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <LayerDiagram />
        </CardContent>
      </Card>

      <TableSection
        title="Silver layer"
        description="Per-row enrichment of the Unity Catalog scan. One row per schema or table."
        tables={SILVER_TABLES}
        accent="hsl(45, 80%, 55%)"
        schemaName="bhe_silver"
      />

      <TableSection
        title="Gold layer"
        description="Curated dimensions and aggregations. Closed vocabularies (canonical sources, affiliates), many-to-many bridges (use case <-> source / affiliate), and pre-rolled summaries."
        tables={GOLD_TABLES}
        accent="hsl(140, 60%, 45%)"
        schemaName="bhe_gold"
      />
    </div>
  );
}

function LayerDiagram() {
  const layers = [
    {
      name: "Bronze",
      sub: "raw catalog scan",
      bg: "hsl(25, 60%, 55%)",
      items: ["information_schema.schemata", "information_schema.tables"],
    },
    {
      name: "Silver",
      sub: "bhe_silver - enriched rows",
      bg: "hsl(45, 80%, 55%)",
      items: [
        "silver_schemas",
        "silver_tables",
        "use_cases",
        "departments",
        "company_profile",
      ],
    },
    {
      name: "Gold",
      sub: "bhe_gold - dimensions + bridges",
      bg: "hsl(140, 60%, 45%)",
      items: [
        "source_system_canonical",
        "source_system_aliases",
        "affiliates",
        "use_case_source_requirements",
        "use_case_affiliates",
      ],
    },
    {
      name: "App",
      sub: "FastAPI + React",
      bg: "hsl(220, 60%, 55%)",
      items: ["Source Systems", "Value & Readiness", "Dashboards", "REST APIs"],
    },
  ];
  return (
    <div className="flex items-stretch gap-2 overflow-x-auto py-2">
      {layers.map((l, i) => (
        <div key={l.name} className="flex items-center gap-2 shrink-0">
          <div
            className="rounded-lg p-3 text-white min-w-[180px]"
            style={{ background: l.bg }}
          >
            <div className="text-sm font-semibold">{l.name}</div>
            <div className="text-[11px] opacity-90">{l.sub}</div>
            <ul className="text-[11px] mt-2 space-y-0.5 opacity-95">
              {l.items.map((it) => (
                <li key={it}>&middot; {it}</li>
              ))}
            </ul>
          </div>
          {i < layers.length - 1 && (
            <ArrowRight className="text-muted-foreground" size={20} />
          )}
        </div>
      ))}
    </div>
  );
}

function TableSection({
  title,
  description,
  tables,
  accent,
  schemaName,
}: {
  title: string;
  description: string;
  tables: TableSpec[];
  accent: string;
  schemaName: string;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <span
            className="inline-block w-3 h-3 rounded-sm"
            style={{ background: accent }}
          />
          <CardTitle className="text-base">{title}</CardTitle>
          <Badge variant="outline" className="text-[10px] font-mono">
            {schemaName}
          </Badge>
          <span className="text-xs text-muted-foreground ml-auto">
            {tables.length} table{tables.length === 1 ? "" : "s"}
          </span>
        </div>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {tables.map((t) => (
            <TableCard key={t.name} t={t} schemaName={schemaName} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function TableCard({ t, schemaName }: { t: TableSpec; schemaName: string }) {
  return (
    <div className="border rounded-md p-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="font-mono text-sm font-medium">
          <span className="text-muted-foreground">{schemaName}.</span>
          {t.name}
        </div>
        <div className="flex items-center gap-1.5">
          {t.llmDerived && (
            <Badge
              variant="outline"
              className="text-[10px] border-violet-300 text-violet-700"
            >
              <Sparkles size={10} className="mr-0.5" />
              LLM-derived
            </Badge>
          )}
          {t.manualEdit && (
            <Badge
              variant="outline"
              className="text-[10px] border-amber-300 text-amber-700"
            >
              user-editable
            </Badge>
          )}
        </div>
      </div>
      <div className="text-sm text-muted-foreground mt-1.5">{t.purpose}</div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3 text-xs">
        <div>
          <div className="font-medium uppercase tracking-wide text-[10px] text-muted-foreground mb-1">
            Key columns
          </div>
          <div className="flex flex-wrap gap-1">
            {t.columns.map((c) => (
              <code
                key={c}
                className="bg-muted px-1.5 py-0.5 rounded text-[11px]"
              >
                {c}
              </code>
            ))}
          </div>
        </div>
        <div>
          <div className="font-medium uppercase tracking-wide text-[10px] text-muted-foreground mb-1">
            Written by
          </div>
          <div className="space-y-0.5 text-muted-foreground">
            {t.writtenBy.map((w) => (
              <div key={w}>&middot; {w}</div>
            ))}
          </div>
        </div>
        <div>
          <div className="font-medium uppercase tracking-wide text-[10px] text-muted-foreground mb-1">
            Read by
          </div>
          <div className="space-y-0.5 text-muted-foreground">
            {t.readBy.map((r) => (
              <div key={r}>&middot; {r}</div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pipelines tab
// ---------------------------------------------------------------------------

type JobSpec = {
  name: string;
  file: string;
  purpose: string;
  stages: string[];
  dependsOn: string[];
  writes: string[];
  cadence: string;
  cost: "low" | "medium" | "high";
  idempotent: boolean;
};

const JOBS: JobSpec[] = [
  {
    name: "ai_enrich_metadata",
    file: "src/jobs/ai_enrich_metadata.py",
    purpose:
      "Reads the raw catalog scan and produces silver_schemas + the gold rollups (schema_inventory, schema_taxonomy, source_summary, workspace_summary, env_consistency). The first thing that runs after a fresh catalog ingest.",
    stages: [
      "scan information_schema",
      "rule-based classification (PRODUCTION / dev / qa)",
      "ai_query for schema_friendly_name + suggested_domain + suggested_department",
      "MERGE into silver_schemas (preserves is_user_edited)",
      "rebuild gold rollup tables",
    ],
    dependsOn: ["fresh information_schema scan"],
    writes: [
      "bhe_silver.silver_schemas",
      "bhe_gold.schema_inventory",
      "bhe_gold.schema_taxonomy",
      "bhe_gold.source_summary",
      "bhe_gold.workspace_summary",
      "bhe_gold.env_consistency",
    ],
    cadence: "On demand or daily after catalog refresh",
    cost: "medium",
    idempotent: true,
  },
  {
    name: "ai_enrich_tables",
    file: "src/jobs/ai_enrich_tables.py",
    purpose:
      "Per-table enrichment: business_friendly_name, ai_definition, and a free-text source_system label. Uses a staged pattern (raw response persisted before MERGE) to call ai_query exactly ONCE per table - not once per output field, which the optimizer would otherwise do.",
    stages: [
      "build prompt with schema context from schema_inventory",
      "ai_query() into a staging table (single call per row)",
      "from_json() parse + REGEXP_REPLACE cleanup",
      "MERGE into silver_tables (preserves is_user_edited)",
    ],
    dependsOn: ["ai_enrich_metadata (needs schema_inventory)"],
    writes: ["bhe_silver.silver_tables"],
    cadence: "On demand or weekly (most expensive job)",
    cost: "high",
    idempotent: true,
  },
  {
    name: "normalize_source_systems",
    file: "src/jobs/normalize_source_systems.py",
    purpose:
      "Folds the ~1,000 free-text source_system values produced by ai_enrich_tables onto the closed canonical list (~50 entries). Three-pass strategy: deterministic, LLM, manual.",
    stages: [
      "seed_canonical - upsert canonical + alias seed CSVs",
      "extract_unmapped - distinct silver_tables.source_system values not yet in alias table",
      "apply_deterministic - exact + normalized matches",
      "apply_llm_fallback - single batched ai_query against canonical vocab",
      "apply_to_silver - stamp silver_tables.source_system_canonical",
    ],
    dependsOn: ["ai_enrich_tables (needs raw source_system values)"],
    writes: [
      "bhe_gold.source_system_canonical",
      "bhe_gold.source_system_aliases",
      "bhe_silver.silver_tables (canonical column)",
    ],
    cadence: "On demand or after ai_enrich_tables",
    cost: "low",
    idempotent: true,
  },
  {
    name: "company_research",
    file: "src/jobs/company_research.py",
    purpose:
      "Multi-step LLM workflow that researches the customer (BHE) and produces departments, use_cases (with $ values), use_case_entities, and the legacy sankey_mappings. Emits live progress to job_progress so the UI can show a real-time tree.",
    stages: [
      "research company profile",
      "generate 10-25 departments",
      "generate 3-10 high-value use cases per department (with $ estimates)",
      "generate required entities per use case",
      "generate legacy Sankey mappings",
    ],
    dependsOn: ["company name input"],
    writes: [
      "bhe_silver.company_profile",
      "bhe_silver.departments",
      "bhe_silver.use_cases",
      "bhe_silver.use_case_entities",
      "bhe_silver.sankey_mappings",
      "bhe_silver.job_progress",
    ],
    cadence: "Once per customer (re-run only on rescope)",
    cost: "high",
    idempotent: true,
  },
  {
    name: "build_value_model",
    file: "src/jobs/build_value_model.py",
    purpose:
      "Produces the joins that power the Value & Readiness page: which canonical sources each use case requires, and which affiliates each use case applies to. Single ai_query per use case returns BOTH mappings (required_sources + applicable_affiliates) in one JSON response.",
    stages: [
      "seed_program_affiliate_map - upsert from program_affiliate_map_seed.csv",
      "extract_unresolved_use_cases - bound LLM cost to NEW use cases",
      "apply_llm_mapping - one ai_query returns both source + affiliate mappings, EXPLODE + MERGE into both child tables (with ROW_NUMBER dedup so duplicates can't enter)",
      "validate - row counts to job log",
    ],
    dependsOn: [
      "normalize_source_systems (needs canonical list as closed vocab)",
      "company_research (needs use_cases)",
    ],
    writes: [
      "bhe_gold.affiliates",
      "bhe_gold.program_affiliate_map",
      "bhe_gold.use_case_source_requirements",
      "bhe_gold.use_case_affiliates",
    ],
    cadence: "On demand after either dependency changes",
    cost: "medium",
    idempotent: true,
  },
  {
    name: "build_glossary",
    file: "src/jobs/build_glossary.py",
    purpose:
      "Materializes a flat glossary view over the enriched catalog for downstream search and BI consumption.",
    stages: ["CREATE OR REPLACE TABLE from joined silver+gold sources"],
    dependsOn: ["ai_enrich_metadata", "ai_enrich_tables"],
    writes: ["bhe_gold.glossary"],
    cadence: "After every enrichment refresh",
    cost: "low",
    idempotent: true,
  },
];

function PipelinesTab() {
  return (
    <div className="space-y-5 max-w-6xl">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Job dependency order</CardTitle>
          <CardDescription>
            Run order from a cold start. Each job is idempotent and respects
            <code className="bg-muted px-1 mx-1 rounded">is_user_edited</code>
            so re-runs never clobber manual edits.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DependencyChain />
        </CardContent>
      </Card>

      <div className="space-y-3">
        {JOBS.map((j) => (
          <JobCard key={j.name} j={j} />
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Operational notes</CardTitle>
        </CardHeader>
        <CardContent className="text-sm space-y-2 text-muted-foreground">
          <p>
            <span className="text-foreground font-medium">Deployment:</span>{" "}
            All jobs are defined as Databricks Asset Bundle resources under
            <code className="bg-muted px-1 mx-1 rounded">resources/*.job.yml</code>
            and deploy via{" "}
            <code className="bg-muted px-1 rounded">databricks bundle deploy</code>.
          </p>
          <p>
            <span className="text-foreground font-medium">
              Manual edit contract:
            </span>{" "}
            Every gold and silver MERGE has a guard like{" "}
            <code className="bg-muted px-1 rounded">
              WHEN MATCHED AND NOT t.is_user_edited
            </code>{" "}
            so anything an analyst overrides through the UI persists across
            re-runs.
          </p>
          <p>
            <span className="text-foreground font-medium">
              Cost containment:
            </span>{" "}
            All LLM-driven jobs have an{" "}
            <code className="bg-muted px-1 rounded">extract_unresolved</code>{" "}
            stage that bounds <code className="bg-muted px-1 rounded">ai_query</code>{" "}
            cost to NEW rows only. A full re-run flag{" "}
            (<code className="bg-muted px-1 rounded">--reseed</code> /{" "}
            <code className="bg-muted px-1 rounded">--remap-unmapped</code>)
            is available when seed CSVs change.
          </p>
          <p>
            <span className="text-foreground font-medium">Self-healing:</span>{" "}
            <code className="bg-muted px-1 rounded">build_value_model</code>{" "}
            and{" "}
            <code className="bg-muted px-1 rounded">normalize_source_systems</code>{" "}
            include a deduplication pass (ROW_NUMBER over the composite key,
            prioritizing must_have / manual / high-confidence) that runs
            before the MERGE so duplicate rows can't accumulate in the gold
            tables.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function DependencyChain() {
  const chain = [
    ["catalog scan"],
    ["ai_enrich_metadata"],
    ["ai_enrich_tables", "company_research"],
    ["normalize_source_systems"],
    ["build_value_model", "build_glossary"],
    ["UI / dashboards"],
  ];
  return (
    <div className="flex items-center gap-1 overflow-x-auto py-2">
      {chain.map((step, i) => (
        <div key={i} className="flex items-center gap-1 shrink-0">
          <div className="flex flex-col gap-1">
            {step.map((s) => (
              <div
                key={s}
                className="border rounded-md px-2.5 py-1.5 text-xs font-mono bg-muted/30"
              >
                {s}
              </div>
            ))}
          </div>
          {i < chain.length - 1 && (
            <ArrowRight className="text-muted-foreground mx-1" size={16} />
          )}
        </div>
      ))}
    </div>
  );
}

function JobCard({ j }: { j: JobSpec }) {
  const costColor =
    j.cost === "high"
      ? "text-rose-700 border-rose-300"
      : j.cost === "medium"
        ? "text-amber-700 border-amber-300"
        : "text-emerald-700 border-emerald-300";
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <CardTitle className="text-sm font-mono">{j.name}</CardTitle>
            <CardDescription className="text-xs font-mono mt-0.5">
              {j.file}
            </CardDescription>
          </div>
          <div className="flex items-center gap-1.5">
            <Badge variant="outline" className={"text-[10px] " + costColor}>
              {j.cost} cost
            </Badge>
            {j.idempotent && (
              <Badge variant="outline" className="text-[10px]">
                idempotent
              </Badge>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted-foreground">{j.purpose}</p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
          <div>
            <div className="font-medium uppercase tracking-wide text-[10px] text-muted-foreground mb-1">
              Stages
            </div>
            <ol className="space-y-0.5 text-muted-foreground list-decimal list-inside">
              {j.stages.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </div>
          <div className="space-y-2">
            <div>
              <div className="font-medium uppercase tracking-wide text-[10px] text-muted-foreground mb-1">
                Depends on
              </div>
              <div className="text-muted-foreground">
                {j.dependsOn.map((d, i) => (
                  <div key={i}>&middot; {d}</div>
                ))}
              </div>
            </div>
            <div>
              <div className="font-medium uppercase tracking-wide text-[10px] text-muted-foreground mb-1">
                Writes
              </div>
              <div className="text-muted-foreground font-mono">
                {j.writes.map((w) => (
                  <div key={w}>&middot; {w}</div>
                ))}
              </div>
            </div>
            <div>
              <div className="font-medium uppercase tracking-wide text-[10px] text-muted-foreground mb-1">
                Cadence
              </div>
              <div className="text-muted-foreground">{j.cadence}</div>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
