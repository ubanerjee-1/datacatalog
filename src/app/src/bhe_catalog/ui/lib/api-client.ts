import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

// --- Setup / Onboarding ---

export interface SetupStatus {
  config: {
    catalog: string;
    raw_schema: string;
    silver_schema: string;
    gold_schema: string;
    warehouse_id: string;
    llm_endpoint: string;
    host: string;
  };
  config_check: { ok: boolean; message: string };
  identity: {
    type: "service_principal" | "user" | "unknown";
    user_name: string;
    display_name: string;
    client_id?: string;
    host: string;
    error?: string;
  };
  warehouse_access: { ok: boolean; message: string };
  catalog_access: { ok: boolean; message: string };
  llm_access: { ok: boolean; message: string };
  schemas: {
    ok: boolean;
    message: string;
    state: Record<string, boolean>;
  };
  tables: {
    ok: boolean;
    silver_present: string[];
    silver_missing: string[];
    gold_present: string[];
    gold_missing: string[];
  };
  data: { ok: boolean; counts: Record<string, number> };
  company: { present: boolean; company_name: string };
  genie: {
    deployable: boolean;
    deployed: boolean;
    space_id: string;
    url: string;
    source: "" | "app_config" | "env";
  };
  grants_sql: string[];
  is_setup_ready: boolean;
  is_data_ready: boolean;
  is_complete: boolean;
}

export interface GenieDeployResult {
  space_id: string;
  mode: "created" | "updated";
  tables: number;
  example_questions: number;
  url: string;
}

export async function fetchSetupStatus(): Promise<SetupStatus> {
  const { data } = await api.get("/setup/status");
  return data;
}

export async function bootstrapSchemas() {
  const { data } = await api.post("/setup/bootstrap-schemas");
  return data;
}

export async function bootstrapTables() {
  const { data } = await api.post("/setup/bootstrap-tables");
  return data;
}

export async function deployGenieSpace(
  options?: { forceNew?: boolean },
): Promise<GenieDeployResult> {
  const { data } = await api.post("/setup/deploy-genie-space", null, {
    params: options?.forceNew ? { force_new: true } : undefined,
  });
  return data;
}

export async function nukeSetup(confirmCatalog: string) {
  const { data } = await api.post("/setup/nuke", null, {
    params: { confirm_catalog: confirmCatalog },
  });
  return data;
}

// --- Catalog ---

export async function fetchCatalogStats() {
  const { data } = await api.get("/catalog/stats");
  return data;
}

export async function fetchSchemas(params: {
  domain?: string;
  environment?: string;
  department?: string;
  classification?: string;
  search?: string;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get("/catalog/schemas", { params });
  return data;
}

export async function fetchSchemaFilters() {
  const { data } = await api.get("/catalog/schemas/filters");
  return data;
}

export async function fetchTables(
  catalogName: string,
  schemaName: string,
  params?: { search?: string; limit?: number; offset?: number },
) {
  const { data } = await api.get(
    `/catalog/tables/${catalogName}/${schemaName}`,
    { params },
  );
  return data;
}

export async function updateSchema(
  catalogName: string,
  schemaName: string,
  body: Record<string, string>,
) {
  const { data } = await api.put(
    `/catalog/schemas/${catalogName}/${schemaName}`,
    body,
  );
  return data;
}

export async function updateTable(
  catalogName: string,
  schemaName: string,
  tableName: string,
  body: Record<string, string>,
) {
  const { data } = await api.put(
    `/catalog/tables/${catalogName}/${schemaName}/${tableName}`,
    body,
  );
  return data;
}

// --- Source Systems Browser ---

export async function fetchSourceSystems(params?: {
  search?: string;
  category?: string;
  include_empty?: boolean;
}) {
  const { data } = await api.get("/source-systems", { params });
  return data;
}

export async function fetchSourceSystemDetail(name: string) {
  const { data } = await api.get(`/source-systems/${encodeURIComponent(name)}`);
  return data;
}

export async function fetchSourceSystemTables(
  name: string,
  params?: {
    search?: string;
    catalog?: string;
    schema?: string;
    limit?: number;
    offset?: number;
  },
) {
  const { data } = await api.get(
    `/source-systems/${encodeURIComponent(name)}/tables`,
    { params },
  );
  return data;
}

// --- Value & Readiness ---

export type ValueFormula = "simple" | "must";

export type UseCaseStatus =
  | "not_started"
  | "in_progress"
  | "delivered"
  | "on_hold";

export const USE_CASE_STATUS_ORDER: UseCaseStatus[] = [
  "not_started",
  "in_progress",
  "delivered",
  "on_hold",
];

export const USE_CASE_STATUS_LABEL: Record<UseCaseStatus, string> = {
  not_started: "Not started",
  in_progress: "In progress",
  delivered: "Delivered",
  on_hold: "On hold",
};

export type ValueFilters = {
  affiliate?: string;
  priority?: string;
  status?: UseCaseStatus;
  department?: string;
  search?: string;
  formula?: ValueFormula;
};

export async function fetchValueAffiliates() {
  const { data } = await api.get("/value/affiliates");
  return data;
}

export async function fetchValueSummary(params?: ValueFilters) {
  const { data } = await api.get("/value/summary", { params });
  return data;
}

export async function fetchValueUseCases(
  params?: ValueFilters & { limit?: number },
) {
  const { data } = await api.get("/value/use-cases", { params });
  return data;
}

export async function fetchValueUseCaseDetail(
  useCaseId: string,
  params?: { affiliate?: string },
) {
  const { data } = await api.get(
    `/value/use-cases/${encodeURIComponent(useCaseId)}`,
    { params },
  );
  return data;
}

export async function fetchValueSourceRollup(
  params?: Omit<ValueFilters, "formula"> & { only_missing?: boolean },
) {
  const { data } = await api.get("/value/source-rollup", { params });
  return data;
}

export async function fetchValueSourceDetail(
  canonical: string,
  params?: { affiliate?: string },
) {
  const { data } = await api.get(
    `/value/source/${encodeURIComponent(canonical)}`,
    { params },
  );
  return data;
}

export async function fetchValueSankey(
  params?: ValueFilters & { top_use_cases?: number },
) {
  const { data } = await api.get("/value/sankey", { params });
  return data;
}

// --- Gaps ---

export interface GapsCanonical {
  name: string;
  category: string | null;
  affiliates_needing: number;
  affiliates_present: number;
  affiliates_gap: number;
  total_use_cases: number;
  total_must_links: number;
  total_value_affected: number;
}

export interface GapsAffiliate {
  affiliate_name: string;
  affiliate_code: string | null;
  business_type: string | null;
  region: string | null;
  description: string | null;
  required_count: number;
  present_count: number;
  gap_count: number;
  available_count: number;
}

export type GapsCellState = "covered" | "gap" | "available";

export interface GapsCell {
  canonical: string;
  affiliate: string;
  state: GapsCellState;
  uc_count: number;
  must_count: number;
  total_value: number;
  is_required: boolean;
  is_present: boolean;
}

export interface GapsMatrixResponse {
  canonicals: GapsCanonical[];
  affiliates: GapsAffiliate[];
  cells: GapsCell[];
  summary: {
    canonical_count: number;
    affiliate_count: number;
    gap_count: number;
    covered_count: number;
    available_count: number;
    total_gap_value: number;
  };
}

export async function fetchGapsMatrix(): Promise<GapsMatrixResponse> {
  const { data } = await api.get("/gaps/matrix");
  return data;
}

// --- Sankey ---

export async function fetchSankeyData(params?: {
  department?: string;
  use_case?: string;
  source?: string;
}) {
  const { data } = await api.get("/sankey/data", { params });
  return data;
}

export async function fetchSankeyFilters() {
  const { data } = await api.get("/sankey/filters");
  return data;
}

export async function createSankeyMapping(body: {
  source_system: string;
  source_category?: string;
  use_case: string;
  department: string;
  entity_name?: string;
  relevance?: string;
}) {
  const { data } = await api.post("/sankey/mappings", body);
  return data;
}

export async function updateSankeyMapping(
  id: string,
  body: Record<string, string>,
) {
  const { data } = await api.put(`/sankey/mappings/${id}`, body);
  return data;
}

export async function deleteSankeyMapping(id: string) {
  const { data } = await api.delete(`/sankey/mappings/${id}`);
  return data;
}

// --- Company ---

export async function fetchCompanyProfile() {
  const { data } = await api.get("/company/profile");
  return data;
}

// --- Branding ---

export interface Branding {
  catalog_name: string;
  logo_url: string;
  has_uploaded_logo: boolean;
}

export async function fetchBranding(): Promise<Branding> {
  const { data } = await api.get("/branding");
  return data;
}

export async function updateBranding(body: {
  catalog_name?: string;
  logo_url?: string;
}) {
  const { data } = await api.put("/company/branding", body);
  return data;
}

export async function uploadBrandingLogo(file: File) {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post("/branding/logo", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function deleteBrandingLogo() {
  const { data } = await api.delete("/branding/logo");
  return data;
}

export async function fetchDepartments() {
  const { data } = await api.get("/company/departments");
  return data;
}

export async function updateDepartment(
  id: string,
  body: Record<string, string>,
) {
  const { data } = await api.put(`/company/departments/${id}`, body);
  return data;
}

export async function fetchUseCases(params?: {
  department?: string;
  category?: string;
}) {
  const { data } = await api.get("/company/use-cases", { params });
  return data;
}

export async function updateUseCase(id: string, body: Record<string, unknown>) {
  const { data } = await api.put(`/company/use-cases/${id}`, body);
  return data;
}

export async function updateUseCaseStatus(
  id: string,
  body: { status: UseCaseStatus; status_notes?: string | null },
) {
  const { data } = await api.patch(
    `/company/use-cases/${encodeURIComponent(id)}/status`,
    body,
  );
  return data;
}

export async function createUseCase(body: {
  use_case_name: string;
  description?: string;
  department?: string;
  category?: string;
  business_value?: string;
  estimated_value_usd?: number | null;
  value_rationale?: string;
  priority?: string;
  data_requirements?: string[];
}) {
  const { data } = await api.post("/company/use-cases", body);
  return data;
}

export async function deleteUseCase(id: string) {
  const { data } = await api.delete(`/company/use-cases/${id}`);
  return data;
}

// --- Structured Use Case generation (PR 2 of UC redesign) ---

export type UseCaseLens = "ready" | "gap" | "both";
export type UseCaseGeneratedLens = "ready" | "gap";
export type UseCaseTimeHorizon = "any" | "quick_win" | "strategic";
export type UseCaseValueType = "any" | "cost" | "revenue" | "risk";

export interface UseCaseGenerateIn {
  affiliate: string;
  department: string;
  count: number;
  lens: UseCaseLens;
  time_horizon?: UseCaseTimeHorizon;
  value_type?: UseCaseValueType;
  prioritize_regulatory?: boolean;
  canonical_filter?: string[];
}

export interface UseCaseCandidate {
  candidate_id: string;
  use_case_name: string;
  description: string;
  department: string;
  affiliate: string;
  business_value: string;
  estimated_value_usd: number | null;
  value_rationale: string;
  priority: string;
  category: string;
  lens: UseCaseGeneratedLens;
  time_horizon: "quick_win" | "strategic" | null;
  value_type: "cost" | "revenue" | "risk" | "mixed" | null;
  is_regulatory: boolean;
  data_requirements: string[];
  required_canonicals: string[];
}

export interface UseCaseGenerateOut {
  preview_id: string;
  affiliate: string;
  department: string;
  lens: UseCaseLens;
  candidates: UseCaseCandidate[];
  canonicals_present: string[];
  canonicals_missing: string[];
  table_sample_count: number;
  expires_at: string | null;
}

export async function generateUseCases(
  body: UseCaseGenerateIn,
): Promise<UseCaseGenerateOut> {
  const { data } = await api.post("/use-cases/generate", body);
  return data;
}

export interface UseCaseGenerateCommitOut {
  inserted: number;
  skipped: number;
  use_case_ids: string[];
}

export async function commitGeneratedUseCases(
  preview_id: string,
  selected_ids: string[],
): Promise<UseCaseGenerateCommitOut> {
  const { data } = await api.post("/use-cases/generate/commit", {
    preview_id,
    selected_ids,
  });
  return data;
}

// --- Program discovery (catalog -> program -> affiliate via LLM) ---

export interface ProgramDiscoveryProposal {
  proposal_id: string;
  catalog_pattern: string;
  program_name: string;
  affiliate_name: string;
  sample_catalogs: string[];
  schema_count: number;
  confidence: "high" | "medium" | "low";
  rationale: string;
}

export interface ProgramsDiscoverOut {
  preview_id: string;
  proposals: ProgramDiscoveryProposal[];
  company_name: string;
  affiliates_considered: string[];
  expires_at: string | null;
}

export interface ProgramsDiscoverCommitOut {
  rules_inserted: number;
  rules_skipped: number;
  maps_inserted: number;
  maps_skipped: number;
  populate_gold_run_id: string | null;
}

export async function discoverPrograms(
  body: { top_n?: number; min_schema_count?: number } = {},
): Promise<ProgramsDiscoverOut> {
  const { data } = await api.post("/programs/discover", {
    top_n: body.top_n ?? 25,
    min_schema_count: body.min_schema_count ?? 3,
  });
  return data;
}

export async function commitDiscoveredPrograms(args: {
  preview_id: string;
  selected_ids: string[];
  // Optional — but the client SHOULD always send these back so the commit is
  // robust to in-process cache misses (e.g. when the app runs with
  // --workers > 1 and discover + commit hit different workers).
  proposals?: ProgramDiscoveryProposal[];
  edits?: Record<
    string,
    Partial<{
      catalog_pattern: string;
      program_name: string;
      affiliate_name: string;
    }>
  >;
  run_populate_gold?: boolean;
}): Promise<ProgramsDiscoverCommitOut> {
  const { data } = await api.post("/programs/discover/commit", {
    preview_id: args.preview_id,
    selected_ids: args.selected_ids,
    proposals: args.proposals ?? [],
    edits: args.edits ?? {},
    run_populate_gold: args.run_populate_gold ?? true,
  });
  return data;
}

export async function fetchEntities(params?: {
  use_case_name?: string;
  matched_only?: boolean;
}) {
  const { data } = await api.get("/company/entities", { params });
  return data;
}

// --- Edit Center: gold dim tables ---

export interface EditAffiliate {
  affiliate_name: string;
  affiliate_code: string | null;
  business_type: string | null;
  region: string | null;
  description: string | null;
  is_active: boolean;
  is_user_edited: boolean;
}

export async function fetchEditAffiliates(): Promise<EditAffiliate[]> {
  const { data } = await api.get("/edit/affiliates");
  return data;
}

export async function createEditAffiliate(body: Omit<EditAffiliate, "is_user_edited">) {
  const { data } = await api.post("/edit/affiliates", body);
  return data;
}

export async function updateEditAffiliate(
  affiliate_name: string,
  body: Partial<Omit<EditAffiliate, "affiliate_name" | "is_user_edited">>,
) {
  const { data } = await api.put(
    `/edit/affiliates/${encodeURIComponent(affiliate_name)}`,
    body,
  );
  return data;
}

export async function deleteEditAffiliate(affiliate_name: string) {
  const { data } = await api.delete(
    `/edit/affiliates/${encodeURIComponent(affiliate_name)}`,
  );
  return data;
}

export interface EditCanonicalSource {
  canonical: string;
  category: string | null;
  description: string | null;
  is_active: boolean;
}

export async function fetchEditCanonicalSources(): Promise<EditCanonicalSource[]> {
  const { data } = await api.get("/edit/canonical-sources");
  return data;
}

export async function createEditCanonicalSource(body: EditCanonicalSource) {
  const { data } = await api.post("/edit/canonical-sources", body);
  return data;
}

export async function updateEditCanonicalSource(
  canonical: string,
  body: Partial<Omit<EditCanonicalSource, "canonical">>,
) {
  const { data } = await api.put(
    `/edit/canonical-sources/${encodeURIComponent(canonical)}`,
    body,
  );
  return data;
}

export async function deleteEditCanonicalSource(canonical: string) {
  const { data } = await api.delete(
    `/edit/canonical-sources/${encodeURIComponent(canonical)}`,
  );
  return data;
}

export interface EditProgramAffiliateRow {
  program: string;
  affiliate_name: string;
  affiliation_strength: string | null;
  notes: string | null;
  is_user_edited: boolean;
}

export async function fetchEditProgramAffiliateMap(): Promise<EditProgramAffiliateRow[]> {
  const { data } = await api.get("/edit/program-affiliate-map");
  return data;
}

export async function createEditProgramAffiliateMap(body: {
  program: string;
  affiliate_name: string;
  affiliation_strength?: string;
  notes?: string;
}) {
  const { data } = await api.post("/edit/program-affiliate-map", body);
  return data;
}

export async function updateEditProgramAffiliateMap(
  program: string,
  affiliate_name: string,
  body: { affiliation_strength?: string; notes?: string },
) {
  const { data } = await api.put(
    `/edit/program-affiliate-map/${encodeURIComponent(program)}/${encodeURIComponent(affiliate_name)}`,
    body,
  );
  return data;
}

export async function deleteEditProgramAffiliateMap(
  program: string,
  affiliate_name: string,
) {
  const { data } = await api.delete(
    `/edit/program-affiliate-map/${encodeURIComponent(program)}/${encodeURIComponent(affiliate_name)}`,
  );
  return data;
}

// --- Edit Center: AI mappings (LLM-derived; manual edits are sticky) ---

export interface EditUseCaseAffiliate {
  use_case_id: string;
  use_case_name: string | null;
  affiliate_name: string;
  applicability: string | null;
  rationale: string | null;
  mapped_by: string | null;
  is_user_edited: boolean;
}

export async function fetchEditUseCaseAffiliates(
  params?: { use_case_id?: string },
): Promise<EditUseCaseAffiliate[]> {
  const { data } = await api.get("/edit/use-case-affiliates", { params });
  return data;
}

export async function upsertEditUseCaseAffiliate(body: {
  use_case_id: string;
  affiliate_name: string;
  applicability?: string;
  rationale?: string;
}) {
  const { data } = await api.post("/edit/use-case-affiliates", body);
  return data;
}

export async function deleteEditUseCaseAffiliate(
  use_case_id: string,
  affiliate_name: string,
) {
  const { data } = await api.delete(
    `/edit/use-case-affiliates/${encodeURIComponent(use_case_id)}/${encodeURIComponent(affiliate_name)}`,
  );
  return data;
}

export interface EditUseCaseSourceRequirement {
  use_case_id: string;
  use_case_name: string | null;
  required_canonical: string;
  necessity: string | null;
  data_need_excerpt: string | null;
  confidence: string | null;
  mapped_by: string | null;
  is_user_edited: boolean;
}

export async function fetchEditUseCaseSourceRequirements(
  params?: { use_case_id?: string },
): Promise<EditUseCaseSourceRequirement[]> {
  const { data } = await api.get("/edit/use-case-source-requirements", { params });
  return data;
}

export async function upsertEditUseCaseSourceRequirement(body: {
  use_case_id: string;
  required_canonical: string;
  necessity?: string;
  data_need_excerpt?: string;
  confidence?: string;
}) {
  const { data } = await api.post("/edit/use-case-source-requirements", body);
  return data;
}

export async function deleteEditUseCaseSourceRequirement(
  use_case_id: string,
  required_canonical: string,
) {
  const { data } = await api.delete(
    `/edit/use-case-source-requirements/${encodeURIComponent(use_case_id)}/${encodeURIComponent(required_canonical)}`,
  );
  return data;
}

// --- Edit Center: use case entities ---

export async function createUseCaseEntity(body: {
  use_case_id: string;
  use_case_name: string;
  entity_name: string;
  entity_type?: string;
  description?: string;
  is_matched?: boolean;
  matched_source?: string;
}) {
  const { data } = await api.post("/edit/use-case-entities", body);
  return data;
}

export async function deleteUseCaseEntity(entity_id: string) {
  const { data } = await api.delete(
    `/edit/use-case-entities/${encodeURIComponent(entity_id)}`,
  );
  return data;
}

export function downloadSchemaExtractor() {
  window.open("/api/tools/schema-extractor", "_blank");
}

// --- Jobs ---

export interface PipelineJobStatus {
  last_run: string | null;
  total_schemas?: number;
  enriched?: number;
  remaining?: number;
  classified?: number;
  status?: string;
  run_page_url?: string;
  job_id?: number;
}

export interface PipelineStatusResponse {
  populate_gold: PipelineJobStatus;
  enrich_schemas: PipelineJobStatus;
  generate_taxonomy: PipelineJobStatus;
  enrich_tables: PipelineJobStatus;
  normalize_sources: PipelineJobStatus;
  value_model: PipelineJobStatus;
  glossary: PipelineJobStatus;
}

export async function fetchPipelineStatus(): Promise<PipelineStatusResponse> {
  const { data } = await api.get("/jobs/pipeline-status");
  return data;
}

export async function triggerEnrichJob() {
  const { data } = await api.post("/jobs/enrich");
  return data;
}

export interface CompanyResearchOptions {
  reset?: boolean;
  steps?: string[];
  force?: boolean;
}

export async function triggerCompanyResearch(
  companyName: string,
  options: CompanyResearchOptions = {},
) {
  const { data } = await api.post("/jobs/company-research", {
    company_name: companyName,
    reset: options.reset ?? false,
    steps: options.steps,
    force: options.force ?? false,
  });
  return data;
}

export interface CompanyResearchStatus {
  state: "empty" | "partial" | "complete";
  counts: Record<string, number>;
  complete_steps: string[];
  missing_steps: string[];
  all_steps: string[];
}

export async function fetchCompanyResearchStatus(): Promise<CompanyResearchStatus> {
  const { data } = await api.get("/company/research-status");
  return data;
}

export async function fetchJobStatus(runId: string) {
  const { data } = await api.get(`/jobs/${runId}/status`);
  return data;
}

export async function fetchJobProgress(runId: string) {
  const { data } = await api.get(`/jobs/${runId}/progress`);
  return data;
}

export async function fetchActiveCompanyResearch() {
  const { data } = await api.get("/jobs/company-research/active");
  return data;
}

// --- File Upload & Ingest ---

export async function uploadFile(file: File, fileType: string) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("file_type", fileType);
  const { data } = await api.post("/upload/file", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function listUploadedFiles() {
  const { data } = await api.get("/upload/files");
  return data;
}

export async function ingestSchemas(filename?: string) {
  const { data } = await api.post("/ingest/schemas", null, {
    params: filename ? { filename } : {},
  });
  return data;
}

export async function ingestTables(filename?: string) {
  const { data } = await api.post("/ingest/tables", null, {
    params: filename ? { filename } : {},
  });
  return data;
}

// --- Gold Layer & Analytics ---

export async function triggerPopulateGold() {
  const { data } = await api.post("/jobs/populate-gold");
  return data;
}

export async function fetchSourceSummary() {
  const { data } = await api.get("/analytics/source-summary");
  return data;
}

export async function fetchWorkspaceSummary() {
  const { data } = await api.get("/analytics/workspace-summary");
  return data;
}

export async function fetchEnvConsistency(params?: {
  program?: string;
  issue_type?: string;
}) {
  const { data } = await api.get("/analytics/env-consistency", { params });
  return data;
}

export async function fetchSchemaInventory(params?: {
  program?: string;
  affiliate?: string;
  environment?: string;
  zone?: string;
  search?: string;
  enriched_only?: boolean;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get("/analytics/schema-inventory", { params });
  return data;
}

export async function fetchSchemaExplorer(params?: {
  program?: string;
  affiliate?: string;
  zone?: string;
  search?: string;
  enriched_only?: boolean;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get("/analytics/schema-explorer", { params });
  return data;
}

export async function fetchCatalogTree(params?: {
  search?: string;
  enriched_only?: boolean;
}) {
  const { data } = await api.get("/analytics/catalog-tree", { params });
  return data;
}

export async function fetchSchemaTables(params: {
  schema_name: string;
  search?: string;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get("/analytics/schema-tables", { params });
  return data;
}

export async function fetchSchemaTaxonomy(schema_name: string) {
  const { data } = await api.get("/analytics/schema-taxonomy", {
    params: { schema_name },
  });
  return data;
}

export async function triggerTableEnrichment(params?: {
  batch_size?: number;
  max_batches?: number;
}) {
  const { data } = await api.post("/jobs/enrich-tables", null, { params });
  return data;
}

export async function fetchTableEnrichmentStatus() {
  const { data } = await api.get("/jobs/enrich-tables/status");
  return data;
}

// --- Orphan-job triggers (B-008 + B-014) ---
// These three jobs ship with the bundle but historically had no UI
// button; users had to trigger them from Workflows. Wired into Step 7
// of the wizard in the strict dependency order required by B-014:
//   normalize-sources → value-model → glossary

export async function triggerNormalizeSourcesJob() {
  const { data } = await api.post("/jobs/normalize-sources/run");
  return data;
}

export async function fetchNormalizeSourcesStatus() {
  const { data } = await api.get("/jobs/normalize-sources/status");
  return data;
}

export async function triggerValueModelJob() {
  const { data } = await api.post("/jobs/value-model/run");
  return data;
}

export async function fetchValueModelStatus() {
  const { data } = await api.get("/jobs/value-model/status");
  return data;
}

export async function triggerGlossaryJob() {
  const { data } = await api.post("/jobs/glossary/run");
  return data;
}

export async function fetchGlossaryStatus() {
  const { data } = await api.get("/jobs/glossary/status");
  return data;
}

// --- Classification Rules ---

export async function fetchRules(category?: string) {
  const { data } = await api.get("/rules", {
    params: category ? { category } : {},
  });
  return data;
}

export async function createRule(rule: {
  rule_id?: string;
  category: string;
  pattern: string;
  label: string;
  description?: string;
  metadata?: Record<string, string>;
  is_active?: boolean;
  display_order?: number;
}) {
  const { data } = await api.post("/rules", rule);
  return data;
}

export async function updateRule(
  ruleId: string,
  updates: Partial<{
    pattern: string;
    label: string;
    description: string;
    category: string;
    metadata: Record<string, string>;
    is_active: boolean;
    display_order: number;
  }>,
) {
  const { data } = await api.put(`/rules/${ruleId}`, updates);
  return data;
}

export async function deleteRule(ruleId: string) {
  const { data } = await api.delete(`/rules/${ruleId}`);
  return data;
}

export async function testRules(catalogName: string, schemaName?: string) {
  const { data } = await api.post("/rules/test", {
    catalog_name: catalogName,
    schema_name: schemaName || "default",
  });
  return data;
}

// --- Source Taxonomy ---

export async function triggerTaxonomyGeneration() {
  const { data } = await api.post("/jobs/generate-taxonomy");
  return data;
}

export async function fetchTaxonomy(params?: {
  program?: string;
  affiliate?: string;
  environment?: string;
  search?: string;
  dim_filters?: string;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get("/taxonomy", { params });
  return data;
}

export async function fetchTaxonomyTables(params?: {
  search?: string;
  dim_filters?: string;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get("/taxonomy/tables", { params });
  return data;
}

export async function fetchTaxonomyPivot(params: {
  rows_dim: string;
  cols_dim: string;
  metric: string;
}) {
  const { data } = await api.get("/taxonomy/pivot", { params });
  return data;
}

export async function updateTaxonomy(
  schemaKey: string,
  dimension: string,
  value: string,
  createdBy?: string,
) {
  const { data } = await api.put(
    `/taxonomy/${encodeURIComponent(schemaKey)}/${dimension}`,
    { value, created_by: createdBy || "user" },
  );
  return data;
}

export async function fetchTaxonomyHistory(schemaKey: string) {
  const { data } = await api.get(
    `/taxonomy/${encodeURIComponent(schemaKey)}/history`,
  );
  return data;
}

export async function fetchTaxonomyInspection() {
  const { data } = await api.get("/taxonomy/inspect");
  return data;
}

export async function fetchTaxonomyAllowedValues() {
  const { data } = await api.get("/taxonomy/allowed-values");
  return data;
}

export async function triggerTaxonomyReprocessing() {
  const { data } = await api.post("/jobs/reprocess-taxonomy");
  return data;
}

// --- BI & AI Artifacts ---

export interface Artifact {
  artifact_id: string;
  artifact_name: string;
  artifact_type: string;
  description: string;
  platform: string;
  business_owner: string;
  business_team: string;
  technical_owner: string;
  access_level: string;
  location_url: string;
  workspace_name: string;
  folder_path: string;
  topics: string;
  affiliate: string;
  data_domain: string;
  department: string;
  use_case_id: string;
  status: string;
  refresh_frequency: string;
  last_refreshed: string;
  created_date: string;
  last_modified: string;
  certified: boolean;
  source_schemas: string;
  source_tables: string;
  ai_summary: string;
  ai_suggested_tags: string;
  ai_data_quality_notes: string;
  is_user_edited: boolean;
  enriched_at: string;
  ingested_at: string;
  updated_at: string;
  ingested_by: string;
}

export interface ArtifactListResponse {
  total: number;
  artifacts: Artifact[];
  limit: number;
  offset: number;
}

export interface ArtifactFilters {
  platforms: string[];
  types: string[];
  teams: string[];
  statuses: string[];
  domains: string[];
  departments: string[];
  affiliates: string[];
}

export interface ArtifactStats {
  total: number;
  certified: number;
  stale: number;
  by_type: { value: string; count: number }[];
  by_platform: { value: string; count: number }[];
  by_team: { value: string; count: number }[];
  by_status: { value: string; count: number }[];
  by_domain: { value: string; count: number }[];
}

export interface ArtifactVocabulary {
  types: string[];
  statuses: string[];
  access_levels: string[];
  refresh_frequencies: string[];
}

export async function fetchArtifacts(params?: {
  search?: string;
  platform?: string;
  type?: string;
  team?: string;
  status?: string;
  domain?: string;
  department?: string;
  certified?: boolean;
  sort_by?: string;
  sort_dir?: "asc" | "desc";
  limit?: number;
  offset?: number;
}): Promise<ArtifactListResponse> {
  const { data } = await api.get("/artifacts", { params });
  return data;
}

export async function fetchArtifactDetail(artifactId: string): Promise<Artifact> {
  const { data } = await api.get(`/artifacts/${encodeURIComponent(artifactId)}`);
  return data;
}

export async function fetchArtifactFilters(): Promise<ArtifactFilters> {
  const { data } = await api.get("/artifacts/filters");
  return data;
}

export async function fetchArtifactStats(): Promise<ArtifactStats> {
  const { data } = await api.get("/artifacts/stats");
  return data;
}

export async function fetchArtifactVocabulary(): Promise<ArtifactVocabulary> {
  const { data } = await api.get("/artifacts/vocabulary");
  return data;
}

export async function updateArtifact(
  artifactId: string,
  updates: Partial<Omit<Artifact, "artifact_id">>,
) {
  const { data } = await api.put(
    `/artifacts/${encodeURIComponent(artifactId)}`,
    updates,
  );
  return data;
}

export interface ArtifactCreate {
  artifact_name: string;
  platform: string;
  artifact_type?: string;
  description?: string;
  business_owner?: string;
  business_team?: string;
  technical_owner?: string;
  access_level?: string;
  location_url?: string;
  workspace_name?: string;
  folder_path?: string;
  topics?: string;
  affiliate?: string;
  data_domain?: string;
  department?: string;
  use_case_id?: string;
  status?: string;
  refresh_frequency?: string;
  last_refreshed?: string;
  created_date?: string;
  certified?: boolean;
  source_schemas?: string;
  source_tables?: string;
}

export async function createArtifact(payload: ArtifactCreate) {
  const { data } = await api.post(`/artifacts`, payload);
  return data as { status: string; artifact_id: string };
}

export async function deleteArtifact(artifactId: string) {
  const { data } = await api.delete(
    `/artifacts/${encodeURIComponent(artifactId)}`,
  );
  return data;
}

export async function ingestArtifacts(filename: string, replace = false) {
  const { data } = await api.post("/ingest/artifacts", null, {
    params: { filename, replace },
  });
  return data;
}

export async function triggerArtifactEnrichment(batchSize = 500) {
  const { data } = await api.post("/jobs/enrich-artifacts", null, {
    params: { batch_size: batchSize },
  });
  return data;
}

export async function populateArtifactSummary() {
  const { data } = await api.post("/jobs/populate-artifact-summary");
  return data;
}

// --- Knowledge Articles ---

export interface KnowledgeNode {
  node_id: string;
  parent_id: string;
  node_type: "folder" | "article";
  title: string;
  summary: string;
  content_format: "" | "markdown" | "pdf" | "docx";
  volume_path: string;
  original_filename: string;
  mime_type: string;
  file_size_bytes: number;
  tags: string[];
  sort_order: number;
  version: number;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeArticleContent {
  node: KnowledgeNode;
  body_markdown: string;
  raw_url: string;
}

export interface KnowledgeLink {
  link_id: string;
  node_id: string;
  target_type: string;
  target_key: string;
  created_by: string;
  created_at: string;
}

export async function fetchKnowledgeTree(): Promise<KnowledgeNode[]> {
  const { data } = await api.get("/knowledge/tree");
  return data;
}

export async function fetchKnowledgeArticle(
  nodeId: string,
): Promise<KnowledgeArticleContent> {
  const { data } = await api.get(`/knowledge/articles/${nodeId}`);
  return data;
}

export async function createKnowledgeFolder(body: {
  title: string;
  parent_id?: string;
  summary?: string;
}): Promise<KnowledgeNode> {
  const { data } = await api.post("/knowledge/folders", body);
  return data;
}

export async function createKnowledgeArticle(body: {
  title: string;
  parent_id?: string;
  content_md?: string;
  summary?: string;
  tags?: string;
}): Promise<KnowledgeNode> {
  const { data } = await api.post("/knowledge/articles", body);
  return data;
}

export async function uploadKnowledgeArticle(args: {
  file: File;
  title: string;
  parent_id?: string;
  summary?: string;
  tags?: string;
}): Promise<KnowledgeNode> {
  const form = new FormData();
  form.append("file", args.file);
  form.append("title", args.title);
  if (args.parent_id) form.append("parent_id", args.parent_id);
  if (args.summary) form.append("summary", args.summary);
  if (args.tags) form.append("tags", args.tags);
  const { data } = await api.post("/knowledge/articles/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function updateKnowledgeNode(
  nodeId: string,
  body: {
    title?: string;
    summary?: string;
    parent_id?: string;
    content_md?: string;
    tags?: string;
    sort_order?: number;
  },
): Promise<KnowledgeNode> {
  const { data } = await api.put(`/knowledge/nodes/${nodeId}`, body);
  return data;
}

export async function deleteKnowledgeNode(nodeId: string, hard = false) {
  const { data } = await api.delete(`/knowledge/nodes/${nodeId}`, {
    params: { hard },
  });
  return data;
}

export async function searchKnowledge(q: string): Promise<KnowledgeNode[]> {
  const { data } = await api.get("/knowledge/search", { params: { q } });
  return data;
}

export async function listKnowledgeLinks(params: {
  node_id?: string;
  target_type?: string;
  target_key?: string;
}): Promise<KnowledgeLink[]> {
  const { data } = await api.get("/knowledge/links", { params });
  return data;
}

export async function createKnowledgeLink(body: {
  node_id: string;
  target_type: string;
  target_key: string;
}): Promise<KnowledgeLink> {
  const { data } = await api.post("/knowledge/links", body);
  return data;
}

export async function deleteKnowledgeLink(linkId: string) {
  const { data } = await api.delete(`/knowledge/links/${linkId}`);
  return data;
}

// --- Use case proposal generator -------------------------------------------

export async function generateUseCaseProposal(
  useCaseId: string,
  body: { additional_context: string; regenerate: boolean },
): Promise<KnowledgeNode> {
  // LLM round-trip can take 20-40s; bump timeout above the default.
  const { data } = await api.post(
    `/knowledge/use-cases/${encodeURIComponent(useCaseId)}/generate-proposal`,
    body,
    { timeout: 120_000 },
  );
  return data;
}

export async function fetchUseCaseProposalLink(
  useCaseId: string,
): Promise<KnowledgeLink | null> {
  const links = await listKnowledgeLinks({
    target_type: "use_case",
    target_key: useCaseId,
  });
  return links.length > 0 ? links[0] : null;
}
