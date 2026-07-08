"""Baut aus einem Claude-Report-Entwurf ein PBIR-Report-Paket (Enhanced Report
Format) zusammen, das die Fabric REST API als neuen Report anlegen kann.

Ein PBIR-Report besteht aus mehreren JSON-Dateien in einer festen Ordnerstruktur:

    definition.pbir                                  Bindung an das Semantic Model
    definition/report.json                           Report-Ebene (Theme etc.)
    definition/pages/pages.json                      Seitenreihenfolge
    definition/pages/<page>/page.json                eine Seite
    definition/pages/<page>/visuals/<vis>/visual.json  ein Visual

Alle Teile werden base64-kodiert an die API übergeben.

HINWEIS: PBIR ist formatstreng. Diese erste Fassung deckt die gängigen Visual-
Typen ab; einzelne Feld-/Rollen-Zuordnungen justieren wir ggf. anhand der
Fabric-Fehlermeldungen nach.
"""
import base64
import json
import uuid
from typing import Any

# Claude-Visual-Typ  ->  PBIR-visualType
VISUAL_TYPE = {
    "card": "card",
    "columnChart": "clusteredColumnChart",
    "barChart": "clusteredBarChart",
    "lineChart": "lineChart",
    "pieChart": "pieChart",
    "table": "tableEx",
}

# DAX-Aggregationsfunktion (für Spalten, die als Wert genutzt werden): 0 = Summe
_AGG_SUM = 0


def short_id() -> str:
    return uuid.uuid4().hex[:20]


def grid_position(index: int) -> dict[str, int]:
    """Rasterplatzierung (2 Spalten) für das n-te Visual einer Seite."""
    col, row = index % 2, index // 2
    return {"x": 40 + col * 620, "y": 40 + row * 240, "width": 580, "height": 200}


def make_part(path: str, content: dict[str, Any]) -> dict[str, str]:
    payload = base64.b64encode(json.dumps(content).encode("utf-8")).decode("ascii")
    return {"path": path, "payload": payload, "payloadType": "InlineBase64"}


def lookups(schema: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Baut Nachschlage-Maps: Measure-Name -> Tabelle, Spalten-Name -> Tabelle."""
    measure_table = {m["[Measure]"]: m["[Table]"] for m in schema["measures"]}
    column_table: dict[str, str] = {}
    for c in schema["columns"]:
        column_table.setdefault(c["[Column]"], c["[Table]"])  # erster Treffer gewinnt
    return measure_table, column_table


def _resolve(name: str, measure_table: dict[str, str],
             column_table: dict[str, str]) -> tuple[str, str, bool]:
    """Ermittelt (Tabelle, Feldname, ist_measure) – robust gegen die Schreibweisen,
    die Claude liefert: "Feld", "Tabelle.Feld" oder den Anzeigenamen "Summe von Feld"."""
    name = name.removeprefix("Summe von ").strip()  # Anzeigename -> reine Spalte
    if "." in name:
        entity, prop = name.split(".", 1)
        return entity, prop, prop in measure_table
    if name in measure_table:
        return measure_table[name], name, True
    return column_table.get(name, ""), name, False


def _field(name: str, measure_table: dict[str, str], column_table: dict[str, str]) -> dict[str, Any]:
    """Erzeugt eine PBIR-Wert-Projektion – Measure direkt, Spalte summiert."""
    entity, prop, is_measure = _resolve(name, measure_table, column_table)
    if is_measure:
        return {
            "field": {"Measure": {
                "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
            "queryRef": f"{entity}.{prop}",
            "nativeQueryRef": prop,
        }
    return {
        "field": {"Aggregation": {
            "Expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
            "Function": _AGG_SUM}},
        "queryRef": f"Sum({entity}.{prop})",
        "nativeQueryRef": f"Summe von {prop}",
    }


def _category_field(name: str, measure_table: dict[str, str],
                    column_table: dict[str, str]) -> dict[str, Any]:
    entity, prop, _ = _resolve(name, measure_table, column_table)
    return {
        "field": {"Column": {
            "Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
    }


def roles_for(pbir_type: str, cat: list, values: list) -> dict[str, Any]:
    """Ordnet Kategorie-/Wert-Projektionen den Daten-Rollen eines PBIR-Visualtyps zu.

    Nimmt bereits gebaute Projektionen (nicht Namen), damit derselbe Code sowohl
    beim Neubau als auch beim Umtypisieren bestehender Visuals greift.
    """
    if pbir_type == "card":
        return {"Values": {"projections": values}}
    if pbir_type == "tableEx":
        return {"Values": {"projections": cat + values}}
    # clusteredColumnChart, clusteredBarChart, lineChart, pieChart:
    # interner Rollenname ist "Category" (Anzeigename bei Kreis = "Legende") + "Y"
    return {"Category": {"projections": cat}, "Y": {"projections": values}}


def build_projections(measures: list[str], category: str,
                      measure_table, column_table) -> tuple[list, list]:
    """Baut (Kategorie-Projektionen, Wert-Projektionen) aus Feldnamen."""
    values = [_field(m, measure_table, column_table) for m in (measures or [])]
    cat = [_category_field(category, measure_table, column_table)] if category else []
    return cat, values


def title_property(text: str) -> list[dict[str, Any]]:
    """PBIR-Titel-Objekt (Text-Literal) für ein Visual."""
    return [{"properties": {
        "text": {"expr": {"Literal": {"Value": f"'{text}'"}}},
        "show": {"expr": {"Literal": {"Value": "true"}}},
    }}]


def _query_state(visual: dict[str, Any], measure_table, column_table) -> dict[str, Any]:
    """Baut den queryState aus einem Visual-Entwurf (Feld-Namen)."""
    cat, values = build_projections(visual.get("measures", []), visual.get("category") or "",
                                    measure_table, column_table)
    return roles_for(VISUAL_TYPE.get(visual["type"], "card"), cat, values)


def new_visual_content(visual: dict[str, Any], pos: dict[str, int],
                       measure_table, column_table) -> tuple[str, dict[str, Any]]:
    """Erzeugt (Visual-ID, visual.json-Inhalt) für ein Visual aus dem Entwurf."""
    vis_id = short_id()
    content = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/1.0.0/schema.json",
        "name": vis_id,
        "position": {**pos, "z": 0, "tabOrder": pos.get("x", 0)},
        "visual": {
            "visualType": VISUAL_TYPE.get(visual["type"], "card"),
            "query": {"queryState": _query_state(visual, measure_table, column_table)},
            "drillFilterOtherVisuals": True,
        },
    }
    return vis_id, content


def _visual_part(page: str, visual: dict[str, Any], pos: dict[str, int],
                 measure_table, column_table) -> dict[str, str]:
    vis_id, content = new_visual_content(visual, pos, measure_table, column_table)
    return make_part(f"definition/pages/{page}/visuals/{vis_id}/visual.json", content)


def build_pbir(design: dict[str, Any], schema: dict[str, Any], semantic_model_id: str,
               display_name: str) -> list[dict[str, str]]:
    """Setzt das komplette PBIR-Paket als Liste von base64-Teilen zusammen."""
    measure_table, column_table = lookups(schema)
    parts: list[dict[str, str]] = []

    # 1) Bindung an das bestehende Semantic Model (Live-Verbindung)
    parts.append(make_part("definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/1.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {
            "byConnection": {
                "connectionString": None,
                "pbiServiceModelId": None,
                "pbiModelVirtualServerName": "sobe_wowvirtualserver",
                "pbiModelDatabaseName": semantic_model_id,
                "name": "EntityDataSource",
                "connectionType": "pbiServiceXmlaStyleLive",
            }
        },
    }))

    # 2) Report-Ebene (Standard-Theme)
    parts.append(make_part("definition/report.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.0.0/schema.json",
        "themeCollection": {"baseTheme": {
            "name": "CY24SU10",
            "reportVersionAtImport": "5.55",
            "type": "SharedResources",
        }},
        "layoutOptimization": "None",
    }))

    # 2b) PBIR-Formatversion (von Fabric zwingend erwartet)
    parts.append(make_part("definition/version.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0",
    }))

    # 3) Seiten
    page_ids: list[str] = []
    for page in design["pages"]:
        page_id = short_id()
        page_ids.append(page_id)

        # Visuals rasterförmig platzieren (2 Spalten)
        for i, visual in enumerate(page["visuals"]):
            parts.append(_visual_part(page_id, visual, grid_position(i),
                                      measure_table, column_table))

        parts.append(make_part(f"definition/pages/{page_id}/page.json", {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.0.0/schema.json",
            "name": page_id,
            "displayName": page.get("name", "Seite"),
            "displayOption": "FitToPage",
            "height": 720,
            "width": 1280,
        }))

    parts.append(make_part("definition/pages/pages.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": page_ids,
        "activePageName": page_ids[0],
    }))

    return parts
