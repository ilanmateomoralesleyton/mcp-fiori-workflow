"""
Validación estructural del lote de workflows para la carga batch.

Lógica pura: NO toca SAP. Recibe el JSON canónico (técnico) que Claude arma desde
el Excel de negocio y valida que cada workflow y cada paso estén completos y
coherentes. No corta ante el primer error: acumula todos los problemas por workflow
para reportarlos juntos (política "continuar y reportar").

Estructura canónica esperada por workflow:
{
  "workflow_key": "WF-001",
  "scenario_id": "WS02000471",
  "subject": "Liberación TI",
  "description": "",
  "valid_from": "2026-01-01T00:00:00.000Z",
  "valid_to": "",
  "start_conditions": [
    {"condition_id": "$0008$PurchasingGroup", "parameters": {"PurchasingGroup": "109"}}
  ],
  "steps": [
    {"name": "Jefatura", "principal": {"type": "USER", "id": "VPARDO"},
     "amount_min": 0, "amount_max": 1000000, "currency": "CLP",
     "is_optional": "0", "exclude_requestors": "2", "step_id": null}
  ]
}
"""

from typing import Tuple

_VALID_PRINCIPAL_TYPES = {"USER", "RULE", "ROLE"}


def validate_batch(workflows: list) -> Tuple[list, list]:
    """
    Valida la estructura del lote. Devuelve (plan, errores).

    plan:    lista de resúmenes por workflow (para mostrar al usuario antes de crear).
    errores: lista de {workflow_key, problemas: [...]}. Vacía si todo está correcto.
    """
    plan: list = []
    errores: list = []
    seen_keys = set()

    for i, wf in enumerate(workflows):
        key = wf.get("workflow_key") or f"(fila {i + 1})"
        problemas: list = []

        if not wf.get("workflow_key"):
            problemas.append("Falta 'workflow_key' (identificador para agrupar y reportar).")
        elif wf["workflow_key"] in seen_keys:
            problemas.append(f"workflow_key duplicado en la planilla: '{wf['workflow_key']}'.")
        else:
            seen_keys.add(wf["workflow_key"])

        if not wf.get("scenario_id"):
            problemas.append("Falta 'scenario_id'.")
        if not wf.get("subject"):
            problemas.append("Falta 'subject' (nombre del workflow).")
        if not wf.get("valid_from"):
            problemas.append("Falta 'valid_from' (fecha de inicio). No se asume una fecha por defecto.")

        steps = wf.get("steps") or []
        if not steps:
            problemas.append("El workflow no tiene pasos (steps).")

        for j, st in enumerate(steps):
            sp = f"paso {j + 1}"
            if not st.get("name"):
                problemas.append(f"{sp}: falta 'name'.")

            pr = st.get("principal") or {}
            ptype = pr.get("type")
            if ptype not in _VALID_PRINCIPAL_TYPES:
                problemas.append(
                    f"{sp}: 'principal.type' debe ser USER, RULE o ROLE (recibido: {ptype!r})."
                )
            if not pr.get("id"):
                problemas.append(f"{sp}: falta 'principal.id' (el liberador).")

            amin = st.get("amount_min")
            amax = st.get("amount_max")
            for label, val in (("amount_min", amin), ("amount_max", amax)):
                if val is not None and not isinstance(val, int):
                    problemas.append(f"{sp}: '{label}' debe ser entero o nulo (recibido: {val!r}).")
            if isinstance(amin, int) and isinstance(amax, int) and amin > amax:
                problemas.append(
                    f"{sp}: 'amount_min' ({amin}) no puede ser mayor que 'amount_max' ({amax})."
                )

            if str(st.get("is_optional", "1")) not in ("0", "1"):
                problemas.append(f"{sp}: 'is_optional' debe ser '0' u '1'.")
            if str(st.get("exclude_requestors", "2")) not in ("1", "2"):
                problemas.append(f"{sp}: 'exclude_requestors' debe ser '1' o '2'.")

        plan.append({
            "workflow_key": wf.get("workflow_key"),
            "scenario_id": wf.get("scenario_id"),
            "subject": wf.get("subject"),
            "valid_from": wf.get("valid_from"),
            "total_pasos": len(steps),
            "pasos": [
                {
                    "name": st.get("name"),
                    "principal": st.get("principal"),
                    "amount_min": st.get("amount_min"),
                    "amount_max": st.get("amount_max"),
                    "currency": st.get("currency", "CLP"),
                }
                for st in steps
            ],
        })

        if problemas:
            errores.append({"workflow_key": wf.get("workflow_key") or key, "problemas": problemas})

    return plan, errores
