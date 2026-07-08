"""Bestehende Reports anpassen: PBIR-Definition einlesen, für Claude
zusammenfassen und strukturierte Änderungen präzise anwenden.

Claude liefert nie rohes PBIR, sondern eine Liste von Aktionen; dieser Code
setzt sie auf den echten Dateien um, sodass Formatierung erhalten bleibt.

Unterstützte Aktionen (erweiterbar):
  add_visual      Visual (Karte/Diagramm/Tabelle) ergänzen
  remove_visual   Visual löschen
  change_type     Visual-Typ wechseln (z. B. Balken -> Säule)
  set_position    Visual verschieben/vergrößern
  add_page        neue Seite anlegen
  remove_page     Seite löschen
"""
import base64
import json
import math
import re
from typing import Any

from . import report_builder as rb

_VIS_RE = re.compile(r"^definition/pages/([^/]+)/visuals/([^/]+)/visual\.json$")
_PAGE_RE = re.compile(r"^definition/pages/([^/]+)/page\.json$")
_PAGES_JSON = "definition/pages/pages.json"

# PBIR-visualType -> Claude-freundlicher Typ (Rückrichtung von rb.VISUAL_TYPE)
_FRIENDLY = {v: k for k, v in rb.VISUAL_TYPE.items()}


# ── Parsen / Serialisieren ─────────────────────────────────────
def parse_parts(parts: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, str]]:
    """Trennt JSON-Dateien (bearbeitbar) von sonstigen Teilen (durchreichen)."""
    docs: dict[str, Any] = {}
    raw: dict[str, str] = {}
    for p in parts:
        if p["path"].endswith(".json") or p["path"].endswith(".pbir"):
            docs[p["path"]] = json.loads(base64.b64decode(p["payload"]))
        else:
            raw[p["path"]] = p["payload"]
    return docs, raw


_ACTION_LABEL = {
    "add_visual": "Visual hinzufügen",
    "remove_visual": "Visual entfernen",
    "change_type": "Visual-Typ ändern",
    "set_title": "Titel setzen",
    "set_fields": "Felder tauschen",
    "set_position": "Visual verschieben",
    "add_page": "Seite anlegen",
    "remove_page": "Seite entfernen",
}


def describe_edits(edits: list[dict[str, Any]]) -> list[str]:
    """Menschliche Kurzbeschreibung der geplanten Aktionen (für die Vorschau)."""
    out: list[str] = []
    for e in edits:
        label = _ACTION_LABEL.get(e.get("action"), e.get("action", "?"))
        detail = e.get("title") or e.get("visual_type") or e.get("page_name") or e.get("visual_id") or ""
        out.append(f"{label}: {detail}".strip().rstrip(":"))
    return out


def has_enhanced_visuals(docs: dict[str, Any]) -> bool:
    """True, wenn die Definition im neuen PBIR-Format vorliegt (Datei pro Visual)."""
    return any(_VIS_RE.match(p) for p in docs)


# ── Klassisches Format (Power BI Desktop): report.json mit sections ─────────
def classic_report_path(docs: dict[str, Any]) -> str | None:
    """Findet die Haupt-report.json des klassischen Formats (Marker: 'sections')."""
    for path, content in docs.items():
        if isinstance(content, dict) and "sections" in content:
            return path
    return None


def is_classic(docs: dict[str, Any]) -> bool:
    return classic_report_path(docs) is not None


def _cfg_load(vc: dict[str, Any]) -> dict[str, Any]:
    """visualContainer.config kann String (JSON) oder bereits Objekt sein."""
    cfg = vc.get("config")
    if isinstance(cfg, str):
        try:
            return json.loads(cfg)
        except Exception:  # noqa: BLE001
            return {}
    return cfg or {}


def _cfg_store(vc: dict[str, Any], cfg: dict[str, Any]) -> None:
    vc["config"] = json.dumps(cfg) if isinstance(vc.get("config"), str) else cfg


def _classic_vid(cfg: dict[str, Any], sec_i: int, vc_i: int) -> str:
    return cfg.get("name") or f"{sec_i}:{vc_i}"


def classic_summarize(docs: dict[str, Any]) -> list[dict[str, Any]]:
    """Seiten-/Visual-Übersicht für das klassische Format."""
    path = classic_report_path(docs)
    if not path:
        return []
    pages: list[dict[str, Any]] = []
    for si, sec in enumerate(docs[path].get("sections", [])):
        visuals = []
        for vi, vc in enumerate(sec.get("visualContainers", [])):
            sv = _cfg_load(vc).get("singleVisual", {})
            fields = []
            for projs in (sv.get("projections") or {}).values():
                for p in projs:
                    qr = p.get("queryRef")
                    if qr:
                        fields.append(qr.split(".")[-1])
            visuals.append({"visual_id": _classic_vid(_cfg_load(vc), si, vi),
                            "type": _FRIENDLY.get(sv.get("visualType", "?"), sv.get("visualType", "?")),
                            "fields": fields})
        pages.append({"page_id": sec.get("name", str(si)),
                      "name": sec.get("displayName", sec.get("name", f"Seite {si+1}")),
                      "visuals": visuals})
    return pages


def classic_apply(docs: dict[str, Any], edits: list[dict[str, Any]]) -> list[str]:
    """Wendet Aktionen im klassischen Format an (aktuell: change_type, remove_visual)."""
    path = classic_report_path(docs)
    report = docs[path]
    log: list[str] = []

    for e in edits:
        action = e.get("action")
        vid = e.get("visual_id")
        try:
            if action == "change_type":
                vc, _, _ = _classic_find(report, vid)
                cfg = _cfg_load(vc)
                new = rb.VISUAL_TYPE.get(e.get("visual_type", ""), e.get("visual_type", ""))
                cfg.setdefault("singleVisual", {})["visualType"] = new
                _cfg_store(vc, cfg)
                log.append(f"Visual {vid} → Typ {e.get('visual_type')}.")
            elif action == "remove_visual":
                _classic_find(report, vid, remove=True)
                log.append(f"Visual {vid} entfernt.")
            else:
                log.append(f"Aktion '{action}' wird im klassischen Format noch nicht unterstützt.")
        except Exception as ex:  # noqa: BLE001
            log.append(f"Aktion '{action}' übersprungen: {ex}")
    return log


def _classic_find(report: dict[str, Any], vid: str, remove: bool = False):
    for sec in report.get("sections", []):
        vcs = sec.get("visualContainers", [])
        for vi, vc in enumerate(vcs):
            if _classic_vid(_cfg_load(vc), report["sections"].index(sec), vi) == vid \
                    or _cfg_load(vc).get("name") == vid:
                if remove:
                    vcs.pop(vi)
                return vc, sec, vi
    raise ValueError(f"Visual {vid} nicht gefunden")


def to_parts(docs: dict[str, Any], raw: dict[str, str]) -> list[dict[str, str]]:
    parts = [rb.make_part(path, obj) for path, obj in docs.items()]
    parts += [{"path": p, "payload": pl, "payloadType": "InlineBase64"} for p, pl in raw.items()]
    return parts


# ── Zusammenfassung für Claude ─────────────────────────────────
def _visual_fields(content: dict[str, Any]) -> list[str]:
    qs = content.get("visual", {}).get("query", {}).get("queryState", {})
    out: list[str] = []
    for role in qs.values():
        for proj in role.get("projections", []):
            ref = proj.get("nativeQueryRef")
            if ref:
                # Anzeigepräfix entfernen, damit Claude die reine Spalte referenziert
                out.append(ref.removeprefix("Summe von ").strip())
    return out


def summarize(docs: dict[str, Any]) -> list[dict[str, Any]]:
    """Erzeugt eine kompakte Seiten-/Visual-Übersicht (für den Claude-Prompt)."""
    page_names = {m.group(1): docs[path].get("displayName", path)
                  for path in docs if (m := _PAGE_RE.match(path))}
    pages: dict[str, dict[str, Any]] = {
        pid: {"page_id": pid, "name": name, "visuals": []}
        for pid, name in page_names.items()
    }
    for path, content in docs.items():
        m = _VIS_RE.match(path)
        if not m:
            continue
        pid, vid = m.group(1), m.group(2)
        vtype = content.get("visual", {}).get("visualType", "?")
        pages.setdefault(pid, {"page_id": pid, "name": pid, "visuals": []})
        pages[pid]["visuals"].append({
            "visual_id": vid,
            "type": _FRIENDLY.get(vtype, vtype),
            "fields": _visual_fields(content),
        })
    return list(pages.values())


# ── Änderungen anwenden ────────────────────────────────────────
def _first_page_id(docs: dict[str, Any]) -> str:
    pages = docs.get(_PAGES_JSON, {}).get("pageOrder", [])
    return pages[0] if pages else "page1"


def _visual_path(docs: dict[str, Any], visual_id: str) -> str | None:
    for path in docs:
        m = _VIS_RE.match(path)
        if m and m.group(2) == visual_id:
            return path
    return None


def _next_position(docs: dict[str, Any], page_id: str) -> dict[str, int]:
    """Platziert ein neues Visual unter den vorhandenen (kein Überlappen)."""
    bottom = 0
    for path, content in docs.items():
        m = _VIS_RE.match(path)
        if m and m.group(1) == page_id:
            pos = content.get("position", {})
            bottom = max(bottom, pos.get("y", 0) + pos.get("height", 0))
    if bottom == 0:
        return rb.grid_position(0)
    return {"x": 40, "y": bottom + 20, "width": 580, "height": 200}


def _split_roles(query_state: dict[str, Any]) -> tuple[list, list]:
    """Trennt vorhandene Projektionen in Kategorie- und Wert-Projektionen."""
    cat, values = [], []
    for role, obj in query_state.items():
        target = cat if role in ("Category", "Legend") else values
        target.extend(obj.get("projections", []))
    return cat, values


def _auto_layout(docs: dict[str, Any], page_id: str) -> None:
    """Ordnet alle Visuals einer Seite sauber auf die volle Fläche an:
    Karten in einer Reihe oben, übrige Visuals als 2-Spalten-Raster darunter.
    Setzt die Seite zudem auf Standardgröße zurück (frühere Adds verlängern sie)."""
    W, H, m = 1280, 720, 24

    page_path = f"definition/pages/{page_id}/page.json"
    if page_path in docs:
        docs[page_path]["width"] = W
        docs[page_path]["height"] = H
        docs[page_path]["displayOption"] = "FitToPage"

    # Visuals der Seite in aktueller Lesereihenfolge (oben-links zuerst)
    items = []
    for path, content in docs.items():
        mt = _VIS_RE.match(path)
        if mt and mt.group(1) == page_id:
            pos = content.get("position", {})
            items.append((pos.get("y", 0), pos.get("x", 0), path, content))
    items.sort()
    visuals = [c for _, _, _, c in items]
    cards = [c for c in visuals if c.get("visual", {}).get("visualType") == "card"]
    others = [c for c in visuals if c.get("visual", {}).get("visualType") != "card"]

    def place(c, x, y, w, h, i):
        c["position"] = {"x": round(x), "y": round(y), "z": 0,
                         "width": round(w), "height": round(h), "tabOrder": i}

    top = m
    if cards:
        n = len(cards); cw = (W - m * (n + 1)) / n; ch = 130
        for i, c in enumerate(cards):
            place(c, m + i * (cw + m), m, cw, ch, i)
        top = m + ch + m

    if others:
        n = len(others); cols = 2 if n > 1 else 1; rows = math.ceil(n / cols)
        gw = (W - m * (cols + 1)) / cols
        gh = (H - top - m * rows) / rows
        for i, c in enumerate(others):
            r, cix = divmod(i, cols)
            place(c, m + cix * (gw + m), top + r * (gh + m), gw, gh, len(cards) + i)


def _require_visual(docs: dict[str, Any], e: dict[str, Any]) -> str:
    vid = e.get("visual_id")
    if not vid:
        raise ValueError("keine visual_id angegeben")
    path = _visual_path(docs, vid)
    if not path:
        raise ValueError(f"Visual {vid} nicht gefunden")
    return path


def apply_edits(docs: dict[str, Any], edits: list[dict[str, Any]],
                schema: dict[str, Any]) -> list[str]:
    """Wendet die Aktionsliste auf docs an; gibt eine Klartext-Protokollliste zurück.

    Jede Aktion ist gekapselt: eine fehlerhafte Änderung wird protokolliert und
    übersprungen, statt den ganzen Vorgang abzubrechen.
    """
    measure_table, column_table = rb.lookups(schema)
    log: list[str] = []

    for e in edits:
        action = e.get("action")
        try:
            _apply_one(docs, e, action, measure_table, column_table, log)
        except Exception as ex:  # noqa: BLE001
            log.append(f"Aktion '{action}' übersprungen: {ex}")

    return log


def _apply_one(docs, e, action, measure_table, column_table, log) -> None:
        if action == "add_visual":
            page_id = e.get("page_id") or _first_page_id(docs)
            pos = _next_position(docs, page_id)
            visual = {
                "type": e.get("visual_type", "card"),
                "measures": e.get("measures", []),
                "category": e.get("category", ""),
            }
            vis_id, content = rb.new_visual_content(visual, pos, measure_table, column_table)
            title = e.get("title")
            if title:
                content["visual"].setdefault("visualContainerObjects", {})["title"] = \
                    rb.title_property(title)
            docs[f"definition/pages/{page_id}/visuals/{vis_id}/visual.json"] = content
            # Seite bei Bedarf verlängern, damit das neue Visual sichtbar bleibt
            page_path = f"definition/pages/{page_id}/page.json"
            if page_path in docs:
                needed = pos["y"] + pos["height"] + 40
                docs[page_path]["height"] = max(docs[page_path].get("height", 720), needed)
            log.append(f"Visual '{title or visual['type']}' hinzugefügt.")

        elif action == "remove_visual":
            path = _require_visual(docs, e)
            del docs[path]
            log.append(f"Visual {e['visual_id']} entfernt.")

        elif action == "change_type":
            path = _require_visual(docs, e)
            new_type = e.get("visual_type", "")
            pbir_type = rb.VISUAL_TYPE.get(new_type, new_type)
            qs = docs[path]["visual"].get("query", {}).get("queryState", {})
            cat, values = _split_roles(qs)
            docs[path]["visual"]["visualType"] = pbir_type
            docs[path]["visual"].setdefault("query", {})["queryState"] = \
                rb.roles_for(pbir_type, cat, values)
            log.append(f"Visual {e['visual_id']} -> Typ {new_type}.")

        elif action == "set_title":
            path = _require_visual(docs, e)
            docs[path]["visual"].setdefault("visualContainerObjects", {})["title"] = \
                rb.title_property(e.get("title", ""))
            log.append(f"Titel von {e['visual_id']} gesetzt: „{e.get('title', '')}“")

        elif action == "set_fields":
            path = _require_visual(docs, e)
            pbir_type = docs[path]["visual"].get("visualType", "card")
            # Vorhandene Rollen behalten, nur die genannten ersetzen
            old_cat, old_values = _split_roles(
                docs[path]["visual"].get("query", {}).get("queryState", {}))
            if e.get("category"):
                cat, _ = rb.build_projections([], e["category"], measure_table, column_table)
            else:
                cat = old_cat
            if e.get("measures"):
                _, values = rb.build_projections(e["measures"], "", measure_table, column_table)
            else:
                values = old_values
            docs[path]["visual"].setdefault("query", {})["queryState"] = \
                rb.roles_for(pbir_type, cat, values)
            log.append(f"Felder von {e['visual_id']} aktualisiert.")

        elif action == "set_position":
            path = _require_visual(docs, e)
            p = docs[path].setdefault("position", {})
            for k in ("x", "y", "width", "height"):
                if e.get(k) is not None:
                    p[k] = e[k]
            log.append(f"Visual {e['visual_id']} verschoben/skaliert.")

        elif action == "auto_layout":
            page_id = e.get("page_id") or _first_page_id(docs)
            _auto_layout(docs, page_id)
            log.append("Visuals neu angeordnet und auf die volle Fläche skaliert.")

        elif action == "add_page":
            page_id = rb.short_id()
            docs[f"definition/pages/{page_id}/page.json"] = {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.0.0/schema.json",
                "name": page_id,
                "displayName": e.get("page_name", "Neue Seite"),
                "displayOption": "FitToPage",
                "height": 720, "width": 1280,
            }
            docs[_PAGES_JSON].setdefault("pageOrder", []).append(page_id)
            log.append(f"Seite '{e.get('page_name', 'Neue Seite')}' angelegt.")

        elif action == "remove_page":
            pid = e["page_id"]
            for path in [p for p in docs if p.startswith(f"definition/pages/{pid}/")]:
                del docs[path]
            order = docs[_PAGES_JSON].get("pageOrder", [])
            if pid in order:
                order.remove(pid)
            if docs[_PAGES_JSON].get("activePageName") == pid and order:
                docs[_PAGES_JSON]["activePageName"] = order[0]
            log.append(f"Seite {pid} entfernt.")

        else:
            log.append(f"Unbekannte Aktion: {action}")
