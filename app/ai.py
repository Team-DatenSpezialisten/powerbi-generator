"""Claude-Integration: übersetzt natürliche Sprache in DAX bzw. in einen
Report-Entwurf – jeweils mit dem Semantic-Model-Schema als Kontext.

Nutzt Structured Outputs (output_config.format), damit Claude garantiert
gültiges JSON nach unserem Schema liefert – kein Nachparsen von Prosa nötig.
"""
import json
from typing import Any

from anthropic import Anthropic

from .config import settings

_client = Anthropic(api_key=settings.anthropic_api_key)


def _complete_json(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    msg = _client.messages.create(
        model=settings.claude_model,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    # Bei aktivem Thinking steht zuerst ein thinking-Block; den ersten
    # text-Block herausgreifen. Der enthält das schema-konforme JSON.
    for block in msg.content:
        if block.type == "text":
            return json.loads(block.text)
    raise RuntimeError("Keine Textantwort von Claude erhalten.")


# ── NL -> DAX ──────────────────────────────────────────────────
_DAX_SCHEMA = {
    "type": "object",
    "properties": {
        "dax": {"type": "string", "description": "Ausführbare DAX-Query, beginnend mit EVALUATE"},
        "explanation": {"type": "string", "description": "Kurze Erklärung auf Deutsch"},
    },
    "required": ["dax", "explanation"],
    "additionalProperties": False,
}


def question_to_dax(question: str, schema_text: str) -> dict[str, Any]:
    system = (
        "Du bist DAX-Experte für Power-BI-Semantic-Models. Erzeuge zu der Frage "
        "des Nutzers eine korrekte, ausführbare DAX-Query, die mit EVALUATE beginnt. "
        "Verwende AUSSCHLIESSLICH die unten aufgeführten Tabellen, Spalten und Measures. "
        "Erfinde keine Spalten. Gib eine kompakte, aggregierte Ergebnistabelle zurück.\n\n"
        f"Verfügbares Model-Schema:\n{schema_text}"
    )
    return _complete_json(system, question, _DAX_SCHEMA)


# ── NL -> Report-Entwurf (Phase 2: Basis für PBIR-Generierung) ──
_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "visuals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["card", "columnChart", "barChart",
                                             "lineChart", "pieChart", "table"],
                                },
                                "title": {"type": "string"},
                                "measures": {"type": "array", "items": {"type": "string"}},
                                "category": {"type": "string",
                                             "description": "Optionale Achsen-/Gruppierungsspalte, sonst leer"},
                            },
                            "required": ["type", "title", "measures", "category"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "visuals"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "pages"],
    "additionalProperties": False,
}


_VISUAL_TYPES = "card, columnChart, barChart, lineChart, pieChart, table"

# Flaches Schema (kein verschachteltes Objekt, wenige Enums) – sonst lehnt die
# Structured-Outputs-API mit "Schema is too complex" ab.
_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Kurze Erklärung der Änderungen auf Deutsch"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_visual", "remove_visual", "change_type",
                                 "set_title", "set_fields", "set_position", "auto_layout",
                                 "add_page", "remove_page"],
                    },
                    "page_id": {"type": "string"},
                    "visual_id": {"type": "string", "description": "Ziel-Visual (visual_id aus der Übersicht)"},
                    "visual_type": {"type": "string",
                                    "description": f"Visual-Typ für add_visual/change_type. Erlaubt: {_VISUAL_TYPES}"},
                    "title": {"type": "string", "description": "Titel (add_visual/set_title)"},
                    "measures": {"type": "array", "items": {"type": "string"},
                                 "description": "Werte-Felder (add_visual/set_fields)"},
                    "category": {"type": "string", "description": "Kategorie-/Achsen-Spalte (add_visual/set_fields)"},
                    "page_name": {"type": "string"},
                    "x": {"type": "integer"}, "y": {"type": "integer"},
                    "width": {"type": "integer"}, "height": {"type": "integer"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "edits"],
    "additionalProperties": False,
}


def plan_edits(request: str, report_summary: list, schema_text: str) -> dict[str, Any]:
    """Übersetzt einen Änderungswunsch in strukturierte Aktionen auf dem Report."""
    system = (
        "Du passt ein bestehendes Power-BI-Dashboard an. Unten siehst du die "
        "aktuellen Seiten und Visuals (mit visual_id) sowie das Datenmodell. "
        "Übersetze den Wunsch des Nutzers in eine minimale Liste von Aktionen. "
        "Referenziere bestehende Visuals über ihre visual_id. Für neue Visuals "
        "nur existierende Measures/Spalten verwenden. Ändere nur, was gewünscht ist. "
        "set_title/set_fields/set_position/change_type/remove_visual brauchen zwingend "
        "die visual_id eines BESTEHENDEN Visuals. Ein neues Visual samt Titel wird in "
        "EINER add_visual-Aktion angelegt (Titel im Feld title) – niemals set_title auf "
        "ein gerade neu hinzugefügtes Visual anwenden. "
        "Für Layout-Wünsche wie 'ordne an', 'verteile', 'fülle die Fläche', 'skaliere auf "
        "volle Größe', 'gleichmäßig anordnen' nutze GENAU EINE auto_layout-Aktion – nicht "
        "mehrere set_position. Feldnamen immer als reine Spalte/Measure (z. B. 'Erlös Netto', "
        "nicht 'Summe von Erlös Netto').\n\n"
        f"Aktuelle Struktur:\n{json.dumps(report_summary, ensure_ascii=False, indent=2)}\n\n"
        f"Datenmodell:\n{schema_text}"
    )
    return _complete_json(system, request, _EDIT_SCHEMA)


def design_report(prompt: str, schema_text: str) -> dict[str, Any]:
    """Lässt Claude ein Dashboard-Layout entwerfen (Seiten + Visuals).

    Dieser Entwurf ist die KI-seitig anspruchsvollste Stelle und bereits jetzt
    testbar. Der Entwurf wird in Phase 2 in ein PBIR-Report-Definition-JSON
    übersetzt und via Fabric REST API in den Workspace geschrieben.
    """
    system = (
        "Du bist BI-Analyst und entwirfst ein Power-BI-Dashboard. Wähle passende "
        "Visual-Typen und ordne ihnen Measures und Kategorie-Spalten aus dem Model zu. "
        "Verwende nur existierende Measures/Spalten. Halte es fokussiert (1-2 Seiten, "
        "3-6 Visuals).\n\n"
        f"Verfügbares Model-Schema:\n{schema_text}"
    )
    return _complete_json(system, prompt, _REPORT_SCHEMA)
