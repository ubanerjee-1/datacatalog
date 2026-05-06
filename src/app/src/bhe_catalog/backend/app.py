from .core import create_app
from .router import reconcile_stale_runs, router

app = create_app(routers=[router])


@app.on_event("startup")
async def _reconcile_orphan_runs() -> None:
    """B-009: mark stranded RUNNING rows in `bhe_silver.job_runs` as FAILED.

    Inline-thread jobs (research, enrichment, populate-gold, taxonomy)
    lose their thread when the app process restarts, but their durable
    run-state row stays RUNNING forever without this. Best-effort: a
    failure here (e.g. table missing on a fresh deploy before bootstrap)
    must not block app startup.
    """
    try:
        reconcile_stale_runs()
    except Exception:
        pass
