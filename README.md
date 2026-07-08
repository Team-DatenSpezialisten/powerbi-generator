# PowerBI-Generator – Proof of Concept

KI-gesteuerte Statistik- und Dashboard-Generierung für Power BI: Der Nutzer
formuliert in natürlicher Sprache, was er sehen will – **Claude** übersetzt das
gegen das echte Semantic-Model-Schema in DAX bzw. in einen Dashboard-Entwurf,
und die **Power BI REST API** liefert die Daten bzw. schreibt (Phase 2) den
Report in den Workspace.

```
  Nutzer-Prompt
        │
        ▼
  FastAPI  ──►  Claude (Opus 4.8, Structured Output)
        │            │  kennt Tabellen/Spalten/Measures des Models
        │            ▼
        │        DAX-Query  /  Report-Entwurf
        ▼            │
  Power BI REST API ◄┘   (Service Principal, Entra ID)
        │
        ▼
  Echte Daten aus eurem Semantic Model
```

## Was der PoC beweist

| Endpunkt          | Zeigt |
|-------------------|-------|
| `POST /ask`       | **Die Kette funktioniert end-to-end**: NL → DAX → echte Daten aus Power BI. Sofort testbar. |
| `POST /design-report` | Der KI-Design-Schritt: Claude entwirft ein Dashboard-Layout (Seiten + Visuals + Measure-Zuordnung). Grundlage für die Report-Generierung. |
| `GET /schema`     | Auslesen der Model-Metadaten, die Claude als Kontext bekommt. |

## Voraussetzungen (einmalig im Microsoft-Tenant)

1. **App-Registrierung** in Microsoft Entra ID (Azure AD) anlegen → `Client ID`,
   `Client Secret` und `Tenant ID` notieren.
2. **Sicherheitsgruppe** anlegen, den Service Principal (die App) hinzufügen.
3. Im **Power-BI-Admin-Portal → Tenant-Einstellungen** aktivieren (für diese Gruppe):
   - *Service principals can use Power BI APIs*
   - *Dataset Execute Queries REST API*
4. Den Service Principal als **Member/Viewer im Ziel-Workspace** hinzufügen
   (mindestens Build-Rechte am Dataset).
5. `Workspace ID` und `Dataset ID` aus der Power-BI-URL holen.

> Datenschutz: An Claude gehen nur **Schema** (Tabellen-/Spalten-/Measure-Namen)
> und der Nutzer-Prompt – **keine** Rohdaten. Die Daten werden erst durch die
> von Claude erzeugte DAX-Query in eurer Power-BI-Umgebung berechnet.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

copy .env.example .env        # dann .env mit euren Werten füllen
```

## Starten

```bash
uvicorn app.main:app --reload
```

Interaktive API-Doku: http://127.0.0.1:8000/docs

### Beispiel

```bash
curl -X POST http://127.0.0.1:8000/ask ^
  -H "Content-Type: application/json" ^
  -d "{\"question\": \"Umsatz pro Region im letzten Jahr, absteigend\"}"
```

Antwort: die generierte DAX-Query, eine Erklärung und die echten Ergebniszeilen.

## Projektstruktur

```
app/
  config.py     Settings aus .env (Pydantic)
  auth.py       Entra-ID-Token per Service Principal (MSAL)
  powerbi.py    REST-Wrapper: Schema lesen, DAX ausführen
  ai.py         Claude: NL→DAX und NL→Report-Entwurf (Structured Output)
  main.py       FastAPI-Endpunkte
```

## Roadmap

- **Phase 1 (dieser PoC):** NL → DAX → Daten ✅ · KI-Report-Entwurf ✅
- **Phase 2 – Report generieren:** `design-report`-Entwurf → **PBIR**-JSON
  (Enhanced Report Format, ab 2026 Standard) → via **Fabric REST API**
  (`POST /workspaces/{id}/reports` mit base64-kodierter Definition) als neuer
  Report in den Workspace schreiben. Robuster über kuratierte PBIR-Vorlagen,
  die Claude mit Measures/Feldern füllt, als über frei generiertes Layout.
- **Phase 3 – Bestehende Dashboards anpassen:** Power BI Embedded (JS-SDK) für
  Filter/Bookmarks/Seiten + REST API zum Aktualisieren von Report-Definitionen.
- **Web-Frontend:** Chat-Oberfläche vor die API setzen.
- **Härtung:** Service-Principal-Secret in Azure Key Vault, DAX-Validierung/
  Guardrails, Logging, Auth vor die eigenen Endpunkte.
