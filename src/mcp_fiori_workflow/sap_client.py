"""
SAP HTTP Client para SWF_FLEX_DEF_SRV
Maneja autenticación Basic y token CSRF automáticamente.
"""

import httpx
import base64
from typing import Optional


class SAPClient:
    def __init__(self, host: str, client: str, username: str, password: str):
        self.base_url   = f"{host}/sap/opu/odata/sap/SWF_FLEX_DEF_SRV"
        self.sap_client = client
        credentials     = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.auth_header = f"Basic {credentials}"
        self._csrf_token: Optional[str] = None
        self._http = httpx.Client(
            verify=False,
            timeout=30.0,
            headers={
                "Authorization": self.auth_header,
                "Accept":        "application/json",
                "sap-client":    self.sap_client,
            },
        )

    def _get_csrf_token(self) -> str:
        resp = self._http.get(
            f"{self.base_url}/$metadata",
            headers={"x-csrf-token": "Fetch"},
            params={"sap-client": self.sap_client},
        )
        resp.raise_for_status()
        token = resp.headers.get("x-csrf-token")
        if not token:
            raise RuntimeError("No se obtuvo CSRF token del servidor SAP")
        self._csrf_token = token
        return token

    def _csrf(self) -> str:
        if not self._csrf_token:
            self._get_csrf_token()
        return self._csrf_token  # type: ignore

    # ── Lectura ───────────────────────────────────────────────────────────────

    def get(self, path: str, params: dict = None) -> dict:
        p = {"sap-client": self.sap_client, **(params or {})}
        resp = self._http.get(f"{self.base_url}/{path}", params=p)
        resp.raise_for_status()
        return resp.json()

    def get_text(self, path: str, params: dict = None) -> str:
        p = {"sap-client": self.sap_client, **(params or {})}
        resp = self._http.get(
            f"{self.base_url}/{path}",
            params=p,
            headers={"Accept": "text/plain"},
        )
        resp.raise_for_status()
        return resp.text

    # ── FunctionImports ───────────────────────────────────────────────────────

    def get_function(self, function_name: str, params: dict = None) -> dict:
        """FunctionImport tipo GET (CopyWorkflow devuelve XML en d.XmlResource)."""
        p = {"sap-client": self.sap_client, **(params or {})}
        resp = self._http.get(f"{self.base_url}/{function_name}", params=p)
        resp.raise_for_status()
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}
        return {}

    def post_function(self, function_name: str, params: dict = None) -> dict:
        """FunctionImport tipo POST (ActivateWorkflow, DeactivateWorkflow)."""
        p = {"sap-client": self.sap_client, **(params or {})}
        resp = self._http.post(
            f"{self.base_url}/{function_name}",
            params=p,
            headers={
                "x-csrf-token":   self._csrf(),
                "Content-Length": "0",
            },
        )
        resp.raise_for_status()
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}
        return {}

    # ── Escritura de workflow ─────────────────────────────────────────────────

    def create_workflow(self, xml_content: str) -> dict:
        """
        Crea un nuevo workflow (borrador) enviando el XML via POST a /Workflows/$value.
        SAP devuelve el WorkflowId asignado en el JSON de respuesta.
        """
        resp = self._http.post(
            f"{self.base_url}/Workflows/$value",
            params={"sap-client": self.sap_client},
            content=xml_content.encode("utf-8"),
            headers={
                "x-csrf-token": self._csrf(),
                "Content-Type": "text/plain",
                "Accept":       "application/json",
            },
        )
        resp.raise_for_status()
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}
        return {}

    def update_workflow(self, workflow_id: str, xml_content: str) -> dict:
        """Actualiza el XML de un workflow en borrador via PUT /$value."""
        resp = self._http.put(
            f"{self.base_url}/Workflows('{workflow_id}')/$value",
            params={"sap-client": self.sap_client},
            content=xml_content.encode("utf-8"),
            headers={
                "x-csrf-token": self._csrf(),
                "Content-Type": "text/plain",
                "Accept":       "application/json",
            },
        )
        resp.raise_for_status()
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}
        return {"status": "ok"}

    def close(self):
        self._http.close()
