"""Zentrale Konfiguration – liest alle Secrets/IDs aus der .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Claude
    anthropic_api_key: str
    claude_model: str = "claude-opus-4-8"

    # Entra ID Service Principal
    azure_tenant_id: str
    pbi_client_id: str
    pbi_client_secret: str

    # Power BI Ziel
    pbi_workspace_id: str
    # Optional: Standard-Dataset. Leer lassen und pro Anfrage per dataset_id
    # wählen (IDs via GET /datasets auflisten).
    pbi_dataset_id: str = ""

    # ── Web-Login (Entra SSO) ──────────────────────────────────
    # Signier-Schlüssel für das Session-Cookie (leer = zufällig pro Start).
    session_secret: str = ""
    # Muss als Redirect-URI in der App-Registrierung eingetragen sein.
    redirect_uri: str = "http://localhost:8000/auth/callback"
    # Optionale Whitelist (kommagetrennte E-Mails); leer = ganzer Tenant erlaubt.
    allowed_users: str = ""

    # ── SharePoint (Microsoft Graph, Phase 3) ──────────────────
    # Wurzel-Website, aus der Datei-Datenquellen gelesen werden, z. B.
    # https://datenspezialisten.sharepoint.com/sites/PowerBI_Test
    # Leer = SharePoint-Import deaktiviert.
    sharepoint_site_url: str = ""

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.azure_tenant_id}"

    @property
    def allowed_users_list(self) -> list[str]:
        return [u.strip().lower() for u in self.allowed_users.split(",") if u.strip()]


settings = Settings()  # type: ignore[call-arg]
