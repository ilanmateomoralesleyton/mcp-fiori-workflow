"""
Compuerta de parámetros (parameter gate) para el MCP de Flexible Workflows.

Objetivo (requisito de negocio):
  - El MCP SIEMPRE debe consultar a la persona usuaria por los parámetros que
    falten ANTES de ejecutar una herramienta, incluso si son opcionales.
  - Cada pregunta ofrece opciones para seleccionar y la ÚLTIMA opción siempre
    permite escribir un valor manual (texto libre).
  - El servidor NUNCA asume valores ni inventa defaults. Un campo solo queda
    vacío / sin cambios si la persona usuaria lo elige explícitamente.

Mecánica:
  - `gate_params(tool, args)` revisa los parámetros declarados para la
    herramienta. Un parámetro se considera "no resuelto" cuando su clave NO está
    presente en `args` (ausencia = aún no preguntado). Si la clave está presente
    —aunque su valor sea ""— se considera una decisión explícita de la persona.
  - Si faltan parámetros requeridos, o si quedan opcionales sin preguntar y la
    llamada no trae `_confirmado: true`, el gate devuelve un payload
    `necesito_datos` con las preguntas y opciones. Claude debe entonces preguntar
    a la persona y volver a llamar a la herramienta con los valores recogidos y
    `_confirmado: true`.
  - El flag `_confirmado` lo añade Claude SOLO después de haber preguntado; sirve
    para no volver a pedir los opcionales que la persona decidió omitir.
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Catálogos de opciones reutilizables ─────────────────────────────────────────

KNOWN_SCENARIOS = {
    "WS02000458": "Liberación global de solicitud de pedido",
    "WS02000471": "Liberación de posición solicitud pedido",
    "WS00800157": "Workflow de pedido (genérico)",
    "WS00800173": "Workflow de pedido (genérico 2)",
    "WS02000434": "Workflow adicional",
    "WS02000438": "Workflow adicional 2",
}

SCENARIO_OPTIONS = [f"{sid} — {name}" for sid, name in KNOWN_SCENARIOS.items()]

AGENT_RULE_OPTIONS = [
    "$0008$/RULE/MMPUR_MGR_RQSTR — Gestor del solicitante",
    "$0008$/RULE/MMPUR_MGR_L_APPR — Gestor del último aprobador",
    "$0008$/RULE/MMPUR_MGR_OF_MGR — Gestor del gestor del solicitante",
    "$0008$/RULE/MMPUR_ACC_RESP — Responsable del objeto de imputación",
    "$0008$/RULE/MMPUR_PR_BD_AGNT — Determinación mediante BAdI",
    "Usuario SAP específico — indica el ID de usuario (ej: VPARDO)",
]

CURRENCY_OPTIONS    = ["CLP — Peso chileno", "USD — Dólar", "EUR — Euro"]
FORCE_OPTIONS       = ["false — no forzar", "true — forzar aunque haya advertencias"]
IS_OPTIONAL_OPTIONS = ["1 — opcional (se puede saltar)", "0 — obligatorio"]
EXCLUDE_REQ_OPTIONS = ["2 — excluir solicitantes como agentes", "1 — no excluir"]
DIRECTION_OPTIONS   = ["up — subir el paso", "down — bajar el paso"]
START_CONDITION_OPTIONS = [
    "$0008$PurchasingGroup — Grupo de compras",
    "$0008$PurchasingOrganization — Organización de compras",
    "$0008$CreationIndicator — Indicador de creación",
]

# Texto fijo que SIEMPRE va como última opción de cada pregunta.
FREE_TEXT_OPTION = "✏️ Otro — escribir un valor manualmente"
DEFAULT_EMPTY_LABEL = "∅ Dejar vacío / no especificar (se omite el parámetro)"

# Herramientas cuyo "liberador" se resuelve con user_id O agent_rule_id
# (mutuamente excluyentes): se pregunta como un único parámetro compuesto.
AGENT_TOOLS = {"add_workflow_step", "replace_step_agent"}


# ── Especificación de parámetros ────────────────────────────────────────────────

@dataclass
class Param:
    name: str
    question: str
    options: list = field(default_factory=list)
    required: bool = False
    hint: str = ""
    # Etiqueta de la opción "no especificar". Para campos de edición donde vacío
    # significaría sobrescribir, se usa una etiqueta de "no cambiar".
    empty_label: str = DEFAULT_EMPTY_LABEL


_NO_CHANGE = "∅ Dejar sin cambiar (no modificar este campo)"

_WF_ID = lambda q="¿Sobre qué workflow (WorkflowId)?": Param(  # noqa: E731
    "workflow_id", q, required=True,
    hint="Usa list_workflows para ver los WorkflowId disponibles.",
)
_STEP_IDX = Param(
    "step_index", "¿Sobre qué paso? (índice, 0 = primer paso)", required=True,
    hint="Usa get_workflow_steps para ver los pasos y sus índices.",
)


PARAM_SPECS: dict = {
    # ── LECTURA ─────────────────────────────────────────────────────────────
    "list_scenarios": [
        Param("scenario_ids", "¿Qué escenarios deseas listar?",
              options=SCENARIO_OPTIONS + ["Todos los escenarios conocidos"],
              required=False,
              hint="Para 'Todos', omite el parámetro o pasa la lista completa de IDs."),
    ],
    "list_workflows": [
        Param("scenario_id", "¿De qué escenario deseas listar los workflows?",
              options=SCENARIO_OPTIONS, required=True),
    ],
    "get_workflow_steps": [_WF_ID("¿De qué workflow deseas ver los pasos?")],
    "get_workflow_xml":   [_WF_ID("¿De qué workflow deseas el XML completo?")],

    # ── CICLO DE VIDA ───────────────────────────────────────────────────────
    "copy_workflow":   [_WF_ID("¿Qué workflow activo deseas copiar a borrador?")],
    "delete_workflow": [_WF_ID("¿Qué borrador (DRAFT) deseas eliminar?")],
    "activate_workflow":   [_WF_ID("¿Qué borrador deseas activar?")],
    "deactivate_workflow": [_WF_ID("¿Qué workflow activo deseas desactivar?")],
    "upgrade_workflow": [
        _WF_ID("¿Qué workflow deseas actualizar a la nueva versión del escenario?"),
        Param("force", "¿Forzar la actualización aunque haya advertencias?",
              options=FORCE_OPTIONS, required=False),
    ],
    "create_workflow": [
        Param("scenario_id", "¿En qué escenario crear el workflow?",
              options=SCENARIO_OPTIONS, required=True),
        Param("subject", "¿Cuál será el nombre (subject) del workflow?", required=True),
        Param("valid_from", "¿Desde qué fecha es válido? (ISO 8601)", required=True,
              hint="Ej: 2026-01-01T00:00:00.000Z"),
        Param("description", "¿Descripción del workflow?", required=False),
        Param("valid_to", "¿Hasta qué fecha es válido? (ISO 8601)", required=False,
              empty_label="∅ Sin límite (omitir validTo)"),
        Param("purchasing_group", "¿Condición de inicio por grupo de compras?",
              required=False, hint="Ej: 109. Vacío = sin condición de grupo de compras.",
              empty_label="∅ Sin condición de inicio"),
    ],

    # ── EDICIÓN DE CABECERA ─────────────────────────────────────────────────
    "update_workflow_header": [
        _WF_ID("¿Qué workflow DRAFT deseas modificar?"),
        Param("subject", "¿Nuevo nombre (subject)?", required=False, empty_label=_NO_CHANGE),
        Param("description", "¿Nueva descripción?", required=False, empty_label=_NO_CHANGE),
        Param("valid_from", "¿Nueva fecha de inicio (ISO 8601)?", required=False, empty_label=_NO_CHANGE),
        Param("valid_to", "¿Nueva fecha de fin (ISO 8601)?", required=False, empty_label=_NO_CHANGE),
    ],
    "update_start_condition": [
        _WF_ID("¿Qué workflow DRAFT deseas modificar?"),
        Param("condition_id", "¿Qué condición de inicio deseas establecer?",
              options=START_CONDITION_OPTIONS, required=True),
        Param("parameters", "¿Cuál es el valor de la condición?", required=True,
              hint='Objeto JSON nombre→valor, ej: {"PurchasingGroup": "109"}'),
    ],

    # ── EDICIÓN DE PASOS ────────────────────────────────────────────────────
    "add_workflow_step": [
        _WF_ID("¿A qué workflow DRAFT agregar el paso?"),
        Param("name", "¿Cuál es el nombre del paso?", required=True),
        # liberador → parámetro compuesto (ver AGENT_TOOLS)
        Param("amount_min", "¿Monto mínimo (exclusivo) para que aplique el paso?",
              required=False, hint="Ej: 1000001 = 'mayor a 1.000.000'."),
        Param("amount_max", "¿Monto máximo (inclusivo)?", required=False,
              empty_label="∅ Sin tope superior"),
        Param("currency", "¿En qué moneda son los montos?",
              options=CURRENCY_OPTIONS, required=False),
        Param("insert_at_index", "¿En qué posición insertar el paso?",
              required=False, hint="0 = antes del primero.",
              empty_label="∅ Al final del workflow"),
        Param("is_optional", "¿El paso es opcional u obligatorio?",
              options=IS_OPTIONAL_OPTIONS, required=False),
        Param("exclude_requestors", "¿Excluir a los solicitantes como agentes?",
              options=EXCLUDE_REQ_OPTIONS, required=False),
    ],
    "delete_workflow_step": [_WF_ID(), _STEP_IDX],
    "move_workflow_step": [
        _WF_ID(), _STEP_IDX,
        Param("direction", "¿Hacia dónde mover el paso?",
              options=DIRECTION_OPTIONS, required=True),
    ],
    "rename_workflow_step": [
        _WF_ID(), _STEP_IDX,
        Param("new_name", "¿Cuál es el nuevo nombre del paso?", required=True),
    ],
    "replace_step_agent": [
        _WF_ID(), _STEP_IDX,
        # liberador → parámetro compuesto (ver AGENT_TOOLS)
    ],
    "update_step_conditions": [
        _WF_ID(), _STEP_IDX,
        Param("amount_min", "¿Monto mínimo (exclusivo)?", required=False),
        Param("amount_max", "¿Monto máximo (inclusivo)?", required=False,
              empty_label="∅ Sin tope superior"),
        Param("currency", "¿En qué moneda?", options=CURRENCY_OPTIONS, required=False),
    ],

    # ── CATÁLOGO ────────────────────────────────────────────────────────────
    # Las herramientas batch (create_workflows_batch / activate_workflows_batch)
    # quedan a propósito FUERA del gate: su control es el flujo confirm + las
    # consultas de duplicados/activación, no la compuerta parámetro a parámetro.
    "get_scenario_catalog": [
        Param("scenario_id", "¿De qué escenario quieres leer el catálogo (condiciones/reglas)?",
              options=SCENARIO_OPTIONS, required=True),
    ],

    # ── ORDEN ───────────────────────────────────────────────────────────────
    "get_workflow_order": [
        Param("scenario_id", "¿De qué escenario ver el orden de prioridad?",
              options=SCENARIO_OPTIONS, required=True),
    ],
    "save_workflow_order": [
        Param("scenario_id", "¿De qué escenario modificar el orden?",
              options=SCENARIO_OPTIONS, required=True),
        Param("workflow_ids_ordered", "¿Cuál es el nuevo orden de WorkflowIds?",
              required=True,
              hint="Lista en orden de prioridad (primero = mayor). Usa get_workflow_order primero."),
    ],
}


# ── Construcción de preguntas ───────────────────────────────────────────────────

def _build_options(param: Param) -> list:
    """Arma la lista de opciones: reales + (vacío si opcional) + texto libre al final."""
    opts = list(param.options)
    if not param.required:
        opts.append(param.empty_label)
    opts.append(FREE_TEXT_OPTION)  # SIEMPRE la última: texto libre
    return opts


def _question(param: Param) -> dict:
    q = {
        "parametro": param.name,
        "pregunta": param.question,
        "opciones": _build_options(param),
    }
    if param.hint:
        q["pista"] = param.hint
    return q


def _agent_question() -> dict:
    """Pregunta compuesta del liberador (user_id O agent_rule_id)."""
    return {
        "parametro": "liberador",
        "pregunta": "¿Quién será el liberador / agente de este paso?",
        "opciones": AGENT_RULE_OPTIONS + [FREE_TEXT_OPTION],
        "pista": (
            "Si eliges una regla, vuelve a llamar con agent_rule_id=<ID de la regla>. "
            "Si eliges un usuario SAP, vuelve a llamar con user_id=<usuario>."
        ),
    }


# ── Gate ─────────────────────────────────────────────────────────────────────────

def gate_params(tool: str, args: dict) -> Optional[dict]:
    """
    Revisa los parámetros de `tool`. Devuelve un payload `necesito_datos` si hay
    que preguntar a la persona usuaria, o None si se puede ejecutar.

    No muta `args` (el flag `_confirmado` lo retira el servidor antes de despachar).
    """
    specs = PARAM_SPECS.get(tool, [])
    confirmado = bool(args.get("_confirmado"))

    # Requeridos ausentes: siempre se preguntan (aunque venga _confirmado).
    requeridos = [s for s in specs if s.required and s.name not in args]
    # Opcionales aún no preguntados: solo si la llamada no está confirmada.
    opcionales = [] if confirmado else [s for s in specs if not s.required and s.name not in args]

    preguntas = [_question(s) for s in requeridos + opcionales]

    # Liberador compuesto para herramientas de agente.
    if tool in AGENT_TOOLS and "user_id" not in args and "agent_rule_id" not in args:
        preguntas.insert(0, _agent_question())

    if not preguntas:
        return None

    return {
        "status": "necesito_datos",
        "herramienta": tool,
        "instruccion": (
            f"Faltan datos para ejecutar '{tool}'. Antes de continuar, haz a la persona "
            "usuaria UNA pregunta por cada ítem de 'preguntas', mostrando sus 'opciones' "
            "como una lista numerada. La ÚLTIMA opción de cada pregunta SIEMPRE permite "
            "escribir un valor manual. No asumas, no inventes ni completes valores por tu "
            "cuenta. Si la persona elige la opción de dejar vacío / sin cambiar, NO incluyas "
            "ese parámetro al volver a llamar. Cuando tengas todas las respuestas, vuelve a "
            f"llamar a '{tool}' con los parámetros resueltos y además \"_confirmado\": true."
        ),
        "preguntas": preguntas,
    }
