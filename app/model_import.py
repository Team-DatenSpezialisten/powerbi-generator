"""Phase 2: Semantic Models aus hochgeladenen Dateien (CSV/Excel) erzeugen.

Der Weg ist derselbe, der schon beim KI-Test-Modell funktioniert hat: Die Daten
werden als Power-Query-Inline-Tabelle (#table(...), Import-Modus) direkt in die
Modell-Definition (model.bim / TMSL) eingebettet. Kein externer Connector nötig –
das Modell ist selbst-enthaltend. Nach dem Anlegen lädt ein Refresh die Daten.

Ablauf:
  parse_upload(dateiname, bytes) -> parsed  (Spalten + Typen + Zeilen)
  build_model_bim(name, parsed)  -> model.bim-Dict für ein NEUES Modell
  build_table_object(parsed)     -> eine Tabelle zum Anhängen an ein BESTEHENDES Modell

Grenze: Die Daten liegen in der Definition, daher für PoC/kleine bis mittlere
Dateien gedacht (Zeilen werden auf MAX_ROWS gedeckelt). Große/laufende Quellen
kommen später über echte Connectoren (SharePoint/Addison).
"""
import csv
import datetime as dt
import io
import re
from typing import Any

# Obergrenze eingebetteter Zeilen – hält die Definition handhabbar.
MAX_ROWS = 10000

_DATE_FORMATS = [
    "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M",
]
_BOOL_TRUE = {"true", "wahr", "ja", "yes"}
_BOOL_FALSE = {"false", "falsch", "nein", "no"}


# ── Werte-Parser (robust gegen deutsche Zahlen-/Datumsformate) ─────────────
def _is_null(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _to_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if float(v).is_integer() else None
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        if re.fullmatch(r"[+-]?\d{1,3}(\.\d{3})+", s):   # 1.234.567 (Tausenderpunkte)
            return int(s.replace(".", ""))
    return None


def _to_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("€", "").replace("$", "").replace("%", "").replace(" ", "")
        if not s:
            return None
        if "." in s and "," in s:      # 1.234,56  -> de: Punkt=Tausender, Komma=Dezimal
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:                  # 1234,56   -> de: Komma=Dezimal
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _to_date(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        s = v.strip()
        if s:
            for fmt in _DATE_FORMATS:
                try:
                    return dt.datetime.strptime(s, fmt)
                except ValueError:
                    continue
    return None


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _BOOL_TRUE:
            return True
        if s in _BOOL_FALSE:
            return False
    return None


def _infer_type(values: list[Any]) -> str:
    """Bestimmt den Spaltentyp: boolean | int64 | double | dateTime | string."""
    non_null = [v for v in values if not _is_null(v)]
    if not non_null:
        return "string"
    if all(_to_bool(v) is not None for v in non_null):
        return "boolean"
    if all(_to_int(v) is not None for v in non_null):
        return "int64"
    if all(_to_float(v) is not None for v in non_null):
        return "double"
    if all(_to_date(v) is not None for v in non_null):
        return "dateTime"
    return "string"


# ── Datei einlesen ─────────────────────────────────────────────────────────
def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace")


def _read_csv(content: bytes) -> tuple[list, list[list]]:
    text = _decode(content)
    sample = text[:4096]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        delim = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        raise ValueError("Die CSV-Datei enthält keine Zeilen.")
    return rows[0], rows[1:]


def _read_xlsx(content: bytes) -> tuple[list, list[list]]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError("Für Excel-Dateien fehlt die Bibliothek 'openpyxl'.") from e
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    header: list | None = None
    data: list[list] = []
    for row in ws.iter_rows(values_only=True):
        if row is None or all(c is None for c in row):
            continue
        if header is None:
            header = list(row)
        else:
            data.append(list(row))
    wb.close()
    if header is None:
        raise ValueError("Das Excel-Blatt ist leer.")
    return header, data


def _clean_headers(header: list) -> list[str]:
    out: list[str] = []
    used: set[str] = set()
    for i, h in enumerate(header):
        name = (str(h).strip() if h is not None else "") or f"Spalte_{i + 1}"
        cand, k = name, 1
        while cand.lower() in used:
            k += 1
            cand = f"{name}_{k}"
        used.add(cand.lower())
        out.append(cand)
    return out


def sanitize_name(name: str) -> str:
    """Bereinigt Modell-/Tabellennamen (Umlaute erlaubt, Sonderzeichen raus)."""
    name = re.sub(r"[^\w äöüÄÖÜß.\-]", " ", name or "", flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] or "Tabelle"


def parse_upload(filename: str, content: bytes) -> dict[str, Any]:
    """Liest CSV/Excel, erkennt Spalten & Typen, liefert die Zeilen."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("xlsx", "xlsm"):
        header, rows = _read_xlsx(content)
    else:
        header, rows = _read_csv(content)

    header = _clean_headers(header)
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    columns: list[dict[str, Any]] = []
    for i, name in enumerate(header):
        vals = [r[i] if i < len(r) else None for r in rows]
        dtype = _infer_type(vals)
        has_time = dtype == "dateTime" and any(
            (d := _to_date(v)) and (d.hour or d.minute or d.second)
            for v in vals if not _is_null(v)
        )
        columns.append({"name": name, "dtype": dtype, "has_time": bool(has_time)})

    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return {
        "table_name": sanitize_name(base),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


# ── model.bim / TMSL aufbauen ──────────────────────────────────────────────
_M_TYPE = {"int64": "Int64.Type", "double": "number", "boolean": "logical",
           "string": "text"}
_TMSL_TYPE = {"int64": "int64", "double": "double", "boolean": "boolean",
              "dateTime": "dateTime", "string": "string"}


def _m_type(dtype: str, has_time: bool) -> str:
    if dtype == "dateTime":
        return "datetime" if has_time else "date"
    return _M_TYPE[dtype]


def _m_value(v: Any, dtype: str, has_time: bool) -> str:
    if _is_null(v):
        return "null"
    if dtype == "int64":
        i = _to_int(v)
        return "null" if i is None else str(i)
    if dtype == "double":
        f = _to_float(v)
        return "null" if f is None else repr(f)
    if dtype == "boolean":
        b = _to_bool(v)
        return "null" if b is None else ("true" if b else "false")
    if dtype == "dateTime":
        d = _to_date(v)
        if d is None:
            return "null"
        if has_time:
            return f"#datetime({d.year},{d.month},{d.day},{d.hour},{d.minute},{d.second})"
        return f"#date({d.year},{d.month},{d.day})"
    # string
    s = str(v).replace('"', '""').replace("\r", " ").replace("\n", " ")
    return f'"{s}"'


def _tmsl_column(name: str, dtype: str) -> dict[str, Any]:
    col: dict[str, Any] = {
        "name": name,
        "dataType": _TMSL_TYPE[dtype],
        "sourceColumn": name,
        "summarizeBy": "sum" if dtype in ("int64", "double") else "none",
    }
    if dtype == "dateTime":
        col["formatString"] = "General Date"
    return col


def _build_m(columns: list[dict[str, Any]], rows: list[list]) -> list[str]:
    type_decl = ", ".join(
        f'#"{c["name"]}" = {_m_type(c["dtype"], c["has_time"])}' for c in columns)
    row_lines = []
    for r in rows:
        vals = [_m_value(r[i] if i < len(r) else None, c["dtype"], c["has_time"])
                for i, c in enumerate(columns)]
        row_lines.append("            {" + ", ".join(vals) + "}")
    return [
        "let",
        "    Source = #table(",
        f"        type table [{type_decl}],",
        "        {",
        ",\n".join(row_lines),
        "        }",
        "    )",
        "in",
        "    Source",
    ]


def build_table_object(parsed: dict[str, Any]) -> dict[str, Any]:
    """Baut ein TMSL-Tabellenobjekt (Spalten + Inline-Import-Partition)."""
    name = parsed["table_name"]
    columns = parsed["columns"]
    return {
        "name": name,
        "columns": [_tmsl_column(c["name"], c["dtype"]) for c in columns],
        "partitions": [{
            "name": name,
            "mode": "import",
            "source": {"type": "m", "expression": _build_m(columns, parsed["rows"])},
        }],
    }


def build_model_bim_tables(model_name: str, table_objs: list[dict[str, Any]]) -> dict[str, Any]:
    """Baut ein model.bim für ein neues Modell mit einer oder mehreren Tabellen."""
    return {
        "name": model_name,
        "compatibilityLevel": 1600,
        "model": {
            "culture": "de-DE",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "tables": table_objs,
        },
    }


def build_model_bim(model_name: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """Baut ein komplettes model.bim für ein neues Semantic Model (eine Tabelle)."""
    return build_model_bim_tables(model_name, [build_table_object(parsed)])


def merge_parsed(parsed_list: list[dict[str, Any]],
                 table_name: str | None = None) -> dict[str, Any]:
    """Führt mehrere gleich strukturierte Dateien zu EINER Tabelle zusammen.

    Voraussetzung: identische Spaltennamen in identischer Reihenfolge (dieselbe
    Struktur, wie sie in einem Ordner liegen soll). Bei Abweichung: klarer Fehler.
    Typen werden über die kombinierten Zeilen neu bestimmt (robuster als je Datei).
    """
    if not parsed_list:
        raise ValueError("Keine Dateien zum Zusammenführen.")
    base_cols = [c["name"] for c in parsed_list[0]["columns"]]
    rows: list[list] = []
    truncated = False
    for p in parsed_list:
        cols = [c["name"] for c in p["columns"]]
        if cols != base_cols:
            raise ValueError(
                f"Spalten passen nicht zusammen ({cols} ≠ {base_cols}). "
                "Nur Dateien gleicher Struktur können an eine Tabelle angehängt werden.")
        rows.extend(p["rows"])
        truncated = truncated or p["truncated"]
    truncated = truncated or len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    columns: list[dict[str, Any]] = []
    for i, name in enumerate(base_cols):
        vals = [r[i] if i < len(r) else None for r in rows]
        dtype = _infer_type(vals)
        has_time = dtype == "dateTime" and any(
            (d := _to_date(v)) and (d.hour or d.minute or d.second)
            for v in vals if not _is_null(v))
        columns.append({"name": name, "dtype": dtype, "has_time": bool(has_time)})

    return {
        "table_name": sanitize_name(table_name or parsed_list[0]["table_name"]),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def display_value(v: Any) -> str:
    """Wert für die Vorschau-Anzeige JSON-sicher in Text wandeln."""
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat(sep=" ")[:19]
    return str(v)
