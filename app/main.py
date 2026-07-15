"""FastAPI-App: Web-Frontend + API für den PowerBI-Generator.

Zugriff nur nach Entra-Login (Datenspezialisten-Tenant). Ablauf beim Anpassen:
  1. /plan-modify   – Änderungen planen und als Vorschau zurückgeben (kein Schreiben)
  2. /apply-modify  – geplante Änderungen anwenden (mit Backup für Rückgängig)
  3. /undo          – letzte Aktion (Erstellen/Anpassen) zurücknehmen

Starten:  uvicorn app.main:app --reload
"""
import base64
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import (ai, auth_web, fabric, model_import, powerbi, report_builder,
               report_editor, sharepoint)
from .config import settings

app = FastAPI(title="PowerBI-Generator")
app.add_middleware(SessionMiddleware,
                   secret_key=settings.session_secret or secrets.token_hex(32))

_INDEX = Path(__file__).parent / "static" / "index.html"

# Letzte Aktion pro Nutzer (In-Memory, ein Level) – für „Rückgängig".
_last_action: dict[str, dict] = {}


# ── Auth ───────────────────────────────────────────────────────
def require_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Nicht angemeldet")
    return user


@app.get("/login", include_in_schema=False)
def login(request: Request):
    flow = auth_web.build_flow()
    request.session["flow"] = flow
    return RedirectResponse(flow["auth_uri"])


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(request: Request):
    flow = request.session.pop("flow", None)
    if not flow:
        return RedirectResponse("/login")
    result = auth_web.complete_flow(flow, dict(request.query_params))
    if "error" in result:
        raise HTTPException(status_code=401, detail=result.get("error_description", "Login fehlgeschlagen"))
    user = auth_web.user_from_result(result)
    if not user:
        raise HTTPException(status_code=403, detail="Kein Zugriff (falscher Tenant oder nicht freigeschaltet).")
    request.session["user"] = user
    return RedirectResponse("/")


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/me")
def me(user: dict = Depends(require_user)) -> dict:
    return user


@app.get("/", include_in_schema=False)
def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login")
    return FileResponse(_INDEX)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Hilfen ─────────────────────────────────────────────────────
@lru_cache(maxsize=32)
def _schema(dataset_id: str) -> dict:
    """Schema-Objekt (Spalten/Measures) gecacht – wird pro Generierung mehrfach
    gebraucht (Claude-Kontext, Visual-Validierung, PBIR-Bau). Nur lesend nutzen;
    bei Modelländerungen über _clear_schema_cache() invalidieren."""
    return powerbi.get_schema(dataset_id)


@lru_cache(maxsize=32)
def _schema_text(dataset_id: str) -> str:
    return powerbi.schema_as_text(_schema(dataset_id))


def _clear_schema_cache() -> None:
    """Beide Schema-Caches leeren (nach neuem Measure / neuer Tabelle)."""
    _schema.cache_clear()
    _schema_text.cache_clear()


def _resolve(dataset_id: str | None) -> str:
    return dataset_id or settings.pbi_dataset_id


def _dataset_of_report(docs: dict) -> str | None:
    conn = docs.get("definition.pbir", {}).get("datasetReference", {}).get("byConnection")
    return conn.get("pbiModelDatabaseName") if conn else None


def _load_report(report_id: str, prefer_enhanced: bool = True) -> tuple[dict, dict, str]:
    """Lädt eine Report-Definition und bestimmt das Format.

    Rückgabe (docs, raw, kind) mit kind = "enhanced" | "classic".
    prefer_enhanced=True versucht bei Alt-Format eine Konvertierung ins neue
    Format (reicher editierbar); klappt das nicht, wird der Klassik-Pfad genutzt.
    prefer_enhanced=False liest nur nativ (schnell, für die Übersicht).
    """
    docs, raw = report_editor.parse_parts(fabric.get_report_definition(report_id))
    if report_editor.has_enhanced_visuals(docs):
        return docs, raw, "enhanced"
    if prefer_enhanced:
        try:
            cd, cr = report_editor.parse_parts(
                fabric.get_report_definition(report_id, fmt="PBIR"))
            if report_editor.has_enhanced_visuals(cd):
                return cd, cr, "enhanced"
        except Exception:  # noqa: BLE001
            pass
    if report_editor.is_classic(docs):
        return docs, raw, "classic"
    raise RuntimeError("Report-Format wird nicht unterstützt.")


def _visual_renders(dax: str | None, dataset_id: str) -> bool:
    """True, wenn die Test-DAX ausführbar ist UND mindestens eine Zeile liefert.
    Leeres Ergebnis = das Visual bliebe im Report leer -> als nicht darstellbar werten."""
    if not dax:
        return False
    try:
        return bool(powerbi.execute_dax(dax, dataset_id))
    except Exception:  # noqa: BLE001
        return False


def _validate_design(design: dict, schema: dict, dataset_id: str) -> list[str]:
    """Testet jedes Visual per DAX gegen die Daten; entfernt nicht darstellbare
    Visuals aus dem Entwurf und gibt deren Titel zurück (für einen Hinweis).

    Die DAX-Tests laufen parallel (ein Cloud-Roundtrip je Visual) – gleiche
    Prüfung wie zuvor, nur nebenläufig statt sequenziell."""
    mt, ct = report_builder.lookups(schema)
    visuals = [v for page in design.get("pages", []) for v in page.get("visuals", [])]
    daxes = [report_builder.test_dax_for_visual(v, mt, ct) for v in visuals]
    with ThreadPoolExecutor(max_workers=8) as ex:
        ok_flags = list(ex.map(lambda d: _visual_renders(d, dataset_id), daxes))
    ok_by_id = {id(v): flag for v, flag in zip(visuals, ok_flags)}

    dropped: list[str] = []
    for page in design.get("pages", []):
        keep = []
        for v in page.get("visuals", []):
            if ok_by_id.get(id(v)):
                keep.append(v)
            else:
                dropped.append(v.get("title") or v.get("type"))
        page["visuals"] = keep
    return dropped


def _drop_failed_repairs(docs: dict, edits: list, dataset_id: str) -> list[str]:
    """Prüft die von Claude reparierten Visuals erneut; entfernt die, die danach
    immer noch fehlerhaft rendern (erfüllt „reparieren, sonst entfernen")."""
    touched = {e.get("visual_id") for e in edits
               if e.get("action") in ("set_fields", "change_type") and e.get("visual_id")}
    # (visual_id, pfad, test-dax) für jedes reparierte Visual, das prüfbar ist
    checks: list[tuple[str, str, str]] = []
    for vid in touched:
        paths = [p for p in docs if p.endswith(f"/visuals/{vid}/visual.json")]
        if not paths:
            continue
        dax = report_editor.visual_test_dax(docs[paths[0]])
        if dax:
            checks.append((vid, paths[0], dax))

    with ThreadPoolExecutor(max_workers=8) as ex:
        ok_flags = list(ex.map(lambda c: _visual_renders(c[2], dataset_id), checks))

    removed: list[str] = []
    for (vid, path, _), ok in zip(checks, ok_flags):
        if not ok:
            del docs[path]
            removed.append(vid)
    return removed


def _mark_broken(summary: list, docs: dict, dataset_id: str) -> None:
    """Testet bestehende Visuals per DAX (parallel) und markiert nicht darstellbare (broken)."""
    checks = [(vid, report_editor.visual_test_dax(content))
              for vid, content in report_editor.iter_visuals(docs)]
    checks = [(vid, dax) for vid, dax in checks if dax]  # nur prüfbare
    with ThreadPoolExecutor(max_workers=8) as ex:
        ok_flags = list(ex.map(lambda c: _visual_renders(c[1], dataset_id), checks))
    broken = {vid for (vid, _), ok in zip(checks, ok_flags) if not ok}
    for page in summary:
        for v in page.get("visuals", []):
            v["broken"] = v["visual_id"] in broken


def _schema_for(report_id: str, docs: dict) -> tuple[str, dict]:
    ds = powerbi.get_report_dataset_id(report_id) or _dataset_of_report(docs)
    if not ds:
        raise RuntimeError("Datenmodell des Reports nicht ermittelbar.")
    return ds, _schema(ds)


# ── Request-Modelle ────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str
    dataset_id: str | None = None


class CreateReportRequest(BaseModel):
    prompt: str
    dataset_id: str | None = None
    display_name: str | None = None


class AddMeasureRequest(BaseModel):
    prompt: str
    dataset_id: str | None = None


class PreviewCreateRequest(BaseModel):
    prompt: str
    dataset_id: str | None = None
    display_name: str | None = None


class ConfirmCreateRequest(BaseModel):
    preview_report_id: str
    display_name: str


class PreviewModifyRequest(BaseModel):
    report_id: str
    prompt: str


class ConfirmModifyRequest(BaseModel):
    report_id: str
    edits: list[dict]
    preview_report_id: str | None = None


class DiscardRequest(BaseModel):
    preview_report_id: str


class SharePointTable(BaseModel):
    folder: str                          # Ordnerpfad relativ zur Bibliothekswurzel
    table_name: str | None = None        # sonst = Ordner-/Dateiname
    files: list[str] | None = None       # nur diese Dateien; leer/None = alle im Ordner


class SharePointModelRequest(BaseModel):
    model_name: str
    tables: list[SharePointTable]


class SharePointPlanRequest(BaseModel):
    prompt: str
    root: str = ""    # Unterordner als Startpunkt (leer = ganze Bibliothek)


# Präfix für temporäre Vorschau-Reports (aus der Auswahl ausgeblendet).
_PREVIEW_PREFIX = "_Vorschau_"


# ── Daten & Auswahl ────────────────────────────────────────────
@app.get("/datasets")
def datasets(user: dict = Depends(require_user)) -> dict:
    try:
        return {"datasets": powerbi.list_datasets()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/reports")
def reports(user: dict = Depends(require_user)) -> dict:
    try:
        reports = [r for r in fabric.list_reports()
                   if not (r.get("displayName") or "").startswith(_PREVIEW_PREFIX)]
        return {"reports": reports, "workspace_id": settings.pbi_workspace_id}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/embed-info")
def embed_info(report_id: str, user: dict = Depends(require_user)) -> dict:
    """Embed-URL + Token für die visuelle Vorschau (Power BI Embedded)."""
    try:
        rep = powerbi.get_report(report_id)
        return {"embedUrl": rep["embedUrl"], "reportId": rep["id"],
                "token": powerbi.generate_embed_token(report_id)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/report-overview")
def report_overview(report_id: str, user: dict = Depends(require_user)) -> dict:
    """Aktueller Aufbau eines Reports (Seiten + Visuals) – zum Planen von Änderungen.

    Liest nur das native Format (schnell). Alt-Format-Reports werden hier NICHT
    konvertiert – das würde die langsame Konvertierung auslösen; stattdessen
    Hinweis-Flag. (Der tatsächliche Anpassen-Schritt versucht die Konvertierung.)
    """
    try:
        docs, _, kind = _load_report(report_id, prefer_enhanced=False)
        pages = (report_editor.summarize(docs) if kind == "enhanced"
                 else report_editor.classic_summarize(docs))
        return {"pages": pages, "kind": kind}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/schema")
def schema(dataset_id: str | None = None, user: dict = Depends(require_user)) -> dict[str, str]:
    try:
        return {"schema": _schema_text(_resolve(dataset_id))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/ask")
def ask(req: AskRequest, user: dict = Depends(require_user)) -> dict:
    try:
        ds = _resolve(req.dataset_id)
        plan = ai.question_to_dax(req.question, _schema_text(ds))
        return {"dax": plan["dax"], "explanation": plan["explanation"],
                "rows": powerbi.execute_dax(plan["dax"], ds)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


# ── Erstellen ──────────────────────────────────────────────────
def _add_measure_to_bim(bim: dict, table: str, name: str, dax: str, fmt: str | None) -> None:
    model = bim.get("model", bim)
    tbl = next((t for t in model.get("tables", []) if t.get("name") == table), None)
    if tbl is None:
        raise RuntimeError(f"Tabelle '{table}' im Modell nicht gefunden.")
    if any(m.get("name") == name for m in tbl.get("measures", [])):
        raise RuntimeError(f"Ein Measure namens '{name}' existiert bereits.")
    measure = {"name": name, "expression": dax}
    if fmt:
        measure["formatString"] = fmt
    tbl.setdefault("measures", []).append(measure)


@app.post("/add-measure")
def add_measure(req: AddMeasureRequest, user: dict = Depends(require_user)) -> dict:
    """Phase 1: KI erzeugt ein Measure und schreibt es ins Semantic Model (TMSL)."""
    try:
        ds = _resolve(req.dataset_id)
        spec = ai.generate_measure(req.prompt, _schema_text(ds))
        # DAX vor dem Schreiben gegen die Daten prüfen (kein kaputtes Measure anlegen)
        try:
            powerbi.execute_dax(f'EVALUATE ROW("v", {spec["dax"]})', ds)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Die generierte DAX ist nicht auswertbar: {str(e)[:200]}")

        parts = fabric.get_model_definition(ds, fmt="TMSL")
        bim_part = next(p for p in parts if p["path"].endswith(".bim"))
        bim = json.loads(base64.b64decode(bim_part["payload"]))
        _add_measure_to_bim(bim, spec["table"], spec["name"], spec["dax"], spec.get("format_string"))
        bim_part["payload"] = base64.b64encode(
            json.dumps(bim, ensure_ascii=False).encode("utf-8")).decode("ascii")
        fabric.update_model_definition(ds, parts)
        _clear_schema_cache()  # Schema-Cache invalidieren (neues Measure)
        return {"measure": spec["name"], "table": spec["table"], "dax": spec["dax"],
                "format": spec.get("format_string"), "explanation": spec["explanation"]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/create-report")
def create_report(req: CreateReportRequest, user: dict = Depends(require_user)) -> dict:
    try:
        ds = _resolve(req.dataset_id)
        design = ai.design_report(req.prompt, _schema_text(ds))
        schema = _schema(ds)
        _validate_design(design, schema, ds)  # kaputte Visuals aussortieren
        name = req.display_name or design.get("title", "KI-Report")
        parts = report_builder.build_pbir(design, schema, ds, name)
        result = fabric.create_report(name, parts)
        rid = result.get("id")
        if rid:
            _last_action[user["email"]] = {"type": "create", "report_id": rid, "name": name}
        return {"created": name, "design": design, "fabric_result": result}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


# ── Phase 2: Modell/Tabelle aus Datei (CSV/Excel) ──────────────
# Definitions-Eigenschaften für ein neues Semantic Model (wie beim Test-Modell).
_PBISM = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
               "semanticModel/definitionProperties/1.0.0/schema.json",
    "version": "4.2",
    "settings": {},
}


def _add_table_to_bim(bim: dict, table_obj: dict) -> None:
    model = bim.get("model", bim)
    if any(t.get("name") == table_obj["name"] for t in model.get("tables", [])):
        raise RuntimeError(f"Eine Tabelle namens '{table_obj['name']}' existiert bereits.")
    model.setdefault("tables", []).append(table_obj)


def _refresh_and_count(model_id: str, table_name: str,
                       tries: int = 10, delay: int = 5) -> int | None:
    """Aktualisiert das Modell (lädt die Import-Daten) und zählt die Zeilen.

    Gibt die Zeilenzahl zurück, sobald die Aktualisierung durch ist, sonst None
    (Daten laden im Hintergrund weiter). Best effort – blockiert höchstens kurz.
    """
    try:
        powerbi.refresh_dataset(model_id)
    except Exception:  # noqa: BLE001
        return None
    query = f'EVALUATE ROW("n", COUNTROWS(\'{table_name}\'))'
    for _ in range(tries):
        time.sleep(delay)
        try:
            rows = powerbi.execute_dax(query, model_id)
            return rows[0].get("[n]") if rows else None
        except Exception:  # noqa: BLE001
            continue
    return None


@app.post("/import/preview")
async def import_preview(file: UploadFile = File(...),
                         user: dict = Depends(require_user)) -> dict:
    """Datei einlesen und die erkannten Spalten/Typen zurückgeben (kein Schreiben)."""
    try:
        parsed = model_import.parse_upload(file.filename or "Tabelle", await file.read())
        sample = [[model_import.display_value(v) for v in r] for r in parsed["rows"][:8]]
        return {
            "suggested_table": parsed["table_name"],
            "columns": [{"name": c["name"], "type": c["dtype"]} for c in parsed["columns"]],
            "sample_rows": sample,
            "row_count": parsed["row_count"],
            "truncated": parsed["truncated"],
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/import/create-model")
async def import_create_model(file: UploadFile = File(...),
                              model_name: str = Form(...),
                              table_name: str | None = Form(None),
                              user: dict = Depends(require_user)) -> dict:
    """Neues Semantic Model aus einer Datei anlegen und die Daten laden."""
    try:
        name = model_import.sanitize_name(model_name)
        parsed = model_import.parse_upload(file.filename or name, await file.read())
        if table_name:
            parsed["table_name"] = model_import.sanitize_name(table_name)
        bim = model_import.build_model_bim(name, parsed)
        parts = [report_builder.make_part("definition.pbism", _PBISM),
                 report_builder.make_part("model.bim", bim)]
        try:
            res = fabric.create_semantic_model(name, parts)
        except RuntimeError as e:
            if "AlreadyInUse" in str(e) or "already exists" in str(e).lower():
                raise RuntimeError(f"Ein Modell namens '{name}' existiert bereits – "
                                   "bitte einen anderen Namen wählen.")
            raise
        mid = res.get("id")
        loaded = _refresh_and_count(mid, parsed["table_name"]) if mid else None
        return {"model_id": mid, "model_name": name, "table_name": parsed["table_name"],
                "rows": loaded, "truncated": parsed["truncated"],
                "columns": [{"name": c["name"], "type": c["dtype"]} for c in parsed["columns"]]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/import/add-table")
async def import_add_table(file: UploadFile = File(...),
                           dataset_id: str = Form(...),
                           table_name: str | None = Form(None),
                           user: dict = Depends(require_user)) -> dict:
    """Eine Tabelle aus einer Datei an ein bestehendes Semantic Model anhängen."""
    try:
        parsed = model_import.parse_upload(file.filename or "Tabelle", await file.read())
        if table_name:
            parsed["table_name"] = model_import.sanitize_name(table_name)
        table_obj = model_import.build_table_object(parsed)

        parts = fabric.get_model_definition(dataset_id, fmt="TMSL")
        bim_part = next(p for p in parts if p["path"].endswith(".bim"))
        bim = json.loads(base64.b64decode(bim_part["payload"]))
        _add_table_to_bim(bim, table_obj)
        bim_part["payload"] = base64.b64encode(
            json.dumps(bim, ensure_ascii=False).encode("utf-8")).decode("ascii")
        fabric.update_model_definition(dataset_id, parts)

        loaded = _refresh_and_count(dataset_id, parsed["table_name"])
        _clear_schema_cache()  # Schema-Cache invalidieren (neue Tabelle)
        return {"model_id": dataset_id, "table_name": parsed["table_name"],
                "rows": loaded, "truncated": parsed["truncated"],
                "columns": [{"name": c["name"], "type": c["dtype"]} for c in parsed["columns"]]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


# ── Phase 3: SharePoint als Datei-Datenquelle ──────────────────
@app.get("/sharepoint/browse")
def sharepoint_browse(path: str = "", user: dict = Depends(require_user)) -> dict:
    """Listet Ordner/Dateien der konfigurierten SharePoint-Bibliothek (zum Durchsuchen).

    'path' ist relativ zur Bibliothekswurzel (leer = Wurzel). Basis für den
    späteren Prompt-gesteuerten Modellbau: hier wählt man die Datenquellen-Ordner.
    """
    if not settings.sharepoint_site_url:
        raise HTTPException(status_code=400,
                            detail="SharePoint ist nicht konfiguriert (SHAREPOINT_SITE_URL fehlt).")
    try:
        return {"path": path, "site_url": settings.sharepoint_site_url,
                "items": sharepoint.list_folder(path)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


def _table_from_folder(folder: str, table_name: str | None,
                       only: list[str] | None = None) -> tuple[dict, dict]:
    """Lädt Datendateien eines SharePoint-Ordners und hängt sie (gleiche Struktur)
    zu EINER Tabelle zusammen. 'only' grenzt auf bestimmte Dateinamen ein – nötig,
    wenn in einem Ordner mehrere unterschiedliche Datensätze liegen.
    Rückgabe: (TMSL-Tabellenobjekt, Info fürs Frontend)."""
    path = folder.strip("/")                       # "/" (Wurzel) -> ""
    files = [it for it in sharepoint.list_folder(path)
             if it["type"] == "file" and (it["name"] or "").lower().endswith(sharepoint.DATA_EXT)]
    if only:
        wanted = {f.lower() for f in only}
        files = [f for f in files if (f["name"] or "").lower() in wanted]
    if not files:
        raise RuntimeError(f"Ordner '{folder}' enthält keine passenden CSV/Excel-Dateien.")
    # Name: explizit > einzelner Dateiname > Ordnername
    name = table_name or (
        files[0]["name"].rsplit(".", 1)[0] if len(files) == 1
        else (path.rsplit("/", 1)[-1] if path else "Daten"))
    parsed_list = [model_import.parse_upload(f["name"], sharepoint.download_file(f["id"]))
                   for f in files]
    merged = model_import.merge_parsed(parsed_list, name)
    info = {"table_name": merged["table_name"], "files": [f["name"] for f in files],
            "rows": merged["row_count"], "truncated": merged["truncated"],
            "columns": [{"name": c["name"], "type": c["dtype"]} for c in merged["columns"]]}
    return model_import.build_table_object(merged), info


@app.post("/sharepoint/plan-model")
def sharepoint_plan_model(req: SharePointPlanRequest,
                          user: dict = Depends(require_user)) -> dict:
    """Phase A3: Prompt + Ordnerbaum -> Claude wählt die Datenquellen (nur Vorschau).

    Schreibt nichts. Das Ergebnis (tables) geht danach unverändert an
    /sharepoint/create-model – wie bei plan-modify -> confirm-modify.
    """
    if not settings.sharepoint_site_url:
        raise HTTPException(status_code=400,
                            detail="SharePoint ist nicht konfiguriert (SHAREPOINT_SITE_URL fehlt).")
    try:
        tree = [t for t in sharepoint.folder_tree(req.root) if t["files"]]  # leere Ordner raus
        if not tree:
            raise RuntimeError("In der SharePoint-Bibliothek wurden keine CSV/Excel-Dateien gefunden.")
        plan = ai.plan_sharepoint_model(req.prompt, tree)

        # Claude darf nur existierende Ordner wählen – erfundene Pfade aussortieren
        known = {t["folder"] for t in tree}
        tables = [t for t in plan.get("tables", []) if t.get("folder") in known]
        dropped = [t.get("folder") for t in plan.get("tables", []) if t.get("folder") not in known]
        if not tables:
            raise RuntimeError("Zum Wunsch wurden keine passenden Ordner gefunden.")

        # Dateiauswahl gegen die echte Ablage prüfen (Claude darf nichts erfinden);
        # leere Auswahl = alle Dateien des Ordners
        files_by_folder = {t["folder"]: t["files"] for t in tree}
        for t in tables:
            avail = files_by_folder.get(t["folder"], [])
            chosen = [f for f in (t.get("files") or []) if f in avail]
            t["files"] = chosen or avail
        tables = [t for t in tables if t["files"]]
        if not tables:
            raise RuntimeError("Zum Wunsch wurden keine passenden Dateien gefunden.")
        return {"model_name": plan["model_name"], "summary": plan["summary"],
                "tables": tables, "dropped": dropped, "tree": tree}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/sharepoint/create-model")
def sharepoint_create_model(req: SharePointModelRequest,
                            user: dict = Depends(require_user)) -> dict:
    """Baut ein neues Semantic Model aus SharePoint-Ordnern (Ordner = Tabelle).

    Phase A2 – noch ohne KI: die Ordner werden explizit übergeben. Jeder Ordner
    wird zu einer Tabelle (alle gleich strukturierten Dateien darin angehängt).
    """
    if not settings.sharepoint_site_url:
        raise HTTPException(status_code=400,
                            detail="SharePoint ist nicht konfiguriert (SHAREPOINT_SITE_URL fehlt).")
    if not req.tables:
        raise HTTPException(status_code=400, detail="Keine Ordner/Tabellen angegeben.")
    try:
        name = model_import.sanitize_name(req.model_name)
        table_objs: list[dict] = []
        infos: list[dict] = []
        for t in req.tables:
            obj, info = _table_from_folder(t.folder, t.table_name, t.files)
            table_objs.append(obj)
            infos.append(info)

        bim = model_import.build_model_bim_tables(name, table_objs)
        parts = [report_builder.make_part("definition.pbism", _PBISM),
                 report_builder.make_part("model.bim", bim)]
        try:
            res = fabric.create_semantic_model(name, parts)
        except RuntimeError as e:
            if "AlreadyInUse" in str(e) or "already exists" in str(e).lower():
                raise RuntimeError(f"Ein Modell namens '{name}' existiert bereits – "
                                   "bitte einen anderen Namen wählen.")
            raise
        mid = res.get("id")
        loaded = _refresh_and_count(mid, infos[0]["table_name"]) if mid and infos else None
        if mid:
            _clear_schema_cache()  # falls das neue Modell direkt bespielt wird
        return {"model_id": mid, "model_name": name, "tables": infos,
                "rows_first_table": loaded}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


# ── Anpassen: planen -> anwenden ───────────────────────────────
@app.post("/preview-create")
def preview_create(req: PreviewCreateRequest, user: dict = Depends(require_user)) -> dict:
    """Neues Dashboard als temporären Vorschau-Report erzeugen (zum Begutachten)."""
    try:
        ds = _resolve(req.dataset_id)
        design = ai.design_report(req.prompt, _schema_text(ds))
        schema = _schema(ds)
        dropped = _validate_design(design, schema, ds)  # kaputte Visuals aussortieren
        name = req.display_name or design.get("title", "KI-Report")
        parts = report_builder.build_pbir(design, schema, ds, name)
        temp = fabric.create_report(f"{_PREVIEW_PREFIX}{report_builder.short_id()}", parts)
        return {"design": design, "title": name, "preview_report_id": temp.get("id"),
                "dropped": dropped}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/confirm-create")
def confirm_create(req: ConfirmCreateRequest, user: dict = Depends(require_user)) -> dict:
    """Vorschau-Report annehmen: auf den finalen Namen umbenennen (bleibt bestehen)."""
    name = req.display_name
    try:
        try:
            fabric.rename_item(req.preview_report_id, name)
        except RuntimeError as e:
            # Name schon vergeben -> mit Kürzel eindeutig machen
            if "AlreadyInUse" in str(e):
                name = f"{req.display_name} ({report_builder.short_id()[:4]})"
                fabric.rename_item(req.preview_report_id, name)
            else:
                raise
        _last_action[user["email"]] = {"type": "create", "report_id": req.preview_report_id,
                                       "name": name}
        return {"created": name, "report_id": req.preview_report_id}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/preview-modify")
def preview_modify(req: PreviewModifyRequest, user: dict = Depends(require_user)) -> dict:
    """Änderungen planen und als temporären Vorschau-Report zum Einbetten erzeugen.

    Das Original bleibt unberührt; die Änderungen landen in einer Kopie, die man
    visuell begutachten und dann bestätigen oder verwerfen kann.
    """
    try:
        docs, raw, kind = _load_report(req.report_id)
        ds, schema = _schema_for(req.report_id, docs)
        schema_txt = powerbi.schema_as_text(schema)
        if kind == "enhanced":
            summary = report_editor.summarize(docs)
            _mark_broken(summary, docs, ds)  # kaputte Visuals für Claude markieren
        else:
            summary = report_editor.classic_summarize(docs)
        plan = ai.plan_edits(req.prompt, summary, schema_txt)

        if kind == "enhanced":
            applied = report_editor.apply_edits(docs, plan["edits"], schema)
            for vid in _drop_failed_repairs(docs, plan["edits"], ds):
                applied.append(f"Visual {vid} war nach der Reparatur weiter fehlerhaft "
                               "und wurde entfernt.")
        else:
            applied = report_editor.classic_apply(docs, plan["edits"])

        # Temporären Vorschau-Report anlegen (klappt evtl. nicht fürs klassische Format)
        preview_id = None
        try:
            temp = fabric.create_report(f"{_PREVIEW_PREFIX}{report_builder.short_id()}",
                                        report_editor.to_parts(docs, raw))
            preview_id = temp.get("id")
        except Exception:  # noqa: BLE001
            preview_id = None

        return {"summary": plan["summary"], "edits": plan["edits"], "applied": applied,
                "preview": report_editor.describe_edits(plan["edits"]),
                "report_id": req.report_id, "preview_report_id": preview_id, "kind": kind}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/confirm-modify")
def confirm_modify(req: ConfirmModifyRequest, user: dict = Depends(require_user)) -> dict:
    """Geplante Änderungen ins Original schreiben (Backup für Rückgängig) und
    den Vorschau-Report aufräumen."""
    try:
        docs, raw, kind = _load_report(req.report_id)
        backup = report_editor.to_parts(docs, raw)  # Snapshot VOR den Änderungen
        if kind == "enhanced":
            _, schema = _schema_for(req.report_id, docs)
            applied = report_editor.apply_edits(docs, req.edits, schema)
        else:
            applied = report_editor.classic_apply(docs, req.edits)
        fabric.update_report_definition(req.report_id, report_editor.to_parts(docs, raw))
        _last_action[user["email"]] = {"type": "modify", "report_id": req.report_id, "parts": backup}
        if req.preview_report_id:
            try:
                fabric.delete_report(req.preview_report_id)
            except Exception:  # noqa: BLE001
                pass
        return {"applied": applied}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/discard-preview")
def discard_preview(req: DiscardRequest, user: dict = Depends(require_user)) -> dict:
    """Temporären Vorschau-Report verwerfen (Original bleibt unverändert)."""
    try:
        fabric.delete_report(req.preview_report_id)
        return {"discarded": True}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/undo")
def undo(user: dict = Depends(require_user)) -> dict:
    """Letzte Aktion des Nutzers zurücknehmen."""
    act = _last_action.pop(user["email"], None)
    if not act:
        raise HTTPException(status_code=400, detail="Keine Aktion zum Rückgängigmachen.")
    try:
        if act["type"] == "modify":
            fabric.update_report_definition(act["report_id"], act["parts"])
            return {"undone": "Änderungen wurden zurückgenommen."}
        fabric.delete_report(act["report_id"])
        return {"undone": f"Report „{act['name']}“ wurde gelöscht."}
    except Exception as e:  # noqa: BLE001
        _last_action[user["email"]] = act  # bei Fehler nicht verlieren
        raise HTTPException(status_code=502, detail=str(e))
