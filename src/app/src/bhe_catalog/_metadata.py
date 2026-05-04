from pathlib import Path

app_name = "bhe-catalog"
app_entrypoint = "bhe_catalog.backend.app:app"
app_slug = "bhe_catalog"
api_prefix = "/api"
dist_dir = Path(__file__).parent / "__dist__"