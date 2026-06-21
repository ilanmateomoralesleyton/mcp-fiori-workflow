"""
SAP HTTP Client para SWF_FLEX_DEF_SRV
Maneja autenticación Basic y token CSRF automáticamente.

Flujo real descubierto desde el código fuente de la app Fiori (NW_APS_BPM_SWE):
- CopyWorkflow  → GET → response XML con <d:XmlResource>...XML...</d:XmlResource>
- CreateWorkflow → GET → mismo formato, XML interno en d:XmlResource
- POST /Workflows → body: application/xml (el XML interno extraído)
- PUT /Workflows('{id}')/$value → body: application/xml
- GET /Workflows('{id}')/$value → response: XML directo
"""

import httpx
import base64
import xml.etree.ElementTree as ET
from typing import Optional


# Namespace OData para parsear d:XmlResource
ODATA_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices"


class SAPClient:
    def __init__(self, host: str, client: str, username: str, password: str):
        self.base_url    = f"{host}/sap/opu/odata/sap/SWF_FLEX_DEF_SRV"
        self.sap_client  = client
        credentials      = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.auth_header = f"Basic {credentials}"
        self._csrf_token: Optional[str] = None
        self._http = httpx.Client(
            verify=False,
            timeout=60.0,
            headers={
                "Authorization": self.auth_header,
                "Accept":        "application/json",
                "sap-client":    self.sap_client,
            },
        )

    def _get_csrf_token(self) -> str:
        # SAP OData v2 responde $metadata solo en application/xml — no en JSON.
        # Forzamos Accept: application/xml aquí para evitar el 406 Not Acceptable.
        resp = self._http.get(
            f"{self.base_url}/$metadata",
            headers={
                "x-csrf-token": "Fetch",
                "Accept":       "application/xml, text/xml, */*",
            },
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

    # ── Lectura JSON ──────────────────────────────────────────────────────────

    def get(self, path: str, params: dict = None) -> dict:
        p = {"sap-client": self.sap_client, **(params or {})}
        resp = self._http.get(f"{self.base_url}/{path}", params=p)
        resp.raise_for_status()
        return resp.json()

    # ── Lectura XML directo (GET /$value) ─────────────────────────────────────

    def get_xml(self, path: str, params: dict = None) -> str:
        """
        Lee un endpoint que devuelve XML directo (ej: Workflows('{id}')/$value).
        Devuelve el texto XML crudo.
        """
        p = {"sap-client": self.sap_client, **(params or {})}
        resp = self._http.get(
            f"{self.base_url}/{path}",
            params=p,
            headers={"Accept": "application/xml, text/xml, */*"},
        )
        resp.raise_for_status()
        return resp.text

    # ── FunctionImports que devuelven XML con d:XmlResource ───────────────────

    def get_function_xml_resource(self, function_name: str, params: dict = None) -> str:
        """
        Llama a un FunctionImport GET que devuelve XML con estructura:
          <entry>...<d:XmlResource>XML DEL WORKFLOW</d:XmlResource>...</entry>

        Extrae y devuelve el XML interno (el workflow XML real).
        Usado por: CopyWorkflow, CreateWorkflow, UpgradeWorkflow.
        """
        p = {"sap-client": self.sap_client, **(params or {})}
        resp = self._http.get(
            f"{self.base_url}/{function_name}",
            params=p,
            headers={"Accept": "application/xml, text/xml, */*"},
        )
        resp.raise_for_status()

        # Parsear el XML del response y extraer d:XmlResource
        try:
            root = ET.fromstring(resp.text)
            # Buscar <d:XmlResource> con namespace
            xml_resource_el = root.find(f".//{{{ODATA_NS}}}XmlResource")
            if xml_resource_el is not None and xml_resource_el.text:
                return xml_resource_el.text.strip()
        except ET.ParseError:
            pass

        # Fallback: buscar sin namespace (por si SAP omite el namespace en algún caso)
        try:
            root = ET.fromstring(resp.text)
            xml_resource_el = root.find(".//XmlResource")
            if xml_resource_el is not None and xml_resource_el.text:
                return xml_resource_el.text.strip()
        except ET.ParseError:
            pass

        raise RuntimeError(
            f"No se encontró <d:XmlResource> en el response de {function_name}. "
            f"Response recibido: {resp.text[:500]}"
        )

    # ── FunctionImports POST (Activate, Deactivate) ───────────────────────────

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

    def create_workflow(self, xml_content: str) -> str:
        """
        Crea un nuevo workflow (borrador) via POST a /Workflows.
        Body: application/xml (el XML del workflow con id='').
        SAP devuelve XML con el WorkflowId asignado en <d:WorkflowId>.
        Devuelve el nuevo WorkflowId como string.
        """
        resp = self._http.post(
            f"{self.base_url}/Workflows",
            params={"sap-client": self.sap_client},
            content=xml_content.encode("utf-8"),
            headers={
                "x-csrf-token": self._csrf(),
                "Content-Type": "application/xml",
                "Accept":       "application/xml",
            },
        )
        resp.raise_for_status()

        # Extraer WorkflowId del response XML
        try:
            root = ET.fromstring(resp.text)
            wf_id_el = root.find(f".//{{{ODATA_NS}}}WorkflowId")
            if wf_id_el is not None and wf_id_el.text:
                return wf_id_el.text.strip()
            # Fallback sin namespace
            wf_id_el = root.find(".//WorkflowId")
            if wf_id_el is not None and wf_id_el.text:
                return wf_id_el.text.strip()
        except ET.ParseError:
            pass

        # Si no pudimos extraer el ID, devolver el response para debug
        raise RuntimeError(
            f"POST /Workflows OK pero no se encontró WorkflowId en el response. "
            f"Response: {resp.text[:500]}"
        )

    def update_workflow(self, workflow_id: str, xml_content: str) -> None:
        """
        Actualiza el XML de un workflow en borrador via PUT /Workflows('{id}')/$value.
        Body: application/xml.
        """
        resp = self._http.put(
            f"{self.base_url}/Workflows('{workflow_id}')/$value",
            params={"sap-client": self.sap_client},
            content=xml_content.encode("utf-8"),
            headers={
                "x-csrf-token": self._csrf(),
                "Content-Type": "application/xml",
            },
        )
        resp.raise_for_status()

    def close(self):
        self._http.close()

    def delete_workflow(self, workflow_id: str) -> None:
        """Elimina un workflow (solo DRAFT) via DELETE /Workflows('{id}')."""
        resp = self._http.delete(
            f"{self.base_url}/Workflows('{workflow_id}')",
            params={"sap-client": self.sap_client},
            headers={"x-csrf-token": self._csrf()},
        )
        resp.raise_for_status()

    def save_workflow_order(self, scenario_id: str, xml_content: str) -> None:
        """Actualiza el orden de workflows de un escenario via PUT /WorkflowOrders('{id}')/$value."""
        resp = self._http.put(
            f"{self.base_url}/WorkflowOrders('{scenario_id}')/$value",
            params={"sap-client": self.sap_client},
            content=xml_content.encode("utf-8"),
            headers={
                "x-csrf-token": self._csrf(),
                "Content-Type": "application/xml",
            },
        )
        resp.raise_for_status()
