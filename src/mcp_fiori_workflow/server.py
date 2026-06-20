"""
MCP Server: SAP Fiori Flexible Workflow Manager
Controla la app F2190 (Gestionar Workflows) vía OData SWF_FLEX_DEF_SRV.

Herramientas:
  list_scenarios       → listar escenarios disponibles
  list_workflows       → listar workflows de un escenario
  get_workflow_steps   → leer pasos y agentes de un workflow
  copy_workflow        → crear borrador editable desde uno activo
  replace_step_agent   → cambiar el agente/liberador de un paso
  activate_workflow    → activar un borrador
  deactivate_workflow  → desactivar un workflow activo
  get_workflow_xml     → obtener XML completo (inspección técnica)
"""

import os
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from mcp_fiori_workflow.sap_client import SAPClient
from mcp_fiori_workflow.workflow_xml import (
    parse_activities,
    replace_principals_in_activity,
    summarize_xml,
    clear_workflow_id,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
SAP_HOST     = os.environ.get("SAP_HOST",     "https://vhereqs4ci.sap.reutter.cl:44300")
SAP_CLIENT   = os.environ.get("SAP_CLIENT",   "200")
SAP_USER     = os.environ.get("SAP_USER",     "")
SAP_PASSWORD = os.environ.get("SAP_PASSWORD", "")

KNOWN_SCENARIOS = {
    "WS02000458": "Liberación global de solicitud de pedido",
    "WS02000471": "Liberación de posición solicitud pedido",
    "WS00800157": "Workflow de pedido (genérico)",
    "WS00800173": "Workflow de pedido (genérico 2)",
    "WS02000434": "Workflow adicional",
    "WS02000438": "Workflow adicional 2",
}

# ── Server ────────────────────────────────────────────────────────────────────
app = Server("sap-fiori-workflow")


def get_client() -> SAPClient:
    if not SAP_USER or not SAP_PASSWORD:
        raise RuntimeError(
            "Faltan credenciales. Define SAP_USER y SAP_PASSWORD como variables de entorno."
        )
    return SAPClient(SAP_HOST, SAP_CLIENT, SAP_USER, SAP_PASSWORD)


def ok(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


def err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}, ensure_ascii=False))]


# ── Herramientas ──────────────────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_scenarios",
            description=(
                "Lista los escenarios de Flexible Workflow disponibles en SAP (WS02000471, etc.). "
                "Punto de partida para conocer qué escenarios existen antes de listar workflows."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs a consultar. Si se omite, usa los escenarios conocidos de REUTTER.",
                    }
                },
            },
        ),
        Tool(
            name="list_workflows",
            description=(
                "Lista todos los workflows de un escenario SAP (activos, inactivos y borradores). "
                "Incluye WorkflowId, nombre, estado (ACTIVE/DRAFT/INACTIVE) y usuario que lo creó."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_id": {
                        "type": "string",
                        "description": "ID del escenario, ej: 'WS02000471'",
                    }
                },
                "required": ["scenario_id"],
            },
        ),
        Tool(
            name="get_workflow_steps",
            description=(
                "Lee los pasos de un workflow y muestra qué agente/liberador tiene asignado cada uno. "
                "Devuelve índice, nombre del paso, tipo de agente (USER/RULE) e ID del agente."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "WorkflowId a leer"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="copy_workflow",
            description=(
                "Copia un workflow activo creando un nuevo borrador (DRAFT) editable. "
                "El flujo es: CopyWorkflow (obtiene XML) → POST a SAP (crea el borrador). "
                "Devuelve el WorkflowId del nuevo borrador listo para modificar."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "WorkflowId del workflow activo a copiar",
                    }
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="replace_step_agent",
            description=(
                "Reemplaza el agente/liberador de un paso en un workflow DRAFT. "
                "Acepta usuario SAP específico (user_id) o regla estándar (agent_rule_id). "
                "IMPORTANTE: el workflow debe estar en estado DRAFT — usa copy_workflow primero."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "WorkflowId del borrador DRAFT",
                    },
                    "step_index": {
                        "type": "integer",
                        "description": "Índice del paso (0 = primer paso). Obtenerlo con get_workflow_steps.",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Usuario SAP como liberador, ej: 'VPARDO'. Mutuamente exclusivo con agent_rule_id.",
                    },
                    "agent_rule_id": {
                        "type": "string",
                        "description": (
                            "Regla estándar SAP. Opciones: "
                            "'$0008$/RULE/MMPUR_MGR_RQSTR' (gestor del solicitante), "
                            "'$0008$/RULE/MMPUR_MGR_L_APPR' (gestor del último aprobador), "
                            "'$0008$/RULE/MMPUR_MGR_OF_MGR' (gestor del gestor), "
                            "'$0008$/RULE/MMPUR_ACC_RESP' (responsable imputación), "
                            "'$0008$/RULE/MMPUR_PR_BD_AGNT' (BAdI). "
                            "Mutuamente exclusivo con user_id."
                        ),
                    },
                },
                "required": ["workflow_id", "step_index"],
            },
        ),
        Tool(
            name="activate_workflow",
            description="Activa un workflow en borrador (DRAFT → ACTIVE). El workflow entra en producción.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "WorkflowId del borrador a activar"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="deactivate_workflow",
            description="Desactiva un workflow activo (ACTIVE → INACTIVE). Útil para retirar el workflow anterior tras activar el nuevo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "WorkflowId activo a desactivar"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="get_workflow_xml",
            description="Obtiene el XML completo de un workflow. Para inspección técnica o debug.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                },
                "required": ["workflow_id"],
            },
        ),
    ]


# ── Dispatcher ────────────────────────────────────────────────────────────────
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        client = get_client()
        try:
            return await _dispatch(name, arguments, client)
        finally:
            client.close()
    except RuntimeError as e:
        return err(str(e))
    except Exception as e:
        logger.exception(f"Error en tool '{name}'")
        return err(f"Error inesperado: {e}")


async def _dispatch(name: str, args: dict, client: SAPClient) -> list[TextContent]:

    # ── list_scenarios ────────────────────────────────────────────────────────
    if name == "list_scenarios":
        ids = args.get("scenario_ids") or list(KNOWN_SCENARIOS.keys())
        filter_str = " or ".join(f"ScenarioId eq '{sid}'" for sid in ids)
        data    = client.get("Scenarios", {"$filter": filter_str})
        results = data.get("d", {}).get("results", [])
        return ok({
            "scenarios": [
                {
                    "ScenarioId":   r["ScenarioId"],
                    "Subject":      r["Subject"],
                    "Description":  r.get("Description", ""),
                }
                for r in results
            ],
            "total": len(results),
        })

    # ── list_workflows ────────────────────────────────────────────────────────
    elif name == "list_workflows":
        scenario_id = args["scenario_id"]
        data    = client.get("Workflows", {
            "$filter":  f"ScenarioId eq '{scenario_id}'",
            "$orderby": "OrderNumber asc",
            "$top":     "200",
        })
        results = data.get("d", {}).get("results", [])
        return ok({
            "scenario_id":   scenario_id,
            "scenario_name": KNOWN_SCENARIOS.get(scenario_id, ""),
            "workflows": [
                {
                    "WorkflowId":          r["WorkflowId"],
                    "Subject":             r["Subject"],
                    "Status":              r["Status"],
                    "OrderNumber":         r["OrderNumber"],
                    "ValidFrom":           r.get("ValidFrom", ""),
                    "CreationUser":        r.get("CreationUser", ""),
                    "LastChangeUser":      r.get("LastChangeUser", ""),
                    "LastChangeTimestamp": r.get("LastChangeTimestamp", ""),
                    "IsReadOnly":          r.get("IsReadOnly", False),
                }
                for r in results
            ],
            "total": len(results),
        })

    # ── get_workflow_steps ────────────────────────────────────────────────────
    elif name == "get_workflow_steps":
        workflow_id = args["workflow_id"]
        xml_text    = client.get_text(f"Workflows('{workflow_id}')/$value")
        return ok(summarize_xml(xml_text))

    # ── copy_workflow ─────────────────────────────────────────────────────────
    elif name == "copy_workflow":
        workflow_id = args["workflow_id"]

        # 1. Llamar CopyWorkflow para obtener el XML del original
        copy_data  = client.get_function("CopyWorkflow", {"WorkflowId": f"'{workflow_id}'"})
        xml_copy   = copy_data.get("d", {}).get("XmlResource") or copy_data.get("raw", "")

        if not xml_copy:
            return err("CopyWorkflow no devolvió XML. Verifica que el WorkflowId existe y está activo.")

        # 2. Asegurarse de que el id esté vacío (SAP lo asigna al crear)
        xml_clean = clear_workflow_id(xml_copy)

        # 3. POST a /Workflows/$value para crear el borrador en SAP
        create_resp = client.create_workflow(xml_clean)

        # 4. El response contiene el nuevo WorkflowId
        new_id = (
            create_resp.get("d", {}).get("WorkflowId")
            or create_resp.get("WorkflowId")
            or ""
        )

        if not new_id:
            return ok({
                "warning": "El borrador se creó pero no pudimos extraer el WorkflowId del response.",
                "raw_response": create_resp,
                "suggestion": "Usa list_workflows filtrando por Status=DRAFT para encontrar el nuevo borrador.",
            })

        return ok({
            "success":          True,
            "new_draft_id":     new_id,
            "original_id":      workflow_id,
            "message":          f"Borrador creado con ID '{new_id}'. Usa replace_step_agent para modificar sus pasos.",
        })

    # ── replace_step_agent ────────────────────────────────────────────────────
    elif name == "replace_step_agent":
        workflow_id   = args["workflow_id"]
        step_index    = args["step_index"]
        user_id       = args.get("user_id")
        agent_rule_id = args.get("agent_rule_id")

        if not user_id and not agent_rule_id:
            return err("Debes proporcionar user_id o agent_rule_id.")
        if user_id and agent_rule_id:
            return err("Solo puedes proporcionar user_id O agent_rule_id, no ambos.")

        # Verificar estado DRAFT
        wf_data = client.get(f"Workflows('{workflow_id}')")
        status  = wf_data.get("d", {}).get("Status", "")
        if status != "DRAFT":
            return err(
                f"El workflow '{workflow_id}' está en estado '{status}'. "
                "Solo se pueden modificar borradores en estado DRAFT. "
                "Usa copy_workflow primero."
            )

        # Leer XML actual
        xml_original = client.get_text(f"Workflows('{workflow_id}')/$value")
        activities   = parse_activities(xml_original)

        if step_index >= len(activities):
            return err(
                f"step_index {step_index} inválido. "
                f"El workflow tiene {len(activities)} pasos (0-{len(activities)-1})."
            )

        before = activities[step_index]

        # Construir nuevos principals
        if user_id:
            new_principals = [{"id": user_id, "type": "USER"}]
        else:
            new_principals = [{"id": agent_rule_id, "type": "RULE"}]

        # Modificar XML
        xml_modified = replace_principals_in_activity(xml_original, step_index, new_principals)

        # Guardar en SAP
        client.update_workflow(workflow_id, xml_modified)

        # Verificar leyendo de vuelta
        xml_after      = client.get_text(f"Workflows('{workflow_id}')/$value")
        activities_after = parse_activities(xml_after)
        after = activities_after[step_index]

        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "step_index":  step_index,
            "step_name":   before.name,
            "before":      {"principals": before.principals},
            "after":       {"principals": after.principals},
            "next_step":   "Usa activate_workflow para activar este borrador cuando estés conforme.",
        })

    # ── activate_workflow ─────────────────────────────────────────────────────
    elif name == "activate_workflow":
        workflow_id = args["workflow_id"]
        data   = client.post_function("ActivateWorkflow", {"WorkflowId": f"'{workflow_id}'"})
        result = data.get("d", data)
        return ok({
            "success":    True,
            "workflow_id": workflow_id,
            "new_status": result.get("Status", "ACTIVE"),
            "subject":    result.get("Subject", ""),
            "message":    "Workflow activado correctamente.",
        })

    # ── deactivate_workflow ───────────────────────────────────────────────────
    elif name == "deactivate_workflow":
        workflow_id = args["workflow_id"]
        data   = client.post_function("DeactivateWorkflow", {"WorkflowId": f"'{workflow_id}'"})
        result = data.get("d", data)
        return ok({
            "success":    True,
            "workflow_id": workflow_id,
            "new_status": result.get("Status", "INACTIVE"),
            "message":    "Workflow desactivado correctamente.",
        })

    # ── get_workflow_xml ──────────────────────────────────────────────────────
    elif name == "get_workflow_xml":
        workflow_id = args["workflow_id"]
        xml_text    = client.get_text(f"Workflows('{workflow_id}')/$value")
        return ok({"workflow_id": workflow_id, "xml": xml_text})

    else:
        return err(f"Herramienta desconocida: {name}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())


def run():
    """Entry point para el script instalado (mcp-fiori-workflow)."""
    import asyncio
    asyncio.run(main())
