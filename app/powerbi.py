"""Dünner Wrapper um die Power BI REST API.

Zwei Kernfunktionen für den PoC:
  - get_schema():  Tabellen/Spalten/Measures des Semantic Models auslesen
                   (als Kontext für Claude)
  - execute_dax(): eine DAX-Query gegen das Dataset ausführen und Zeilen zurückgeben
"""
from typing import Any

import requests

from .auth import get_access_token
from .config import settings

_BASE = "https://api.powerbi.com/v1.0/myorg"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }


def list_datasets(workspace_id: str | None = None) -> list[dict[str, str]]:
    """Listet alle Semantic Models (Datasets) eines Workspace auf.

    Basis für die Dashboard-Auswahl: Frontend/Nutzer wählt hier, gegen welches
    Model gearbeitet werden soll.
    """
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_BASE}/groups/{workspace_id}/datasets"
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"datasets {resp.status_code}: {resp.text}")
    return [{"id": d["id"], "name": d["name"]} for d in resp.json()["value"]]


def get_report(report_id: str, workspace_id: str | None = None) -> dict[str, Any]:
    """Report-Metadaten inkl. embedUrl und datasetId."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_BASE}/groups/{workspace_id}/reports/{report_id}"
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"report {resp.status_code}: {resp.text}")
    return resp.json()


def get_report_dataset_id(report_id: str, workspace_id: str | None = None) -> str | None:
    """Ermittelt das Datenmodell (Dataset) eines bestehenden Reports."""
    return get_report(report_id, workspace_id).get("datasetId")


def generate_embed_token(report_id: str, workspace_id: str | None = None) -> str:
    """Erzeugt ein Embed-Token (View) für die Vorschau im Frontend.

    Voraussetzung: Der Ziel-Workspace liegt auf einer Kapazität (Premium/Fabric/
    Embedded) – sonst lehnt Power BI die Token-Erzeugung ab (Lizenzfehler).
    """
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_BASE}/groups/{workspace_id}/reports/{report_id}/GenerateToken"
    resp = requests.post(url, headers=_headers(), json={"accessLevel": "View"}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"GenerateToken {resp.status_code}: {resp.text}")
    return resp.json()["token"]


def execute_dax(dax: str, dataset_id: str | None = None) -> list[dict[str, Any]]:
    """Führt eine DAX-Query über die executeQueries-API aus und liefert die Zeilen.

    Voraussetzung: In den Power-BI-Tenant-Einstellungen muss
    'Dataset Execute Queries REST API' für Service Principals aktiviert sein,
    und der Service Principal braucht mindestens Build-/Read-Rechte am Dataset.
    """
    dataset_id = dataset_id or settings.pbi_dataset_id
    url = f"{_BASE}/datasets/{dataset_id}/executeQueries"
    body = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }
    resp = requests.post(url, headers=_headers(), json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"executeQueries {resp.status_code}: {resp.text}")

    # Struktur: results[0].tables[0].rows -> Liste von {SpaltenName: Wert}
    return resp.json()["results"][0]["tables"][0]["rows"]


def get_schema(dataset_id: str | None = None) -> dict[str, Any]:
    """Liest Spalten und Measures des Semantic Models über DAX-INFO-Funktionen.

    Nutzt INFO.VIEW.* (liefert lesbare Namen statt interner IDs). Falls euer
    Model diese Funktionen nicht kennt (sehr alte Engine), auf INFO.COLUMNS()/
    INFO.MEASURES() umstellen.
    """
    columns = execute_dax(
        'EVALUATE SELECTCOLUMNS(INFO.VIEW.COLUMNS(), '
        '"Table", [Table], "Column", [Name], "DataType", [DataType])',
        dataset_id,
    )
    measures = execute_dax(
        'EVALUATE SELECTCOLUMNS(INFO.VIEW.MEASURES(), '
        '"Table", [Table], "Measure", [Name], "Expression", [Expression])',
        dataset_id,
    )
    return {"columns": columns, "measures": measures}


def schema_as_text(schema: dict[str, Any]) -> str:
    """Formatiert das Schema kompakt als Text für den Claude-Prompt."""
    lines: list[str] = ["# Tabellen & Spalten"]
    by_table: dict[str, list[str]] = {}
    for c in schema["columns"]:
        by_table.setdefault(c["[Table]"], []).append(f'{c["[Column]"]} ({c["[DataType]"]})')
    for table, cols in sorted(by_table.items()):
        lines.append(f"- {table}: " + ", ".join(cols))

    lines.append("\n# Measures")
    for m in schema["measures"]:
        lines.append(f'- \'{m["[Table]"]}\'[{m["[Measure]"]}]')
    return "\n".join(lines)
