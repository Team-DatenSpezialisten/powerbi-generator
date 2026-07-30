"""Claude-Integration: übersetzt natürliche Sprache in DAX bzw. in einen
Report-Entwurf – jeweils mit dem Semantic-Model-Schema als Kontext.

Nutzt Structured Outputs (output_config.format), damit Claude garantiert
gültiges JSON nach unserem Schema liefert – kein Nachparsen von Prosa nötig.
"""
import json
from typing import Any

from anthropic import Anthropic

from .config import settings

# max_retries: SDK wiederholt 429/5xx/529 automatisch mit Backoff –
# höher gesetzt, damit kurzzeitige API-Überlastung (529) den Nutzer nicht erreicht.
_client = Anthropic(api_key=settings.anthropic_api_key, max_retries=5)


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
        "Erfinde keine Spalten. Setze Tabellennamen immer in einfache Anführungszeichen "
        "(z. B. 'Verkäufe'[Umsatz]). Gib eine kompakte, aggregierte Ergebnistabelle zurück.\n\n"
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


_MEASURE_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "string", "description": "Existierende Tabelle, an die das Measure gehängt wird (Anker)"},
        "name": {"type": "string", "description": "Sprechender Measure-Name"},
        "dax": {"type": "string", "description": "Nur der DAX-Ausdruck, ohne 'Measure =' Präfix"},
        "format_string": {"type": "string", "description": "z. B. 0.00 | 0.0% | #,##0 €  (leer = Standard)"},
        "explanation": {"type": "string", "description": "Kurze Erklärung auf Deutsch"},
    },
    "required": ["table", "name", "dax", "format_string", "explanation"],
    "additionalProperties": False,
}


def generate_measure(request: str, schema_text: str) -> dict[str, Any]:
    """Erzeugt zu einem Wunsch ein neues DAX-Measure fürs Semantic Model."""
    system = (
        "Du bist DAX-Experte für Power-BI-Semantic-Models. Erzeuge zum Wunsch des "
        "Nutzers EIN neues Measure. Verwende ausschließlich die unten aufgeführten "
        "Tabellen, Spalten und vorhandenen Measures. 'table' muss eine EXISTIERENDE "
        "Tabelle sein (nur der Anker fürs Measure – meist die Faktentabelle mit den "
        "relevanten Spalten). 'dax' ist NUR der Ausdruck. Setze Tabellennamen in DAX "
        "IMMER in einfache Anführungszeichen (z. B. SUM('Verkäufe'[Umsatz]), "
        "COUNTROWS('Verkäufe')) – wichtig bei Umlauten/Leerzeichen. "
        "Wähle ein sinnvolles format_string.\n\n"
        f"Verfügbares Model-Schema:\n{schema_text}"
    )
    return _complete_json(system, request, _MEASURE_SCHEMA)


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
        "Diagramme (columnChart/barChart/lineChart/pieChart) und table brauchen ZWINGEND "
        "mindestens einen Wert in 'measures' (ein Measure oder eine numerische Spalte) – "
        "sonst bleiben sie leer. Bei vagen Wünschen wie 'ein beliebiges Kreisdiagramm' "
        "wähle selbst eine sinnvolle numerische Kennzahl als measures UND eine passende "
        "category (Gruppierungsspalte). "
        "Für Layout-Wünsche wie 'ordne an', 'verteile', 'fülle die Fläche', 'skaliere auf "
        "volle Größe', 'gleichmäßig anordnen' nutze GENAU EINE auto_layout-Aktion – nicht "
        "mehrere set_position. Feldnamen immer als reine Spalte/Measure (z. B. 'Erlös Netto', "
        "nicht 'Summe von Erlös Netto'). "
        "Visuals mit \"broken\": true rendern fehlerhaft (Feld nicht darstellbar). Wenn der "
        "Nutzer 'kaputte reparieren/entfernen' wünscht, betrifft das genau diese – repariere "
        "sie (set_fields mit einem darstellbaren Feld) oder entferne sie (remove_visual).\n\n"
        f"Aktuelle Struktur:\n{json.dumps(report_summary, ensure_ascii=False, indent=2)}\n\n"
        f"Datenmodell:\n{schema_text}"
    )
    return _complete_json(system, request, _EDIT_SCHEMA)


# ── Prompt -> Auswahl von SharePoint-Ordnern (Phase 3) ─────────
_SP_MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "model_name": {"type": "string", "description": "Sprechender Name für das neue Semantic Model"},
        "summary": {"type": "string", "description": "Kurze Erklärung auf Deutsch: was wurde ausgewählt und warum"},
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "Sprechender Tabellenname im Modell"},
                    "folder": {"type": "string", "description": "EXAKTER Ordnerpfad aus der Liste"},
                    "include_subfolders": {
                        "type": "boolean",
                        "description": "true = ALLE Dateien unterhalb dieses Ordners (auch aus "
                                       "Unterordnern) gehören zu dieser Tabelle; künftige Dateien "
                                       "fließen automatisch mit. Dann 'files' leer lassen.",
                    },
                    "files": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Nur bei include_subfolders=false: exakte Dateinamen aus "
                                       "diesem Ordner, die zu DIESER Tabelle gehören. "
                                       "Leer = alle Dateien direkt im Ordner.",
                    },
                    "sheet": {
                        "type": "string",
                        "description": "Nur bei Excel: EXAKTER Blattname aus 'sheets'. Bei "
                                       "mehreren Blättern PFLICHT. Sonst leer lassen.",
                    },
                },
                "required": ["table_name", "folder", "include_subfolders", "files", "sheet"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["model_name", "summary", "tables"],
    "additionalProperties": False,
}


def plan_sharepoint_model(request: str, tree: list) -> dict[str, Any]:
    """Wählt zum Wunsch des Nutzers die passenden SharePoint-Dateien als Tabellen aus.

    Eine Tabelle = ein Ordner + die darin gewählten Dateien. Mehrere Dateien
    landen nur dann in einer Tabelle, wenn sie dieselbe Struktur haben.
    """
    system = (
        "Du wählst aus einer SharePoint-Ablage die passenden Datenquellen für ein neues "
        "Power-BI-Semantic-Model aus. Unten siehst du alle Ordner mit ihren Datendateien "
        "(die Verschachtelung steht im Pfad, z. B. 'ISH_data/2026-01').\n"
        "Regeln:\n"
        "- Eine Tabelle besteht aus EINEM Ordner ('folder') plus entweder allen Dateien "
        "darunter (include_subfolders=true) oder einer Dateiauswahl daraus ('files').\n"
        "- WICHTIG – gleichartige Dateien über Unterordner: Liegen inhaltlich gleiche Daten "
        "in mehreren Unterordnern (typisch: ein Ordner je Monat/Lieferung, z. B. "
        "'Daten/2026-01', 'Daten/2026-02'), dann ist das EINE Tabelle: nimm den "
        "ÜBERGEORDNETEN Ordner ('Daten') mit include_subfolders=true und lass 'files' leer. "
        "Dadurch fließen später hinzugefügte Ordner/Dateien automatisch mit. Erzeuge in dem "
        "Fall NIEMALS eine Tabelle je Unterordner.\n"
        "- Einzelne, klar abgegrenzte Dateien (z. B. Stammdaten) nimmst du mit "
        "include_subfolders=false und nennst sie in 'files'.\n"
        "- Dateien mit unterschiedlichem Inhalt (z. B. 'marketing.csv' und 'vertrieb.csv') "
        "gehören NIE in dieselbe Tabelle. Liegen sie im selben Ordner, mach daraus ZWEI "
        "Tabellen mit demselben 'folder', aber unterschiedlichen 'files'.\n"
        "- Eine Tabelle darf nur Dateien EINES Typs enthalten (nicht CSV und Excel mischen).\n"
        "- EXCEL-BLÄTTER: Bei Excel-Dateien steht unter 'sheets', welche Blätter es gibt. "
        "Hat eine Datei mehrere, MUSST du in 'sheet' das richtige nennen – sonst wird "
        "blind das erste genommen, und das ist oft ein Deck-/Trennblatt (z. B. 'DATA_AREA ->') "
        "oder eine Auswertung statt der Daten. Wähle das Blatt mit den eigentlichen "
        "Datensätzen; im Zweifel das, dessen Name zum Tabelleninhalt passt. Enthält eine "
        "Datei mehrere fachlich verschiedene Blätter, die BEIDE gebraucht werden, mach "
        "daraus zwei Tabellen mit derselben Datei, aber unterschiedlichem 'sheet'.\n"
        "- Verwende in 'folder' und 'files' AUSSCHLIESSLICH exakt die Pfade/Namen aus der "
        "Liste – erfinde nichts und verändere nichts.\n"
        "- Wähle nur, was zum Wunsch des Nutzers passt. Ist der Wunsch unspezifisch "
        "(z. B. 'alles'), nimm alle Dateien und gruppiere sie sinnvoll.\n"
        "- Gib jeder Tabelle einen sprechenden Namen (z. B. aus dem Datei-/Ordnernamen).\n\n"
        f"Verfügbare Ordner:\n{json.dumps(tree, ensure_ascii=False, indent=2)}"
    )
    return _complete_json(system, request, _SP_MODEL_SCHEMA)


# ── Beziehungen zwischen den Tabellen eines neuen Modells ──────
_REL_SCHEMA = {
    "type": "object",
    "properties": {
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from_table": {"type": "string", "description": "Tabelle der VIELEN-Seite (Fakten)"},
                    "from_column": {"type": "string", "description": "Fremdschlüssel-Spalte dort"},
                    "to_table": {"type": "string", "description": "Tabelle der EINEN-Seite (Stammdaten)"},
                    "to_column": {"type": "string", "description": "Schlüsselspalte dort – MUSS eindeutig sein"},
                    "reason": {"type": "string", "description": "Kurze Begründung auf Deutsch"},
                },
                "required": ["from_table", "from_column", "to_table", "to_column", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["relationships"],
    "additionalProperties": False,
}


def plan_relationships(tables: list) -> dict[str, Any]:
    """Schlägt Beziehungen (viele-zu-eins) zwischen den Tabellen vor.

    'tables' enthält je Tabelle die Spalten und – sofern die Tabelle vollständig
    eingelesen wurde – die nachweislich eindeutigen Spalten. Nur solche dürfen
    die EINE-Seite sein; sonst lässt sich das Modell nicht laden.
    """
    system = (
        "Du modellierst ein Power-BI-Datenmodell (Sternschema). Unten stehen die "
        "Tabellen mit ihren Spalten. Schlage die Beziehungen vor.\n"
        "Regeln:\n"
        "- Jede Beziehung ist VIELE-zu-EINS: 'from' ist die Faktentabelle (viele "
        "Zeilen je Schlüssel), 'to' ist die Stammdatentabelle (ein Eintrag je Schlüssel).\n"
        "- 'to_column' MUSS in der Liste 'unique_columns' der Zieltabelle stehen. "
        "Steht sie nicht dort, ist sie kein gültiger Schlüssel – dann die Beziehung "
        "NICHT vorschlagen.\n"
        "- Verbinde nur, was inhaltlich zusammengehört. Gleiche/ähnliche Spaltennamen "
        "sind ein Hinweis (z. B. 'Gebiets-Code' <-> 'Gebiet'), aber prüfe die Bedeutung.\n"
        "- Verbinde NICHT zwei Stammdatentabellen über eine Spalte, die in beiden "
        "mehrfach vorkommt – das ergäbe viele-zu-viele.\n"
        "- Eine Tabelle, die mit KEINER anderen verbunden ist, ist im Modell nutzlos. "
        "Prüfe für jede Tabelle, ob sie über eine gemeinsame Spalte an eine andere "
        "anschließt – auch Stammdaten dürfen an Stammdaten hängen (z. B. hat jeder "
        "Eintrag ein Gebiet, und Gebiete ist der eindeutige Gebiets-Schlüssel). "
        "Erfinde aber keine Verbindung, wenn es inhaltlich keine gibt.\n"
        "- Lieber eine Beziehung weglassen als eine falsche vorschlagen. Gibt es keine "
        "sinnvolle, gib eine leere Liste zurück.\n\n"
        f"Tabellen:\n{json.dumps(tables, ensure_ascii=False, indent=2)}"
    )
    return _complete_json(system, "Welche Beziehungen gehören in dieses Modell?", _REL_SCHEMA)


# ── Prompt -> Auswahl von SQL-Tabellen (Addison, Variante C) ───
_SQL_MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "model_name": {"type": "string", "description": "Sprechender Name für das neue Semantic Model"},
        "summary": {"type": "string", "description": "Kurze Erklärung auf Deutsch: was wurde gewählt und warum"},
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "EXAKTER Tabellenname aus der Liste"},
                    "table_name": {"type": "string", "description": "Sprechender Tabellenname im Modell"},
                },
                "required": ["table", "table_name"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["model_name", "summary", "tables"],
    "additionalProperties": False,
}


def plan_sql_model(request: str, schema_tables: list) -> dict[str, Any]:
    """Wählt aus dem Addison-SQL-Schema die passenden Tabellen für ein Modell.

    'schema_tables' = kuratierte, nicht-leere Tabellen mit Spalten (schema_overview).
    Der Mandantenfilter wird NICHT von Claude gesetzt (der Nutzer wählt den
    Mandanten in der UI, das Backend filtert) – Claude wählt nur die Tabellen.
    """
    system = (
        "Du wählst aus einer Addison-Buchhaltungsdatenbank (MS SQL) die passenden "
        "Tabellen für ein neues Power-BI-Semantic-Model. Unten stehen die verfügbaren "
        "Tabellen mit Zeilenzahl und Spalten.\n"
        "Regeln:\n"
        "- Verwende in 'table' AUSSCHLIESSLICH exakt die Tabellennamen aus der Liste – "
        "erfinde nichts.\n"
        "- Wähle nur, was zum Wunsch des Nutzers passt, aber nimm die dazugehörigen "
        "Stammdaten-/Dimensionstabellen mit, damit sinnvolle Beziehungen entstehen "
        "(z. B. zu Salden/Bewegungen die passende Stamm-Tabelle: SaldenSachkonten + "
        "StammSachkonten, KontenblattDebitoren + StammDebitoren usw.).\n"
        "- Die Daten sind eine Multi-Mandanten-DB; der Mandantenfilter wird automatisch "
        "gesetzt – darum musst du dich NICHT kümmern.\n"
        "- Bevorzuge für Übersichts-/Kennzahl-Dashboards die aggregierten Salden-Tabellen "
        "(SaldenSachkonten) gegenüber den sehr großen Bewegungstabellen (KontenblattSachkonten), "
        "außer der Nutzer will ausdrücklich Einzelbuchungen.\n"
        "- Gib jeder Tabelle einen sprechenden Namen und dem Modell einen passenden Namen.\n\n"
        f"Verfügbare Tabellen:\n{json.dumps(schema_tables, ensure_ascii=False, indent=2)}"
    )
    return _complete_json(system, request, _SQL_MODEL_SCHEMA)


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
