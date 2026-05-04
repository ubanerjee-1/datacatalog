import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  fetchRules,
  createRule,
  updateRule,
  deleteRule,
  testRules,
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
  Settings2,
  Plus,
  Trash2,
  Save,
  TestTube,
  Power,
  PowerOff,
  X,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/rules")({
  component: RulesPage,
});

const CATEGORY_INFO: Record<
  string,
  { title: string; desc: string; fields: string[] }
> = {
  program: {
    title: "Programs",
    desc: "Map catalog prefixes to program names and affiliates. Pattern = catalog prefix (e.g. 'pacxedam').",
    fields: ["affiliate"],
  },
  zone: {
    title: "Data Zones",
    desc: "Map catalog zone suffixes to zone types and medallion layers (raw → bronze, standardized → silver, published → gold).",
    fields: ["layer"],
  },
  environment: {
    title: "Environments",
    desc: "Map environment codes in catalog names (e.g. 'dev02', 'qa02', 'prod02') to environment labels.",
    fields: [],
  },
  ignore_catalog: {
    title: "Ignore Catalogs",
    desc: "Catalog name patterns to exclude. Use * as wildcard (e.g. '__databricks_internal_*', 'samples').",
    fields: [],
  },
  ignore_schema: {
    title: "Ignore Schemas",
    desc: "Schema name patterns to exclude (e.g. 'information_schema', 'migration_*', 'default').",
    fields: [],
  },
  federated_source: {
    title: "Federated Sources",
    desc: "Catalog patterns for federated/external data sources (e.g. '*_oracle', '*_fivetran').",
    fields: [],
  },
};

function RulesPage() {
  const queryClient = useQueryClient();
  const [activeCategory, setActiveCategory] = useState("program");
  const [testCatalog, setTestCatalog] = useState("");
  const [testSchema, setTestSchema] = useState("default");
  const [testResult, setTestResult] = useState<any>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["rules"],
    queryFn: () => fetchRules(),
  });

  const testMutation = useMutation({
    mutationFn: () => testRules(testCatalog, testSchema),
    onSuccess: (data) => setTestResult(data),
  });

  const allRules = data?.rules || [];
  const categoryRules = allRules.filter(
    (r: any) => r.category === activeCategory,
  );
  const info = CATEGORY_INFO[activeCategory] || {
    title: activeCategory,
    desc: "",
    fields: [],
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Settings2 className="h-6 w-6" />
          Classification Rules
        </h1>
        <p className="text-muted-foreground">
          Configure how catalog and schema names are parsed into programs,
          environments, zones, and affiliates
        </p>
      </div>

      {/* Test Panel */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm flex items-center gap-2">
            <TestTube className="h-4 w-4" />
            Test Rules
          </CardTitle>
          <CardDescription>
            Enter a catalog name to see how rules parse it
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex gap-2 items-end">
            <div className="flex-1 space-y-1">
              <label className="text-xs text-muted-foreground">
                Catalog Name
              </label>
              <Input
                placeholder="e.g. pacxedam_qa02_standardized"
                value={testCatalog}
                onChange={(e) => setTestCatalog(e.target.value)}
              />
            </div>
            <div className="w-48 space-y-1">
              <label className="text-xs text-muted-foreground">
                Schema Name
              </label>
              <Input
                placeholder="e.g. pac_pub_db"
                value={testSchema}
                onChange={(e) => setTestSchema(e.target.value)}
              />
            </div>
            <Button
              onClick={() => testMutation.mutate()}
              disabled={!testCatalog || testMutation.isPending}
              size="sm"
            >
              <TestTube className="h-4 w-4 mr-1" />
              Test
            </Button>
          </div>
          {testResult?.parsed && (
            <div className="mt-3 p-3 bg-muted/50 rounded-lg text-sm space-y-1">
              {testResult.parsed.skip ? (
                <p className="text-amber-500 font-medium">
                  Skipped by ignore rule
                </p>
              ) : (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                  {Object.entries(testResult.parsed)
                    .filter(([k]) => k !== "skip")
                    .map(([k, v]) => (
                      <div key={k}>
                        <span className="text-muted-foreground text-xs">
                          {k}:
                        </span>
                        <span className="ml-1 font-mono text-xs">
                          {String(v) || "—"}
                        </span>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Category Tabs + Rules */}
      <div className="flex gap-4">
        <div className="w-48 space-y-1">
          {Object.entries(CATEGORY_INFO).map(([cat, info]) => (
            <button
              key={cat}
              onClick={() => setActiveCategory(cat)}
              className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                activeCategory === cat
                  ? "bg-primary text-primary-foreground"
                  : "hover:bg-muted"
              }`}
            >
              <div className="font-medium">{info.title}</div>
              <div className="text-[10px] opacity-70">
                {allRules.filter((r: any) => r.category === cat).length} rules
              </div>
            </button>
          ))}
        </div>

        <div className="flex-1">
          <Card>
            <CardHeader className="pb-3">
              <div className="flex justify-between items-start">
                <div>
                  <CardTitle className="text-lg">{info.title}</CardTitle>
                  <CardDescription>{info.desc}</CardDescription>
                </div>
                <AddRuleButton
                  category={activeCategory}
                  extraFields={info.fields}
                  onAdded={() =>
                    queryClient.invalidateQueries({ queryKey: ["rules"] })
                  }
                />
              </div>
            </CardHeader>
            <CardContent>
              {isLoading ? (
                <div className="text-muted-foreground text-sm py-4 text-center">
                  Loading...
                </div>
              ) : categoryRules.length === 0 ? (
                <div className="text-muted-foreground text-sm py-8 text-center">
                  No rules in this category yet.
                </div>
              ) : (
                <div className="space-y-2">
                  {categoryRules.map((rule: any) => (
                    <RuleRow
                      key={rule.rule_id}
                      rule={rule}
                      extraFields={info.fields}
                      onUpdated={() =>
                        queryClient.invalidateQueries({ queryKey: ["rules"] })
                      }
                    />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rule Row Component
// ---------------------------------------------------------------------------

function RuleRow({
  rule,
  extraFields,
  onUpdated,
}: {
  rule: any;
  extraFields: string[];
  onUpdated: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({
    pattern: rule.pattern || "",
    label: rule.label || "",
    description: rule.description || "",
    metadata: rule.metadata || {},
  });

  const updateMut = useMutation({
    mutationFn: (updates: any) => updateRule(rule.rule_id, updates),
    onSuccess: () => {
      setEditing(false);
      onUpdated();
    },
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteRule(rule.rule_id),
    onSuccess: onUpdated,
  });

  const toggleMut = useMutation({
    mutationFn: () =>
      updateRule(rule.rule_id, { is_active: !rule.is_active }),
    onSuccess: onUpdated,
  });

  if (editing) {
    return (
      <div className="border rounded-lg p-3 space-y-2 bg-muted/20">
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-xs text-muted-foreground">Pattern</label>
            <Input
              value={form.pattern}
              onChange={(e) => setForm({ ...form, pattern: e.target.value })}
              className="h-8 text-sm font-mono"
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Label</label>
            <Input
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              className="h-8 text-sm"
            />
          </div>
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Description</label>
          <Input
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            className="h-8 text-sm"
          />
        </div>
        {extraFields.map((field) => (
          <div key={field}>
            <label className="text-xs text-muted-foreground capitalize">
              {field}
            </label>
            <Input
              value={form.metadata[field] || ""}
              onChange={(e) =>
                setForm({
                  ...form,
                  metadata: { ...form.metadata, [field]: e.target.value },
                })
              }
              className="h-8 text-sm"
            />
          </div>
        ))}
        <div className="flex gap-1">
          <Button
            size="sm"
            onClick={() => updateMut.mutate(form)}
            disabled={updateMut.isPending}
          >
            <Save className="h-3 w-3 mr-1" />
            Save
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setEditing(false)}
          >
            <X className="h-3 w-3 mr-1" />
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div
      className={`border rounded-lg p-3 flex items-center gap-3 group ${!rule.is_active ? "opacity-50" : ""}`}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <code className="text-sm font-mono bg-muted px-1.5 py-0.5 rounded">
            {rule.pattern}
          </code>
          <span className="text-sm">→</span>
          <span className="text-sm font-medium">{rule.label}</span>
          {extraFields.map(
            (f) =>
              rule.metadata?.[f] && (
                <Badge key={f} variant="outline" className="text-[10px]">
                  {f}: {rule.metadata[f]}
                </Badge>
              ),
          )}
          {!rule.is_active && (
            <Badge variant="secondary" className="text-[10px]">
              disabled
            </Badge>
          )}
        </div>
        {rule.description && (
          <p className="text-xs text-muted-foreground mt-0.5 truncate">
            {rule.description}
          </p>
        )}
      </div>
      <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={() => toggleMut.mutate()}
          title={rule.is_active ? "Disable" : "Enable"}
        >
          {rule.is_active ? (
            <PowerOff className="h-3 w-3" />
          ) : (
            <Power className="h-3 w-3" />
          )}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0"
          onClick={() => setEditing(true)}
          title="Edit"
        >
          <Settings2 className="h-3 w-3" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0 text-destructive"
          onClick={() => {
            if (confirm("Delete this rule?")) deleteMut.mutate();
          }}
          title="Delete"
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add Rule Button / Dialog
// ---------------------------------------------------------------------------

function AddRuleButton({
  category,
  extraFields,
  onAdded,
}: {
  category: string;
  extraFields: string[];
  onAdded: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    pattern: "",
    label: "",
    description: "",
    metadata: {} as Record<string, string>,
  });

  const mutation = useMutation({
    mutationFn: () =>
      createRule({
        category,
        pattern: form.pattern,
        label: form.label,
        description: form.description,
        metadata: form.metadata,
      }),
    onSuccess: () => {
      setOpen(false);
      setForm({ pattern: "", label: "", description: "", metadata: {} });
      onAdded();
    },
  });

  if (!open) {
    return (
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Plus className="h-3 w-3 mr-1" />
        Add Rule
      </Button>
    );
  }

  return (
    <div className="border rounded-lg p-3 space-y-2 w-80">
      <p className="text-sm font-medium">New {category} rule</p>
      <Input
        placeholder="Pattern (e.g. pacxedam)"
        value={form.pattern}
        onChange={(e) => setForm({ ...form, pattern: e.target.value })}
        className="h-8 text-sm font-mono"
      />
      <Input
        placeholder="Label (e.g. PacifiCorp EDAM)"
        value={form.label}
        onChange={(e) => setForm({ ...form, label: e.target.value })}
        className="h-8 text-sm"
      />
      <Input
        placeholder="Description (optional)"
        value={form.description}
        onChange={(e) => setForm({ ...form, description: e.target.value })}
        className="h-8 text-sm"
      />
      {extraFields.map((field) => (
        <Input
          key={field}
          placeholder={`${field} (e.g. PacifiCorp)`}
          value={form.metadata[field] || ""}
          onChange={(e) =>
            setForm({
              ...form,
              metadata: { ...form.metadata, [field]: e.target.value },
            })
          }
          className="h-8 text-sm"
        />
      ))}
      <div className="flex gap-1">
        <Button
          size="sm"
          onClick={() => mutation.mutate()}
          disabled={!form.pattern || !form.label || mutation.isPending}
        >
          <Plus className="h-3 w-3 mr-1" />
          Create
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setOpen(false)}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
