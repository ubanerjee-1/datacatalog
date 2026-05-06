from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional
from .. import __version__


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls):
        return cls(version=__version__)


# --- Catalog Models ---

class SchemaOut(BaseModel):
    catalog_name: str
    schema_name: str
    schema_owner: str = ""
    comment: str = ""
    created: str = ""
    last_altered: str = ""
    workspace_url: str = ""
    environment: str = ""
    zone: str = ""
    program: str = ""
    classification: str = ""
    ai_definition: str = ""
    business_friendly_name: str = ""
    suggested_department: str = ""
    suggested_domain: str = ""
    data_sensitivity: str = ""
    is_user_edited: bool = False


class SchemaUpdateIn(BaseModel):
    ai_definition: Optional[str] = None
    business_friendly_name: Optional[str] = None
    suggested_department: Optional[str] = None
    suggested_domain: Optional[str] = None
    data_sensitivity: Optional[str] = None


class TableOut(BaseModel):
    table_catalog: str
    table_schema: str
    table_name: str
    table_type: str = ""
    table_owner: str = ""
    comment: str = ""
    created: str = ""
    last_altered: str = ""
    data_source_format: str = ""
    classification: str = ""
    ai_definition: str = ""
    business_friendly_name: str = ""
    is_user_edited: bool = False


class TableUpdateIn(BaseModel):
    ai_definition: Optional[str] = None
    business_friendly_name: Optional[str] = None


# --- Stats Models ---

class CatalogStatsOut(BaseModel):
    total_catalogs: int = 0
    total_schemas: int = 0
    # `enrichable_schemas` is the count of PRODUCTION schemas -- the actual
    # universe the AI enrichment job operates on. We keep `total_schemas` as
    # the full count (incl. DEV/QA/SBX) so the "Schemas" stat tells the truth
    # about the catalog size, but use `enrichable_schemas` as the denominator
    # for AI Coverage so the % reflects what the job can really cover.
    enrichable_schemas: int = 0
    total_tables: int = 0
    enrichable_tables: int = 0
    enriched_schemas: int = 0
    enriched_tables: int = 0
    environments: list[dict] = Field(default_factory=list)
    domains: list[dict] = Field(default_factory=list)
    table_types: list[dict] = Field(default_factory=list)
    departments: list[dict] = Field(default_factory=list)


# --- Sankey Models ---

class SankeyNodeOut(BaseModel):
    id: str
    name: str
    category: str
    level: int
    color: str = ""
    metadata: dict = Field(default_factory=dict)


class SankeyLinkOut(BaseModel):
    source: str
    target: str
    value: int = 1
    color: str = ""
    relevance: str = ""


class SankeyDataOut(BaseModel):
    nodes: list[SankeyNodeOut]
    links: list[SankeyLinkOut]
    metadata: dict = Field(default_factory=dict)


class SankeyMappingIn(BaseModel):
    source_system: str
    source_category: str = ""
    use_case: str
    department: str
    entity_name: str = ""
    relevance: str = "Secondary"


class SankeyMappingUpdateIn(BaseModel):
    source_system: Optional[str] = None
    source_category: Optional[str] = None
    use_case: Optional[str] = None
    department: Optional[str] = None
    entity_name: Optional[str] = None
    relevance: Optional[str] = None


# --- Company Models ---

class CompanyResearchIn(BaseModel):
    company_name: str
    reset: bool = False
    steps: list[str] | None = None
    force: bool = False


class CompanyProfileOut(BaseModel):
    id: str = ""
    company_name: str = ""
    industry: str = ""
    sub_industry: str = ""
    description: str = ""
    headquarters: str = ""
    key_business_units: list[str] = Field(default_factory=list)
    strategic_priorities: list[str] = Field(default_factory=list)
    regulatory_environment: str = ""
    catalog_name: str = ""
    logo_url: str = ""
    primary_domain: str = ""
    branding_user_edited: bool = False


class BrandingOut(BaseModel):
    """Minimal payload for the top-bar Logo component. Cacheable, always 200."""
    catalog_name: str = ""
    logo_url: str = ""
    has_uploaded_logo: bool = False


class BrandingUpdateIn(BaseModel):
    catalog_name: Optional[str] = None
    logo_url: Optional[str] = None


class DepartmentOut(BaseModel):
    id: str = ""
    department_name: str = ""
    description: str = ""
    key_functions: list[str] = Field(default_factory=list)
    data_needs: str = ""
    is_user_edited: bool = False


class DepartmentUpdateIn(BaseModel):
    department_name: Optional[str] = None
    description: Optional[str] = None
    data_needs: Optional[str] = None


"""Delivery-lifecycle vocabulary for a use case.

- not_started : pure opportunity; data-readiness gaps reflect investment needed
- in_progress : actively being built or piloted; value is "in flight"
- delivered   : productionized; its estimated value is now realized
- on_hold     : intentionally deferred (dependency blocked, repriori​tized, etc.)
"""
USE_CASE_STATUSES: tuple[str, ...] = (
    "not_started",
    "in_progress",
    "delivered",
    "on_hold",
)


class UseCaseOut(BaseModel):
    id: str = ""
    use_case_name: str = ""
    description: str = ""
    department: str = ""
    category: str = ""
    business_value: str = ""
    estimated_value_usd: Optional[float] = None
    value_rationale: str = ""
    data_requirements: list[str] = Field(default_factory=list)
    priority: str = "Medium"
    status: str = "not_started"
    status_notes: str = ""
    status_updated_at: Optional[str] = None
    is_user_edited: bool = False
    # PR 2 generation lens fields. NULL on rows created before the
    # generator was wired up (chat-create + bulk company_research),
    # which the UI renders as "manual" lens / no extra badges.
    affiliate: Optional[str] = None
    lens: Optional[str] = None
    time_horizon: Optional[str] = None
    value_type: Optional[str] = None
    is_regulatory: Optional[bool] = None
    required_canonicals: list[str] = Field(default_factory=list)


class UseCaseUpdateIn(BaseModel):
    use_case_name: Optional[str] = None
    description: Optional[str] = None
    department: Optional[str] = None
    category: Optional[str] = None
    business_value: Optional[str] = None
    estimated_value_usd: Optional[float] = None
    value_rationale: Optional[str] = None
    priority: Optional[str] = None
    data_requirements: Optional[list[str]] = None
    status: Optional[str] = None
    status_notes: Optional[str] = None


class UseCaseCreateIn(BaseModel):
    use_case_name: str
    description: str = ""
    department: str = ""
    category: str = ""
    business_value: str = ""
    estimated_value_usd: Optional[float] = None
    value_rationale: str = ""
    priority: str = "Medium"
    data_requirements: list[str] = Field(default_factory=list)
    status: str = "not_started"
    status_notes: str = ""


class UseCaseStatusIn(BaseModel):
    """Dedicated payload for PATCH /company/use-cases/{id}/status.

    Keeping this separate from UseCaseUpdateIn lets the UI ship a single-purpose
    "mark delivered" affordance without having to echo every other field.
    """
    status: str
    status_notes: Optional[str] = None


# --- Structured Use Case generation (PR 2 of UC redesign) ---
# These models drive the on-demand generator on the new /use-cases page.
# Generation is decoupled from `company_research` (which stays as the bulk
# day-zero seeder) so users can iterate on individual (affiliate, dept, lens)
# slices without re-running the whole research pipeline.

class UseCaseGenerateIn(BaseModel):
    """Request body for ``POST /api/use-cases/generate`` (dry-run preview).

    The result is cached in-process keyed by a returned ``preview_id`` so the
    user can review/edit the candidates before committing — see
    ``UseCaseGenerateCommitIn``. This avoids paying for the LLM twice.
    """
    affiliate: str
    department: str
    count: int = 5  # 1..20
    lens: str = "ready"  # ready | gap | both
    # Optional steering. ``any`` = no bias (default; LLM picks balanced mix).
    time_horizon: str = "any"  # any | quick_win | strategic
    value_type: str = "any"    # any | cost | revenue | risk
    prioritize_regulatory: bool = False
    # When ``None`` the backend uses canonicals already mapped to the
    # affiliate via the program -> affiliate -> tables join. When provided,
    # the explicit list overrides discovery (still validated against
    # ``gold.source_system_canonical``).
    canonical_filter: Optional[list[str]] = None


class UseCaseCandidate(BaseModel):
    """A single LLM-proposed use case, before it lands in `silver.use_cases`.

    The ``candidate_id`` is a stable hash of ``(name, affiliate, department)``
    so the commit step can reference candidates without echoing the whole
    payload, and so re-running the same generation won't yield duplicates
    once committed.
    """
    candidate_id: str
    use_case_name: str
    description: str = ""
    department: str = ""
    affiliate: str = ""
    business_value: str = ""
    estimated_value_usd: Optional[float] = None
    value_rationale: str = ""
    priority: str = "Medium"
    category: str = ""
    lens: str = "ready"  # ready | gap
    time_horizon: Optional[str] = None
    value_type: Optional[str] = None
    is_regulatory: bool = False
    data_requirements: list[str] = Field(default_factory=list)
    required_canonicals: list[str] = Field(default_factory=list)


class UseCaseGenerateOut(BaseModel):
    """Preview response. ``preview_id`` is short-lived (~10 minutes)."""
    preview_id: str
    affiliate: str
    department: str
    lens: str
    candidates: list[UseCaseCandidate]
    # Surfaces context the LLM used so the UI can show provenance:
    canonicals_present: list[str] = Field(default_factory=list)
    canonicals_missing: list[str] = Field(default_factory=list)
    table_sample_count: int = 0
    expires_at: Optional[str] = None


class UseCaseGenerateCommitIn(BaseModel):
    """Persist a subset of a previewed batch into ``silver.use_cases``."""
    preview_id: str
    selected_ids: list[str] = Field(default_factory=list)


class UseCaseGenerateCommitOut(BaseModel):
    inserted: int
    skipped: int
    use_case_ids: list[str] = Field(default_factory=list)


# --- Program Discovery (LLM-assisted catalog -> program -> affiliate mapping) ---
# Closes the upstream gap that makes `lens=ready` UC generation useless: with
# zero `category=program` rules in `classification_rules`, every schema lands
# in `silver_schemas.program='Other'/'Unknown'` and the affiliate-to-canonical
# join (used by _resolve_canonicals_for_affiliate) returns nothing. This
# discovery flow looks at the top catalog prefixes in `silver_schemas`, the
# closed list of affiliates from `gold.affiliates`, and the company profile,
# then asks the LLM to propose (catalog_pattern, program_name, affiliate_name)
# triples in one shot. Commit writes both `classification_rules` rows
# (category='program') and `program_affiliate_map` rows, then can auto-fire
# populate-gold so silver_schemas.program backfills.

class ProgramDiscoveryProposal(BaseModel):
    """A single LLM-proposed catalog -> program -> affiliate mapping."""
    proposal_id: str  # stable hash of catalog_pattern; idempotent across runs
    catalog_pattern: str  # bare prefix, e.g. "pacmdl" (no glob)
    program_name: str  # human-friendly label, e.g. "PacifiCorp Meter Data"
    affiliate_name: str  # must be in gold.affiliates closed vocab
    sample_catalogs: list[str] = Field(default_factory=list)
    schema_count: int = 0
    confidence: str = "medium"  # high | medium | low
    rationale: str = ""


class ProgramsDiscoverIn(BaseModel):
    """Request body for ``POST /api/programs/discover`` (dry-run preview).

    The LLM uses the top ``top_n`` catalog prefixes by schema count plus the
    full affiliate list to propose mappings. Defaults are tuned to fit in
    one prompt without truncation while still covering the long tail.
    """
    top_n: int = 25  # 5..50; how many prefixes to consider
    min_schema_count: int = 3  # ignore one-off prefixes


class ProgramsDiscoverOut(BaseModel):
    """Preview response. ``preview_id`` is short-lived (~10 minutes)."""
    preview_id: str
    proposals: list[ProgramDiscoveryProposal]
    company_name: str = ""
    affiliates_considered: list[str] = Field(default_factory=list)
    expires_at: Optional[str] = None


class ProgramsDiscoverCommitIn(BaseModel):
    """Persist selected proposals into ``classification_rules`` +
    ``program_affiliate_map``. Optional ``edits`` lets the UI override
    program_name / affiliate_name / catalog_pattern per proposal_id before
    writing.

    The server cache (keyed by ``preview_id``) is in-process, so when the app
    runs with ``--workers > 1`` the discover and commit calls may hit
    different workers and the cache lookup misses. To make the flow robust
    across restarts and worker splits, the client SHOULD also send back the
    full ``proposals`` payload it received from /programs/discover; the
    backend prefers the cached entry when present and falls back to this
    inline payload otherwise.
    """
    preview_id: str
    selected_ids: list[str] = Field(default_factory=list)
    edits: dict[str, dict] = Field(default_factory=dict)
    run_populate_gold: bool = True
    proposals: Optional[list[ProgramDiscoveryProposal]] = None


class ProgramsDiscoverCommitOut(BaseModel):
    rules_inserted: int
    rules_skipped: int
    maps_inserted: int
    maps_skipped: int
    populate_gold_run_id: Optional[str] = None


# --- Edit Center: bhe_gold dimension tables ---

class AffiliateUpsertIn(BaseModel):
    affiliate_name: str
    affiliate_code: Optional[str] = None
    business_type: Optional[str] = None
    region: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True


class AffiliateUpdateIn(BaseModel):
    affiliate_code: Optional[str] = None
    business_type: Optional[str] = None
    region: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class CanonicalSourceUpsertIn(BaseModel):
    canonical: str
    category: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True


class CanonicalSourceUpdateIn(BaseModel):
    category: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ProgramAffiliateMapUpsertIn(BaseModel):
    program: str
    affiliate_name: str
    affiliation_strength: str = "primary"
    notes: Optional[str] = None


class ProgramAffiliateMapUpdateIn(BaseModel):
    affiliation_strength: Optional[str] = None
    notes: Optional[str] = None


class UseCaseAffiliateUpsertIn(BaseModel):
    use_case_id: str
    affiliate_name: str
    applicability: str = "primary"  # primary | secondary
    rationale: Optional[str] = None


class UseCaseSourceRequirementUpsertIn(BaseModel):
    use_case_id: str
    required_canonical: str
    necessity: str = "must_have"  # must_have | nice_to_have
    data_need_excerpt: Optional[str] = None
    confidence: str = "high"  # high | med | low


class UseCaseEntityUpsertIn(BaseModel):
    use_case_id: str
    use_case_name: str
    entity_name: str
    entity_type: Optional[str] = None
    description: Optional[str] = None
    is_matched: bool = False
    matched_source: Optional[str] = None


class UseCaseEntityOut(BaseModel):
    entity_id: str = ""
    use_case_id: str = ""
    use_case_name: str = ""
    entity_name: str = ""
    entity_type: str = ""
    description: str = ""
    is_matched: bool = False
    matched_source: str = ""


# --- Job Models ---

class JobTriggerOut(BaseModel):
    run_id: str
    job_id: str
    status: str = "QUEUED"


class JobStatusOut(BaseModel):
    run_id: str
    status: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    error: Optional[str] = None


# --- Analytics Models ---

class SourceSummaryRow(BaseModel):
    program: str = ""
    affiliate: str = ""
    dev_schemas: int = 0
    qa_schemas: int = 0
    prod_schemas: int = 0
    dev_tables: int = 0
    qa_tables: int = 0
    prod_tables: int = 0
    total_tables: int = 0
    consistency_score: float = 0.0
    schemas_only_dev: str = "[]"
    schemas_only_qa: str = "[]"
    schemas_only_prod: str = "[]"


class WorkspaceSummaryRow(BaseModel):
    workspace_id: str = ""
    workspace_url: str = ""
    workspace_name: str = ""
    affiliates: str = ""
    programs: str = ""
    environments: str = ""
    catalog_count: int = 0
    schema_count: int = 0
    table_count: int = 0


class EnvConsistencyRow(BaseModel):
    program: str = ""
    affiliate: str = ""
    schema_name: str = ""
    in_dev: bool = False
    in_qa: bool = False
    in_prod: bool = False
    dev_tables: int = 0
    qa_tables: int = 0
    prod_tables: int = 0
    issue_type: str = ""


class SchemaInventoryRow(BaseModel):
    schema_key: str = ""
    workspace_name: str = ""
    catalog_name: str = ""
    schema_name: str = ""
    program: str = ""
    affiliate: str = ""
    environment: str = ""
    zone: str = ""
    classification: str = ""
    table_count: int = 0
    view_count: int = 0
    definition: str = ""
    business_name: str = ""
    source_system: str = ""
    data_domain: str = ""
    department_owner: str = ""
    sensitivity: str = ""
    data_quality_tier: str = ""
    is_user_edited: bool = False


# --- Taxonomy Models ---

TAXONOMY_DIMENSIONS = [
    "category", "department", "data_domain", "integration_pattern",
    "criticality", "vendor_type", "industry_vertical", "use_case",
]

TAXONOMY_ALLOWED_VALUES: dict[str, list[str]] = {
    "category": [
        "Analytics", "Asset Management", "CRM", "Compliance",
        "Customer Operations", "Cybersecurity", "Data Integration",
        "Data Warehouse", "Document Management", "ERP",
        "Energy Trading", "Finance", "GIS", "Grid Operations",
        "HR", "IT Service Management", "Metering",
        "Smart Metering", "Work Management",
    ],
    "department": [
        "Asset Management", "Customer Service", "Energy Trading",
        "Finance", "Grid Operations", "HR", "IT/Data Engineering",
        "Legal", "Operations", "Risk Management",
    ],
    "data_domain": [
        "Asset", "Customer", "Energy Trading", "Financial",
        "Grid", "HR", "Infrastructure", "Metering",
        "Operational", "Weather",
    ],
    "integration_pattern": [
        "API Integration", "Batch", "Delta Sharing", "File-based",
        "Legacy", "Real-time Replication", "Streaming",
    ],
    "criticality": [
        "T1 - Mission Critical", "T2 - Important", "T3 - Supporting",
    ],
    "vendor_type": [
        "Cloud SaaS", "Commercial COTS", "Delta Share Provider",
        "Internal", "Open Source", "Oracle Database",
    ],
    "industry_vertical": [
        "Energy", "Finance", "General", "Utilities",
    ],
    "use_case": [
        "Asset Performance", "Billing", "Customer Relationship Management",
        "Data Replication", "Energy Trading", "Grid Monitoring",
        "Load Analytics", "Meter-to-Cash", "Regulatory Compliance",
        "Weather Forecasting", "Workforce Management",
    ],
}


class TaxonomyRow(BaseModel):
    taxonomy_id: str = ""
    schema_key: str = ""
    dimension: str = ""
    value: str = ""
    source: str = ""
    confidence: Optional[float] = None
    ai_reasoning: str = ""
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    created_by: str = ""
    created_at: Optional[str] = None


class TaxonomyUpdateIn(BaseModel):
    value: str
    created_by: str = "user"


# --- BI & AI Artifacts ---

ARTIFACT_TYPES: tuple[str, ...] = (
    "BI Report",
    "Dashboard",
    "Genie Space",
    "AI Agent",
    "ML Model Endpoint",
    "Notebook",
    "Other",
)

ARTIFACT_STATUSES: tuple[str, ...] = (
    "Active",
    "Draft",
    "Under Review",
    "Deprecated",
)

ARTIFACT_ACCESS_LEVELS: tuple[str, ...] = (
    "Public",
    "Restricted",
    "Confidential",
)

ARTIFACT_REFRESH_FREQUENCIES: tuple[str, ...] = (
    "Real-time",
    "Hourly",
    "Daily",
    "Weekly",
    "Monthly",
    "On-demand",
    "None",
)


class ArtifactOut(BaseModel):
    artifact_id: str = ""
    artifact_name: str = ""
    artifact_type: str = ""
    description: str = ""
    platform: str = ""
    business_owner: str = ""
    business_team: str = ""
    technical_owner: str = ""
    access_level: str = ""
    location_url: str = ""
    workspace_name: str = ""
    folder_path: str = ""
    topics: str = ""
    affiliate: str = ""
    data_domain: str = ""
    department: str = ""
    use_case_id: str = ""
    status: str = ""
    refresh_frequency: str = ""
    last_refreshed: str = ""
    created_date: str = ""
    last_modified: str = ""
    certified: bool = False
    source_schemas: str = ""
    source_tables: str = ""
    ai_summary: str = ""
    ai_suggested_tags: str = ""
    ai_data_quality_notes: str = ""
    is_user_edited: bool = False
    enriched_at: str = ""
    ingested_at: str = ""
    updated_at: str = ""
    ingested_by: str = ""


class ArtifactUpdateIn(BaseModel):
    artifact_name: Optional[str] = None
    artifact_type: Optional[str] = None
    description: Optional[str] = None
    platform: Optional[str] = None
    business_owner: Optional[str] = None
    business_team: Optional[str] = None
    technical_owner: Optional[str] = None
    access_level: Optional[str] = None
    location_url: Optional[str] = None
    workspace_name: Optional[str] = None
    folder_path: Optional[str] = None
    topics: Optional[str] = None
    affiliate: Optional[str] = None
    data_domain: Optional[str] = None
    department: Optional[str] = None
    use_case_id: Optional[str] = None
    status: Optional[str] = None
    refresh_frequency: Optional[str] = None
    last_refreshed: Optional[str] = None
    created_date: Optional[str] = None
    last_modified: Optional[str] = None
    certified: Optional[bool] = None
    source_schemas: Optional[str] = None
    source_tables: Optional[str] = None


class ArtifactCreateIn(BaseModel):
    """Body for POST /artifacts (manual single-record entry, B-016).

    artifact_name + platform are required (used to derive a deterministic
    artifact_id so re-creating an existing artifact upserts in place).
    Everything else is optional and matches ArtifactUpdateIn.
    """
    artifact_name: str
    platform: str
    artifact_type: Optional[str] = "BI Report"
    description: Optional[str] = None
    business_owner: Optional[str] = None
    business_team: Optional[str] = None
    technical_owner: Optional[str] = None
    access_level: Optional[str] = None
    location_url: Optional[str] = None
    workspace_name: Optional[str] = None
    folder_path: Optional[str] = None
    topics: Optional[str] = None
    affiliate: Optional[str] = None
    data_domain: Optional[str] = None
    department: Optional[str] = None
    use_case_id: Optional[str] = None
    status: Optional[str] = "Active"
    refresh_frequency: Optional[str] = None
    last_refreshed: Optional[str] = None
    created_date: Optional[str] = None
    certified: Optional[bool] = False
    source_schemas: Optional[str] = None
    source_tables: Optional[str] = None


class ArtifactStatsOut(BaseModel):
    total: int = 0
    certified: int = 0
    stale: int = 0
    by_type: list[dict] = Field(default_factory=list)
    by_platform: list[dict] = Field(default_factory=list)
    by_team: list[dict] = Field(default_factory=list)
    by_status: list[dict] = Field(default_factory=list)
    by_domain: list[dict] = Field(default_factory=list)


class ArtifactFiltersOut(BaseModel):
    platforms: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    affiliates: list[str] = Field(default_factory=list)


# --- Knowledge Articles ---

KNOWLEDGE_NODE_TYPES: tuple[str, ...] = ("folder", "article")
KNOWLEDGE_CONTENT_FORMATS: tuple[str, ...] = ("markdown", "pdf", "docx")
KNOWLEDGE_LINK_TARGETS: tuple[str, ...] = (
    "catalog",
    "schema",
    "table",
    "artifact",
    "use_case",
    "department",
    "page",
)


class KnowledgeNodeOut(BaseModel):
    """A folder or an article in the knowledge tree.

    `node_id` is a stable UUID — safe to use as a foreign key from anywhere
    else in the app (cross-app linking phase). `volume_path` and content
    fields are only populated for articles.
    """
    node_id: str = ""
    parent_id: str = ""
    node_type: str = "folder"
    title: str = ""
    summary: str = ""
    content_format: str = ""
    volume_path: str = ""
    original_filename: str = ""
    mime_type: str = ""
    file_size_bytes: int = 0
    tags: list[str] = Field(default_factory=list)
    sort_order: int = 0
    version: int = 1
    created_by: str = ""
    updated_by: str = ""
    created_at: str = ""
    updated_at: str = ""


class KnowledgeArticleContentOut(BaseModel):
    """Article metadata + the body to render.

    For markdown: ``body_markdown`` is set.
    For pdf/docx: ``raw_url`` points at /api/knowledge/articles/{id}/raw and
    the client embeds (pdf) or downloads (docx).
    """
    node: KnowledgeNodeOut
    body_markdown: str = ""
    raw_url: str = ""


class KnowledgeFolderCreateIn(BaseModel):
    title: str
    parent_id: Optional[str] = None
    summary: Optional[str] = None


class KnowledgeArticleCreateIn(BaseModel):
    title: str
    parent_id: Optional[str] = None
    content_md: str = ""
    summary: Optional[str] = None
    tags: Optional[str] = None  # comma-separated


class KnowledgeNodeUpdateIn(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    parent_id: Optional[str] = None  # use to move the node
    content_md: Optional[str] = None  # markdown articles only
    tags: Optional[str] = None
    sort_order: Optional[int] = None


class KnowledgeLinkOut(BaseModel):
    link_id: str
    node_id: str
    target_type: str
    target_key: str
    created_by: str = ""
    created_at: str = ""


class KnowledgeLinkCreateIn(BaseModel):
    node_id: str
    target_type: str
    target_key: str


class ProposalGenerateIn(BaseModel):
    """Input for generating a KB proposal article from a use case.

    ``regenerate=True`` overwrites the existing linked article (incrementing
    its version) rather than creating a duplicate.
    """
    additional_context: str = ""
    regenerate: bool = False
