#!/usr/bin/env python3
"""Interactive deploy script for the BHE Data Catalog app.

Walks through every step needed to ship the app to a Databricks workspace:

  1. Prompts for target env (workspace URL, CLI profile, catalog, warehouse)
  2. Rewrites ``databricks.yml`` + ``src/app/app.yml`` with those values
  3. Builds the SPA (``npx vite build``)
  4. ``databricks bundle deploy`` (creates/updates app, jobs, pipelines)
  5. Reads the app's service principal from the deployed app resource
  6. Grants the SP ``USE_CATALOG`` + per-schema ``SELECT``/``MODIFY`` on the
     target catalog so it can actually query data
  7. ``databricks bundle run bhe_catalog_app`` (starts/restarts the app)

Re-running is safe: every mutation is idempotent. Values you accept from the
prompt get persisted to ``databricks.yml`` and ``src/app/app.yml`` so the next
invocation shows the last-used answers as the default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
DATABRICKS_YML = REPO_ROOT / "databricks.yml"
APP_YML = REPO_ROOT / "src" / "app" / "app.yml"
APP_DIR = REPO_ROOT / "src" / "app"
DIST_DIR = APP_DIR / "src" / "bhe_catalog" / "__dist__"
BUNDLE_APP_RESOURCE = "bhe_catalog_app"  # resource key in resources/*.yml


# ----------------------------------------------------------------------------
# tiny helpers
# ----------------------------------------------------------------------------

def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def info(msg: str) -> None:
    print(_c("36", "==>") + f" {msg}")


def warn(msg: str) -> None:
    print(_c("33", "WARN:") + f" {msg}")


def die(msg: str, code: int = 1) -> None:
    print(_c("31", "ERROR:") + f" {msg}", file=sys.stderr)
    sys.exit(code)


def _resolve_exe(name: str) -> str:
    """Resolve an executable name to its absolute path.

    Why: on Windows, tools like `npx`, `databricks`, and `npm` install as
    `.cmd` shims (e.g. `npx.cmd`, `databricks.cmd`). `subprocess.run([...])`
    without `shell=True` calls Win32 `CreateProcess`, which only resolves
    `.exe` from PATH — `.cmd`/`.bat`/`.ps1` are invisible to it. Pre-resolving
    via `shutil.which()` (which respects PATHEXT and finds the `.cmd`)
    sidesteps this without falling back to `shell=True` (avoids quoting bugs
    and command-injection foot-guns).

    On macOS / Linux this is effectively a no-op — `which` returns the same
    binary the kernel would have found anyway.
    """
    resolved = shutil.which(name)
    return resolved or name  # let subprocess raise the original error if absent


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess with nice logging. Raises on non-zero unless check=False."""
    if not cmd:
        die("run() called with empty command")
    # Resolve the program (cmd[0]) but keep the printable form using the
    # short name — full Windows paths look noisy in logs.
    resolved_cmd = [_resolve_exe(cmd[0]), *cmd[1:]]
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    info(f"$ {printable}")
    result = subprocess.run(
        resolved_cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        capture_output=capture,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        if capture:
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
        die(f"command failed with exit {result.returncode}", code=result.returncode)
    return result


def prompt(question: str, default: str | None = None, *, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{question}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw or not required:
            return raw
        print("  (required)")


# ----------------------------------------------------------------------------
# YAML surgery (line-oriented — keeps comments + key ordering intact)
# ----------------------------------------------------------------------------

def _normalize_host(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"
    return url


def update_databricks_yml(target: str, host: str, profile: str, catalog: str, warehouse_id: str) -> None:
    """Rewrite the target block's host/profile and the per-target ``catalog`` var."""
    text = DATABRICKS_YML.read_text()
    lines = text.splitlines()

    # 1) Top-level default for `catalog` and `warehouse_id` — keep them in sync
    #    with the last deploy so a plain `databricks bundle deploy` still DTRT.
    def _set_default(varname: str, value: str) -> None:
        nonlocal lines
        # Find block `  <varname>:` then the `    default: "..."` child
        for i, line in enumerate(lines):
            m = re.match(rf"^(\s*){re.escape(varname)}:\s*$", line)
            if not m:
                continue
            indent = m.group(1)
            for j in range(i + 1, min(i + 8, len(lines))):
                dm = re.match(rf"^{indent}\s+default:\s*.*$", lines[j])
                if dm:
                    lines[j] = f'{indent}  default: "{value}"'
                    return

    _set_default("catalog", catalog)
    _set_default("warehouse_id", warehouse_id)

    # 2) Target block: rewrite workspace.host, workspace.profile and the
    #    per-target variable override for `catalog`.
    target_re = re.compile(rf"^(\s*){re.escape(target)}:\s*$")
    in_target = False
    target_indent = ""
    new_lines: list[str] = []
    i = 0
    patched = {"host": False, "profile": False, "catalog": False}
    while i < len(lines):
        line = lines[i]
        if not in_target:
            m = target_re.match(line)
            if m:
                in_target = True
                target_indent = m.group(1)
            new_lines.append(line)
            i += 1
            continue

        # We're inside the target block. Stop when we hit a line at the same
        # or lesser indent than the target header (next target or top-level).
        if line.strip() and not line.startswith(target_indent + " "):
            in_target = False
            new_lines.append(line)
            i += 1
            continue

        stripped = line.strip()
        if stripped.startswith("host:"):
            new_lines.append(re.sub(r"host:\s*.*", f"host: {host}", line))
            patched["host"] = True
        elif stripped.startswith("profile:"):
            new_lines.append(re.sub(r"profile:\s*.*", f"profile: {profile}", line))
            patched["profile"] = True
        elif stripped.startswith("catalog:") and "variables" not in stripped:
            # per-target variable: e.g. `      catalog: "foo"`
            new_lines.append(re.sub(r'catalog:\s*.*', f'catalog: "{catalog}"', line))
            patched["catalog"] = True
        else:
            new_lines.append(line)
        i += 1

    missing = [k for k, v in patched.items() if not v]
    if missing:
        warn(
            f"Could not locate {missing} under target `{target}` in databricks.yml; "
            "leaving those fields untouched."
        )

    new_text = "\n".join(new_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    if new_text != text:
        DATABRICKS_YML.write_text(new_text)
        info(f"updated {DATABRICKS_YML.relative_to(REPO_ROOT)}")


def update_app_yml(
    *,
    warehouse_id: str,
    catalog: str,
    raw_schema: str,
    silver_schema: str,
    gold_schema: str,
    llm_endpoint: str,
) -> None:
    """Rewrite the ``value:`` line beneath each known env var in app.yml."""
    wanted = {
        "DATABRICKS_WAREHOUSE_ID": warehouse_id,
        "BHE_CATALOG": catalog,
        "BHE_RAW_SCHEMA": raw_schema,
        "BHE_SILVER_SCHEMA": silver_schema,
        "BHE_GOLD_SCHEMA": gold_schema,
        "LLM_ENDPOINT": llm_endpoint,
    }

    text = APP_YML.read_text()
    lines = text.splitlines()
    name_re = re.compile(r'^(\s*-\s*name:\s*)([A-Z_0-9]+)\s*$')

    i = 0
    changed = False
    while i < len(lines):
        m = name_re.match(lines[i])
        if m and m.group(2) in wanted:
            name = m.group(2)
            # Look ahead for the matching `value:` line (next non-blank line).
            for j in range(i + 1, min(i + 4, len(lines))):
                vm = re.match(r'^(\s*value:\s*).*$', lines[j])
                if vm:
                    new_line = f'{vm.group(1)}"{wanted[name]}"'
                    if lines[j] != new_line:
                        lines[j] = new_line
                        changed = True
                    break
        i += 1

    if changed:
        new_text = "\n".join(lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        APP_YML.write_text(new_text)
        info(f"updated {APP_YML.relative_to(REPO_ROOT)}")


# ----------------------------------------------------------------------------
# read current values to use as prompt defaults
# ----------------------------------------------------------------------------

def read_current_app_env() -> dict[str, str]:
    """Best-effort read of current env values in app.yml for prompt defaults."""
    out: dict[str, str] = {}
    if not APP_YML.exists():
        return out
    lines = APP_YML.read_text().splitlines()
    name_re = re.compile(r'^\s*-\s*name:\s*([A-Z_0-9]+)\s*$')
    val_re = re.compile(r'^\s*value:\s*"?([^"]*)"?\s*$')
    for i, line in enumerate(lines):
        m = name_re.match(line)
        if not m:
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            vm = val_re.match(lines[j])
            if vm:
                out[m.group(1)] = vm.group(1)
                break
    return out


def read_target_block(target: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not DATABRICKS_YML.exists():
        return out
    lines = DATABRICKS_YML.read_text().splitlines()
    target_re = re.compile(rf"^(\s*){re.escape(target)}:\s*$")
    in_target = False
    target_indent = ""
    for line in lines:
        if not in_target:
            m = target_re.match(line)
            if m:
                in_target = True
                target_indent = m.group(1)
            continue
        if line.strip() and not line.startswith(target_indent + " "):
            break
        hm = re.match(r"^\s*host:\s*(.+)\s*$", line)
        pm = re.match(r"^\s*profile:\s*(.+)\s*$", line)
        if hm:
            out["host"] = hm.group(1).strip().strip('"')
        elif pm:
            out["profile"] = pm.group(1).strip().strip('"')
    return out


# ----------------------------------------------------------------------------
# Databricks CLI wrappers
# ----------------------------------------------------------------------------

@dataclass
class DeployContext:
    target: str
    profile: str
    host: str
    catalog: str
    warehouse_id: str
    raw_schema: str
    silver_schema: str
    gold_schema: str
    llm_endpoint: str

    @property
    def app_name(self) -> str:
        return f"bhe-data-catalog-{self.target}"


def ensure_tools() -> None:
    # Python: we're already running on it, so check the running interpreter's
    # version directly. Probing PATH for "python3" is unreliable on Windows
    # where the python.org installer ships only "python" and "py" — Gabriel
    # at BHE hit exactly that on Win 11 + Python 3.12.
    if sys.version_info < (3, 10):
        die(
            f"Python 3.10+ required (running {sys.version.split()[0]}). "
            "Install a newer Python from https://www.python.org/downloads/ "
            "or via `uv python install 3.11`."
        )
    for tool, hint in [
        ("databricks", "install from https://docs.databricks.com/dev-tools/cli/install.html"),
        ("npx", "install Node.js 20+"),
    ]:
        if shutil.which(tool) is None:
            die(f"`{tool}` not found on PATH — {hint}")


def validate_profile(profile: str) -> None:
    info(f"validating CLI profile `{profile}`")
    result = run(
        ["databricks", "auth", "token", "-p", profile],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        die(
            f"profile `{profile}` is not authenticated. "
            f"Run: databricks auth login -p {profile}",
            code=2,
        )


def get_app_service_principal(app_name: str, profile: str, max_wait_s: int = 30) -> str:
    """Poll `databricks apps get` until it returns a service_principal_client_id."""
    deadline = time.time() + max_wait_s
    last_err: str | None = None
    while time.time() < deadline:
        result = run(
            ["databricks", "apps", "get", app_name, "-p", profile, "--output", "json"],
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                sp = data.get("service_principal_client_id")
                if sp:
                    return sp
                last_err = "app resource exists but has no service_principal_client_id yet"
            except json.JSONDecodeError as e:
                last_err = f"could not parse apps get output: {e}"
        else:
            last_err = (result.stderr or result.stdout or "").strip()
        time.sleep(2)
    die(f"could not read app SP after {max_wait_s}s: {last_err}")
    return ""  # unreachable


def grant_uc(catalog: str, sp_id: str, profile: str, schemas: dict[str, list[str]]) -> None:
    """Idempotent UC grants: catalog-level + per-schema."""
    info("granting catalog-level privileges to the app service principal")
    run(
        [
            "databricks", "grants", "update", "catalog", catalog, "-p", profile,
            "--json",
            json.dumps({"changes": [{
                "principal": sp_id,
                "add": ["USE_CATALOG", "USE_SCHEMA", "SELECT"],
            }]}),
        ],
        capture=True,
    )
    for schema, privs in schemas.items():
        info(f"granting {privs} on {catalog}.{schema}")
        run(
            [
                "databricks", "grants", "update", "schema", f"{catalog}.{schema}",
                "-p", profile,
                "--json",
                json.dumps({"changes": [{"principal": sp_id, "add": privs}]}),
            ],
            capture=True,
        )


# ----------------------------------------------------------------------------
# main flow
# ----------------------------------------------------------------------------

def gather_context(args: argparse.Namespace) -> DeployContext:
    target = args.target or prompt("Bundle target (dev/prod)", default="dev")

    current_target = read_target_block(target)
    current_env = read_current_app_env()

    host = args.workspace_url or prompt(
        "Databricks workspace URL",
        default=current_target.get("host") or None,
    )
    host = _normalize_host(host)

    profile = args.profile or prompt(
        "Databricks CLI profile",
        default=current_target.get("profile") or None,
    )

    catalog = args.catalog or prompt(
        "Unity Catalog",
        default=current_env.get("BHE_CATALOG") or "your_catalog",
    )

    warehouse_id = args.warehouse_id or prompt(
        "SQL warehouse ID",
        default=current_env.get("DATABRICKS_WAREHOUSE_ID") or None,
    )

    raw_schema = args.raw_schema or prompt(
        "Raw (ingest) schema",
        default=current_env.get("BHE_RAW_SCHEMA") or "bhe_raw",
    )
    silver_schema = args.silver_schema or prompt(
        "Silver schema",
        default=current_env.get("BHE_SILVER_SCHEMA") or "bhe_silver",
    )
    gold_schema = args.gold_schema or prompt(
        "Gold schema",
        default=current_env.get("BHE_GOLD_SCHEMA") or "bhe_gold",
    )
    llm_endpoint = args.llm_endpoint or prompt(
        "LLM model-serving endpoint",
        default=current_env.get("LLM_ENDPOINT") or "databricks-claude-sonnet-4-6",
    )

    return DeployContext(
        target=target,
        profile=profile,
        host=host,
        catalog=catalog,
        warehouse_id=warehouse_id,
        raw_schema=raw_schema,
        silver_schema=silver_schema,
        gold_schema=gold_schema,
        llm_endpoint=llm_endpoint,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", help="Bundle target (default: prompt, usually 'dev')")
    parser.add_argument("--workspace-url", help="Databricks workspace URL")
    parser.add_argument("--profile", help="Databricks CLI profile")
    parser.add_argument("--catalog", help="Unity Catalog name")
    parser.add_argument("--warehouse-id", help="SQL warehouse ID")
    parser.add_argument("--raw-schema", help="Raw schema name")
    parser.add_argument("--silver-schema", help="Silver schema name")
    parser.add_argument("--gold-schema", help="Gold schema name")
    parser.add_argument("--llm-endpoint", help="LLM model-serving endpoint")
    parser.add_argument("--skip-build", action="store_true", help="Skip `npx vite build`")
    parser.add_argument("--skip-grants", action="store_true", help="Skip UC grants (use if SP already granted)")
    parser.add_argument("--yes", action="store_true", help="Skip final confirmation prompt")
    args = parser.parse_args()

    ensure_tools()

    ctx = gather_context(args)

    # Show a plan before we start mutating anything.
    print()
    print(_c("1", "DEPLOYMENT PLAN"))
    print(f"  Bundle target    : {ctx.target}")
    print(f"  Workspace        : {ctx.host}")
    print(f"  CLI profile      : {ctx.profile}")
    print(f"  Unity Catalog    : {ctx.catalog}")
    print(f"  SQL warehouse    : {ctx.warehouse_id}")
    print(f"  Schemas          : {ctx.raw_schema} / {ctx.silver_schema} / {ctx.gold_schema}")
    print(f"  LLM endpoint     : {ctx.llm_endpoint}")
    print(f"  App resource     : {ctx.app_name}")
    print()

    if not args.yes:
        if prompt("Proceed?", default="y").lower() not in {"y", "yes"}:
            die("aborted by user", code=0)

    validate_profile(ctx.profile)

    # --- mutate config files ---
    update_databricks_yml(
        target=ctx.target,
        host=ctx.host,
        profile=ctx.profile,
        catalog=ctx.catalog,
        warehouse_id=ctx.warehouse_id,
    )
    update_app_yml(
        warehouse_id=ctx.warehouse_id,
        catalog=ctx.catalog,
        raw_schema=ctx.raw_schema,
        silver_schema=ctx.silver_schema,
        gold_schema=ctx.gold_schema,
        llm_endpoint=ctx.llm_endpoint,
    )

    # --- build SPA ---
    if not args.skip_build:
        # On a fresh clone (or any machine that hasn't run `npm install` in
        # src/app/), `node_modules/` doesn't exist and `npx vite build` fails
        # with a generic "command not found" / "system cannot find the path
        # specified" — particularly opaque on Windows. Bootstrap on demand.
        node_modules = APP_DIR / "node_modules"
        if not node_modules.is_dir():
            info("node_modules not found — running npm install (one-time)")
            run(["npm", "install"], cwd=APP_DIR)
        info("building the frontend with vite")
        # On Windows, `npx.cmd` sometimes returns exit code 1 even when vite
        # itself succeeds (it prints "The system cannot find the path
        # specified." while probing for npx in alternate locations, and cmd
        # carries that error code through to the parent). Run with check=False
        # and verify the build by checking that __dist__/index.html exists,
        # which is the only artifact downstream steps actually need.
        result = run(["npx", "vite", "build"], cwd=APP_DIR, check=False)
        index_html = DIST_DIR / "index.html"
        assets_dir = DIST_DIR / "assets"
        if not index_html.is_file() or not assets_dir.is_dir():
            if result.returncode != 0:
                die(f"vite build failed with exit {result.returncode}")
            die(
                f"vite build returned 0 but {index_html.relative_to(REPO_ROOT)} "
                f"is missing — something is wrong with the build config"
            )
        if result.returncode != 0:
            warn(
                f"npx exited with code {result.returncode} but vite produced a "
                f"valid build — treating as success (Windows npx.cmd quirk)"
            )
    else:
        info("--skip-build given; reusing existing src/bhe_catalog/__dist__")

    # --- deploy bundle ---
    info(f"deploying bundle to target `{ctx.target}` on {ctx.host}")
    run(
        [
            "databricks", "bundle", "deploy",
            "-t", ctx.target,
            "-p", ctx.profile,
            "--var", f"catalog={ctx.catalog}",
            "--var", f"warehouse_id={ctx.warehouse_id}",
        ],
    )

    # --- grant UC access to the app SP ---
    if not args.skip_grants:
        info(f"resolving service principal for `{ctx.app_name}`")
        sp_id = get_app_service_principal(ctx.app_name, ctx.profile)
        info(f"app service principal: {sp_id}")
        grant_uc(
            catalog=ctx.catalog,
            sp_id=sp_id,
            profile=ctx.profile,
            schemas={
                ctx.silver_schema: ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE"],
                ctx.gold_schema:   ["USE_SCHEMA", "SELECT", "MODIFY"],
                ctx.raw_schema:    ["USE_SCHEMA", "SELECT", "MODIFY", "READ_VOLUME", "WRITE_VOLUME"],
            },
        )
    else:
        info("--skip-grants given; not touching UC grants")

    # --- start/restart the app ---
    info("starting the app (this can take ~1-2min on a cold start)")
    run(
        [
            "databricks", "bundle", "run", BUNDLE_APP_RESOURCE,
            "-t", ctx.target,
            "-p", ctx.profile,
        ],
    )

    print()
    print(_c("32", "✓ Deployment complete."))
    print(f"  {ctx.host}/apps/{ctx.app_name}")
    print()
    print("Next steps:")
    print("  - Open the app URL printed above (logs in as your Databricks user).")
    print(f"  - If anything is blank, tail logs: databricks apps logs {ctx.app_name} -p {ctx.profile}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("interrupted", code=130)
