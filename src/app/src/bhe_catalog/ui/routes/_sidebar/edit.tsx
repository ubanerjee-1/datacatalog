import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  fetchDepartments,
  updateDepartment,
  fetchUseCases,
  updateUseCase,
  createUseCase,
  deleteUseCase,
  fetchEditAffiliates,
  createEditAffiliate,
  updateEditAffiliate,
  deleteEditAffiliate,
  fetchEditCanonicalSources,
  createEditCanonicalSource,
  updateEditCanonicalSource,
  deleteEditCanonicalSource,
  fetchEditProgramAffiliateMap,
  createEditProgramAffiliateMap,
  updateEditProgramAffiliateMap,
  deleteEditProgramAffiliateMap,
  fetchEditUseCaseAffiliates,
  upsertEditUseCaseAffiliate,
  deleteEditUseCaseAffiliate,
  fetchEditUseCaseSourceRequirements,
  upsertEditUseCaseSourceRequirement,
  deleteEditUseCaseSourceRequirement,
  USE_CASE_STATUS_LABEL,
  USE_CASE_STATUS_ORDER,
  type EditAffiliate,
  type EditCanonicalSource,
  type EditProgramAffiliateRow,
  type EditUseCaseAffiliate,
  type EditUseCaseSourceRequirement,
  type UseCaseStatus,
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
  PenSquare,
  Save,
  X,
  Trash2,
  Plus,
  Search,
  Building2,
  Database,
  Boxes,
  Layers,
  ListChecks,
  Link2,
  Sparkles,
  ShieldCheck,
  AlertCircle,
  Info,
} from "lucide-react";
import { toast } from "sonner";

export const Route = createFileRoute("/_sidebar/edit")({
  component: EditPage,
});

// ---------------------------------------------------------------------------
// Edit Center
//
// Single hub for analysts to review/override every customer-editable table.
// Sections are grouped by data layer:
//
//   Foundation   - dimensions seeded from CSVs (affiliates, canonical sources,
//                  program -> affiliate map). Manual edits flagged with
//                  is_user_edited=true survive re-seeding.
//   Catalog      - business artifacts authored by analysts (departments,
//                  use cases, free-text data entities).
//   AI Mappings  - LLM-derived bridges (UC -> affiliate, UC -> source). Manual
//                  edits set mapped_by='manual' so the build_value_model job
//                  does NOT overwrite them on subsequent runs.
// ---------------------------------------------------------------------------

type SectionKey =
  | "affiliates"
  | "canonical-sources"
  | "program-map"
  | "departments"
  | "use-cases"
  | "uc-affiliates"
  | "uc-source-reqs";

interface SectionDef {
  key: SectionKey;
  group: "Foundation" | "Business Catalog" | "AI Mappings";
  label: string;
  description: string;
  icon: React.ReactNode;
}

const SECTIONS: SectionDef[] = [
  {
    key: "affiliates",
    group: "Foundation",
    label: "Affiliates",
    description: "BHE operating subsidiaries",
    icon: <Building2 className="h-4 w-4" />,
  },
  {
    key: "canonical-sources",
    group: "Foundation",
    label: "Canonical Sources",
    description: "Closed vocabulary the LLM uses",
    icon: <Database className="h-4 w-4" />,
  },
  {
    key: "program-map",
    group: "Foundation",
    label: "Program → Affiliate",
    description: "Catalog program routing",
    icon: <Link2 className="h-4 w-4" />,
  },
  {
    key: "departments",
    group: "Business Catalog",
    label: "Departments",
    description: "Org units that consume data",
    icon: <Layers className="h-4 w-4" />,
  },
  {
    key: "use-cases",
    group: "Business Catalog",
    label: "Use Cases",
    description: "Business value & data needs",
    icon: <ListChecks className="h-4 w-4" />,
  },
  {
    key: "uc-affiliates",
    group: "AI Mappings",
    label: "UC → Affiliates",
    description: "Which affiliates a UC applies to",
    icon: <Sparkles className="h-4 w-4" />,
  },
  {
    key: "uc-source-reqs",
    group: "AI Mappings",
    label: "UC → Source Requirements",
    description: "Canonical sources each UC needs",
    icon: <Boxes className="h-4 w-4" />,
  },
];

function EditPage() {
  const [active, setActive] = useState<SectionKey>("affiliates");

  const grouped = useMemo(() => {
    const g: Record<string, SectionDef[]> = {};
    for (const s of SECTIONS) {
      g[s.group] = g[s.group] || [];
      g[s.group].push(s);
    }
    return g;
  }, []);

  const activeSection = SECTIONS.find((s) => s.key === active)!;

  return (
    <div className="flex h-full">
      {/* Left rail */}
      <aside className="w-64 border-r bg-muted/30 overflow-y-auto flex-shrink-0">
        <div className="p-4 border-b bg-background">
          <h1 className="text-lg font-semibold flex items-center gap-2">
            <PenSquare className="h-5 w-5" />
            Edit Center
          </h1>
          <p className="text-xs text-muted-foreground mt-1">
            Override AI-generated metadata. Manual edits survive pipeline re-runs.
          </p>
        </div>
        {Object.entries(grouped).map(([group, items]) => (
          <div key={group} className="py-2">
            <div className="px-4 py-1 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
              {group}
            </div>
            {items.map((s) => (
              <button
                key={s.key}
                onClick={() => setActive(s.key)}
                className={`w-full text-left px-4 py-2 text-sm flex items-start gap-2 transition ${
                  active === s.key
                    ? "bg-primary text-primary-foreground"
                    : "hover:bg-muted"
                }`}
              >
                <span
                  className={`mt-0.5 ${
                    active === s.key
                      ? "text-primary-foreground"
                      : "text-muted-foreground"
                  }`}
                >
                  {s.icon}
                </span>
                <span className="flex-1 min-w-0">
                  <div className="font-medium leading-tight">{s.label}</div>
                  <div
                    className={`text-[11px] mt-0.5 leading-tight ${
                      active === s.key
                        ? "text-primary-foreground/80"
                        : "text-muted-foreground"
                    }`}
                  >
                    {s.description}
                  </div>
                </span>
              </button>
            ))}
          </div>
        ))}
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto">
        <div className="p-6 max-w-6xl">
          <div className="mb-4 flex items-start justify-between gap-4">
            <div>
              <h2 className="text-2xl font-bold flex items-center gap-2">
                {activeSection.icon}
                {activeSection.label}
              </h2>
              <p className="text-sm text-muted-foreground mt-1">
                {activeSection.description}
              </p>
            </div>
            <SectionContract sectionKey={active} />
          </div>

          {active === "affiliates" && <AffiliatesEditor />}
          {active === "canonical-sources" && <CanonicalSourcesEditor />}
          {active === "program-map" && <ProgramAffiliateMapEditor />}
          {active === "departments" && <DepartmentsEditor />}
          {active === "use-cases" && <UseCasesEditor />}
          {active === "uc-affiliates" && <UseCaseAffiliatesEditor />}
          {active === "uc-source-reqs" && <UseCaseSourceReqsEditor />}
        </div>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-section "edit contract" callout - tells the user how their changes
// interact with the build pipelines.
// ---------------------------------------------------------------------------

function SectionContract({ sectionKey }: { sectionKey: SectionKey }) {
  const contracts: Record<
    SectionKey,
    { tone: "stable" | "merge" | "override"; text: string }
  > = {
    affiliates: {
      tone: "stable",
      text: "Sticky. Edits set is_user_edited=true; the seed CSV won't overwrite them.",
    },
    "canonical-sources": {
      tone: "stable",
      text: "Sticky. New canonicals immediately show up in the LLM's allowed-values list on the next mapping run.",
    },
    "program-map": {
      tone: "stable",
      text: "Sticky. Affects affiliate-scoped presence everywhere (Value, Gaps).",
    },
    departments: {
      tone: "stable",
      text: "Sticky. Department naming flows into use case grouping.",
    },
    "use-cases": {
      tone: "stable",
      text: "Sticky. Editing a UC's description re-triggers AI mapping on the next build run for stale (non-manual) source/affiliate rows.",
    },
    "uc-affiliates": {
      tone: "override",
      text: "AI-derived. Manual edits set mapped_by=manual and are preserved across pipeline re-runs.",
    },
    "uc-source-reqs": {
      tone: "override",
      text: "AI-derived. Manual edits set mapped_by=manual and are preserved across pipeline re-runs.",
    },
  };
  const c = contracts[sectionKey];
  const tone =
    c.tone === "stable"
      ? "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200"
      : c.tone === "override"
        ? "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
        : "border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-200";

  return (
    <div
      className={`text-xs border rounded-md px-3 py-2 max-w-md flex items-start gap-2 ${tone}`}
    >
      {c.tone === "override" ? (
        <ShieldCheck className="h-4 w-4 flex-shrink-0 mt-0.5" />
      ) : (
        <Info className="h-4 w-4 flex-shrink-0 mt-0.5" />
      )}
      <div>{c.text}</div>
    </div>
  );
}

// ===========================================================================
// AFFILIATES
// ===========================================================================

function AffiliatesEditor() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["edit-affiliates"],
    queryFn: fetchEditAffiliates,
  });

  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<Partial<EditAffiliate>>({});

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.toLowerCase();
    return data.filter(
      (a) =>
        !q ||
        a.affiliate_name.toLowerCase().includes(q) ||
        (a.affiliate_code ?? "").toLowerCase().includes(q) ||
        (a.business_type ?? "").toLowerCase().includes(q),
    );
  }, [data, search]);

  const saveMut = useMutation({
    mutationFn: ({ name, body }: { name: string; body: Partial<EditAffiliate> }) =>
      updateEditAffiliate(name, body),
    onSuccess: () => {
      toast.success("Affiliate updated");
      qc.invalidateQueries({ queryKey: ["edit-affiliates"] });
      setEditing(null);
    },
    onError: (e: Error) => toast.error(`Update failed: ${e.message}`),
  });

  const createMut = useMutation({
    mutationFn: createEditAffiliate,
    onSuccess: () => {
      toast.success("Affiliate created");
      qc.invalidateQueries({ queryKey: ["edit-affiliates"] });
      setCreating(false);
      setDraft({});
    },
    onError: (e: Error) => toast.error(`Create failed: ${e.message}`),
  });

  const deleteMut = useMutation({
    mutationFn: deleteEditAffiliate,
    onSuccess: () => {
      toast.success("Affiliate deactivated");
      qc.invalidateQueries({ queryKey: ["edit-affiliates"] });
    },
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  });

  return (
    <div className="space-y-3">
      <Toolbar
        search={search}
        onSearch={setSearch}
        onAdd={() => {
          setCreating(true);
          setDraft({ is_active: true });
        }}
        addLabel="Add affiliate"
      />

      {creating && (
        <Card className="border-primary/40">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">New affiliate</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <Input
                placeholder="Affiliate name (required)"
                value={draft.affiliate_name ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, affiliate_name: e.target.value })
                }
              />
              <Input
                placeholder="Code (e.g. PAC)"
                value={draft.affiliate_code ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, affiliate_code: e.target.value })
                }
              />
              <Input
                placeholder="Business type"
                value={draft.business_type ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, business_type: e.target.value })
                }
              />
              <Input
                placeholder="Region"
                value={draft.region ?? ""}
                onChange={(e) => setDraft({ ...draft, region: e.target.value })}
              />
            </div>
            <Input
              placeholder="Description"
              value={draft.description ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, description: e.target.value })
              }
            />
            <CrudButtons
              onSave={() =>
                createMut.mutate({
                  affiliate_name: draft.affiliate_name ?? "",
                  affiliate_code: draft.affiliate_code ?? null,
                  business_type: draft.business_type ?? null,
                  region: draft.region ?? null,
                  description: draft.description ?? null,
                  is_active: draft.is_active ?? true,
                })
              }
              onCancel={() => {
                setCreating(false);
                setDraft({});
              }}
              saveDisabled={!draft.affiliate_name}
              saving={createMut.isPending}
            />
          </CardContent>
        </Card>
      )}

      {isLoading && <SkeletonRows />}
      {data && filtered.length === 0 && !isLoading && (
        <EmptyState text="No affiliates match." />
      )}

      <div className="space-y-2">
        {filtered.map((a) => (
          <Card key={a.affiliate_name}>
            <CardContent className="p-3">
              {editing === a.affiliate_name ? (
                <div className="space-y-2">
                  <div className="text-xs text-muted-foreground">
                    Editing <strong>{a.affiliate_name}</strong>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <Input
                      placeholder="Code"
                      value={draft.affiliate_code ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, affiliate_code: e.target.value })
                      }
                    />
                    <Input
                      placeholder="Business type"
                      value={draft.business_type ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, business_type: e.target.value })
                      }
                    />
                    <Input
                      placeholder="Region"
                      value={draft.region ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, region: e.target.value })
                      }
                    />
                    <ToggleField
                      label="Active"
                      value={draft.is_active ?? true}
                      onChange={(v) => setDraft({ ...draft, is_active: v })}
                    />
                  </div>
                  <Input
                    placeholder="Description"
                    value={draft.description ?? ""}
                    onChange={(e) =>
                      setDraft({ ...draft, description: e.target.value })
                    }
                  />
                  <CrudButtons
                    onSave={() =>
                      saveMut.mutate({
                        name: a.affiliate_name,
                        body: {
                          affiliate_code: draft.affiliate_code,
                          business_type: draft.business_type,
                          region: draft.region,
                          description: draft.description,
                          is_active: draft.is_active,
                        },
                      })
                    }
                    onCancel={() => setEditing(null)}
                    saving={saveMut.isPending}
                  />
                </div>
              ) : (
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium">{a.affiliate_name}</span>
                      {a.affiliate_code && (
                        <Badge variant="outline" className="text-[10px]">
                          {a.affiliate_code}
                        </Badge>
                      )}
                      {a.business_type && (
                        <Badge variant="secondary" className="text-[10px]">
                          {a.business_type}
                        </Badge>
                      )}
                      {!a.is_active && (
                        <Badge variant="destructive" className="text-[10px]">
                          inactive
                        </Badge>
                      )}
                      {a.is_user_edited && <EditedBadge />}
                    </div>
                    {a.description && (
                      <p className="text-xs text-muted-foreground mt-1">
                        {a.description}
                      </p>
                    )}
                    {a.region && (
                      <p className="text-[11px] text-muted-foreground mt-0.5">
                        Region: {a.region}
                      </p>
                    )}
                  </div>
                  <RowActions
                    onEdit={() => {
                      setEditing(a.affiliate_name);
                      setDraft({
                        affiliate_code: a.affiliate_code,
                        business_type: a.business_type,
                        region: a.region,
                        description: a.description,
                        is_active: a.is_active,
                      });
                    }}
                    onDelete={() => {
                      if (
                        confirm(
                          `Deactivate ${a.affiliate_name}? It will be hidden from rollups but kept for history.`,
                        )
                      ) {
                        deleteMut.mutate(a.affiliate_name);
                      }
                    }}
                    deleteDisabled={!a.is_active}
                  />
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

// ===========================================================================
// CANONICAL SOURCES
// ===========================================================================

function CanonicalSourcesEditor() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["edit-canonical-sources"],
    queryFn: fetchEditCanonicalSources,
  });

  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<Partial<EditCanonicalSource>>({});

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.toLowerCase();
    return data.filter(
      (c) =>
        !q ||
        c.canonical.toLowerCase().includes(q) ||
        (c.category ?? "").toLowerCase().includes(q),
    );
  }, [data, search]);

  const saveMut = useMutation({
    mutationFn: ({
      canonical,
      body,
    }: {
      canonical: string;
      body: Partial<EditCanonicalSource>;
    }) => updateEditCanonicalSource(canonical, body),
    onSuccess: () => {
      toast.success("Canonical updated");
      qc.invalidateQueries({ queryKey: ["edit-canonical-sources"] });
      setEditing(null);
    },
    onError: (e: Error) => toast.error(`Update failed: ${e.message}`),
  });

  const createMut = useMutation({
    mutationFn: createEditCanonicalSource,
    onSuccess: () => {
      toast.success("Canonical created");
      qc.invalidateQueries({ queryKey: ["edit-canonical-sources"] });
      setCreating(false);
      setDraft({});
    },
    onError: (e: Error) => toast.error(`Create failed: ${e.message}`),
  });

  const deleteMut = useMutation({
    mutationFn: deleteEditCanonicalSource,
    onSuccess: () => {
      toast.success("Canonical deactivated");
      qc.invalidateQueries({ queryKey: ["edit-canonical-sources"] });
    },
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  });

  // Group by category for nicer browsing.
  const grouped = useMemo(() => {
    const g: Record<string, EditCanonicalSource[]> = {};
    for (const c of filtered) {
      const cat = c.category || "Uncategorized";
      g[cat] = g[cat] || [];
      g[cat].push(c);
    }
    return Object.entries(g).sort(([a], [b]) => a.localeCompare(b));
  }, [filtered]);

  return (
    <div className="space-y-3">
      <Toolbar
        search={search}
        onSearch={setSearch}
        onAdd={() => {
          setCreating(true);
          setDraft({ is_active: true });
        }}
        addLabel="Add canonical source"
      />

      {creating && (
        <Card className="border-primary/40">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">New canonical source</CardTitle>
            <CardDescription className="text-[11px]">
              Adding a row makes the canonical immediately eligible in the next
              normalize_source_systems and build_value_model run.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <Input
                placeholder="Canonical name (required)"
                value={draft.canonical ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, canonical: e.target.value })
                }
              />
              <Input
                placeholder="Category (e.g. CIS, Historian)"
                value={draft.category ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, category: e.target.value })
                }
              />
            </div>
            <Input
              placeholder="Description"
              value={draft.description ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, description: e.target.value })
              }
            />
            <CrudButtons
              onSave={() =>
                createMut.mutate({
                  canonical: draft.canonical ?? "",
                  category: draft.category ?? null,
                  description: draft.description ?? null,
                  is_active: draft.is_active ?? true,
                })
              }
              onCancel={() => {
                setCreating(false);
                setDraft({});
              }}
              saveDisabled={!draft.canonical}
              saving={createMut.isPending}
            />
          </CardContent>
        </Card>
      )}

      {isLoading && <SkeletonRows />}
      {data && grouped.length === 0 && !isLoading && (
        <EmptyState text="No canonical sources match." />
      )}

      {grouped.map(([cat, items]) => (
        <div key={cat} className="space-y-1">
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold pt-2">
            {cat} <span className="text-muted-foreground/60">({items.length})</span>
          </div>
          {items.map((c) => (
            <Card key={c.canonical} className="overflow-hidden">
              <CardContent className="p-3">
                {editing === c.canonical ? (
                  <div className="space-y-2">
                    <div className="text-xs text-muted-foreground">
                      Editing <strong>{c.canonical}</strong>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <Input
                        placeholder="Category"
                        value={draft.category ?? ""}
                        onChange={(e) =>
                          setDraft({ ...draft, category: e.target.value })
                        }
                      />
                      <ToggleField
                        label="Active"
                        value={draft.is_active ?? true}
                        onChange={(v) => setDraft({ ...draft, is_active: v })}
                      />
                    </div>
                    <Input
                      placeholder="Description"
                      value={draft.description ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, description: e.target.value })
                      }
                    />
                    <CrudButtons
                      onSave={() =>
                        saveMut.mutate({
                          canonical: c.canonical,
                          body: {
                            category: draft.category,
                            description: draft.description,
                            is_active: draft.is_active,
                          },
                        })
                      }
                      onCancel={() => setEditing(null)}
                      saving={saveMut.isPending}
                    />
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium">{c.canonical}</span>
                        {!c.is_active && (
                          <Badge variant="destructive" className="text-[10px]">
                            inactive
                          </Badge>
                        )}
                      </div>
                      {c.description && (
                        <p className="text-xs text-muted-foreground mt-1">
                          {c.description}
                        </p>
                      )}
                    </div>
                    <RowActions
                      onEdit={() => {
                        setEditing(c.canonical);
                        setDraft({
                          category: c.category,
                          description: c.description,
                          is_active: c.is_active,
                        });
                      }}
                      onDelete={() => {
                        if (
                          confirm(
                            `Deactivate ${c.canonical}? It stays in the table but is hidden from new mappings.`,
                          )
                        )
                          deleteMut.mutate(c.canonical);
                      }}
                      deleteDisabled={!c.is_active}
                    />
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      ))}
    </div>
  );
}

// ===========================================================================
// PROGRAM -> AFFILIATE MAP
// ===========================================================================

function ProgramAffiliateMapEditor() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["edit-program-map"],
    queryFn: fetchEditProgramAffiliateMap,
  });
  const { data: affiliates } = useQuery({
    queryKey: ["edit-affiliates"],
    queryFn: fetchEditAffiliates,
  });

  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<Partial<EditProgramAffiliateRow>>({});

  const rowKey = (r: EditProgramAffiliateRow) =>
    `${r.program}::${r.affiliate_name}`;

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.toLowerCase();
    return data.filter(
      (r) =>
        !q ||
        r.program.toLowerCase().includes(q) ||
        r.affiliate_name.toLowerCase().includes(q),
    );
  }, [data, search]);

  const createMut = useMutation({
    mutationFn: createEditProgramAffiliateMap,
    onSuccess: () => {
      toast.success("Mapping created");
      qc.invalidateQueries({ queryKey: ["edit-program-map"] });
      setCreating(false);
      setDraft({});
    },
    onError: (e: Error) => toast.error(`Create failed: ${e.message}`),
  });

  const saveMut = useMutation({
    mutationFn: ({
      program,
      affiliate_name,
      body,
    }: {
      program: string;
      affiliate_name: string;
      body: { affiliation_strength?: string; notes?: string };
    }) => updateEditProgramAffiliateMap(program, affiliate_name, body),
    onSuccess: () => {
      toast.success("Mapping updated");
      qc.invalidateQueries({ queryKey: ["edit-program-map"] });
      setEditing(null);
    },
    onError: (e: Error) => toast.error(`Update failed: ${e.message}`),
  });

  const deleteMut = useMutation({
    mutationFn: ({
      program,
      affiliate_name,
    }: {
      program: string;
      affiliate_name: string;
    }) => deleteEditProgramAffiliateMap(program, affiliate_name),
    onSuccess: () => {
      toast.success("Mapping deleted");
      qc.invalidateQueries({ queryKey: ["edit-program-map"] });
    },
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  });

  return (
    <div className="space-y-3">
      <Toolbar
        search={search}
        onSearch={setSearch}
        onAdd={() => {
          setCreating(true);
          setDraft({ affiliation_strength: "primary" });
        }}
        addLabel="Add mapping"
      />

      {creating && (
        <Card className="border-primary/40">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">New program → affiliate mapping</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <Input
                placeholder="Program (e.g. CustomerOps)"
                value={draft.program ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, program: e.target.value })
                }
              />
              <SelectInput
                value={draft.affiliate_name ?? ""}
                onChange={(v) => setDraft({ ...draft, affiliate_name: v })}
                options={["", ...(affiliates ?? []).map((a) => a.affiliate_name)]}
                placeholder="Affiliate"
              />
              <SelectInput
                value={draft.affiliation_strength ?? "primary"}
                onChange={(v) => setDraft({ ...draft, affiliation_strength: v })}
                options={["primary", "secondary"]}
              />
              <Input
                placeholder="Notes"
                value={draft.notes ?? ""}
                onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
              />
            </div>
            <CrudButtons
              onSave={() =>
                createMut.mutate({
                  program: draft.program ?? "",
                  affiliate_name: draft.affiliate_name ?? "",
                  affiliation_strength: draft.affiliation_strength ?? "primary",
                  notes: draft.notes ?? "",
                })
              }
              onCancel={() => {
                setCreating(false);
                setDraft({});
              }}
              saveDisabled={!draft.program || !draft.affiliate_name}
              saving={createMut.isPending}
            />
          </CardContent>
        </Card>
      )}

      {isLoading && <SkeletonRows />}
      {data && filtered.length === 0 && !isLoading && (
        <EmptyState text="No mappings match." />
      )}

      <div className="space-y-1.5">
        {filtered.map((r) => {
          const key = rowKey(r);
          return (
            <Card key={key}>
              <CardContent className="p-3">
                {editing === key ? (
                  <div className="space-y-2">
                    <div className="text-xs text-muted-foreground">
                      Editing{" "}
                      <strong>
                        {r.program} → {r.affiliate_name}
                      </strong>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <SelectInput
                        value={draft.affiliation_strength ?? "primary"}
                        onChange={(v) =>
                          setDraft({ ...draft, affiliation_strength: v })
                        }
                        options={["primary", "secondary"]}
                      />
                      <Input
                        placeholder="Notes"
                        value={draft.notes ?? ""}
                        onChange={(e) =>
                          setDraft({ ...draft, notes: e.target.value })
                        }
                      />
                    </div>
                    <CrudButtons
                      onSave={() =>
                        saveMut.mutate({
                          program: r.program,
                          affiliate_name: r.affiliate_name,
                          body: {
                            affiliation_strength:
                              draft.affiliation_strength ?? undefined,
                            notes: draft.notes ?? undefined,
                          },
                        })
                      }
                      onCancel={() => setEditing(null)}
                      saving={saveMut.isPending}
                    />
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0 flex items-center gap-2 flex-wrap">
                      <Badge variant="outline" className="text-[10px]">
                        {r.program}
                      </Badge>
                      <span className="text-muted-foreground">→</span>
                      <Badge className="text-[10px]">{r.affiliate_name}</Badge>
                      <Badge variant="secondary" className="text-[10px]">
                        {r.affiliation_strength}
                      </Badge>
                      {r.is_user_edited && <EditedBadge />}
                      {r.notes && (
                        <span className="text-[11px] text-muted-foreground ml-2">
                          {r.notes}
                        </span>
                      )}
                    </div>
                    <RowActions
                      onEdit={() => {
                        setEditing(key);
                        setDraft({
                          affiliation_strength: r.affiliation_strength,
                          notes: r.notes,
                        });
                      }}
                      onDelete={() => {
                        if (
                          confirm(
                            `Delete mapping ${r.program} → ${r.affiliate_name}?`,
                          )
                        ) {
                          deleteMut.mutate({
                            program: r.program,
                            affiliate_name: r.affiliate_name,
                          });
                        }
                      }}
                    />
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

// ===========================================================================
// DEPARTMENTS
// ===========================================================================

function DepartmentsEditor() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["departments"],
    queryFn: fetchDepartments,
  });

  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.toLowerCase();
    return data.filter(
      (d: { department_name?: string; description?: string }) =>
        !q ||
        (d.department_name ?? "").toLowerCase().includes(q) ||
        (d.description ?? "").toLowerCase().includes(q),
    );
  }, [data, search]);

  const saveMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, string> }) =>
      updateDepartment(id, body),
    onSuccess: () => {
      toast.success("Department updated");
      qc.invalidateQueries({ queryKey: ["departments"] });
      setEditing(null);
    },
    onError: (e: Error) => toast.error(`Update failed: ${e.message}`),
  });

  return (
    <div className="space-y-3">
      <Toolbar search={search} onSearch={setSearch} />
      {isLoading && <SkeletonRows />}
      {data && filtered.length === 0 && !isLoading && (
        <EmptyState text="No departments yet. Run Company Research first." />
      )}
      <div className="space-y-2">
        {filtered.map(
          (d: {
            id: string;
            department_name?: string;
            description?: string;
            data_needs?: string;
            is_user_edited?: boolean;
          }) => (
            <Card key={d.id}>
              <CardContent className="p-3">
                {editing === d.id ? (
                  <div className="space-y-2">
                    <Input
                      placeholder="Department name"
                      value={draft.department_name ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, department_name: e.target.value })
                      }
                    />
                    <Input
                      placeholder="Description"
                      value={draft.description ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, description: e.target.value })
                      }
                    />
                    <Input
                      placeholder="Data needs"
                      value={draft.data_needs ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, data_needs: e.target.value })
                      }
                    />
                    <CrudButtons
                      onSave={() => saveMut.mutate({ id: d.id, body: draft })}
                      onCancel={() => setEditing(null)}
                      saving={saveMut.isPending}
                    />
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{d.department_name}</span>
                        {d.is_user_edited && <EditedBadge />}
                      </div>
                      {d.description && (
                        <p className="text-xs text-muted-foreground mt-1">
                          {d.description}
                        </p>
                      )}
                      {d.data_needs && (
                        <p className="text-[11px] text-muted-foreground mt-0.5">
                          Data needs: {d.data_needs}
                        </p>
                      )}
                    </div>
                    <RowActions
                      onEdit={() => {
                        setEditing(d.id);
                        setDraft({
                          department_name: d.department_name ?? "",
                          description: d.description ?? "",
                          data_needs: d.data_needs ?? "",
                        });
                      }}
                    />
                  </div>
                )}
              </CardContent>
            </Card>
          ),
        )}
      </div>
    </div>
  );
}

// ===========================================================================
// USE CASES
// ===========================================================================

function UseCasesEditor() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["useCases"],
    queryFn: () => fetchUseCases(),
  });

  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [drText, setDrText] = useState("");

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.toLowerCase();
    return data.filter(
      (u: { use_case_name?: string; description?: string; department?: string }) =>
        !q ||
        (u.use_case_name ?? "").toLowerCase().includes(q) ||
        (u.description ?? "").toLowerCase().includes(q) ||
        (u.department ?? "").toLowerCase().includes(q),
    );
  }, [data, search]);

  const saveMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      updateUseCase(id, body),
    onSuccess: () => {
      toast.success("Use case updated");
      qc.invalidateQueries({ queryKey: ["useCases"] });
      setEditing(null);
    },
    onError: (e: Error) => toast.error(`Update failed: ${e.message}`),
  });

  const createMut = useMutation({
    mutationFn: createUseCase,
    onSuccess: () => {
      toast.success("Use case created");
      qc.invalidateQueries({ queryKey: ["useCases"] });
      setCreating(false);
      setDraft({});
      setDrText("");
    },
    onError: (e: Error) => toast.error(`Create failed: ${e.message}`),
  });

  const deleteMut = useMutation({
    mutationFn: deleteUseCase,
    onSuccess: () => {
      toast.success("Use case deleted (with derived gold rows)");
      qc.invalidateQueries({ queryKey: ["useCases"] });
    },
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  });

  const parseDr = (s: string): string[] =>
    s
      .split(/\n|;/)
      .map((x) => x.trim())
      .filter(Boolean);

  return (
    <div className="space-y-3">
      <Toolbar
        search={search}
        onSearch={setSearch}
        onAdd={() => {
          setCreating(true);
          setDraft({ priority: "Medium", status: "not_started" });
          setDrText("");
        }}
        addLabel="Add use case"
      />

      {creating && (
        <Card className="border-primary/40">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">New use case</CardTitle>
            <CardDescription className="text-[11px]">
              The next build_value_model run will derive AI mappings (affiliates +
              source requirements) for this use case automatically. Set delivery
              status so the Value &amp; Readiness page can split realized vs
              opportunity value.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <Input
              placeholder="Use case name (required)"
              value={(draft.use_case_name as string) ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, use_case_name: e.target.value })
              }
            />
            <textarea
              className="w-full text-sm rounded-md border border-input bg-background px-3 py-2 min-h-[60px]"
              placeholder="Description"
              value={(draft.description as string) ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, description: e.target.value })
              }
            />
            <div className="grid grid-cols-4 gap-2">
              <Input
                placeholder="Department"
                value={(draft.department as string) ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, department: e.target.value })
                }
              />
              <Input
                placeholder="Category"
                value={(draft.category as string) ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, category: e.target.value })
                }
              />
              <SelectInput
                value={(draft.priority as string) ?? "Medium"}
                onChange={(v) => setDraft({ ...draft, priority: v })}
                options={["High", "Medium", "Low"]}
              />
              <SelectInput
                value={(draft.status as string) ?? "not_started"}
                onChange={(v) => setDraft({ ...draft, status: v })}
                options={[...USE_CASE_STATUS_ORDER]}
                labelFor={(s) => USE_CASE_STATUS_LABEL[s as UseCaseStatus]}
              />
            </div>
            <Input
              placeholder="Estimated value (USD)"
              type="number"
              value={(draft.estimated_value_usd as number | undefined) ?? ""}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  estimated_value_usd: e.target.value
                    ? Number(e.target.value)
                    : null,
                })
              }
            />
            <Input
              placeholder="Business value (one-line summary)"
              value={(draft.business_value as string) ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, business_value: e.target.value })
              }
            />
            <textarea
              className="w-full text-sm rounded-md border border-input bg-background px-3 py-2 min-h-[40px]"
              placeholder="Value rationale (why this is worth what it is)"
              value={(draft.value_rationale as string) ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, value_rationale: e.target.value })
              }
            />
            <textarea
              className="w-full text-sm rounded-md border border-input bg-background px-3 py-2 min-h-[60px]"
              placeholder="Data requirements (one per line; e.g. 'meter readings', 'outage tickets')"
              value={drText}
              onChange={(e) => setDrText(e.target.value)}
            />
            <CrudButtons
              onSave={() =>
                createMut.mutate({
                  use_case_name: (draft.use_case_name as string) ?? "",
                  description: (draft.description as string) ?? "",
                  department: (draft.department as string) ?? "",
                  category: (draft.category as string) ?? "",
                  priority: (draft.priority as string) ?? "Medium",
                  status:
                    (draft.status as UseCaseStatus) ?? "not_started",
                  business_value: (draft.business_value as string) ?? "",
                  estimated_value_usd:
                    (draft.estimated_value_usd as number | null) ?? null,
                  value_rationale: (draft.value_rationale as string) ?? "",
                  data_requirements: parseDr(drText),
                })
              }
              onCancel={() => {
                setCreating(false);
                setDraft({});
                setDrText("");
              }}
              saveDisabled={!draft.use_case_name}
              saving={createMut.isPending}
            />
          </CardContent>
        </Card>
      )}

      {isLoading && <SkeletonRows />}
      {data && filtered.length === 0 && !isLoading && (
        <EmptyState text="No use cases match." />
      )}

      <div className="space-y-2">
        {filtered.map(
          (uc: {
            id: string;
            use_case_name?: string;
            description?: string;
            department?: string;
            category?: string;
            priority?: string;
            status?: string;
            status_notes?: string;
            status_updated_at?: string | null;
            business_value?: string;
            estimated_value_usd?: number;
            value_rationale?: string;
            data_requirements?: string[];
            is_user_edited?: boolean;
          }) => (
            <Card key={uc.id}>
              <CardContent className="p-3">
                {editing === uc.id ? (
                  <div className="space-y-2">
                    <Input
                      placeholder="Name"
                      value={(draft.use_case_name as string) ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, use_case_name: e.target.value })
                      }
                    />
                    <textarea
                      className="w-full text-sm rounded-md border border-input bg-background px-3 py-2 min-h-[60px]"
                      placeholder="Description"
                      value={(draft.description as string) ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, description: e.target.value })
                      }
                    />
                    <div className="grid grid-cols-4 gap-2">
                      <Input
                        placeholder="Department"
                        value={(draft.department as string) ?? ""}
                        onChange={(e) =>
                          setDraft({ ...draft, department: e.target.value })
                        }
                      />
                      <Input
                        placeholder="Category"
                        value={(draft.category as string) ?? ""}
                        onChange={(e) =>
                          setDraft({ ...draft, category: e.target.value })
                        }
                      />
                      <SelectInput
                        value={(draft.priority as string) ?? "Medium"}
                        onChange={(v) => setDraft({ ...draft, priority: v })}
                        options={["High", "Medium", "Low"]}
                      />
                      <SelectInput
                        value={(draft.status as string) ?? "not_started"}
                        onChange={(v) => setDraft({ ...draft, status: v })}
                        options={[...USE_CASE_STATUS_ORDER]}
                        labelFor={(s) =>
                          USE_CASE_STATUS_LABEL[s as UseCaseStatus]
                        }
                      />
                    </div>
                    <Input
                      placeholder="Estimated value (USD)"
                      type="number"
                      value={
                        (draft.estimated_value_usd as number | undefined) ?? ""
                      }
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          estimated_value_usd: e.target.value
                            ? Number(e.target.value)
                            : null,
                        })
                      }
                    />
                    <Input
                      placeholder="Business value"
                      value={(draft.business_value as string) ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, business_value: e.target.value })
                      }
                    />
                    <textarea
                      className="w-full text-sm rounded-md border border-input bg-background px-3 py-2 min-h-[40px]"
                      placeholder="Value rationale"
                      value={(draft.value_rationale as string) ?? ""}
                      onChange={(e) =>
                        setDraft({ ...draft, value_rationale: e.target.value })
                      }
                    />
                    <textarea
                      className="w-full text-sm rounded-md border border-input bg-background px-3 py-2 min-h-[60px]"
                      placeholder="Data requirements (one per line)"
                      value={drText}
                      onChange={(e) => setDrText(e.target.value)}
                    />
                    <CrudButtons
                      onSave={() =>
                        saveMut.mutate({
                          id: uc.id,
                          body: {
                            use_case_name: draft.use_case_name,
                            description: draft.description,
                            department: draft.department,
                            category: draft.category,
                            priority: draft.priority,
                            status: draft.status,
                            business_value: draft.business_value,
                            estimated_value_usd: draft.estimated_value_usd,
                            value_rationale: draft.value_rationale,
                            data_requirements: parseDr(drText),
                          },
                        })
                      }
                      onCancel={() => setEditing(null)}
                      saving={saveMut.isPending}
                    />
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium">{uc.use_case_name}</span>
                        {uc.priority && (
                          <Badge
                            variant={
                              uc.priority === "High"
                                ? "destructive"
                                : uc.priority === "Low"
                                  ? "secondary"
                                  : "default"
                            }
                            className="text-[10px]"
                          >
                            {uc.priority}
                          </Badge>
                        )}
                        <Badge
                          variant="outline"
                          className={
                            "text-[10px] " +
                            (uc.status === "delivered"
                              ? "border-emerald-500 text-emerald-700"
                              : uc.status === "in_progress"
                                ? "border-sky-500 text-sky-700"
                                : uc.status === "on_hold"
                                  ? "border-slate-400 text-slate-600"
                                  : "border-amber-500 text-amber-700")
                          }
                        >
                          {USE_CASE_STATUS_LABEL[
                            (uc.status as UseCaseStatus) || "not_started"
                          ]}
                        </Badge>
                        {uc.is_user_edited && <EditedBadge />}
                      </div>
                      {uc.description && (
                        <p className="text-xs text-muted-foreground mt-1">
                          {uc.description}
                        </p>
                      )}
                      <div className="text-[11px] text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
                        {uc.department && <span>{uc.department}</span>}
                        {uc.category && (
                          <>
                            <span>·</span>
                            <span>{uc.category}</span>
                          </>
                        )}
                        {uc.estimated_value_usd != null && (
                          <>
                            <span>·</span>
                            <span className="font-semibold text-foreground">
                              ${(uc.estimated_value_usd / 1e6).toFixed(1)}M
                            </span>
                          </>
                        )}
                      </div>
                      {uc.business_value && (
                        <p className="text-[11px] text-muted-foreground mt-1">
                          <strong>Value:</strong> {uc.business_value}
                        </p>
                      )}
                      {(uc.data_requirements ?? []).length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {(uc.data_requirements ?? []).map((d) => (
                            <Badge
                              key={d}
                              variant="outline"
                              className="text-[10px]"
                            >
                              {d}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>
                    <RowActions
                      onEdit={() => {
                        setEditing(uc.id);
                        setDraft({
                          use_case_name: uc.use_case_name ?? "",
                          description: uc.description ?? "",
                          department: uc.department ?? "",
                          category: uc.category ?? "",
                          priority: uc.priority ?? "Medium",
                          status: uc.status ?? "not_started",
                          business_value: uc.business_value ?? "",
                          estimated_value_usd: uc.estimated_value_usd ?? null,
                          value_rationale: uc.value_rationale ?? "",
                        });
                        setDrText((uc.data_requirements ?? []).join("\n"));
                      }}
                      onDelete={() => {
                        if (
                          confirm(
                            `Delete "${uc.use_case_name}" and all its derived AI mappings?`,
                          )
                        ) {
                          deleteMut.mutate(uc.id);
                        }
                      }}
                    />
                  </div>
                )}
              </CardContent>
            </Card>
          ),
        )}
      </div>
    </div>
  );
}

// ===========================================================================
// USE CASE -> AFFILIATES (AI mapping with manual override)
// ===========================================================================

function UseCaseAffiliatesEditor() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["edit-uc-affiliates"],
    queryFn: () => fetchEditUseCaseAffiliates(),
  });
  const { data: affiliates } = useQuery({
    queryKey: ["edit-affiliates"],
    queryFn: fetchEditAffiliates,
  });
  const { data: useCases } = useQuery({
    queryKey: ["useCases"],
    queryFn: () => fetchUseCases(),
  });

  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<{
    use_case_id?: string;
    affiliate_name?: string;
    applicability?: string;
    rationale?: string;
  }>({ applicability: "primary" });
  const [filterMode, setFilterMode] = useState<"all" | "manual" | "ai">("all");

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.toLowerCase();
    return data.filter((r) => {
      if (filterMode === "manual" && !r.is_user_edited) return false;
      if (filterMode === "ai" && r.is_user_edited) return false;
      if (!q) return true;
      return (
        (r.use_case_name ?? "").toLowerCase().includes(q) ||
        r.affiliate_name.toLowerCase().includes(q)
      );
    });
  }, [data, search, filterMode]);

  // Group by use case for sane scrolling.
  const grouped = useMemo(() => {
    const g: Record<string, EditUseCaseAffiliate[]> = {};
    for (const r of filtered) {
      const key = `${r.use_case_id}|||${r.use_case_name ?? ""}`;
      g[key] = g[key] || [];
      g[key].push(r);
    }
    return Object.entries(g);
  }, [filtered]);

  const upsertMut = useMutation({
    mutationFn: upsertEditUseCaseAffiliate,
    onSuccess: () => {
      toast.success("Mapping saved (manual override)");
      qc.invalidateQueries({ queryKey: ["edit-uc-affiliates"] });
      setCreating(false);
      setDraft({ applicability: "primary" });
    },
    onError: (e: Error) => toast.error(`Save failed: ${e.message}`),
  });

  const deleteMut = useMutation({
    mutationFn: ({
      use_case_id,
      affiliate_name,
    }: {
      use_case_id: string;
      affiliate_name: string;
    }) => deleteEditUseCaseAffiliate(use_case_id, affiliate_name),
    onSuccess: () => {
      toast.success("Mapping deleted");
      qc.invalidateQueries({ queryKey: ["edit-uc-affiliates"] });
    },
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  });

  return (
    <div className="space-y-3">
      <Toolbar
        search={search}
        onSearch={setSearch}
        onAdd={() => setCreating(true)}
        addLabel="Add manual mapping"
      >
        <FilterChips
          value={filterMode}
          onChange={setFilterMode}
          options={[
            { value: "all", label: `All (${data?.length ?? 0})` },
            {
              value: "manual",
              label: `Manual (${data?.filter((r) => r.is_user_edited).length ?? 0})`,
            },
            {
              value: "ai",
              label: `AI only (${data?.filter((r) => !r.is_user_edited).length ?? 0})`,
            },
          ]}
        />
      </Toolbar>

      {creating && (
        <Card className="border-primary/40">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Manual mapping</CardTitle>
            <CardDescription className="text-[11px]">
              Upsert: if a row for this UC + affiliate exists, it becomes manual;
              otherwise a new manual row is inserted.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <SelectInput
              value={draft.use_case_id ?? ""}
              onChange={(v) => setDraft({ ...draft, use_case_id: v })}
              options={[
                "",
                ...((useCases ?? []) as Array<{ id: string; use_case_name: string }>).map((u) => u.id),
              ]}
              labelFor={(v) =>
                v
                  ? ((useCases ?? []) as Array<{ id: string; use_case_name: string }>).find((u) => u.id === v)?.use_case_name ?? v
                  : "Use case"
              }
            />
            <div className="grid grid-cols-2 gap-2">
              <SelectInput
                value={draft.affiliate_name ?? ""}
                onChange={(v) => setDraft({ ...draft, affiliate_name: v })}
                options={[
                  "",
                  ...(affiliates ?? []).map((a) => a.affiliate_name),
                ]}
                placeholder="Affiliate"
              />
              <SelectInput
                value={draft.applicability ?? "primary"}
                onChange={(v) => setDraft({ ...draft, applicability: v })}
                options={["primary", "secondary"]}
              />
            </div>
            <Input
              placeholder="Rationale"
              value={draft.rationale ?? ""}
              onChange={(e) => setDraft({ ...draft, rationale: e.target.value })}
            />
            <CrudButtons
              onSave={() =>
                upsertMut.mutate({
                  use_case_id: draft.use_case_id ?? "",
                  affiliate_name: draft.affiliate_name ?? "",
                  applicability: draft.applicability,
                  rationale: draft.rationale,
                })
              }
              onCancel={() => {
                setCreating(false);
                setDraft({ applicability: "primary" });
              }}
              saveDisabled={!draft.use_case_id || !draft.affiliate_name}
              saving={upsertMut.isPending}
            />
          </CardContent>
        </Card>
      )}

      {isLoading && <SkeletonRows />}
      {data && grouped.length === 0 && !isLoading && (
        <EmptyState text="No mappings match." />
      )}

      <div className="space-y-3">
        {grouped.map(([key, rows]) => {
          const [, name] = key.split("|||");
          return (
            <Card key={key}>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">
                  {name || (
                    <span className="text-muted-foreground">(no name)</span>
                  )}
                </CardTitle>
                <CardDescription className="text-[11px]">
                  {rows.length} affiliate{rows.length === 1 ? "" : "s"} mapped
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-1">
                {rows.map((r) => (
                  <div
                    key={`${r.use_case_id}|${r.affiliate_name}`}
                    className="flex items-center justify-between gap-3 border-b last:border-0 pb-1.5 last:pb-0"
                  >
                    <div className="flex-1 min-w-0 flex items-center gap-2 flex-wrap">
                      <Badge className="text-[10px]">{r.affiliate_name}</Badge>
                      <Badge variant="secondary" className="text-[10px]">
                        {r.applicability}
                      </Badge>
                      <Badge
                        variant={
                          r.mapped_by === "manual" ? "default" : "outline"
                        }
                        className="text-[10px]"
                      >
                        {r.mapped_by ?? "unknown"}
                      </Badge>
                      {r.is_user_edited && <EditedBadge />}
                      {r.rationale && (
                        <span className="text-[11px] text-muted-foreground ml-1">
                          {r.rationale}
                        </span>
                      )}
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                      onClick={() => {
                        if (
                          confirm(
                            `Delete ${r.affiliate_name} from this use case?`,
                          )
                        ) {
                          deleteMut.mutate({
                            use_case_id: r.use_case_id,
                            affiliate_name: r.affiliate_name,
                          });
                        }
                      }}
                      title="Delete"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

// ===========================================================================
// USE CASE -> SOURCE REQUIREMENTS (AI mapping with manual override)
// ===========================================================================

function UseCaseSourceReqsEditor() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["edit-uc-source-reqs"],
    queryFn: () => fetchEditUseCaseSourceRequirements(),
  });
  const { data: canonicals } = useQuery({
    queryKey: ["edit-canonical-sources"],
    queryFn: fetchEditCanonicalSources,
  });
  const { data: useCases } = useQuery({
    queryKey: ["useCases"],
    queryFn: () => fetchUseCases(),
  });

  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<{
    use_case_id?: string;
    required_canonical?: string;
    necessity?: string;
    confidence?: string;
    data_need_excerpt?: string;
  }>({ necessity: "must_have", confidence: "high" });
  const [filterMode, setFilterMode] = useState<
    "all" | "manual" | "ai" | "must"
  >("all");

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.toLowerCase();
    return data.filter((r) => {
      if (filterMode === "manual" && !r.is_user_edited) return false;
      if (filterMode === "ai" && r.is_user_edited) return false;
      if (filterMode === "must" && r.necessity !== "must_have") return false;
      if (!q) return true;
      return (
        (r.use_case_name ?? "").toLowerCase().includes(q) ||
        r.required_canonical.toLowerCase().includes(q)
      );
    });
  }, [data, search, filterMode]);

  const grouped = useMemo(() => {
    const g: Record<string, EditUseCaseSourceRequirement[]> = {};
    for (const r of filtered) {
      const key = `${r.use_case_id}|||${r.use_case_name ?? ""}`;
      g[key] = g[key] || [];
      g[key].push(r);
    }
    return Object.entries(g);
  }, [filtered]);

  const upsertMut = useMutation({
    mutationFn: upsertEditUseCaseSourceRequirement,
    onSuccess: () => {
      toast.success("Requirement saved (manual override)");
      qc.invalidateQueries({ queryKey: ["edit-uc-source-reqs"] });
      setCreating(false);
      setDraft({ necessity: "must_have", confidence: "high" });
    },
    onError: (e: Error) => toast.error(`Save failed: ${e.message}`),
  });

  const deleteMut = useMutation({
    mutationFn: ({
      use_case_id,
      required_canonical,
    }: {
      use_case_id: string;
      required_canonical: string;
    }) => deleteEditUseCaseSourceRequirement(use_case_id, required_canonical),
    onSuccess: () => {
      toast.success("Requirement deleted");
      qc.invalidateQueries({ queryKey: ["edit-uc-source-reqs"] });
    },
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  });

  return (
    <div className="space-y-3">
      <Toolbar
        search={search}
        onSearch={setSearch}
        onAdd={() => setCreating(true)}
        addLabel="Add manual requirement"
      >
        <FilterChips
          value={filterMode}
          onChange={setFilterMode}
          options={[
            { value: "all", label: `All (${data?.length ?? 0})` },
            {
              value: "manual",
              label: `Manual (${data?.filter((r) => r.is_user_edited).length ?? 0})`,
            },
            {
              value: "ai",
              label: `AI only (${data?.filter((r) => !r.is_user_edited).length ?? 0})`,
            },
            {
              value: "must",
              label: `Must-have only (${data?.filter((r) => r.necessity === "must_have").length ?? 0})`,
            },
          ]}
        />
      </Toolbar>

      {creating && (
        <Card className="border-primary/40">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Manual requirement</CardTitle>
            <CardDescription className="text-[11px]">
              Upsert: if a row for this UC + canonical exists, it becomes
              manual; otherwise a new manual row is inserted.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <SelectInput
              value={draft.use_case_id ?? ""}
              onChange={(v) => setDraft({ ...draft, use_case_id: v })}
              options={[
                "",
                ...((useCases ?? []) as Array<{ id: string; use_case_name: string }>).map((u) => u.id),
              ]}
              labelFor={(v) =>
                v
                  ? ((useCases ?? []) as Array<{ id: string; use_case_name: string }>).find((u) => u.id === v)?.use_case_name ?? v
                  : "Use case"
              }
            />
            <div className="grid grid-cols-3 gap-2">
              <SelectInput
                value={draft.required_canonical ?? ""}
                onChange={(v) =>
                  setDraft({ ...draft, required_canonical: v })
                }
                options={[
                  "",
                  ...(canonicals ?? [])
                    .filter((c) => c.is_active)
                    .map((c) => c.canonical),
                ]}
                placeholder="Canonical"
              />
              <SelectInput
                value={draft.necessity ?? "must_have"}
                onChange={(v) => setDraft({ ...draft, necessity: v })}
                options={["must_have", "nice_to_have"]}
              />
              <SelectInput
                value={draft.confidence ?? "high"}
                onChange={(v) => setDraft({ ...draft, confidence: v })}
                options={["high", "med", "low"]}
              />
            </div>
            <Input
              placeholder="Data-need excerpt (the phrase from the UC that maps to this source)"
              value={draft.data_need_excerpt ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, data_need_excerpt: e.target.value })
              }
            />
            <CrudButtons
              onSave={() =>
                upsertMut.mutate({
                  use_case_id: draft.use_case_id ?? "",
                  required_canonical: draft.required_canonical ?? "",
                  necessity: draft.necessity,
                  confidence: draft.confidence,
                  data_need_excerpt: draft.data_need_excerpt,
                })
              }
              onCancel={() => {
                setCreating(false);
                setDraft({ necessity: "must_have", confidence: "high" });
              }}
              saveDisabled={!draft.use_case_id || !draft.required_canonical}
              saving={upsertMut.isPending}
            />
          </CardContent>
        </Card>
      )}

      {isLoading && <SkeletonRows />}
      {data && grouped.length === 0 && !isLoading && (
        <EmptyState text="No requirements match." />
      )}

      <div className="space-y-3">
        {grouped.map(([key, rows]) => {
          const [, name] = key.split("|||");
          return (
            <Card key={key}>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">
                  {name || (
                    <span className="text-muted-foreground">(no name)</span>
                  )}
                </CardTitle>
                <CardDescription className="text-[11px]">
                  {rows.length} canonical source
                  {rows.length === 1 ? "" : "s"} required
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-1">
                {rows.map((r) => (
                  <div
                    key={`${r.use_case_id}|${r.required_canonical}`}
                    className="flex items-center justify-between gap-3 border-b last:border-0 pb-1.5 last:pb-0"
                  >
                    <div className="flex-1 min-w-0 flex items-center gap-2 flex-wrap">
                      <Badge className="text-[10px]">
                        {r.required_canonical}
                      </Badge>
                      <Badge
                        variant={
                          r.necessity === "must_have"
                            ? "destructive"
                            : "secondary"
                        }
                        className="text-[10px]"
                      >
                        {r.necessity}
                      </Badge>
                      <Badge variant="outline" className="text-[10px]">
                        {r.confidence}
                      </Badge>
                      <Badge
                        variant={
                          r.mapped_by === "manual" ? "default" : "outline"
                        }
                        className="text-[10px]"
                      >
                        {r.mapped_by ?? "unknown"}
                      </Badge>
                      {r.is_user_edited && <EditedBadge />}
                      {r.data_need_excerpt && (
                        <span className="text-[11px] text-muted-foreground ml-1 truncate">
                          “{r.data_need_excerpt}”
                        </span>
                      )}
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                      onClick={() => {
                        if (
                          confirm(
                            `Delete requirement ${r.required_canonical}?`,
                          )
                        ) {
                          deleteMut.mutate({
                            use_case_id: r.use_case_id,
                            required_canonical: r.required_canonical,
                          });
                        }
                      }}
                      title="Delete"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

// ===========================================================================
// Shared UI primitives
// ===========================================================================

function Toolbar({
  search,
  onSearch,
  onAdd,
  addLabel,
  children,
}: {
  search: string;
  onSearch: (v: string) => void;
  onAdd?: () => void;
  addLabel?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative flex-1 min-w-[220px] max-w-md">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          type="search"
          placeholder="Search..."
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          className="pl-8 h-9"
        />
      </div>
      {children}
      {onAdd && (
        <Button size="sm" onClick={onAdd} className="ml-auto">
          <Plus className="h-3.5 w-3.5 mr-1" />
          {addLabel ?? "Add"}
        </Button>
      )}
    </div>
  );
}

function CrudButtons({
  onSave,
  onCancel,
  saveDisabled,
  saving,
}: {
  onSave: () => void;
  onCancel: () => void;
  saveDisabled?: boolean;
  saving?: boolean;
}) {
  return (
    <div className="flex gap-2">
      <Button
        size="sm"
        onClick={onSave}
        disabled={!!saveDisabled || !!saving}
      >
        <Save className="h-3.5 w-3.5 mr-1" />
        {saving ? "Saving..." : "Save"}
      </Button>
      <Button size="sm" variant="ghost" onClick={onCancel}>
        <X className="h-3.5 w-3.5 mr-1" /> Cancel
      </Button>
    </div>
  );
}

function RowActions({
  onEdit,
  onDelete,
  deleteDisabled,
}: {
  onEdit: () => void;
  onDelete?: () => void;
  deleteDisabled?: boolean;
}) {
  return (
    <div className="flex gap-1 items-center flex-shrink-0">
      <Button
        variant="ghost"
        size="sm"
        className="h-7 w-7 p-0"
        onClick={onEdit}
        title="Edit"
      >
        <PenSquare className="h-3.5 w-3.5" />
      </Button>
      {onDelete && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0 text-destructive hover:text-destructive"
          onClick={onDelete}
          disabled={deleteDisabled}
          title="Delete"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      )}
    </div>
  );
}

function EditedBadge() {
  return (
    <Badge
      variant="default"
      className="text-[10px] bg-amber-100 text-amber-900 hover:bg-amber-100 dark:bg-amber-950 dark:text-amber-200"
      title="Manually edited; preserved across pipeline runs"
    >
      <ShieldCheck className="h-3 w-3 mr-1" />
      edited
    </Badge>
  );
}

function ToggleField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 h-9 px-3 rounded-md border border-input bg-background text-sm cursor-pointer">
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  );
}

function SelectInput({
  value,
  onChange,
  options,
  placeholder,
  labelFor,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  placeholder?: string;
  labelFor?: (v: string) => string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="h-9 rounded-md border border-input bg-background px-3 text-sm w-full"
    >
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt === "" ? (placeholder ?? "—") : labelFor ? labelFor(opt) : opt}
        </option>
      ))}
    </select>
  );
}

function FilterChips<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
}) {
  return (
    <div className="flex items-center gap-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={`h-8 px-3 rounded-full text-xs border transition ${
            value === o.value
              ? "bg-primary text-primary-foreground border-primary"
              : "bg-background hover:bg-muted"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function SkeletonRows() {
  return (
    <div className="space-y-2">
      {[1, 2, 3, 4].map((i) => (
        <Skeleton key={i} className="h-16 w-full" />
      ))}
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <Card>
      <CardContent className="p-6 text-sm text-muted-foreground flex items-center justify-center gap-2">
        <AlertCircle className="h-4 w-4" />
        {text}
      </CardContent>
    </Card>
  );
}
