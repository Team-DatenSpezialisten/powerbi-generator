"""Wrapper um Microsoft Graph (graph.microsoft.com) für SharePoint-Dateizugriff.

Zuständig für Phase 3: Datei-Datenquellen liegen in einer SharePoint-
Dokumentbibliothek; dieses Modul listet Ordner/Dateien und lädt Dateien als
Bytes. Die Bytes gehen anschließend unverändert in model_import.parse_upload(),
sodass daraus – wie beim Upload – ein Semantic Model gebaut werden kann.

Auth: App-only (Service Principal) über auth.get_graph_token(). Voraussetzung ist
eine Graph-App-Berechtigung (empfohlen: Sites.Selected, Leserecht auf die
konkrete Website) mit Admin-Zustimmung. Ohne die Berechtigung antwortet Graph
mit 403.

Adressierung in Graph:  Website (Site) -> Dokumentbibliothek (Drive) -> Pfad.
"""
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .auth import get_graph_token
from .config import settings

_GRAPH = "https://graph.microsoft.com/v1.0"

# Dateitypen, die als Datenquelle taugen (model_import kann genau diese lesen).
DATA_EXT = (".csv", ".xlsx", ".xlsm")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_graph_token()}"}


def _get(url: str) -> dict[str, Any]:
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Graph {resp.status_code}: {resp.text}")
    return resp.json()


@lru_cache(maxsize=8)
def resolve_site(site_url: str) -> str:
    """Website-URL -> Graph site-id. Ergebnis ist stabil, daher gecacht.

    Beispiel: https://datenspezialisten.sharepoint.com/sites/PowerBI_Test
      -> Host  datenspezialisten.sharepoint.com
      -> Pfad  /sites/PowerBI_Test
    """
    p = urlparse(site_url)
    if not p.netloc or not p.path:
        raise RuntimeError(f"Ungültige SharePoint-Website-URL: {site_url!r}")
    data = _get(f"{_GRAPH}/sites/{p.netloc}:{p.path}")
    return data["id"]


@lru_cache(maxsize=8)
def get_drive(site_id: str) -> str:
    """site-id -> drive-id der Standard-Dokumentbibliothek ('Dokumente')."""
    return _get(f"{_GRAPH}/sites/{site_id}/drive")["id"]


def _drive_for(site_url: str | None = None) -> str:
    site_url = site_url or settings.sharepoint_site_url
    if not site_url:
        raise RuntimeError("Keine SharePoint-Website konfiguriert (SHAREPOINT_SITE_URL).")
    return get_drive(resolve_site(site_url))


def _simplify(item: dict[str, Any]) -> dict[str, Any]:
    """Graph-driveItem auf das Nötige reduzieren (für UI/KI)."""
    return {
        "name": item.get("name"),
        "id": item.get("id"),
        "type": "folder" if "folder" in item else "file",
        "size": item.get("size"),
        "child_count": item.get("folder", {}).get("childCount"),
        "modified": item.get("lastModifiedDateTime"),
    }


def _children_url(drive_id: str, path: str) -> str:
    path = (path or "").strip("/")
    if not path:
        return f"{_GRAPH}/drives/{drive_id}/root/children"
    # Pfad-Segmente einzeln kodieren (Umlaute/Leerzeichen), Slashes erhalten
    enc = "/".join(quote(seg) for seg in path.split("/"))
    return f"{_GRAPH}/drives/{drive_id}/root:/{enc}:/children"


def list_folder(path: str = "", site_url: str | None = None) -> list[dict[str, Any]]:
    """Listet Ordner/Dateien unter einem Pfad (relativ zur Bibliothekswurzel)."""
    drive_id = _drive_for(site_url)
    url = _children_url(drive_id, path)
    items: list[dict[str, Any]] = []
    while url:
        data = _get(url)
        items.extend(_simplify(it) for it in data.get("value", []))
        url = data.get("@odata.nextLink")  # Paginierung
    # Ordner zuerst, dann alphabetisch
    items.sort(key=lambda i: (i["type"] != "folder", (i["name"] or "").lower()))
    return items


def folder_tree(root: str = "", site_url: str | None = None,
                max_depth: int = 3) -> list[dict[str, Any]]:
    """Flacher Baum: jeder Ordner mit seinen Datendateien – Kontext für Claude/UI.

    Rückgabe: [{"folder": "Vertrieb", "files": ["2024.csv", "2025.csv"]}, …]
    Wurzel erscheint als "/". Nur Dateien mit DATA_EXT (alles andere ist für den
    Modellbau irrelevant und würde den Prompt nur aufblähen).
    """
    out: list[dict[str, Any]] = []

    def walk(path: str, depth: int) -> None:
        items = list_folder(path, site_url)
        out.append({
            "folder": path or "/",
            "files": [i["name"] for i in items
                      if i["type"] == "file" and (i["name"] or "").lower().endswith(DATA_EXT)],
        })
        if depth < max_depth:
            for it in items:
                if it["type"] == "folder":
                    walk(f"{path}/{it['name']}".strip("/"), depth + 1)

    walk(root.strip("/"), 0)
    return out


def download_file(item_id: str, site_url: str | None = None) -> bytes:
    """Lädt eine Datei (per driveItem-id) als Bytes herunter."""
    drive_id = _drive_for(site_url)
    url = f"{_GRAPH}/drives/{drive_id}/items/{item_id}/content"
    resp = requests.get(url, headers=_headers(), timeout=120)  # folgt Redirect
    if resp.status_code != 200:
        raise RuntimeError(f"Graph download {resp.status_code}: {resp.text}")
    return resp.content


def list_data_files(folder: str = "", recursive: bool = False,
                    site_url: str | None = None) -> list[dict[str, Any]]:
    """Alle Datendateien eines Ordners – optional inklusive Unterordner.

    Rekursiv ist der Normalfall für gewachsene Ablagen (z. B. ein Monatsordner
    je Lieferung): Alle Dateien darunter gehören zu derselben Tabelle.
    Jedes Element bekommt zusätzlich "folder" (wo es liegt).
    """
    out: list[dict[str, Any]] = []

    def walk(path: str) -> None:
        for it in list_folder(path, site_url):
            if it["type"] == "file" and (it["name"] or "").lower().endswith(DATA_EXT):
                out.append({**it, "folder": path})
            elif it["type"] == "folder" and recursive:
                walk(f"{path}/{it['name']}".strip("/"))

    walk((folder or "").strip("/"))
    return out


def download_head(item_id: str, n_bytes: int = 262144,
                  site_url: str | None = None) -> tuple[bytes, bool]:
    """Lädt nur die ersten n_bytes einer Datei (HTTP-Range).

    Für Variante B reicht ein Anfangsstück, um Spalten/Typen/Trennzeichen zu
    erkennen – die eigentlichen Daten holt später Power BI selbst. Bei großen
    Dateien spart das den kompletten Download.
    Rückgabe: (bytes, truncated) – truncated=True heißt: Datei ist länger.
    """
    drive_id = _drive_for(site_url)
    url = f"{_GRAPH}/drives/{drive_id}/items/{item_id}/content"
    resp = requests.get(url, headers={**_headers(), "Range": f"bytes=0-{n_bytes - 1}"},
                        timeout=60)
    if resp.status_code not in (200, 206):
        raise RuntimeError(f"Graph download {resp.status_code}: {resp.text}")
    # 206 = Teilinhalt; 200 = Server ignorierte Range oder Datei ist kleiner
    truncated = resp.status_code == 206 and len(resp.content) >= n_bytes
    return resp.content, truncated


def download_by_path(path: str, site_url: str | None = None) -> bytes:
    """Lädt eine Datei über ihren Pfad (relativ zur Bibliothekswurzel)."""
    drive_id = _drive_for(site_url)
    enc = "/".join(quote(seg) for seg in path.strip("/").split("/"))
    url = f"{_GRAPH}/drives/{drive_id}/root:/{enc}:/content"
    resp = requests.get(url, headers=_headers(), timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Graph download {resp.status_code}: {resp.text}")
    return resp.content
