"""Wrapper um die Fabric REST API (api.fabric.microsoft.com).

Zuständig für:
  - Items/Reports im Workspace auflisten
  - Reports aus PBIR-Definitionen anlegen (create_report)
  - Bestehende Report-Definitionen lesen (get_report_definition)
    und zurückschreiben (update_report_definition) – Basis fürs Anpassen

Die Fabric-API akzeptiert dasselbe AAD-Token wie die Power BI REST API
(gleicher Service Principal), daher wird get_access_token() wiederverwendet.
Viele Schreib-/Lese-Operationen laufen asynchron (HTTP 202 + Operation-URL);
das kapselt _await_lro().
"""
import time
from typing import Any

import requests

from .auth import get_access_token
from .config import settings

_FABRIC = "https://api.fabric.microsoft.com/v1"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }


def _await_lro(resp: requests.Response, with_result: bool) -> dict[str, Any]:
    """Behandelt sofortige (200/201) und asynchrone (202) Fabric-Antworten."""
    if resp.status_code in (200, 201):
        return resp.json() if resp.content else {}

    if resp.status_code == 202:
        op_url = resp.headers.get("Location")
        for _ in range(30):
            time.sleep(int(resp.headers.get("Retry-After", 2)))
            poll = requests.get(op_url, headers=_headers(), timeout=30)
            status = poll.json().get("status")
            if status == "Succeeded":
                if with_result:
                    r = requests.get(f"{op_url}/result", headers=_headers(), timeout=30)
                    return r.json() if r.status_code == 200 else {"status": status}
                return {"status": status}
            if status == "Failed":
                raise RuntimeError(f"Fabric-Operation fehlgeschlagen: {poll.text}")
        raise RuntimeError("Fabric-Operation: Zeitüberschreitung.")

    raise RuntimeError(f"Fabric {resp.status_code}: {resp.text}")


def list_items(workspace_id: str | None = None) -> list[dict[str, Any]]:
    """Listet alle Fabric-Items (Reports, Semantic Models, …) eines Workspace."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/items"
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"fabric items {resp.status_code}: {resp.text}")
    return [
        {"id": it["id"], "type": it["type"], "displayName": it.get("displayName")}
        for it in resp.json().get("value", [])
    ]


def list_reports(workspace_id: str | None = None) -> list[dict[str, Any]]:
    """Nur die Reports des Workspace (für die Auswahl beim Anpassen)."""
    return [it for it in list_items(workspace_id) if it["type"] == "Report"]


def create_report(display_name: str, parts: list[dict[str, str]],
                  workspace_id: str | None = None) -> dict[str, Any]:
    """Legt einen neuen Report aus einer PBIR-Definition an."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/reports"
    body = {"displayName": display_name, "definition": {"parts": parts}}
    resp = requests.post(url, headers=_headers(), json=body, timeout=120)
    return _await_lro(resp, with_result=True)


def rename_item(item_id: str, display_name: str, workspace_id: str | None = None) -> dict[str, Any]:
    """Benennt ein Fabric-Item um (z. B. Vorschau-Report -> finaler Name)."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/items/{item_id}"
    resp = requests.patch(url, headers=_headers(), json={"displayName": display_name}, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"rename_item {resp.status_code}: {resp.text}")
    return resp.json()


def delete_report(report_id: str, workspace_id: str | None = None) -> None:
    """Löscht einen Report (für „Rückgängig" nach dem Erstellen)."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/items/{report_id}"
    resp = requests.delete(url, headers=_headers(), timeout=60)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"delete_report {resp.status_code}: {resp.text}")


def create_semantic_model(display_name: str, parts: list[dict[str, str]],
                          workspace_id: str | None = None) -> dict[str, Any]:
    """Legt ein neues Semantic Model aus einer Definition an (definition.pbism + model.bim)."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/semanticModels"
    body = {"displayName": display_name, "definition": {"parts": parts}}
    resp = requests.post(url, headers=_headers(), json=body, timeout=120)
    return _await_lro(resp, with_result=True)


def get_model_definition(model_id: str, workspace_id: str | None = None,
                         fmt: str = "TMSL") -> list[dict[str, str]]:
    """Liest die Definition eines Semantic Models (TMSL = model.bim JSON)."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/semanticModels/{model_id}/getDefinition"
    if fmt:
        url += f"?format={fmt}"
    resp = requests.post(url, headers=_headers(), timeout=120)
    data = _await_lro(resp, with_result=True)
    return data["definition"]["parts"]


def update_model_definition(model_id: str, parts: list[dict[str, str]],
                            workspace_id: str | None = None) -> dict[str, Any]:
    """Schreibt eine geänderte Semantic-Model-Definition zurück."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/semanticModels/{model_id}/updateDefinition"
    resp = requests.post(url, headers=_headers(), json={"definition": {"parts": parts}}, timeout=120)
    return _await_lro(resp, with_result=False)


def get_report_definition(report_id: str, workspace_id: str | None = None,
                          fmt: str | None = None) -> list[dict[str, str]]:
    """Liest die PBIR-Definition (Liste base64-kodierter Teile) eines Reports.

    fmt=None liest das native Format; fmt="PBIR" erzwingt die Konvertierung ins
    neue Format (klappt nur bei konvertierbaren Reports).
    """
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
    if fmt:
        url += f"?format={fmt}"
    resp = requests.post(url, headers=_headers(), timeout=120)
    data = _await_lro(resp, with_result=True)
    return data["definition"]["parts"]


def update_report_definition(report_id: str, parts: list[dict[str, str]],
                             workspace_id: str | None = None) -> dict[str, Any]:
    """Schreibt eine geänderte PBIR-Definition zurück in den bestehenden Report."""
    workspace_id = workspace_id or settings.pbi_workspace_id
    url = f"{_FABRIC}/workspaces/{workspace_id}/reports/{report_id}/updateDefinition"
    body = {"definition": {"parts": parts}}
    resp = requests.post(url, headers=_headers(), json=body, timeout=120)
    return _await_lro(resp, with_result=False)
