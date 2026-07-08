# Deployment auf AWS App Runner (via ECR)

App Runner liefert automatisch eine **HTTPS-URL** (nötig für den Entra-Login) und
hat kein Request-Timeout-Problem (Power BI arbeitet teils >30 s asynchron).

## 0. Einmalige Voraussetzungen

- **AWS-CLI installieren** (Windows): https://awscli.amazonaws.com/AWSCLIV2.msi
- **Anmelden:** `aws configure` → Access Key, Secret, Region (Empfehlung **eu-central-1** / Frankfurt), Output `json`
- Docker läuft (habt ihr).

## 1. Image bauen & nach ECR pushen

Im Projektordner (Git-Bash o. ä.):
```bash
AWS_REGION=eu-central-1 ./deploy.sh
```
Das Skript legt das ECR-Repo `powerbi-generator` an (falls nötig), baut das Image
und pusht es. Am Ende wird die **Image-URI** ausgegeben, z. B.
`123456789012.dkr.ecr.eu-central-1.amazonaws.com/powerbi-generator:latest`.

## 2. App-Runner-Service anlegen (AWS-Konsole)

1. **App Runner → Create service**
2. **Source:** Container registry → **Amazon ECR** → *Browse* → obiges Image (`:latest`)
3. **Deployment settings:** *Manual* (oder *Automatic* für Auto-Deploy bei neuem Push)
   - **ECR access role:** *Create new role* zulassen (App Runner darf aus ECR ziehen)
4. **Configure service:**
   - **Port:** `8000`
   - **CPU/Memory:** 0.25 vCPU / 0.5 GB reicht
   - **Auto scaling:** min = **1**, max = **1**  ⟵ wichtig (siehe Hinweis unten)
   - **Environment variables** (aus eurer `.env`, hier als Klartext eintragen):

     | Variable | Wert |
     |---|---|
     | `ANTHROPIC_API_KEY` | euer Claude-Key |
     | `CLAUDE_MODEL` | `claude-opus-4-8` (optional) |
     | `AZURE_TENANT_ID` | … |
     | `PBI_CLIENT_ID` | … |
     | `PBI_CLIENT_SECRET` | … |
     | `PBI_WORKSPACE_ID` | … |
     | `SESSION_SECRET` | langer Zufallsstring (STABIL lassen!) |
     | `ALLOWED_USERS` | optional, kommagetrennt |
     | `REDIRECT_URI` | **vorerst leer/Platzhalter** — kommt in Schritt 3 |

   - **Health check (optional):** Protokoll HTTP, Pfad `/health`
5. **Create & deploy** → warten bis *Running*.

## 3. HTTPS-URL eintragen (Henne-Ei)

App Runner zeigt nach dem Deploy die **Default domain**, z. B.
`https://abcd1234.eu-central-1.awsapprunner.com`.

1. In App Runner die Env-Var **`REDIRECT_URI`** setzen auf:
   `https://<eure-app-runner-domain>/auth/callback`
   → App Runner deployt automatisch neu.

## 4. Redirect-URI in Entra ergänzen

1. Entra-Portal → **App-Registrierungen → powerbi-generator → Authentifizierung**
2. Unter **Web → Redirect URIs** zusätzlich eintragen:
   `https://<eure-app-runner-domain>/auth/callback`
3. Speichern. (Die `http://localhost:8000/...`-URI könnt ihr fürs lokale Entwickeln behalten.)

## 5. Testen

`https://<eure-app-runner-domain>/` öffnen → Microsoft-Login → App erscheint.

---

## Wichtige Hinweise

- **min = max = 1 Instanz:** „Rückgängig" und einige Caches liegen aktuell im
  Arbeitsspeicher der Instanz. Mit mehreren Instanzen würde „Rückgängig" mal
  greifen, mal nicht. Für Mehr-Instanz-Betrieb später: Zustand in Redis/DB auslagern.
- **`SESSION_SECRET` stabil halten:** Ändert er sich, werden alle Nutzer abgemeldet.
- **Neue Version ausrollen:** `./deploy.sh` erneut ausführen → in App Runner **Deploy**
  klicken (oder Auto-Deploy aktiviert lassen).
- **Power-BI-Wasserzeichen** in der Vorschau verschwindet, sobald der Workspace einer
  **Kapazität** (Premium/Fabric) zugewiesen ist — rein kosmetisch.
- **Secrets:** In App Runner als Umgebungsvariablen hinterlegt. Für höhere Sicherheit
  später auf **AWS Secrets Manager** umstellen (App Runner unterstützt Secret-Referenzen).
- **`.env` wird nicht ins Image gebaut** (steht in `.dockerignore`) — Secrets kommen
  ausschließlich über App-Runner-Umgebungsvariablen.
