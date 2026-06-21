"""
MCP Server: SAP Fiori Flexible Workflow Manager v0.4.0
Opera la app F2190 (Gestionar Workflows) vía OData SWF_FLEX_DEF_SRV.

Herramientas — paridad completa con la app Fiori:
  LECTURA:
    list_scenarios          → listar escenarios disponibles
    list_workflows          → listar workflows de un escenario
    get_workflow_steps      → leer pasos, agentes y condiciones
    get_workflow_xml        → XML completo (inspección técnica)

  CICLO DE VIDA:
    copy_workflow           → crear borrador desde workflow activo
    create_workflow         → crear workflow nuevo desde cero
    delete_workflow         → eliminar un borrador
    activate_workflow       → activar borrador (DRAFT → ACTIVE)
    deactivate_workflow     → desactivar workflow activo
    upgrade_workflow        → actualizar a nueva versión del escenario

  EDICIÓN DE CABECERA:
    update_workflow_header  → cambiar nombre, descripción, fechas
    update_start_condition  → modificar condiciones de inicio (grupo compras, etc.)

  EDICIÓN DE PASOS:
    add_workflow_step       → agregar nuevo paso
    delete_workflow_step    → eliminar un paso existente
    move_workflow_step      → subir/bajar un paso
    rename_workflow_step    → renombrar un paso
    replace_step_agent      → cambiar agente/liberador de un paso
    update_step_conditions  → modificar condiciones de monto de un paso

  ORDEN:
    get_workflow_order      → ver prioridad de workflows en el escenario
    save_workflow_order     → modificar orden de prioridad
"""

import os
import json
import logging
import xml.etree.ElementTree as ET
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from mcp_fiori_workflow.sap_client import SAPClient
from mcp_fiori_workflow.workflow_xml import (
    parse_activities,
    parse_workflow,
    summarize_xml,
    replace_principals_in_activity,
    update_activity_conditions,
    add_activity,
    delete_activity,
    move_activity,
    rename_activity,
    update_workflow_header,
    update_start_condition,
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

# ── Textos de ayuda para elicitación ─────────────────────────────────────────
# Estos textos se usan en las descripciones de las herramientas para que
# Claude Desktop SIEMPRE pregunte los valores antes de ejecutar.

_SCENARIOS_HELP = (
    "ESCENARIOS DISPONIBLES EN REUTTER:\n"
    "- WS02000458: Liberación global de solicitud de pedido\n"
    "- WS02000471: Liberación de posición de solicitud de pedido\n"
    "SIEMPRE preguntar al usuario cuál escenario aplica antes de continuar."
)

_AGENT_RULES_HELP = (
    "REGLAS DE AGENTE DISPONIBLES (WS02000471):\n"
    "- '$0008$/RULE/MMPUR_MGR_RQSTR': Gestor del solicitante\n"
    "- '$0008$/RULE/MMPUR_MGR_L_APPR': Gestor del último aprobador\n"
    "- '$0008$/RULE/MMPUR_MGR_OF_MGR': Gestor del gestor\n"
    "- '$0008$/RULE/MMPUR_ACC_RESP': Responsable de imputación\n"
    "- '$0008$/RULE/MMPUR_PR_BD_AGNT': Determinación por BAdI\n"
    "O bien un usuario SAP específico (ej: 'VPARDO')."
)

_STEP_CONDITIONS_HELP = (
    "CONDICIONES DE PASO DISPONIBLES (imagen Fiori):\n"
    "- Ninguna (sin condición de monto)\n"
    "- Importe neto es igual o mayor que (amount_min)\n"
    "- El importe neto total es inferior a (amount_max)\n"
    "- Categoría de imputación de posición de solicitud de pedido\n"
    "- ID de catálogo de posición de solicitud de pedido\n"
    "- Indicador de creación (CreationIndicator)\n"
    "- Estado de autorización externa\n"
    "- Grupo de artículos\n"
    "- Centro (Plant)\n"
    "- Grupo de compras de la posición\n"
    "- Organización de compras\n"
    "SIEMPRE preguntar al usuario qué condición quiere aplicar."
)

_START_CONDITIONS_HELP = (
    "CONDICIONES DE INICIO DISPONIBLES:\n"
    "- '$0008$PurchasingGroup': Grupo de compras (ej: '109', '118')\n"
    "- '$0008$PurchasingOrganization': Organización de compras\n"
    "- '$0008$CreationIndicator': Indicador de creación (ej: 'V' = solicitud manual)\n"
    "SIEMPRE preguntar al usuario qué condición de inicio aplicará."
)

_CREATE_WORKFLOW_QUESTIONS = (
    "ANTES DE CREAR UN WORKFLOW, SIEMPRE PREGUNTAR AL USUARIO:\n"
    "1. ¿Es 'Liberación global de solicitud de pedido' (WS02000458) "
    "o 'Liberación de posición de solicitud de pedido' (WS02000471)?\n"
    "2. ¿Cuál será el nombre (subject) del workflow?\n"
    "3. ¿Cuál será la descripción?\n"
    "4. ¿Válido desde qué fecha? (formato YYYY-MM-DD)\n"
    "5. ¿Válido hasta qué fecha? (o sin límite)\n"
    "6. ¿Cuál es la condición de inicio? (grupo de compras, org de compras, etc.)\n"
    "7. ¿Cuántos pasos tendrá el workflow?\n"
    "Para cada paso, preguntar:\n"
    "  - Nombre del paso\n"
    "  - ¿Es opcional (sí/no)?\n"
    "  - ¿Excluir solicitantes como agentes (sí/no)?\n"
    "  - ¿Quién es el liberador? (usuario SAP o regla estándar)\n"
    "  - ¿Condición de monto? (desde/hasta en CLP u otra moneda)\n"
    "  - ¿Tiene plazo (deadline)? Si sí: referencia de tiempo y acción\n"
    "  - ¿Gestión de excepciones? (qué pasa si se rechaza)"
)

# ── Server ────────────────────────────────────────────────────────────────────
app = Server("sap-fiori-workflow")


def get_client() -> SAPClient:
    if not SAP_USER or not SAP_PASSWORD:
        raise RuntimeError("Faltan credenciales. Define SAP_USER y SAP_PASSWORD.")
    return SAPClient(SAP_HOST, SAP_CLIENT, SAP_USER, SAP_PASSWORD)


def ok(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


def err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}, ensure_ascii=False))]


def _check_draft(client: SAPClient, workflow_id: str) -> str:
    """Verifica que el workflow esté en DRAFT. Devuelve el status."""
    wf = client.get(f"Workflows('{workflow_id}')")
    status = wf.get("d", {}).get("Status", "")
    if status != "DRAFT":
        raise ValueError(
            f"El workflow '{workflow_id}' está en estado '{status}'. "
            "Solo se pueden modificar borradores en estado DRAFT. "
            "Usa copy_workflow primero."
        )
    return status


# ── Definición de herramientas ────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ── LECTURA ──────────────────────────────────────────────────────────
        Tool(
            name="list_scenarios",
            description="Lista los escenarios de Flexible Workflow disponibles en SAP (WS02000471, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "IDs a consultar. Si se omite, usa los de REUTTER.",
                    }
                },
            },
        ),
        Tool(
            name="list_workflows",
            description="Lista todos los workflows de un escenario (activos, borradores, inactivos).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_id": {"type": "string", "description": "Ej: 'WS02000471'"},
                },
                "required": ["scenario_id"],
            },
        ),
        Tool(
            name="get_workflow_steps",
            description="Lee los pasos de un workflow: nombre, agente asignado y condiciones de monto.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="get_workflow_xml",
            description="Obtiene el XML completo de un workflow. Para inspección técnica.",
            inputSchema={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
        ),

        # ── CICLO DE VIDA ─────────────────────────────────────────────────────
        Tool(
            name="copy_workflow",
            description=(
                "Copia un workflow activo creando un borrador (DRAFT) editable. "
                "Devuelve el WorkflowId del nuevo borrador."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "WorkflowId del workflow activo a copiar"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="create_workflow",
            description=(
                "Crea un workflow nuevo desde cero. "
                + _CREATE_WORKFLOW_QUESTIONS
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_id": {
                        "type": "string",
                        "description": _SCENARIOS_HELP,
                    },
                    "subject": {"type": "string", "description": "Nombre del workflow"},
                    "description": {"type": "string", "description": "Descripción del workflow"},
                    "valid_from": {"type": "string", "description": "Fecha inicio ISO 8601, ej: '2026-01-01T00:00:00.000Z'"},
                    "valid_to": {"type": "string", "description": "Fecha fin ISO 8601. Omitir si es sin límite."},
                    "purchasing_group": {"type": "string", "description": "Grupo de compras SAP (condición de inicio), ej: '109'"},
                },
                "required": ["scenario_id", "subject"],
            },
        ),
        Tool(
            name="delete_workflow",
            description="Elimina un workflow en estado DRAFT. No se pueden eliminar workflows activos.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "WorkflowId del borrador a eliminar"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="activate_workflow",
            description="Activa un workflow en borrador (DRAFT → ACTIVE).",
            inputSchema={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="deactivate_workflow",
            description="Desactiva un workflow activo (ACTIVE → INACTIVE).",
            inputSchema={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="upgrade_workflow",
            description=(
                "Actualiza un workflow a la nueva versión del escenario SAP. "
                "Útil cuando SAP libera una nueva versión del escenario (ej. 0008 → 0009). "
                "Crea un borrador actualizado; usa activate_workflow después."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "force": {
                        "type": "boolean",
                        "description": "Forzar upgrade aunque haya advertencias. Default: false.",
                        "default": False,
                    },
                },
                "required": ["workflow_id"],
            },
        ),

        # ── EDICIÓN DE CABECERA ───────────────────────────────────────────────
        Tool(
            name="update_workflow_header",
            description=(
                "Actualiza campos de cabecera de un workflow DRAFT. "
                "ANTES DE EJECUTAR, SIEMPRE PREGUNTAR AL USUARIO los campos a modificar:\n"
                "1. ¿Nuevo nombre (subject)?\n"
                "2. ¿Nueva descripción?\n"
                "3. ¿Nueva fecha de inicio (validFrom)? formato YYYY-MM-DD\n"
                "4. ¿Nueva fecha de fin (validTo)? o sin límite\n"
                "Solo preguntar los campos que el usuario quiere cambiar."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "subject": {"type": "string", "description": "Nuevo nombre del workflow"},
                    "description": {"type": "string", "description": "Nueva descripción"},
                    "valid_from": {"type": "string", "description": "Nueva fecha inicio ISO 8601"},
                    "valid_to": {"type": "string", "description": "Nueva fecha fin ISO 8601 (omitir para sin límite)"},
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="update_start_condition",
            description=(
                "Modifica la condición de inicio de un workflow DRAFT. "
                "ANTES DE EJECUTAR, SIEMPRE PREGUNTAR AL USUARIO:\n"
                "1. ¿Qué condición de inicio quiere modificar?\n"
                + _START_CONDITIONS_HELP + "\n"
                "2. ¿Cuál es el nuevo valor de la condición?"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "condition_id": {
                        "type": "string",
                        "description": _START_CONDITIONS_HELP,
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Parámetros de la condición, ej: {\"PurchasingGroup\": \"109\"}",
                    },
                },
                "required": ["workflow_id", "condition_id", "parameters"],
            },
        ),

        # ── EDICIÓN DE PASOS ──────────────────────────────────────────────────
        Tool(
            name="add_workflow_step",
            description=(
                "Agrega un nuevo paso a un workflow DRAFT. "
                "ANTES DE EJECUTAR, SIEMPRE PREGUNTAR AL USUARIO:\n"
                "1. ¿Cuál es el nombre del paso?\n"
                "2. ¿El paso es opcional o obligatorio?\n"
                "3. ¿Se deben excluir los solicitantes como agentes? (sí/no)\n"
                "4. ¿Quién es el liberador? " + _AGENT_RULES_HELP + "\n"
                "5. ¿Tiene condición de monto? " + _STEP_CONDITIONS_HELP + "\n"
                "6. ¿En qué posición del workflow va? (al final, o antes de qué paso)\n"
                "7. ¿Tiene plazo (deadline)? Si sí: referencia de tiempo (ej: 3 días) y acción (enviar mail / marcar como vencido)\n"
                "8. ¿Gestión de excepciones al rechazar? (ej: cancelar workflow)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "name": {"type": "string", "description": "Nombre descriptivo del paso"},
                    "user_id": {"type": "string", "description": "Usuario SAP liberador (ej: 'VPARDO'). Mutuamente exclusivo con agent_rule_id."},
                    "agent_rule_id": {
                        "type": "string",
                        "description": _AGENT_RULES_HELP,
                    },
                    "amount_min": {"type": "integer", "description": "Monto mínimo exclusivo, ej: 1000001 = mayor a 1.000.000"},
                    "amount_max": {"type": "integer", "description": "Monto máximo inclusivo. Omitir si es abierto hacia arriba."},
                    "currency": {"type": "string", "default": "CLP", "description": "Moneda SAP, ej: 'CLP', 'USD'"},
                    "insert_at_index": {"type": "integer", "description": "Posición donde insertar (0 = antes del primer paso). Omitir para agregar al final."},
                    "is_optional": {"type": "string", "default": "1", "description": "'1' = paso opcional (se puede saltar), '0' = obligatorio"},
                    "exclude_requestors": {"type": "string", "default": "2", "description": "'2' = excluir solicitantes como agentes, '1' = no excluir"},
                },
                "required": ["workflow_id", "name"],
            },
        ),
        Tool(
            name="delete_workflow_step",
            description="Elimina un paso de un workflow DRAFT por índice. Obtener el índice con get_workflow_steps.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "step_index": {"type": "integer", "description": "Índice del paso a eliminar (0 = primer paso)"},
                },
                "required": ["workflow_id", "step_index"],
            },
        ),
        Tool(
            name="move_workflow_step",
            description="Sube o baja un paso en el orden del workflow DRAFT.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "step_index": {"type": "integer"},
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "'up' para subir, 'down' para bajar"},
                },
                "required": ["workflow_id", "step_index", "direction"],
            },
        ),
        Tool(
            name="rename_workflow_step",
            description="Renombra un paso de un workflow DRAFT.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "step_index": {"type": "integer"},
                    "new_name": {"type": "string", "description": "Nuevo nombre del paso"},
                },
                "required": ["workflow_id", "step_index", "new_name"],
            },
        ),
        Tool(
            name="replace_step_agent",
            description=(
                "Reemplaza el agente/liberador de un paso en un workflow DRAFT. "
                "ANTES DE EJECUTAR, SIEMPRE PREGUNTAR AL USUARIO:\n"
                "1. ¿Qué paso quiere modificar? (obtener con get_workflow_steps primero)\n"
                "2. ¿Quién será el nuevo liberador?\n"
                + _AGENT_RULES_HELP
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "step_index": {"type": "integer", "description": "0 = primer paso"},
                    "user_id": {"type": "string", "description": "Usuario SAP, ej: 'VPARDO'. Mutuamente exclusivo con agent_rule_id."},
                    "agent_rule_id": {
                        "type": "string",
                        "description": (
                            "Regla estándar SAP. Opciones: "
                            "'$0008$/RULE/MMPUR_MGR_RQSTR' (gestor del solicitante), "
                            "'$0008$/RULE/MMPUR_MGR_L_APPR' (gestor del último aprobador), "
                            "'$0008$/RULE/MMPUR_MGR_OF_MGR' (gestor del gestor), "
                            "'$0008$/RULE/MMPUR_ACC_RESP' (responsable imputación), "
                            "'$0008$/RULE/MMPUR_PR_BD_AGNT' (BAdI)."
                        ),
                    },
                },
                "required": ["workflow_id", "step_index"],
            },
        ),
        Tool(
            name="update_step_conditions",
            description=(
                "Actualiza las condiciones de monto de un paso en un workflow DRAFT. "
                "ANTES DE EJECUTAR, SIEMPRE PREGUNTAR AL USUARIO:\n"
                "1. ¿Qué paso quiere modificar? (obtener con get_workflow_steps primero)\n"
                "2. ¿Tiene monto mínimo? (ej: 1.000.001 CLP = 'mayor a 1.000.000')\n"
                "3. ¿Tiene monto máximo? (ej: 2.000.000 CLP = 'hasta 2.000.000', o abierto hacia arriba)\n"
                "4. ¿En qué moneda? (CLP por defecto)\n"
                + _STEP_CONDITIONS_HELP
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "step_index": {"type": "integer"},
                    "amount_min": {"type": "integer", "description": "Monto mínimo exclusivo, ej: 1000001"},
                    "amount_max": {"type": "integer", "description": "Monto máximo inclusivo. Omitir si es abierto hacia arriba."},
                    "currency": {"type": "string", "default": "CLP"},
                },
                "required": ["workflow_id", "step_index"],
            },
        ),

        # ── ORDEN ─────────────────────────────────────────────────────────────
        Tool(
            name="get_workflow_order",
            description="Obtiene el orden de prioridad de los workflows de un escenario.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_id": {"type": "string", "description": "Ej: 'WS02000471'"},
                },
                "required": ["scenario_id"],
            },
        ),
        Tool(
            name="save_workflow_order",
            description=(
                "Modifica el orden de prioridad de los workflows de un escenario. "
                "ANTES DE EJECUTAR, SIEMPRE:\n"
                "1. Llamar get_workflow_order para mostrar el orden actual al usuario\n"
                "2. Preguntar al usuario qué nuevo orden desea\n"
                "3. Confirmar el nuevo orden antes de guardar"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_id": {"type": "string"},
                    "workflow_ids_ordered": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de WorkflowIds en el orden deseado (primero = mayor prioridad)",
                    },
                },
                "required": ["scenario_id", "workflow_ids_ordered"],
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
    except (ValueError, IndexError) as e:
        return err(str(e))
    except Exception as e:
        logger.exception(f"Error en tool '{name}'")
        return err(f"Error inesperado: {e}")


async def _dispatch(name: str, args: dict, client: SAPClient) -> list[TextContent]:

    # ── list_scenarios ────────────────────────────────────────────────────────
    if name == "list_scenarios":
        ids = args.get("scenario_ids") or list(KNOWN_SCENARIOS.keys())
        filter_str = " or ".join(f"ScenarioId eq '{sid}'" for sid in ids)
        data = client.get("Scenarios", {"$filter": filter_str})
        results = data.get("d", {}).get("results", [])
        return ok({
            "scenarios": [{"ScenarioId": r["ScenarioId"], "Subject": r["Subject"]} for r in results],
            "total": len(results),
        })

    # ── list_workflows ────────────────────────────────────────────────────────
    elif name == "list_workflows":
        scenario_id = args["scenario_id"]
        data = client.get("Workflows", {
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
                    "WorkflowId":     r["WorkflowId"],
                    "Subject":        r["Subject"],
                    "Status":         r["Status"],
                    "OrderNumber":    r["OrderNumber"],
                    "ValidFrom":      r.get("ValidFrom", ""),
                    "CreationUser":   r.get("CreationUser", ""),
                    "LastChangeUser": r.get("LastChangeUser", ""),
                    "IsReadOnly":     r.get("IsReadOnly", False),
                }
                for r in results
            ],
            "total": len(results),
        })

    # ── get_workflow_steps ────────────────────────────────────────────────────
    elif name == "get_workflow_steps":
        workflow_id = args["workflow_id"]
        xml_text = client.get_xml(f"Workflows('{workflow_id}')/$value")
        return ok(summarize_xml(xml_text))

    # ── get_workflow_xml ──────────────────────────────────────────────────────
    elif name == "get_workflow_xml":
        workflow_id = args["workflow_id"]
        xml_text = client.get_xml(f"Workflows('{workflow_id}')/$value")
        return ok({"workflow_id": workflow_id, "xml": xml_text})

    # ── copy_workflow ─────────────────────────────────────────────────────────
    elif name == "copy_workflow":
        workflow_id = args["workflow_id"]
        xml_copy  = client.get_function_xml_resource("CopyWorkflow", {"WorkflowId": f"'{workflow_id}'"})
        xml_clean = clear_workflow_id(xml_copy)
        new_id    = client.create_workflow(xml_clean)
        return ok({
            "success":      True,
            "new_draft_id": new_id,
            "original_id":  workflow_id,
            "message":      f"Borrador '{new_id}' creado. Usa get_workflow_steps para ver sus pasos.",
        })

    # ── create_workflow ───────────────────────────────────────────────────────
    elif name == "create_workflow":
        scenario_id     = args["scenario_id"]
        subject         = args["subject"]
        description     = args.get("description", "")
        valid_from      = args.get("valid_from", "")
        purchasing_group = args.get("purchasing_group", "")

        # Obtener template XML del escenario
        xml_template = client.get_function_xml_resource(
            "CreateWorkflow", {"ScenarioId": f"'{scenario_id}'"}
        )
        xml_clean = clear_workflow_id(xml_template)

        # Aplicar cabecera
        xml_clean = update_workflow_header(
            xml_clean,
            subject=subject,
            description=description if description else None,
            valid_from=valid_from if valid_from else None,
        )

        # Condición de inicio si se especificó grupo de compras
        if purchasing_group:
            root = ET.fromstring(xml_clean)
            sv = root.find("scenarioVersion")
            prefix = f"${sv.text}$" if sv is not None and sv.text else "$0008$"
            xml_clean = update_start_condition(
                xml_clean,
                condition_id=f"{prefix}PurchasingGroup",
                parameters={"PurchasingGroup": purchasing_group},
            )

        new_id = client.create_workflow(xml_clean)
        return ok({
            "success":      True,
            "new_draft_id": new_id,
            "scenario_id":  scenario_id,
            "subject":      subject,
            "message":      f"Workflow '{new_id}' creado como borrador. Agrega pasos con add_workflow_step y actívalo con activate_workflow.",
        })

    # ── delete_workflow ───────────────────────────────────────────────────────
    elif name == "delete_workflow":
        workflow_id = args["workflow_id"]
        # Verificar que sea DRAFT antes de eliminar
        wf = client.get(f"Workflows('{workflow_id}')")
        status = wf.get("d", {}).get("Status", "")
        subject = wf.get("d", {}).get("Subject", "")
        if status not in ("DRAFT",):
            return err(f"Solo se pueden eliminar borradores (DRAFT). El workflow '{workflow_id}' está en estado '{status}'.")

        # DELETE /Workflows('{id}')
        client.delete_workflow(workflow_id)
        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "subject":     subject,
            "message":     "Borrador eliminado correctamente.",
        })

    # ── activate_workflow ─────────────────────────────────────────────────────
    elif name == "activate_workflow":
        workflow_id = args["workflow_id"]
        data   = client.post_function("ActivateWorkflow", {"WorkflowId": f"'{workflow_id}'"})
        result = data.get("d", data)
        return ok({"success": True, "workflow_id": workflow_id, "new_status": result.get("Status", "ACTIVE")})

    # ── deactivate_workflow ───────────────────────────────────────────────────
    elif name == "deactivate_workflow":
        workflow_id = args["workflow_id"]
        data   = client.post_function("DeactivateWorkflow", {"WorkflowId": f"'{workflow_id}'"})
        result = data.get("d", data)
        return ok({"success": True, "workflow_id": workflow_id, "new_status": result.get("Status", "INACTIVE")})

    # ── upgrade_workflow ──────────────────────────────────────────────────────
    elif name == "upgrade_workflow":
        workflow_id = args["workflow_id"]
        force       = args.get("force", False)
        xml_upgrade = client.get_function_xml_resource(
            "UpgradeWorkflow",
            {"WorkflowId": f"'{workflow_id}'", "Force": str(force).lower()},
        )
        xml_clean = clear_workflow_id(xml_upgrade)
        new_id    = client.create_workflow(xml_clean)
        return ok({
            "success":          True,
            "new_draft_id":     new_id,
            "original_id":      workflow_id,
            "message":          f"Borrador actualizado '{new_id}' creado. Revisa los pasos y actívalo cuando estés conforme.",
        })

    # ── update_workflow_header ────────────────────────────────────────────────
    elif name == "update_workflow_header":
        workflow_id = args["workflow_id"]
        _check_draft(client, workflow_id)

        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")
        xml_modified = update_workflow_header(
            xml_original,
            subject     = args.get("subject"),
            description = args.get("description"),
            valid_from  = args.get("valid_from"),
            valid_to    = args.get("valid_to"),
        )
        client.update_workflow(workflow_id, xml_modified)
        wf = parse_workflow(xml_modified)
        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "subject":     wf.subject,
            "description": wf.description,
            "valid_from":  wf.valid_from,
            "valid_to":    wf.valid_to,
        })

    # ── update_start_condition ────────────────────────────────────────────────
    elif name == "update_start_condition":
        workflow_id  = args["workflow_id"]
        condition_id = args["condition_id"]
        parameters   = args["parameters"]
        _check_draft(client, workflow_id)

        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")
        xml_modified = update_start_condition(xml_original, condition_id, parameters)
        client.update_workflow(workflow_id, xml_modified)
        return ok({
            "success":      True,
            "workflow_id":  workflow_id,
            "condition_id": condition_id,
            "parameters":   parameters,
        })

    # ── add_workflow_step ─────────────────────────────────────────────────────
    elif name == "add_workflow_step":
        workflow_id         = args["workflow_id"]
        name_step           = args["name"]
        user_id             = args.get("user_id")
        agent_rule_id       = args.get("agent_rule_id")
        amount_min          = args.get("amount_min")
        amount_max          = args.get("amount_max")
        currency            = args.get("currency", "CLP")
        insert_at           = args.get("insert_at_index")
        is_optional         = args.get("is_optional", "1")
        exclude_requestors  = args.get("exclude_requestors", "2")

        if not user_id and not agent_rule_id:
            return err(
                "Falta el liberador del paso. ¿Quién aprobará este paso?\n"
                + _AGENT_RULES_HELP
            )
        if user_id and agent_rule_id:
            return err("Solo puedes proporcionar user_id O agent_rule_id, no ambos.")

        _check_draft(client, workflow_id)
        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")

        principals = [{"id": user_id, "type": "USER"}] if user_id else [{"id": agent_rule_id, "type": "RULE"}]
        xml_modified = add_activity(
            xml_original, name=name_step, principals=principals,
            amount_min=amount_min, amount_max=amount_max, currency=currency,
            is_optional=is_optional, exclude_requestors=exclude_requestors,
            insert_at_index=insert_at,
        )
        client.update_workflow(workflow_id, xml_modified)

        acts = parse_activities(xml_modified)
        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "total_steps": len(acts),
            "steps": [{"index": a.index, "name": a.name, "principals": [{"id": p.id, "type": p.type} for p in a.principals]} for a in acts],
        })

    # ── delete_workflow_step ──────────────────────────────────────────────────
    elif name == "delete_workflow_step":
        workflow_id = args["workflow_id"]
        step_index  = args["step_index"]
        _check_draft(client, workflow_id)

        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")
        acts_before  = parse_activities(xml_original)
        deleted_name = acts_before[step_index].name if step_index < len(acts_before) else "?"

        xml_modified = delete_activity(xml_original, step_index)
        client.update_workflow(workflow_id, xml_modified)

        acts_after = parse_activities(xml_modified)
        return ok({
            "success":      True,
            "workflow_id":  workflow_id,
            "deleted_step": {"index": step_index, "name": deleted_name},
            "total_steps":  len(acts_after),
        })

    # ── move_workflow_step ────────────────────────────────────────────────────
    elif name == "move_workflow_step":
        workflow_id = args["workflow_id"]
        step_index  = args["step_index"]
        direction   = args["direction"]
        _check_draft(client, workflow_id)

        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")
        xml_modified = move_activity(xml_original, step_index, direction)
        client.update_workflow(workflow_id, xml_modified)

        acts = parse_activities(xml_modified)
        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "direction":   direction,
            "steps": [{"index": a.index, "name": a.name} for a in acts],
        })

    # ── rename_workflow_step ──────────────────────────────────────────────────
    elif name == "rename_workflow_step":
        workflow_id = args["workflow_id"]
        step_index  = args["step_index"]
        new_name    = args["new_name"]
        _check_draft(client, workflow_id)

        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")
        acts_before  = parse_activities(xml_original)
        old_name     = acts_before[step_index].name if step_index < len(acts_before) else "?"

        xml_modified = rename_activity(xml_original, step_index, new_name)
        client.update_workflow(workflow_id, xml_modified)
        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "step_index":  step_index,
            "old_name":    old_name,
            "new_name":    new_name,
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

        _check_draft(client, workflow_id)
        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")
        acts_before  = parse_activities(xml_original)

        principals = [{"id": user_id, "type": "USER"}] if user_id else [{"id": agent_rule_id, "type": "RULE"}]
        xml_modified = replace_principals_in_activity(xml_original, step_index, principals)
        client.update_workflow(workflow_id, xml_modified)

        acts_after = parse_activities(xml_modified)
        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "step_index":  step_index,
            "step_name":   acts_before[step_index].name if step_index < len(acts_before) else "?",
            "before":      [{"id": p.id, "type": p.type} for p in acts_before[step_index].principals] if step_index < len(acts_before) else [],
            "after":       [{"id": p.id, "type": p.type} for p in acts_after[step_index].principals] if step_index < len(acts_after) else [],
            "next_step":   "Usa activate_workflow cuando estés conforme.",
        })

    # ── update_step_conditions ────────────────────────────────────────────────
    elif name == "update_step_conditions":
        workflow_id = args["workflow_id"]
        step_index  = args["step_index"]
        amount_min  = args.get("amount_min")
        amount_max  = args.get("amount_max")
        currency    = args.get("currency", "CLP")

        if amount_min is None and amount_max is None:
            return err("Debes proporcionar al menos amount_min o amount_max.")

        _check_draft(client, workflow_id)
        xml_original = client.get_xml(f"Workflows('{workflow_id}')/$value")
        xml_modified = update_activity_conditions(xml_original, step_index, amount_min, amount_max, currency)
        client.update_workflow(workflow_id, xml_modified)

        acts = parse_activities(xml_modified)
        return ok({
            "success":     True,
            "workflow_id": workflow_id,
            "step_index":  step_index,
            "step_name":   acts[step_index].name if step_index < len(acts) else "?",
            "conditions_updated": {"amount_min": amount_min, "amount_max": amount_max, "currency": currency},
        })

    # ── get_workflow_order ────────────────────────────────────────────────────
    elif name == "get_workflow_order":
        scenario_id = args["scenario_id"]
        xml_text = client.get_xml(f"WorkflowOrders('{scenario_id}')/$value")
        # Parsear el XML de orden
        try:
            root = ET.fromstring(xml_text)
            items = []
            for wf in root.findall(".//workflowRef"):
                items.append({
                    "workflow_id": wf.get("id", ""),
                    "order":       wf.get("order", ""),
                })
            if not items:
                # Fallback: devolver XML crudo
                return ok({"scenario_id": scenario_id, "raw_xml": xml_text})
            return ok({"scenario_id": scenario_id, "order": items})
        except ET.ParseError:
            return ok({"scenario_id": scenario_id, "raw_xml": xml_text})

    # ── save_workflow_order ───────────────────────────────────────────────────
    elif name == "save_workflow_order":
        scenario_id         = args["scenario_id"]
        workflow_ids_ordered = args["workflow_ids_ordered"]

        # Obtener XML actual del orden
        xml_current = client.get_xml(f"WorkflowOrders('{scenario_id}')/$value")

        # Construir nuevo XML de orden
        try:
            root = ET.fromstring(xml_current)
        except ET.ParseError:
            root = ET.Element("workflowOrder")
            root.set("scenarioId", scenario_id)

        # Limpiar y reconstruir con el nuevo orden
        for wf_ref in root.findall(".//workflowRef"):
            root.remove(wf_ref) if wf_ref in list(root) else None

        for idx, wf_id in enumerate(workflow_ids_ordered):
            wf_ref = ET.SubElement(root, "workflowRef")
            wf_ref.set("id", wf_id)
            wf_ref.set("order", str(idx + 1))

        xml_new = ET.tostring(root, encoding="unicode", xml_declaration=True)
        client.save_workflow_order(scenario_id, xml_new)
        return ok({
            "success":    True,
            "scenario_id": scenario_id,
            "new_order":  workflow_ids_ordered,
        })

    else:
        return err(f"Herramienta desconocida: {name}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run():
    import asyncio
    asyncio.run(main())
