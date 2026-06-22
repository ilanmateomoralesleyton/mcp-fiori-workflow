# mcp-fiori-workflow

Servidor **MCP** (Model Context Protocol) para gestionar **Flexible Workflows de SAP S/4HANA** vía OData (`SWF_FLEX_DEF_SRV`), controlando la app Fiori **F2190 – Gestionar Workflows** directamente desde Claude (Desktop).

Permite leer, copiar, editar, crear y activar liberaciones (workflows flexibles) — de forma individual o en **carga masiva desde un Excel** — sin entrar a la GUI de SAP.

---

## Índice

- [Características](#características)
- [Instalación](#instalación)
- [Configuración en Claude Desktop](#configuración-en-claude-desktop)
- [Variables de entorno](#variables-de-entorno)
- [Arquitectura](#arquitectura)
- [Herramientas disponibles](#herramientas-disponibles)
- [Comportamiento: consulta de parámetros](#comportamiento-consulta-de-parámetros)
- [Flujos típicos](#flujos-típicos)
- [Carga masiva desde Excel (batch)](#carga-masiva-desde-excel-batch)
- [Escenarios conocidos](#escenarios-conocidos)
- [Reglas de agente (escenario de compras)](#reglas-de-agente-escenario-de-compras)
- [Compatibilidad](#compatibilidad)
- [Notas técnicas](#notas-técnicas)

---

## Características

- **Paridad con la app F2190:** lectura, ciclo de vida (copiar/crear/activar/desactivar/actualizar versión), edición de cabecera, edición de pasos y orden de prioridad.
- **Carga masiva (batch):** crea N liberaciones desde una planilla de negocio, con validación previa, detección de duplicados y reporte por categoría.
- **Parametrizable y escalable:** arranca con el escenario de compras pero el catálogo de condiciones y reglas se **lee de SAP**, no se hardcodea, de modo que escala a otros escenarios sin tocar código.
- **No asume nada:** ante parámetros faltantes el servidor consulta al usuario con opciones; los valores vacíos solo se aplican si se eligen explícitamente.
- **Conexión 100 % por configuración:** ningún host queda predefinido en el código.

---

## Instalación

```bash
pip install git+https://github.com/ilanmateomoralesleyton/mcp-fiori-workflow.git
```

Requiere **Python 3.11+**.

---

## Configuración en Claude Desktop

Edita `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sap-fiori-workflow": {
      "command": "mcp-fiori-workflow",
      "env": {
        "SAP_HOST": "https://tu-servidor-sap:44300",
        "SAP_CLIENT": "200",
        "SAP_USER": "tu_usuario",
        "SAP_PASSWORD": "tu_password"
      }
    }
  }
}
```

Reinicia Claude Desktop. Puedes definir **varias conexiones** (a distintos servidores/mandantes) repitiendo el bloque con otro nombre y otras variables de entorno.

---

## Variables de entorno

Todas son **obligatorias**. No hay valores por defecto: el host y el mandante dependen de la conexión configurada. Si falta alguna, el servidor responde con un error indicando cuáles.

| Variable | Descripción | Ejemplo |
|----------|-------------|---------|
| `SAP_HOST` | URL base del servidor SAP | `https://servidor:44300` |
| `SAP_CLIENT` | Mandante SAP | `200` |
| `SAP_USER` | Usuario SAP | `IMORALES` |
| `SAP_PASSWORD` | Contraseña SAP | `****` |

---

## Arquitectura

```
src/mcp_fiori_workflow/
├── server.py        Capa MCP: define las herramientas y despacha cada llamada.
├── sap_client.py    Capa HTTP: httpx + Basic Auth + token CSRF; GET/POST/PUT/DELETE OData.
├── workflow_xml.py  Capa de dominio: parsea y construye el XML del workflow (pasos,
│                    agentes, condiciones, cabecera, catálogo del escenario).
├── params.py        Compuerta de parámetros (gate) y catálogos de opciones.
├── batch.py         Validación estructural del lote (lógica pura, sin SAP).
├── __init__.py      __version__ (fuente única de la versión).
└── __main__.py      Entry point: python -m mcp_fiori_workflow
```

Flujo de edición habitual: se trabaja siempre sobre un **borrador (DRAFT)**. Toda edición valida que el workflow esté en estado `DRAFT`; si está activo, primero se copia con `copy_workflow`.

---

## Herramientas disponibles

### Lectura

| Herramienta | Descripción |
|-------------|-------------|
| `list_scenarios` | Lista los escenarios de Flexible Workflow disponibles. |
| `list_workflows` | Lista los workflows de un escenario (activos, borradores, inactivos) con su estado y orden. |
| `get_workflow_steps` | Muestra los pasos de un workflow: nombre, agente asignado y condiciones de monto. |
| `get_workflow_xml` | Devuelve el XML completo del workflow (inspección técnica). |

### Ciclo de vida

| Herramienta | Descripción |
|-------------|-------------|
| `copy_workflow` | Crea un borrador (DRAFT) editable a partir de un workflow activo. |
| `create_workflow` | Crea un workflow nuevo desde cero (borrador vacío). |
| `delete_workflow` | Elimina un borrador (solo estado DRAFT). |
| `activate_workflow` | Activa un borrador (DRAFT → ACTIVE). |
| `deactivate_workflow` | Desactiva un workflow activo (ACTIVE → INACTIVE). |
| `upgrade_workflow` | Actualiza un workflow a la nueva versión del escenario SAP. |

### Edición de cabecera

| Herramienta | Descripción |
|-------------|-------------|
| `update_workflow_header` | Cambia nombre (subject), descripción y fechas de validez. |
| `update_start_condition` | Modifica/crea una condición de inicio (p. ej. grupo de compras). |

### Edición de pasos

| Herramienta | Descripción |
|-------------|-------------|
| `add_workflow_step` | Agrega un nuevo paso (con agente, condiciones de monto, posición, etc.). |
| `delete_workflow_step` | Elimina un paso por índice. |
| `move_workflow_step` | Sube o baja un paso en el orden del workflow. |
| `rename_workflow_step` | Renombra un paso. |
| `replace_step_agent` | Cambia el agente/liberador de un paso. |
| `update_step_conditions` | Modifica las condiciones de monto de un paso. |

### Orden de prioridad

| Herramienta | Descripción |
|-------------|-------------|
| `get_workflow_order` | Muestra el orden de prioridad de los workflows de un escenario. |
| `save_workflow_order` | Modifica el orden de prioridad. |

### Catálogo y carga masiva

| Herramienta | Descripción |
|-------------|-------------|
| `get_scenario_catalog` | Lee de SAP el catálogo del escenario (condiciones, reglas, pasos) para resolver términos de negocio → códigos técnicos. |
| `create_workflows_batch` | Carga masiva: crea N workflows DRAFT desde un Excel ya mapeado (valida → confirma). |
| `activate_workflows_batch` | Activa borradores en lote (todo / nada / parcial). |

---

## Comportamiento: consulta de parámetros

Para evitar que el servidor asuma valores, las herramientas individuales pasan por una **compuerta de parámetros** (`params.py`):

- Si falta cualquier parámetro **requerido**, o un **opcional** aún no preguntado, el servidor no ejecuta: devuelve preguntas con **opciones para seleccionar**, donde la **última opción siempre permite escribir un valor manual** (texto libre).
- **No se asume ni se inventa nada.** Un campo solo queda vacío / sin cambios si el usuario lo elige explícitamente.
- Distinción clave: una clave **ausente** se pregunta; una clave presente —aunque su valor sea `""`— se respeta como decisión consciente.
- Tras preguntar, Claude vuelve a llamar con los valores y `_confirmado: true`, lo que evita re-preguntar los opcionales que el usuario decidió omitir (los requeridos siempre se exigen).

> Las herramientas batch (`create_workflows_batch`, `activate_workflows_batch`) quedan **fuera** de la compuerta: su control son las fases `confirm` y las consultas de duplicados/activación.

---

## Flujos típicos

### Cambiar el liberador de un paso

```
1. list_workflows("WS02000471")
2. get_workflow_steps(workflow_id)
3. copy_workflow(workflow_id)          → devuelve new_draft_id
4. replace_step_agent(new_draft_id, step_index=0, user_id="NUEVO_USUARIO")
5. activate_workflow(new_draft_id)
6. deactivate_workflow(workflow_id)    → desactiva el anterior
```

### Crear un workflow desde cero

```
1. create_workflow(scenario_id, subject, valid_from, ...)  → new_draft_id
2. add_workflow_step(new_draft_id, name, user_id|agent_rule_id, amount_min, ...)
3. (repetir add_workflow_step por cada paso)
4. activate_workflow(new_draft_id)
```

---

## Carga masiva desde Excel (batch)

Diseñada para crear muchas liberaciones a la vez desde una planilla de **negocio** (no técnica).

```
1. Adjuntas el Excel; Claude lo mapea a JSON técnico, usando get_scenario_catalog
   para resolver términos de negocio → códigos del escenario.
2. create_workflows_batch(confirm=false)   → valida, detecta duplicados y muestra
                                              el plan SIN escribir nada en SAP.
3. (Si hay duplicados) decides caso a caso: duplicar u omitir.
4. create_workflows_batch(confirm=true, duplicate_decisions=...)
                                            → crea los borradores (DRAFT), continúa
                                              ante errores y reporta por categoría.
5. activate_workflows_batch(workflow_ids)  → activas TODO / NADA / PARCIAL.
```

Principios:

- **Compras primero, parametrizable:** el catálogo se lee de SAP, así escala a otros escenarios sin tocar código.
- **Nunca duplica ni asume:** los duplicados (mismo `subject` en el escenario, cualquier estado) se consultan caso a caso; si quedan sin decisión, no crea nada.
- **Continúa ante errores:** entrega un reporte `creados / omitidos_por_duplicado / fallidos`.

### Plantilla de Excel (negocio)

**Cabecera (agrupada por `workflow_key`):** `proceso` · `subject` · `descripcion` · `valido_desde` · `valido_hasta` · `condicion_inicio` + `valor_condicion_inicio` (ej. "Grupo de compras" = 109)

**Por paso (una fila):** `paso_orden` · `paso_nombre` · `metodo_determinacion` · `aprobador_valor` · `monto_min` · `monto_max` · `moneda` · `opcional` (Sí/No) · `excluir_solicitantes` (Sí/No)

### JSON canónico (técnico) que recibe `create_workflows_batch`

```json
{
  "workflows": [
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
         "is_optional": "0", "exclude_requestors": "2"},
        {"name": "Gerencia", "principal": {"type": "RULE", "id": "$0008$/RULE/MMPUR_MGR_RQSTR"},
         "amount_min": 1000001}
      ]
    }
  ]
}
```

- **Aprobador = `principal` (método, valor):** `type` es `USER` (usuario SAP), `RULE` (regla de determinación) o `ROLE` (rol).
- `is_optional`: `"1"` opcional, `"0"` obligatorio. `exclude_requestors`: `"2"` excluir solicitantes, `"1"` no excluir.

---

## Escenarios conocidos

El servidor reconoce estos escenarios (solo como etiquetas de ayuda; la autoridad real la tiene SAP):

| ID | Descripción |
|----|-------------|
| `WS02000458` | Liberación global de solicitud de pedido |
| `WS02000471` | Liberación de posición de solicitud de pedido |
| `WS00800157` | Workflow de pedido (genérico) |
| `WS00800173` | Workflow de pedido (genérico 2) |
| `WS02000434` | Workflow adicional |
| `WS02000438` | Workflow adicional 2 |

---

## Reglas de agente (escenario de compras)

| ID | Descripción |
|----|-------------|
| `$0008$/RULE/MMPUR_MGR_RQSTR` | Gestor del iniciador del workflow |
| `$0008$/RULE/MMPUR_MGR_L_APPR` | Gestor del último aprobador |
| `$0008$/RULE/MMPUR_MGR_OF_MGR` | Gestor del gestor del iniciador |
| `$0008$/RULE/MMPUR_ACC_RESP` | Responsable del objeto de imputación |
| `$0008$/RULE/MMPUR_PR_BD_AGNT` | Determinación mediante BAdI |

> Estas reglas se ofrecen como ayuda/fallback. En escenarios distintos al de compras, las reglas válidas se obtienen con `get_scenario_catalog`.

---

## Compatibilidad

- SAP S/4HANA 2023 FPS03 (probado en REUTTER).
- Python 3.11+.
- OData V2 (`SWF_FLEX_DEF_SRV`), autenticación Basic + token CSRF.

---

## Notas técnicas

- **Versión única:** definida en `src/mcp_fiori_workflow/__init__.py` (`__version__`) y leída dinámicamente por `pyproject.toml`.
- **Escritura de workflows:** `POST /Workflows` con el XML completo del workflow; edición vía `PUT /Workflows('{id}')/$value`. La carga batch construye el XML completo (cabecera + pasos) y hace un solo POST por workflow, reutilizando la sesión/CSRF para todo el lote.
- **`get_scenario_catalog` / versión del escenario:** dependen del formato de `scenarioDefinition` que devuelve el FunctionImport `CreateWorkflow` en cada sistema; el parseo es best-effort e incluye el XML crudo para inspección.
