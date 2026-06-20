# mcp-fiori-workflow

Servidor MCP para gestionar **Flexible Workflows de SAP S/4HANA** vía OData (`SWF_FLEX_DEF_SRV`).

Controla la app Fiori **F2190 - Gestionar Workflows** directamente desde Claude Desktop.

## Instalación

```bash
pip install git+https://github.com/ilanmateomoralesleyton/mcp-fiori-workflow.git
```

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

Reinicia Claude Desktop.

## Variables de entorno

| Variable | Descripción | Ejemplo |
|----------|-------------|---------|
| `SAP_HOST` | URL base del servidor SAP | `https://servidor:44300` |
| `SAP_CLIENT` | Mandante SAP | `200` |
| `SAP_USER` | Usuario SAP | `IMORALES` |
| `SAP_PASSWORD` | Contraseña SAP | `****` |

## Herramientas disponibles

| Herramienta | Descripción |
|-------------|-------------|
| `list_scenarios` | Lista escenarios de workflow (WS02000471, etc.) |
| `list_workflows` | Lista workflows de un escenario con su estado |
| `get_workflow_steps` | Muestra pasos y agentes de un workflow |
| `copy_workflow` | Crea borrador editable desde un workflow activo |
| `replace_step_agent` | Cambia el liberador de un paso específico |
| `activate_workflow` | Activa un borrador (DRAFT → ACTIVE) |
| `deactivate_workflow` | Desactiva un workflow activo |
| `get_workflow_xml` | Obtiene el XML completo (inspección técnica) |

## Flujo típico: cambiar un liberador

```
1. list_workflows("WS02000471")
2. get_workflow_steps(workflow_id)
3. copy_workflow(workflow_id)          → devuelve new_draft_id
4. replace_step_agent(new_draft_id, step_index=0, user_id="NUEVO_USUARIO")
5. activate_workflow(new_draft_id)
6. deactivate_workflow(workflow_id)    → desactiva el anterior
```

## Reglas de agente disponibles (WS02000471)

| ID | Descripción |
|----|-------------|
| `$0008$/RULE/MMPUR_MGR_RQSTR` | Gestor del iniciador del workflow |
| `$0008$/RULE/MMPUR_MGR_L_APPR` | Gestor del último aprobador |
| `$0008$/RULE/MMPUR_MGR_OF_MGR` | Gestor del gestor del iniciador |
| `$0008$/RULE/MMPUR_ACC_RESP` | Responsable del objeto de imputación |
| `$0008$/RULE/MMPUR_PR_BD_AGNT` | Determinación mediante BAdI |

## Compatibilidad

- SAP S/4HANA 2023 FPS03 (probado en REUTTER)
- Python 3.11+
