"""Entra-ID-Authentifizierung per Service Principal (Client Credentials Flow).

Liefert ein AAD-Access-Token für die Power BI REST API. MSAL cached das Token
intern und erneuert es automatisch, wenn es abgelaufen ist.
"""
import msal

from .config import settings

# Scope für die Power BI REST API. `.default` = alle im App-Registration-
# Manifest vergebenen Application-Permissions.
_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
# Scope für Microsoft Graph (SharePoint-Dateizugriff, Phase 3). Braucht eine
# eigene Graph-App-Berechtigung (z. B. Sites.Selected) mit Admin-Zustimmung –
# derselbe Service Principal, nur ein anderes Token.
_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

_app: msal.ConfidentialClientApplication | None = None


def _client() -> msal.ConfidentialClientApplication:
    # Lazy: MSAL kontaktiert AAD beim Erzeugen – erst bei Bedarf, nicht beim Start.
    global _app
    if _app is None:
        _app = msal.ConfidentialClientApplication(
            client_id=settings.pbi_client_id,
            client_credential=settings.pbi_client_secret,
            authority=settings.authority,
        )
    return _app


def _acquire(scope: list[str]) -> str:
    app = _client()
    # Erst den MSAL-Cache prüfen, sonst frisch beim AAD anfragen.
    result = app.acquire_token_silent(scope, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=scope)

    if "access_token" not in result:
        raise RuntimeError(
            f"Token-Abruf fehlgeschlagen: {result.get('error')} – "
            f"{result.get('error_description')}"
        )
    return result["access_token"]


def get_access_token() -> str:
    """AAD-Token für Power BI / Fabric REST API."""
    return _acquire(_SCOPE)


def get_graph_token() -> str:
    """AAD-Token für Microsoft Graph (SharePoint-Zugriff)."""
    return _acquire(_GRAPH_SCOPE)
