"""Interaktiver Nutzer-Login via Entra ID (OAuth2 Authorization Code Flow).

Unterschied zu auth.py: Dort meldet sich die App selbst (Service Principal)
gegen Power BI an. Hier melden sich **Personen** an der Web-App an – Single
Sign-on über ihr Microsoft-Konto, beschränkt auf den Datenspezialisten-Tenant.
"""
import msal

from .config import settings

# Nur Identität nötig – MSAL ergänzt openid/profile automatisch.
_SCOPES = ["User.Read"]

_app: msal.ConfidentialClientApplication | None = None


def _client() -> msal.ConfidentialClientApplication:
    # Lazy: MSAL kontaktiert AAD beim Erzeugen – erst beim ersten Login, nicht beim Start.
    global _app
    if _app is None:
        _app = msal.ConfidentialClientApplication(
            client_id=settings.pbi_client_id,
            client_credential=settings.pbi_client_secret,
            authority=settings.authority,  # single-tenant => nur euer Tenant
        )
    return _app


def build_flow() -> dict:
    """Startet den Login-Flow und liefert u. a. die auth_uri zum Weiterleiten."""
    return _client().initiate_auth_code_flow(scopes=_SCOPES, redirect_uri=settings.redirect_uri)


def complete_flow(flow: dict, params: dict) -> dict:
    """Tauscht den zurückgegebenen Code gegen Tokens (inkl. id_token_claims)."""
    return _client().acquire_token_by_auth_code_flow(flow, params)


def user_from_result(result: dict) -> dict | None:
    """Validiert Tenant + optionale Whitelist und liefert die Nutzerinfos."""
    claims = result.get("id_token_claims") or {}
    if claims.get("tid") != settings.azure_tenant_id:
        return None
    email = (claims.get("preferred_username") or claims.get("email") or "").lower()
    allow = settings.allowed_users_list
    if allow and email not in allow:
        return None
    return {"name": claims.get("name", email), "email": email}
