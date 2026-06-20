"""
Helpers para leer y modificar el XML de Flexible Workflow de SAP.

Estructura real del XML (capturada de REUTTER S/4HANA 2023):

  <workflow id="...">
    <processFlow artifactId="80000000">
      <activity multiInstance="0" artifactId="80000001">
        <name>Liberación de 0 a 1.000.000 CLP</name>
        <step id="$0008$ReleasePurchaseRequisitionItem"/>
        <conditions>...</conditions>
        <assignedPrincipals>
          <assignedPrincipal id="VPARDO" type="USER"/>       ← usuario específico
          <!-- O para regla estándar: -->
          <assignedPrincipal id="$0008$/RULE/MMPUR_MGR_RQSTR" type="RULE"/>
        </assignedPrincipals>
        <properties>
          <property id="$0008$IsOptional">1</property>
        </properties>
      </activity>
    </processFlow>
  </workflow>

Nota: El XML de CopyWorkflow devuelve id="" — el id real se asigna cuando SAP
crea el borrador vía POST a /Workflows.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActivityInfo:
    index: int
    artifact_id: str
    name: str
    step_id: str
    principals: list[dict]   # lista de {id, type}  type: USER | RULE | ROLE
    is_optional: str
    exclude_requestors: str


def parse_activities(xml_text: str) -> list[ActivityInfo]:
    """Extrae los pasos (activities) del processFlow."""
    root = ET.fromstring(xml_text)
    activities = []
    process_flow = root.find("processFlow")
    if process_flow is None:
        return []

    for idx, act in enumerate(process_flow.findall("activity")):
        artifact_id = act.get("artifactId", str(idx))
        name_el     = act.find("name")
        step_el     = act.find("step")
        principals_el = act.find("assignedPrincipals")

        name    = name_el.text if name_el is not None else f"Paso {idx+1}"
        step_id = step_el.get("id", "") if step_el is not None else ""

        principals = []
        if principals_el is not None:
            for p in principals_el.findall("assignedPrincipal"):
                principals.append({
                    "id":   p.get("id", ""),
                    "type": p.get("type", "USER"),
                })

        # Propiedades
        is_optional        = "1"
        exclude_requestors = "1"
        for prop in act.findall("properties/property"):
            pid = prop.get("id", "")
            if "$IsOptional"        in pid: is_optional        = prop.text or "1"
            if "$ExcludeRequestors" in pid: exclude_requestors = prop.text or "1"

        activities.append(ActivityInfo(
            index=idx,
            artifact_id=artifact_id,
            name=name,
            step_id=step_id,
            principals=principals,
            is_optional=is_optional,
            exclude_requestors=exclude_requestors,
        ))

    return activities


def replace_principals_in_activity(
    xml_text: str,
    activity_index: int,
    new_principals: list[dict],
) -> str:
    """
    Reemplaza los assignedPrincipals de un paso específico.

    new_principals: lista de dicts con keys:
      - 'id':   usuario SAP (ej: 'VPARDO') o ID de regla (ej: '$0008$/RULE/MMPUR_MGR_RQSTR')
      - 'type': 'USER' | 'RULE' | 'ROLE'  (default: 'USER')

    Ejemplos:
      # Usuario específico:
      [{"id": "JPEREZ", "type": "USER"}]

      # Regla estándar:
      [{"id": "$0008$/RULE/MMPUR_MGR_RQSTR", "type": "RULE"}]

      # Múltiples usuarios:
      [{"id": "VPARDO", "type": "USER"}, {"id": "NPENAFIEL", "type": "USER"}]
    """
    root = ET.fromstring(xml_text)
    process_flow = root.find("processFlow")
    if process_flow is None:
        raise ValueError("El XML no tiene <processFlow>")

    activities = process_flow.findall("activity")
    if activity_index >= len(activities):
        raise IndexError(
            f"step_index {activity_index} inválido. "
            f"El workflow tiene {len(activities)} pasos (0-{len(activities)-1})."
        )

    act = activities[activity_index]

    # Eliminar assignedPrincipals existente
    old = act.find("assignedPrincipals")
    if old is not None:
        act.remove(old)

    # Construir nuevo bloque assignedPrincipals
    ap_el = ET.Element("assignedPrincipals")
    for p in new_principals:
        p_el = ET.SubElement(ap_el, "assignedPrincipal")
        p_el.set("id",   p["id"])
        p_el.set("type", p.get("type", "USER"))

    # Insertar después de <conditions> si existe, sino después de <step>
    _insert_after_first(act, ["conditions", "step"], ap_el)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _insert_after_first(parent: ET.Element, tags_priority: list[str], new_el: ET.Element):
    """Inserta new_el después del primer hijo que coincida con alguno de los tags."""
    children = list(parent)
    insert_pos = None
    for tag in tags_priority:
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


def summarize_xml(xml_text: str) -> dict:
    """Devuelve un resumen legible del workflow."""
    root = ET.fromstring(xml_text)

    wf_id       = root.get("id", "")
    scenario    = _text(root, "scenario")
    subject     = _text(root, "subject")
    description = _text(root, "description")
    valid_from  = _text(root, "validFrom")

    activities = parse_activities(xml_text)

    steps_summary = []
    for act in activities:
        principal_desc = []
        for p in act.principals:
            if p["type"] == "USER":
                principal_desc.append(f"{p['id']} (usuario)")
            elif p["type"] == "RULE":
                principal_desc.append(f"{p['id']} (regla)")
            else:
                principal_desc.append(f"{p['id']} ({p['type']})")

        steps_summary.append({
            "index":       act.index,
            "artifact_id": act.artifact_id,
            "name":        act.name,
            "step_id":     act.step_id,
            "principals":  act.principals,
            "principals_desc": ", ".join(principal_desc) if principal_desc else "(sin agente asignado)",
            "is_optional": act.is_optional,
            "exclude_requestors": act.exclude_requestors,
        })

    return {
        "workflow_id":  wf_id,
        "scenario":     scenario,
        "subject":      subject,
        "description":  description,
        "valid_from":   valid_from,
        "total_steps":  len(steps_summary),
        "steps":        steps_summary,
    }


def clear_workflow_id(xml_text: str) -> str:
    """Limpia el id del workflow para usarlo como nuevo borrador (id='')."""
    root = ET.fromstring(xml_text)
    root.set("id", "")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return child.text if child is not None and child.text else ""
