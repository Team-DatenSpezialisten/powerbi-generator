"""Wrapper um den Addison-SQL-Server (MS SQL, Variante C).

Gegenstück zu sharepoint.py: liest das Schema (Tabellen mit Zeilenzahl, Spalten,
Stichprobe), damit Claude daraus die Datenquellen für ein Semantic Model wählen
kann. Die Daten selbst holt später Power BI über eine Sql.Database()-M-Abfrage –
hier wird nichts eingebettet (wie Variante B, nur mit SQL statt SharePoint).

Auth: SQL-Login aus der .env (read-only empfohlen), Verbindung via pymssql.
Besonderheit Addison: Es ist eine Multi-Mandanten-DB (alle Kanzlei-Kunden in
denselben Tabellen). Auswertungen müssen daher über die Mandanten-/OrgId-Spalte
gefiltert bzw. StammMandant als Dimension eingebunden werden.
"""
from typing import Any

import pymssql

from .config import settings


def is_configured() -> bool:
    return bool(settings.addison_sql_server and settings.addison_sql_user)


def _connect():
    """Öffnet eine Verbindung zur Addison-Replik (kurze Timeouts – nur Metadaten
    und Stichproben, keine großen Ladevorgänge)."""
    if not is_configured():
        raise RuntimeError("Addison-SQL ist nicht konfiguriert (ADDISON_SQL_* fehlt).")
    return pymssql.connect(
        server=settings.addison_sql_server,
        port=str(settings.addison_sql_port),
        user=settings.addison_sql_user,
        password=settings.addison_sql_password,
        database=settings.addison_sql_database,
        login_timeout=15,
        timeout=60,
    )


def ping() -> str:
    """Prüft die Verbindung und gibt die SQL-Server-Version zurück (für Selbsttests)."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT @@VERSION")
        return cur.fetchone()[0]


# ── Schema-Discovery (Kontext für Claude) ──────────────────────
def list_tables(min_rows: int = 1) -> list[dict[str, Any]]:
    """Tabellen mit Zeilenzahl >= min_rows, größte zuerst.

    min_rows=1 blendet die (laut Kollegen vorhandenen) leeren Tabellen aus –
    sie würden Claude nur verwirren. Die Zeilenzahl kommt aus sys.partitions
    (Schätzung ohne Full-Scan, für die Auswahl völlig ausreichend).
    """
    sql = f"""
    SELECT s.name AS schema_name, t.name AS table_name, SUM(p.rows) AS row_count
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
    GROUP BY s.name, t.name
    HAVING SUM(p.rows) >= {int(min_rows)}
    ORDER BY row_count DESC
    """
    with _connect() as conn:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql)
        return [{"schema": r["schema_name"], "table": r["table_name"],
                 "rows": int(r["row_count"])} for r in cur.fetchall()]


def get_columns(schema: str, table: str) -> list[dict[str, str]]:
    """Spalten einer Tabelle (Name + SQL-Datentyp)."""
    sql = """
    SELECT COLUMN_NAME, DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
    ORDER BY ORDINAL_POSITION
    """
    with _connect() as conn:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql, (schema, table))
        return [{"name": r["COLUMN_NAME"], "type": r["DATA_TYPE"]} for r in cur.fetchall()]


def sample_rows(schema: str, table: str, n: int = 20) -> tuple[list[str], list[list]]:
    """Erste n Zeilen einer Tabelle (Header, Zeilen) – zur Plausibilitätsprüfung.

    Schema-/Tabellennamen kommen aus list_tables() (DB-eigene Werte), werden aber
    in eckige Klammern gesetzt, um Sonderzeichen sauber zu quoten.
    """
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT TOP {int(n)} * FROM [{schema}].[{table}]")
        header = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
    return header, rows


def foreign_keys() -> list[dict[str, str]]:
    """Alle EINSPALTIGEN Foreign Keys der DB (parent = viele-Seite -> ref = eine-Seite).

    Das ist die in der Datenbank bereits deklarierte Beziehungsstruktur – Grundlage
    fürs 1:1-Abbild im Semantic Model, ganz ohne KI. Mehrspaltige FKs werden
    ausgelassen, weil Power BI nur Single-Column-Beziehungen kennt (in der Addison-
    Replik gibt es derzeit ohnehin keine mehrspaltigen FKs). Die referenzierte
    Spalte ist per FK-Definition immer ein Primär-/Unique-Key, also eindeutig –
    damit ist die eine-Seite der Power-BI-Beziehung garantiert gültig.
    """
    sql = """
    SELECT tp.name AS parent_table, cp.name AS parent_col,
           tr.name AS ref_table,    cr.name AS ref_col
    FROM sys.foreign_keys fk
    JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
    JOIN sys.tables tp  ON fkc.parent_object_id = tp.object_id
    JOIN sys.columns cp ON fkc.parent_object_id = cp.object_id AND fkc.parent_column_id = cp.column_id
    JOIN sys.tables tr  ON fkc.referenced_object_id = tr.object_id
    JOIN sys.columns cr ON fkc.referenced_object_id = cr.object_id AND fkc.referenced_column_id = cr.column_id
    WHERE fk.object_id IN (
        SELECT constraint_object_id FROM sys.foreign_key_columns
        GROUP BY constraint_object_id HAVING COUNT(*) = 1)
    ORDER BY tp.name, cp.name
    """
    with _connect() as conn:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql)
        return [{"parent_table": r["parent_table"], "parent_col": r["parent_col"],
                 "ref_table": r["ref_table"], "ref_col": r["ref_col"]}
                for r in cur.fetchall()]


def schema_overview(min_rows: int = 1, with_columns: bool = True) -> list[dict[str, Any]]:
    """Kuratierter Schema-Baum für den Claude-Prompt (Gegenstück zu folder_tree).

    Nur nicht-leere Tabellen, größte zuerst, je Tabelle Zeilenzahl und optional
    die Spalten. Das ist der Kontext, aus dem Claude die Datenquellen wählt.
    """
    tables = list_tables(min_rows)
    if not with_columns:
        return tables
    out: list[dict[str, Any]] = []
    for t in tables:
        cols = get_columns(t["schema"], t["table"])
        out.append({**t, "columns": cols})
    return out


# ── Mandanten (Multi-Mandanten-DB) ─────────────────────────────
def list_mandanten(active_only: bool = False) -> list[dict[str, Any]]:
    """Alle Mandanten aus StammMandant (für die Auswahl im Frontend).

    MandantId ist der eindeutige Schlüssel (z. B. 'dbkanz_10000'); Name1 der
    Klarname. Ein v1-Modell wird immer auf GENAU EINEN MandantId gefiltert.
    """
    sql = ("SELECT MandantId, Mandant, Name1, IsAktiv FROM dbo.StammMandant "
           "ORDER BY Name1")
    with _connect() as conn:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql)
        rows = cur.fetchall()
    out = [{"mandant_id": r["MandantId"], "mandant": r["Mandant"],
            "name": (r["Name1"] or "").strip() or r["MandantId"],
            "aktiv": str(r["IsAktiv"] or "").strip().lower() == "aktiv"}
           for r in rows]
    return [m for m in out if m["aktiv"]] if active_only else out


def column_is_unique(schema: str, table: str, column: str,
                     mandant_col: str | None = None,
                     mandant_val: str | None = None) -> bool:
    """True, wenn 'column' (innerhalb des Mandanten) ein echter Schlüssel ist.

    Verlässlicher als die SharePoint-Stichprobe: fragt die Eindeutigkeit direkt
    in SQL ab (COUNT(*) vs COUNT(DISTINCT)). NULLs zählen bei DISTINCT nicht mit,
    eine Spalte mit NULLs gilt also (korrekt) als nicht eindeutig -> keine kaputte
    Beziehung auf der Eine-Seite.
    """
    where, params = "", ()
    if mandant_col and mandant_val is not None:
        where = f" WHERE [{mandant_col}] = %s"
        params = (mandant_val,)
    sql = (f"SELECT COUNT(*) AS total, COUNT(DISTINCT [{column}]) AS dist "
           f"FROM [{schema}].[{table}]{where}")
    with _connect() as conn:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql, params)
        r = cur.fetchone()
    return bool(r) and r["total"] > 0 and r["total"] == r["dist"]
