from __future__ import annotations

import os
from typing import Annotated, Any, AsyncGenerator, TypeAlias
from contextlib import asynccontextmanager

import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from fastapi import Depends, FastAPI, Request

from ._base import LifespanDependency
from ._config import AppConfig, logger
from ._headers import HeadersDependency


class DatabricksClient:
    """Lightweight Databricks API client using requests.

    Authentication resolves in this order:
      1. Explicit ``token`` argument (used for OBO with a per-request user token).
      2. ``DATABRICKS_TOKEN`` env var (local dev via ``start_local.sh``).
      3. OAuth M2M via the Databricks SDK — this picks up ``DATABRICKS_CLIENT_ID``
         / ``DATABRICKS_CLIENT_SECRET`` automatically on Databricks Apps, so the
         app's service principal is used transparently in production.
    """

    def __init__(self, host: str | None = None, token: str | None = None):
        self._sdk_config: Config | None = None
        explicit_host = host or os.environ.get("DATABRICKS_HOST", "")
        explicit_token = token or os.environ.get("DATABRICKS_TOKEN", "")

        if not explicit_host or not explicit_token:
            try:
                self._sdk_config = WorkspaceClient().config
            except Exception as e:
                logger.warning(f"Could not initialize Databricks SDK auth: {e}")

        resolved_host = explicit_host or (
            self._sdk_config.host if self._sdk_config else ""
        )
        self.host = (resolved_host or "").rstrip("/")
        if self.host and not self.host.startswith("http"):
            self.host = f"https://{self.host}"
        self.token = explicit_token
        self._session = requests.Session()
        if explicit_token:
            self._session.headers["Authorization"] = f"Bearer {explicit_token}"

    def _auth_headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self._sdk_config is not None:
            return self._sdk_config.authenticate() or {}
        return {}

    def api(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.host}{path}"
        headers = kwargs.pop("headers", {}) or {}
        # Refresh auth each call so OAuth tokens renew transparently.
        headers = {**self._auth_headers(), **headers}
        resp = self._session.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    @property
    def jobs(self):
        return _JobsAPI(self)

    @property
    def current_user(self):
        return _CurrentUserAPI(self)


class _JobsAPI:
    def __init__(self, client: DatabricksClient):
        self._client = client

    def list(self, name: str | None = None):
        params: dict = {"limit": 25}
        if name:
            params["name"] = name
        result = self._client.api("GET", "/api/2.1/jobs/list", params=params)
        return result.get("jobs", [])

    def run_now(self, job_id: int, python_params: list[str] | None = None) -> dict:
        body: dict = {"job_id": job_id}
        if python_params:
            body["python_params"] = python_params
        return self._client.api("POST", "/api/2.1/jobs/run-now", json=body)

    def get_run(self, run_id: int) -> dict:
        return self._client.api("GET", "/api/2.1/jobs/runs/get", params={"run_id": run_id})


class _CurrentUserAPI:
    def __init__(self, client: DatabricksClient):
        self._client = client

    def me(self) -> dict:
        return self._client.api("GET", "/api/2.0/preview/scim/v2/Me")


class _ConfigDependency(LifespanDependency):
    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.config = AppConfig()
        logger.info(f"Starting app with configuration:\n{app.state.config}")
        yield

    @staticmethod
    def __call__(request: Request) -> AppConfig:
        return request.app.state.config


class _WorkspaceClientDependency(LifespanDependency):
    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.workspace_client = DatabricksClient()
        yield

    @staticmethod
    def __call__(request: Request) -> DatabricksClient:
        return request.app.state.workspace_client


def _get_user_ws(
    headers: HeadersDependency,
) -> DatabricksClient:
    if not headers.token:
        raise ValueError(
            "OBO token is not provided in the header X-Forwarded-Access-Token"
        )
    return DatabricksClient(token=headers.token.get_secret_value())


ConfigDependency: TypeAlias = Annotated[AppConfig, _ConfigDependency.depends()]

ClientDependency: TypeAlias = Annotated[
    DatabricksClient, _WorkspaceClientDependency.depends()
]

UserWorkspaceClientDependency: TypeAlias = Annotated[
    DatabricksClient, Depends(_get_user_ws)
]
