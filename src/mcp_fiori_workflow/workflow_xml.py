"""
Helpers para leer y modificar el XML de Flexible Workflow de SAP.

Estructura completa del XML (extraída del código fuente NW_APS_BPM_SWE/LIB):

<workflow id="..." formatVersion="3.0" originalLanguage="ES">
  <scenario>WS02000471</scenario>
  <scenarioVersion>0008</scenarioVersion>
  <subject>Nombre del workflow</subject>
  <description>Descripción opcional</description>
  <validFrom>2026-01-01T00:00:00.000Z</validFrom>
  <validTo>2026-12-31T00:00:00.000Z</validTo>   <!-- opcional -->
  <startConditions>
    <condition id="$0008$PurchasingGroup">
      <parameterValues>
        <parameterValue name="PurchasingGroup">109</parameterValue>
      </parameterValues>
    </condition>
  </startConditions>
  <processFlow artifactId="80000000">
    <activity multiInstance="0" artifactId="80000001">
      <name hasChanged="true">Nombre del paso</name>
      <step id="$0008$ReleasePurchaseRequisitionItem"/>
      <conditions>
        <condition id="$0008$TotalNetAmountGreater">
          <parameterValues>
            <parameterValue name="NetAmount">0</parameterValue>
            <parameterValue name="Currency">CLP</parameterValue>
          </parameterValues>
        </condition>
      </conditions>
      <assignedPrincipals>
        <assignedPrincipal id="VPARDO" type="USER"/>
        <!-- O regla estándar: -->
        <assignedPrincipal id="$0008$/RULE/MMPUR_MGR_RQSTR" type="RULE"/>
      </assignedPrincipals>
      <outcomeActions>
        <outcomeAction id="REJECTED">
          <action id="$0008$cancel"/>
        </outcomeAction>
      </outcomeActions>
      <properties>
        <property id="$0008$IsOptional">1</property>
        <property id="$0008$ExcludeRequestors">2</property>
      </properties>
    </activity>
  </processFlow>
</workflow>

Orden de elementos en <activity> (del SEQUENCES dict en WorkflowUtil-dbg.js):
  name, taskTitle, step, conditions, deadlines, assignedPrincipals,
  agentRule, teamFunction, team, outcomeActions, properties
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
import copy


# ── Tipos ──────────────────────────────────────────────────────────────────────

@dataclass
class PrincipalInfo:
    id: str
    type: str  # USER | RULE | ROLE


@dataclass
class ConditionInfo:
    id: str
    parameters: dict  # name → value


@dataclass
class ActivityInfo:
    index: int
    artifact_id: str
    name: str
    step_id: str
    principals: list[PrincipalInfo]
    conditions: list[ConditionInfo]
    is_optional: str
    exclude_requestors: str
    multi_instance: str


@dataclass
class WorkflowInfo:
    workflow_id: str
    scenario: str
    scenario_version: str
    subject: str
    description: str
    valid_from: str
    valid_to: str
    start_conditions: list[ConditionInfo]
    activities: list[ActivityInfo]


# ── Parseo ─────────────────────────────────────────────────────────────────────

def _parse_conditions(parent_el: ET.Element) -> list[ConditionInfo]:
    """Parsea el bloque <conditions> o <startConditions> de un elemento."""
    result = []
    for cond in parent_el.findall("condition"):
        cond_id = cond.get("id", "")
        params = {}
        for pv in cond.findall("parameterValues/parameterValue"):
            params[pv.get("name", "")] = pv.text or ""
        result.append(ConditionInfo(id=cond_id, parameters=params))
    return result


def parse_activities(xml_text: str) -> list[ActivityInfo]:
    """Extrae todos los pasos (activities) del processFlow."""
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        return []

    activities = []
    for idx, act in enumerate(process_flow.findall("activity")):
        artifact_id   = act.get("artifactId", str(idx))
        multi_instance = act.get("multiInstance", "0")

        name_el  = act.find("name")
        step_el  = act.find("step")
        name     = name_el.text if name_el is not None else f"Paso {idx+1}"
        step_id  = step_el.get("id", "") if step_el is not None else ""

        # Principals (assignedPrincipals)
        principals = []
        ap_el = act.find("assignedPrincipals")
        if ap_el is not None:
            for p in ap_el.findall("assignedPrincipal"):
                principals.append(PrincipalInfo(id=p.get("id",""), type=p.get("type","USER")))

        # Conditions
        conds_el = act.find("conditions")
        conditions = _parse_conditions(conds_el) if conds_el is not None else []

        # Properties
        is_optional        = "1"
        exclude_requestors = "1"
        for prop in act.findall("properties/property"):
            pid = prop.get("id", "")
            if "$IsOptional"        in pid: is_optional        = prop.text or "1"
            if "$ExcludeRequestors" in pid: exclude_requestors = prop.text or "1"

        activities.append(ActivityInfo(
            index=idx,
            artifact_id=artifact_id,
            multi_instance=multi_instance,
            name=name,
            step_id=step_id,
            principals=principals,
            conditions=conditions,
            is_optional=is_optional,
            exclude_requestors=exclude_requestors,
        ))

    return activities


def parse_workflow(xml_text: str) -> WorkflowInfo:
    """Parsea el XML completo del workflow y devuelve un WorkflowInfo."""
    root = ET.fromstring(xml_text)

    sc_el = root.find("startConditions")
    start_conditions = _parse_conditions(sc_el) if sc_el is not None else []

    return WorkflowInfo(
        workflow_id      = root.get("id", ""),
        scenario         = _text(root, "scenario"),
        scenario_version = _text(root, "scenarioVersion"),
        subject          = _text(root, "subject"),
        description      = _text(root, "description"),
        valid_from       = _text(root, "validFrom"),
        valid_to         = _text(root, "validTo"),
        start_conditions = start_conditions,
        activities       = parse_activities(xml_text),
    )


def summarize_xml(xml_text: str) -> dict:
    """Devuelve un resumen legible del workflow para mostrar en Claude."""
    wf = parse_workflow(xml_text)

    steps = []
    for act in wf.activities:
        principal_desc = []
        for p in act.principals:
            label = "usuario" if p.type == "USER" else "regla" if p.type == "RULE" else p.type
            principal_desc.append(f"{p.id} ({label})")

        cond_desc = []
        for c in act.conditions:
            net = c.parameters.get("NetAmount", "")
            cur = c.parameters.get("Currency", "CLP")
            if "Greater" in c.id and net:
                cond_desc.append(f"monto > {int(net):,} {cur}")
            elif "LessOrEqual" in c.id and net:
                cond_desc.append(f"monto ≤ {int(net):,} {cur}")
            else:
                cond_desc.append(f"{c.id}: {c.parameters}")

        steps.append({
            "index":             act.index,
            "artifact_id":       act.artifact_id,
            "name":              act.name,
            "step_id":           act.step_id,
            "principals":        [{"id": p.id, "type": p.type} for p in act.principals],
            "principals_desc":   ", ".join(principal_desc) or "(sin agente)",
            "conditions_desc":   ", ".join(cond_desc) or "(sin condición de monto)",
            "is_optional":       act.is_optional,
            "multi_instance":    act.multi_instance,
        })

    start_cond_desc = []
    for c in wf.start_conditions:
        for k, v in c.parameters.items():
            start_cond_desc.append(f"{k}={v}")

    return {
        "workflow_id":       wf.workflow_id,
        "scenario":          wf.scenario,
        "scenario_version":  wf.scenario_version,
        "subject":           wf.subject,
        "description":       wf.description,
        "valid_from":        wf.valid_from,
        "valid_to":          wf.valid_to,
        "start_conditions":  start_cond_desc,
        "total_steps":       len(steps),
        "steps":             steps,
    }


# ── Modificaciones de principals ───────────────────────────────────────────────

def replace_principals_in_activity(
    xml_text: str,
    activity_index: int,
    new_principals: list[dict],
) -> str:
    """
    Reemplaza los assignedPrincipals de un paso específico.

    new_principals: lista de dicts {id, type}
      - type: 'USER' | 'RULE' | 'ROLE'
    """
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        raise ValueError("El XML no tiene <processFlow>")

    activities = process_flow.findall("activity")
    _check_index(activity_index, len(activities))
    act = activities[activity_index]

    # Eliminar assignedPrincipals existente
    old = act.find("assignedPrincipals")
    if old is not None:
        act.remove(old)

    # Construir nuevo bloque
    ap_el = ET.Element("assignedPrincipals")
    for p in new_principals:
        p_el = ET.SubElement(ap_el, "assignedPrincipal")
        p_el.set("id",   p["id"])
        p_el.set("type", p.get("type", "USER"))

    _insert_after_tags(act, ["conditions", "step"], ap_el)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ── Modificaciones de condiciones ──────────────────────────────────────────────

def _build_amount_conditions(
    amount_min: Optional[int],
    amount_max: Optional[int],
    currency: str,
    prefix: str = "$0008$",
) -> ET.Element:
    """Construye el elemento <conditions> con condiciones de monto."""
    conds_el = ET.Element("conditions")

    if amount_min is not None:
        c = ET.SubElement(conds_el, "condition")
        c.set("id", f"{prefix}TotalNetAmountGreater")
        pv = ET.SubElement(c, "parameterValues")
        _add_pv(pv, "NetAmount", str(amount_min))
        _add_pv(pv, "Currency", currency)

    if amount_max is not None:
        c2 = ET.SubElement(conds_el, "condition")
        c2.set("id", f"{prefix}TotalNetAmountLessOrEqual")
        pv2 = ET.SubElement(c2, "parameterValues")
        _add_pv(pv2, "NetAmount", str(amount_max))
        _add_pv(pv2, "Currency", currency)

    return conds_el


def update_activity_conditions(
    xml_text: str,
    activity_index: int,
    amount_min: Optional[int] = None,
    amount_max: Optional[int] = None,
    currency: str = "CLP",
) -> str:
    """Actualiza las condiciones de monto de un paso existente."""
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        raise ValueError("El XML no tiene <processFlow>")

    activities = process_flow.findall("activity")
    _check_index(activity_index, len(activities))
    act = activities[activity_index]

    old_conds = act.find("conditions")
    if old_conds is not None:
        act.remove(old_conds)

    if amount_min is not None or amount_max is not None:
        prefix = _get_prefix(root)
        new_conds = _build_amount_conditions(amount_min, amount_max, currency, prefix)
        _insert_after_tags(act, ["step"], new_conds)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ── Agregar / eliminar pasos ───────────────────────────────────────────────────

def add_activity(
    xml_text: str,
    name: str,
    principals: list[dict],
    amount_min: Optional[int] = None,
    amount_max: Optional[int] = None,
    currency: str = "CLP",
    step_id: Optional[str] = None,
    is_optional: str = "1",
    exclude_requestors: str = "2",
    insert_at_index: Optional[int] = None,
) -> str:
    """Agrega un nuevo paso (activity) al processFlow."""
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        raise ValueError("El XML no tiene <processFlow>")

    prefix = _get_prefix(root)
    if step_id is None:
        step_id = f"{prefix}ReleasePurchaseRequisitionItem"

    # Calcular nuevo artifactId
    existing = process_flow.findall("activity")
    max_aid = 80000000
    for act in existing:
        try:
            aid = int(act.get("artifactId", "0"))
            if aid > max_aid:
                max_aid = aid
        except ValueError:
            pass
    new_artifact_id = str(max_aid + 1)

    # Construir nueva activity
    new_act = ET.Element("activity")
    new_act.set("multiInstance", "0")
    new_act.set("artifactId", new_artifact_id)

    name_el = ET.SubElement(new_act, "name")
    name_el.set("hasChanged", "true")
    name_el.text = name

    step_el = ET.SubElement(new_act, "step")
    step_el.set("id", step_id)

    if amount_min is not None or amount_max is not None:
        conds = _build_amount_conditions(amount_min, amount_max, currency, prefix)
        new_act.append(conds)

    ap_el = ET.SubElement(new_act, "assignedPrincipals")
    for p in principals:
        p_el = ET.SubElement(ap_el, "assignedPrincipal")
        p_el.set("id",   p["id"])
        p_el.set("type", p.get("type", "USER"))

    # outcomeActions (necesario para que SAP acepte el paso)
    oa_el = ET.SubElement(new_act, "outcomeActions")
    rej_el = ET.SubElement(oa_el, "outcomeAction")
    rej_el.set("id", "REJECTED")
    act_el = ET.SubElement(rej_el, "action")
    act_el.set("id", f"{prefix}cancel")

    props_el = ET.SubElement(new_act, "properties")
    opt_el = ET.SubElement(props_el, "property")
    opt_el.set("id", f"{prefix}IsOptional")
    opt_el.text = is_optional
    excl_el = ET.SubElement(props_el, "property")
    excl_el.set("id", f"{prefix}ExcludeRequestors")
    excl_el.text = exclude_requestors

    # Insertar en posición
    if insert_at_index is not None and 0 <= insert_at_index < len(existing):
        process_flow.insert(list(process_flow).index(existing[insert_at_index]), new_act)
    else:
        process_flow.append(new_act)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def delete_activity(xml_text: str, activity_index: int) -> str:
    """Elimina un paso del processFlow por índice."""
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        raise ValueError("El XML no tiene <processFlow>")

    activities = process_flow.findall("activity")
    _check_index(activity_index, len(activities))
    process_flow.remove(activities[activity_index])
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def move_activity(xml_text: str, activity_index: int, direction: str) -> str:
    """
    Mueve un paso hacia arriba o hacia abajo en el processFlow.
    direction: 'up' | 'down'
    """
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        raise ValueError("El XML no tiene <processFlow>")

    activities = process_flow.findall("activity")
    _check_index(activity_index, len(activities))
    n = len(activities)

    if direction == "up" and activity_index == 0:
        raise ValueError("El paso ya está en la primera posición.")
    if direction == "down" and activity_index == n - 1:
        raise ValueError("El paso ya está en la última posición.")

    act = activities[activity_index]
    process_flow.remove(act)

    new_index = activity_index - 1 if direction == "up" else activity_index + 1
    activities_after = process_flow.findall("activity")
    if new_index >= len(activities_after):
        process_flow.append(act)
    else:
        process_flow.insert(list(process_flow).index(activities_after[new_index]), act)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ── Modificaciones de cabecera del workflow ────────────────────────────────────

def update_workflow_header(
    xml_text: str,
    subject: Optional[str] = None,
    description: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
) -> str:
    """
    Actualiza campos de cabecera del workflow.
    valid_from / valid_to: formato ISO 8601, ej '2026-01-01T00:00:00.000Z'
    """
    root = ET.fromstring(xml_text)

    if subject is not None:
        el = _ensure_child(root, "subject")
        el.text = subject

    if description is not None:
        el = _ensure_child(root, "description")
        el.text = description

    if valid_from is not None:
        el = _ensure_child(root, "validFrom")
        el.text = valid_from

    if valid_to is not None:
        el = _ensure_child(root, "validTo")
        el.text = valid_to
    elif valid_to == "":
        # Eliminar validTo si se pasa string vacío
        vt = root.find("validTo")
        if vt is not None:
            root.remove(vt)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def update_start_condition(
    xml_text: str,
    condition_id: str,
    parameters: dict,
) -> str:
    """
    Actualiza o crea una condición de inicio (startConditions).

    condition_id: ej '$0008$PurchasingGroup'
    parameters: dict {name: value}, ej {'PurchasingGroup': '109'}
    """
    root = ET.fromstring(xml_text)
    sc_el = root.find("startConditions")
    if sc_el is None:
        sc_el = ET.SubElement(root, "startConditions")
        _reorder_workflow_children(root)

    # Buscar condición existente
    existing_cond = None
    for c in sc_el.findall("condition"):
        if c.get("id") == condition_id:
            existing_cond = c
            break

    if existing_cond is None:
        existing_cond = ET.SubElement(sc_el, "condition")
        existing_cond.set("id", condition_id)

    # Reemplazar parameterValues
    pv_el = existing_cond.find("parameterValues")
    if pv_el is not None:
        existing_cond.remove(pv_el)

    pv_el = ET.SubElement(existing_cond, "parameterValues")
    for k, v in parameters.items():
        _add_pv(pv_el, k, v)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def rename_activity(xml_text: str, activity_index: int, new_name: str) -> str:
    """Renombra un paso del workflow."""
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        raise ValueError("El XML no tiene <processFlow>")

    activities = process_flow.findall("activity")
    _check_index(activity_index, len(activities))
    act = activities[activity_index]

    name_el = act.find("name")
    if name_el is None:
        name_el = ET.SubElement(act, "name")
    name_el.set("hasChanged", "true")
    name_el.text = new_name

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def clear_workflow_id(xml_text: str) -> str:
    """Limpia el id del workflow para crear uno nuevo (id='')."""
    root = ET.fromstring(xml_text)
    root.set("id", "")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return child.text if child is not None and child.text else ""


def _check_index(idx: int, total: int):
    if idx < 0 or idx >= total:
        raise IndexError(
            f"Índice {idx} inválido. El workflow tiene {total} pasos (0-{total-1})."
        )


def _insert_after_tags(parent: ET.Element, tags: list[str], new_el: ET.Element):
    """Inserta new_el después del último hijo que coincida con alguno de los tags."""
    children = list(parent)
    insert_pos = None
    for tag in tags:
        for i, child in enumerate(children):
            if child.tag == tag:
                insert_pos = i + 1
                break
        if insert_pos is not None:
            break
    if insert_pos is None:
        parent.append(new_el)
    else:
        parent.insert(insert_pos, new_el)


def _add_pv(pv_parent: ET.Element, name: str, value: str):
    pv = ET.SubElement(pv_parent, "parameterValue")
    pv.set("name", name)
    pv.text = value


def _get_prefix(root: ET.Element) -> str:
    """Extrae el prefijo de versión del escenario, ej '$0008$'."""
    sv = root.find("scenarioVersion")
    if sv is not None and sv.text:
        return f"${sv.text}$"
    return "$0008$"


def _ensure_child(parent: ET.Element, tag: str) -> ET.Element:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    return el


# Orden de hijos del elemento <workflow> según WorkflowUtil SEQUENCES
_WORKFLOW_CHILD_ORDER = [
    "scenario", "scenarioVersion", "subject", "description",
    "validFrom", "validTo", "applicationObject", "customPrincipalGroups",
    "startConditions", "processFlow", "reviewFlow"
]


def _reorder_workflow_children(root: ET.Element):
    """Reordena los hijos directos de <workflow> según el orden canónico de SAP."""
    children = list(root)
    root_tag = root.tag
    if root_tag != "workflow":
        return
    children.sort(key=lambda el: (
        _WORKFLOW_CHILD_ORDER.index(el.tag)
        if el.tag in _WORKFLOW_CHILD_ORDER else 999
    ))
    for child in root:
        root.remove(child)
    for child in children:
        root.append(child)
