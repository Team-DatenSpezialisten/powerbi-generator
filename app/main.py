"""FastAPI-App: Web-Frontend + API für den PowerBI-Generator.

Zugriff nur nach Entra-Login (Datenspezialisten-Tenant). Ablauf beim Anpassen:
  1. /plan-modify   – Änderungen planen und als Vorschau zurückgeben (kein Schreiben)
  2. /apply-modify  – geplante Änderungen anwenden (mit Backup für Rückgängig)
  3. /undo          – letzte Aktion (Erstellen/Anpassen) zurücknehmen

Starten:  uvicorn app.main:app --reload
"""
import secrets
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import ai, auth_web, fabric, powerbi, report_builder, report_editor
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
def _schema_text(dataset_id: str) -> str:
    return powerbi.schema_as_text(powerbi.get_schema(dataset_id))


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


def _schema_for(report_id: str, docs: dict) -> tuple[str, dict]:
    ds = powerbi.get_report_dataset_id(report_id) or _dataset_of_report(docs)
    if not ds:
        raise RuntimeError("Datenmodell des Reports nicht ermittelbar.")
    return ds, powerbi.get_schema(ds)


# ── Request-Modelle ────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str
    dataset_id: str | None = None


class CreateReportRequest(BaseModel):
    prompt: str
    dataset_id: str | None = None
    display_name: str | None = None


class PreviewModifyRequest(BaseModel):
    report_id: str
    prompt: str


class ConfirmModifyRequest(BaseModel):
    report_id: str
    edits: list[dict]
    preview_report_id: str | None = None


class DiscardRequest(BaseModel):
    preview_report_id: str


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
@app.post("/create-report")
def create_report(req: CreateReportRequest, user: dict = Depends(require_user)) -> dict:
    try:
        ds = _resolve(req.dataset_id)
        design = ai.design_report(req.prompt, _schema_text(ds))
        schema = powerbi.get_schema(ds)
        name = req.display_name or design.get("title", "KI-Report")
        parts = report_builder.build_pbir(design, schema, ds, name)
        result = fabric.create_report(name, parts)
        rid = result.get("id")
        if rid:
            _last_action[user["email"]] = {"type": "create", "report_id": rid, "name": name}
        return {"created": name, "design": design, "fabric_result": result}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


# ── Anpassen: planen -> anwenden ───────────────────────────────
@app.post("/preview-modify")
def preview_modify(req: PreviewModifyRequest, user: dict = Depends(require_user)) -> dict:
    """Änderungen planen und als temporären Vorschau-Report zum Einbetten erzeugen.

    Das Original bleibt unberührt; die Änderungen landen in einer Kopie, die man
    visuell begutachten und dann bestätigen oder verwerfen kann.
    """
    try:
        docs, raw, kind = _load_report(req.report_id)
        _, schema = _schema_for(req.report_id, docs)
        schema_txt = powerbi.schema_as_text(schema)
        summary = (report_editor.summarize(docs) if kind == "enhanced"
                   else report_editor.classic_summarize(docs))
        plan = ai.plan_edits(req.prompt, summary, schema_txt)

        if kind == "enhanced":
            applied = report_editor.apply_edits(docs, plan["edits"], schema)
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
