"""
MCP Server: SAP Fiori Flexible Workflow Manager
(versión definida en mcp_fiori_workflow.__version__)
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

  CATÁLOGO Y CARGA BATCH:
    get_scenario_catalog      → catálogo (condiciones/reglas) del escenario desde SAP
    create_workflows_batch    → crear N workflows DRAFT desde Excel (valida → confirma)
    activate_workflows_batch  → activar en lote (todo / nada / parcial)
"""

import os
import json
import logging
import xml.etree.ElementTree as ET
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from mcp_fiori_workflow import __version__
from mcp_fiori_workflow.batch import validate_batch
from mcp_fiori_workflow.params import gate_params, KNOWN_SCENARIOS
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
    build_workflow_xml,
    parse_scenario_catalog,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
# Todos los datos de conexión provienen EXCLUSIVAMENTE de variables de entorno
# definidas en la conexión MCP (claude_desktop_config.json). Nunca se hardcodea
# un host: cada conexión configurada apunta a su propio servidor SAP.
SAP_HOST     = os.environ.get("SAP_HOST",     "")
SAP_CLIENT   = os.environ.get("SAP_CLIENT",   "")
SAP_USER     = os.environ.get("SAP_USER",     "")
SAP_PASSWORD = os.environ.get("SAP_PASSWORD", "")

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
app = Server("sap-fiori-workflow", version=__version__)


def get_client() -> SAPClient:
    faltantes = [
        nombre for nombre, valor in (
            ("SAP_HOST",     SAP_HOST),
            ("SAP_CLIENT",   SAP_CLIENT),
            ("SAP_USER",     SAP_USER),
            ("SAP_PASSWORD", SAP_PASSWORD),
        ) if not valor
    ]
    if faltantes:
        raise RuntimeError(
            "Faltan variables de entorno de conexión SAP: "
            + ", ".join(faltantes)
            + ". Configúralas en la conexión MCP (claude_desktop_config.json). "
            "El host nunca está predefinido: depende de la conexión configurada."
        )
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


def _scenario_version(client: SAPClient, scenario_id: str) -> str:
    """Obtiene la scenarioVersion (ej '0008') desde la definición del escenario."""
    try:
        xml_def = client.get_function_xml_resource("CreateWorkflow", {"ScenarioId": f"'{scenario_id}'"})
        root = ET.fromstring(xml_def)
        for el in root.iter():
            tag = el.tag.split("}", 1)[1] if "}" in el.tag else el.tag
            if tag == "scenarioVersion" and el.text:
                return el.text.strip()
    except Exception:
        logger.exception(f"No se pudo obtener scenarioVersion de '{scenario_id}'")
    return "0008"


def _detect_duplicates(client: SAPClient, workflows: list) -> list:
    """
    Detecta workflows del lote cuyo subject ya existe en su escenario (cualquier
    status). Hace una sola lectura por escenario distinto. Devuelve una lista de
    {workflow_key, subject, scenario_id, existentes: [{workflow_id, status}]}.
    """
    existentes_por_escenario: dict = {}
    for sid in {wf.get("scenario_id") for wf in workflows if wf.get("scenario_id")}:
        data = client.get("Workflows", {"$filter": f"ScenarioId eq '{sid}'", "$top": "500"})
        mapa: dict = {}
        for r in data.get("d", {}).get("results", []):
            subj = (r.get("Subject") or "").strip().lower()
            mapa.setdefault(subj, []).append({"workflow_id": r.get("WorkflowId"), "status": r.get("Status")})
        existentes_por_escenario[sid] = mapa

    duplicados = []
    for wf in workflows:
        sid = wf.get("scenario_id")
        subj = (wf.get("subject") or "").strip().lower()
        mapa = existentes_por_escenario.get(sid, {})
        if subj and subj in mapa:
            duplicados.append({
                "workflow_key": wf.get("workflow_key"),
                "subject": wf.get("subject"),
                "scenario_id": sid,
                "existentes": mapa[subj],
            })
    return duplicados


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

        # ── CATÁLOGO Y CARGA BATCH ────────────────────────────────────────────
        Tool(
            name="get_scenario_catalog",
            description=(
                "Lee desde SAP la definición de un escenario (condiciones de inicio/paso, "
                "pasos y reglas de agente disponibles, con sus etiquetas). Sirve para "
                "resolver términos de negocio a códigos técnicos al cargar en batch, sin "
                "hardcodear nada. Úsalo antes de create_workflows_batch para mapear el Excel."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario_id": {"type": "string", "description": "Ej: 'WS02000471'"},
                },
                "required": ["scenario_id"],
            },
        ),
        Tool(
            name="create_workflows_batch",
            description=(
                "Crea MÚLTIPLES workflows como borrador (DRAFT) en una sola pasada, a partir "
                "de un Excel ya mapeado a JSON técnico. Flujo en 2 fases:\n"
                "1. confirm=false (default): VALIDA estructura, resuelve duplicados leyendo SAP "
                "y devuelve el plan, errores y duplicados detectados SIN escribir nada. Muestra "
                "el plan al usuario para una sola confirmación.\n"
                "2. confirm=true + duplicate_decisions: crea los borradores. Continúa ante "
                "errores y entrega un reporte (creados / omitidos_por_duplicado / fallidos).\n"
                "Si hay duplicados (mismo subject en el escenario) SIN decisión, NO ejecuta y "
                "pide decidir duplicar u omitir caso a caso. Nunca duplica por su cuenta. "
                "Los workflows quedan en DRAFT; actívalos después con activate_workflows_batch."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflows": {
                        "type": "array",
                        "description": (
                            "Lista de workflows. Cada uno: {workflow_key, scenario_id, subject, "
                            "description?, valid_from, valid_to?, start_conditions?: "
                            "[{condition_id, parameters}], steps: [{name, principal:{type:USER|RULE|"
                            "ROLE, id}, amount_min?, amount_max?, currency?, is_optional?, "
                            "exclude_requestors?, step_id?}]}."
                        ),
                        "items": {"type": "object"},
                    },
                    "confirm": {
                        "type": "boolean",
                        "default": False,
                        "description": "false = solo validar y mostrar plan. true = crear los borradores.",
                    },
                    "duplicate_decisions": {
                        "type": "object",
                        "description": (
                            "Decisión por workflow_key duplicado: 'duplicar' u 'omitir'. "
                            'Ej: {"WF-001": "duplicar", "WF-007": "omitir"}.'
                        ),
                    },
                },
                "required": ["workflows"],
            },
        ),
        Tool(
            name="activate_workflows_batch",
            description=(
                "Activa en lote los borradores indicados (DRAFT → ACTIVE). Úsalo tras "
                "create_workflows_batch y DESPUÉS de consultar al usuario si activar TODO, "
                "NADA o un subconjunto (PARCIAL). Para 'nada' simplemente no se llama. "
                "Continúa ante errores y reporta activados y fallidos."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "WorkflowIds de los borradores a activar (subconjunto elegido por el usuario).",
                    },
                },
                "required": ["workflow_ids"],
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

    # ── Compuerta de parámetros ────────────────────────────────────────────────
    # Antes de ejecutar, exige que estén resueltos todos los parámetros faltantes
    # (requeridos y opcionales). Si faltan, devuelve preguntas con opciones para
    # que Claude consulte a la persona usuaria. Nunca se asumen valores.
    faltan = gate_params(name, args)
    if faltan is not None:
        return ok(faltan)
    args.pop("_confirmado", None)

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
        scenario_id      = args["scenario_id"]
        subject          = args["subject"]
        description      = args.get("description", "")
        valid_from       = args.get("valid_from", "")
        valid_to         = args.get("valid_to", "")
        purchasing_group = args.get("purchasing_group", "")

        # NOTA: CreateWorkflow devuelve metadatos del escenario (scenarioDefinition),
        # NO un XML de workflow. Hay que construir el XML desde cero con la
        # estructura correcta que SAP espera (igual a los workflows existentes).

        # Usamos CreateWorkflow solo para obtener la scenarioVersion
        template_xml = client.get_function_xml_resource(
            "CreateWorkflow", {"ScenarioId": f"'{scenario_id}'"}
        )
        try:
            troot = ET.fromstring(template_xml)
            sv_el = troot.find("scenarioVersion")
            scenario_version = sv_el.text if sv_el is not None and sv_el.text else "0008"
        except Exception:
            scenario_version = "0008"

        prefix = f"${scenario_version}$"
        valid_from_str = valid_from if valid_from else "2026-01-01T00:00:00.000Z"

        # Construir bloques opcionales
        desc_xml     = f"  <description>{description}</description>\n" if description else ""
        valid_to_xml = f"  <validTo>{valid_to}</validTo>\n"            if valid_to    else ""

        start_cond_xml = ""
        if purchasing_group:
            start_cond_xml = (
                f"  <startConditions>\n"
                f"    <condition id=\"{prefix}PurchasingGroup\">\n"
                f"      <parameterValues>\n"
                f"        <parameterValue name=\"PurchasingGroup\">{purchasing_group}</parameterValue>\n"
                f"      </parameterValues>\n"
                f"    </condition>\n"
                f"  </startConditions>\n"
            )

        xml_new = (
            "<?xml version='1.0' encoding='utf-8'?>\n"
            f"<workflow id=\"\" formatVersion=\"3.0\" originalLanguage=\"ES\" "
            f"targetLanguage=\"ES\" originalLanguageText=\"Español\">\n"
            f"  <scenario>{scenario_id}</scenario>\n"
            f"  <scenarioVersion>{scenario_version}</scenarioVersion>\n"
            f"  <subject>{subject}</subject>\n"
            f"{desc_xml}"
            f"  <validFrom>{valid_from_str}</validFrom>\n"
            f"{valid_to_xml}"
            f"{start_cond_xml}"
            f"  <processFlow artifactId=\"80000000\">\n"
            f"  </processFlow>\n"
            f"</workflow>"
        )

        new_id = client.create_workflow(xml_new)
        return ok({
            "success":           True,
            "new_draft_id":      new_id,
            "scenario_id":       scenario_id,
            "scenario_version":  scenario_version,
            "subject":           subject,
            "purchasing_group":  purchasing_group,
            "message": (
                f"Workflow \'{new_id}\' creado como borrador vacío. "
                "Usa add_workflow_step para agregar los pasos y "
                "activate_workflow para activarlo cuando esté listo."
            ),
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

    # ── get_scenario_catalog ──────────────────────────────────────────────────
    elif name == "get_scenario_catalog":
        scenario_id = args["scenario_id"]
        xml_def = client.get_function_xml_resource("CreateWorkflow", {"ScenarioId": f"'{scenario_id}'"})
        return ok(parse_scenario_catalog(xml_def))

    # ── create_workflows_batch ────────────────────────────────────────────────
    elif name == "create_workflows_batch":
        workflows = args.get("workflows") or []
        confirm   = bool(args.get("confirm", False))
        decisions = args.get("duplicate_decisions") or {}

        if not workflows:
            return err("No se recibieron workflows. Envía 'workflows': [...] con al menos un elemento.")

        # 1. Validación estructural (sin tocar SAP)
        plan, errores = validate_batch(workflows)

        # 2. Detección de duplicados (lectura SAP)
        duplicados = _detect_duplicates(client, workflows)

        # ── Fase 1: validar y mostrar plan ──────────────────────────────────
        if not confirm:
            return ok({
                "status": "plan_validado",
                "resumen": {
                    "workflows":       len(workflows),
                    "pasos_totales":   sum(len(wf.get("steps") or []) for wf in workflows),
                    "con_errores":     len(errores),
                    "duplicados":      len(duplicados),
                },
                "plan":                  plan,
                "errores":               errores,
                "duplicados_detectados": duplicados,
                "siguiente_paso": (
                    "Revisa el plan con el usuario. Si hay errores, corrígelos en el origen. "
                    "Por cada duplicado, pregunta al usuario si DUPLICAR u OMITIR (opciones, con "
                    "atajos 'todas'). Luego vuelve a llamar con confirm=true y duplicate_decisions."
                ),
            })

        # ── Fase 2: ejecutar ────────────────────────────────────────────────
        if errores:
            return ok({
                "status":  "errores_de_validacion",
                "mensaje": "No se creó nada: corrige estos errores antes de ejecutar.",
                "errores": errores,
            })

        # Exigir decisión para cada duplicado detectado (no asumir)
        sin_decision = [
            d for d in duplicados
            if decisions.get(d["workflow_key"]) not in ("duplicar", "omitir")
        ]
        if sin_decision:
            return ok({
                "status": "necesito_decision_duplicados",
                "mensaje": "No se creó nada. Hay duplicados sin decisión.",
                "duplicados_pendientes": sin_decision,
                "instruccion": (
                    "Por cada duplicado, pregunta al usuario: ¿DUPLICAR igual u OMITIR? "
                    "(ofrece esas opciones y un atajo 'duplicar todas' / 'omitir todas'). "
                    "Vuelve a llamar con confirm=true y duplicate_decisions "
                    '(ej: {"WF-001": "duplicar"}).'
                ),
            })

        dup_keys = {d["workflow_key"] for d in duplicados}
        sv_cache: dict = {}
        creados, omitidos, fallidos = [], [], []

        for wf in workflows:
            key = wf.get("workflow_key")
            try:
                if key in dup_keys and decisions.get(key) == "omitir":
                    omitidos.append({"workflow_key": key, "subject": wf.get("subject")})
                    continue

                sid = wf["scenario_id"]
                if sid not in sv_cache:
                    sv_cache[sid] = _scenario_version(client, sid)

                xml = build_workflow_xml(
                    scenario_id      = sid,
                    scenario_version = sv_cache[sid],
                    subject          = wf["subject"],
                    description      = wf.get("description", ""),
                    valid_from       = wf["valid_from"],
                    valid_to         = wf.get("valid_to", ""),
                    start_conditions = wf.get("start_conditions") or [],
                )
                for st in (wf.get("steps") or []):
                    principal = st.get("principal") or {}
                    xml = add_activity(
                        xml,
                        name               = st["name"],
                        principals         = [{"id": principal.get("id"), "type": principal.get("type", "USER")}],
                        amount_min         = st.get("amount_min"),
                        amount_max         = st.get("amount_max"),
                        currency           = st.get("currency", "CLP"),
                        step_id            = st.get("step_id"),
                        is_optional        = str(st.get("is_optional", "1")),
                        exclude_requestors = str(st.get("exclude_requestors", "2")),
                    )

                new_id = client.create_workflow(xml)
                creados.append({
                    "workflow_key":  key,
                    "new_draft_id":  new_id,
                    "subject":       wf.get("subject"),
                    "pasos":         len(wf.get("steps") or []),
                    "duplicado":     key in dup_keys,
                })
            except Exception as e:
                logger.exception(f"Error creando workflow '{key}' del batch")
                fallidos.append({"workflow_key": key, "subject": wf.get("subject"), "error": str(e)})

        return ok({
            "status":  "ejecutado",
            "resumen": {
                "creados":   len(creados),
                "omitidos":  len(omitidos),
                "fallidos":  len(fallidos),
            },
            "creados":                 creados,
            "omitidos_por_duplicado":  omitidos,
            "fallidos":                fallidos,
            "borradores_creados":      [c["new_draft_id"] for c in creados],
            "siguiente_paso": (
                "Borradores creados en DRAFT. Revísalos en Fiori (F2190). Para activarlos, "
                "consulta al usuario si activar TODO, NADA o PARCIAL y llama a "
                "activate_workflows_batch con los WorkflowIds elegidos."
            ),
        })

    # ── activate_workflows_batch ──────────────────────────────────────────────
    elif name == "activate_workflows_batch":
        workflow_ids = args.get("workflow_ids") or []
        if not workflow_ids:
            return err("No se recibieron workflow_ids para activar.")

        activados, fallidos = [], []
        for wid in workflow_ids:
            try:
                data = client.post_function("ActivateWorkflow", {"WorkflowId": f"'{wid}'"})
                result = data.get("d", data)
                activados.append({"workflow_id": wid, "status": result.get("Status", "ACTIVE")})
            except Exception as e:
                logger.exception(f"Error activando workflow '{wid}' del batch")
                fallidos.append({"workflow_id": wid, "error": str(e)})

        return ok({
            "status":    "activacion_ejecutada",
            "resumen":   {"activados": len(activados), "fallidos": len(fallidos)},
            "activados": activados,
            "fallidos":  fallidos,
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
