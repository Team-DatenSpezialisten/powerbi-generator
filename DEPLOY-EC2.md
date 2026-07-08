# Deployment auf EC2 (Docker + Caddy, eigene Subdomain)

Läuft als zwei Container: die App (intern Port 8000) und **Caddy** davor, das
automatisch ein HTTPS-Zertifikat für eure Subdomain holt.

## 1. EC2-Instanz starten

- **AMI:** Amazon Linux 2023 (oder Ubuntu 22.04)
- **Typ:** t3.small reicht (2 GB RAM)
- **Elastic IP** zuweisen (empfohlen — feste IP, damit der DNS-Eintrag stabil bleibt)
- **Security Group (Inbound):**
  - 22 (SSH) — nur eure IP
  - 80 (HTTP) — 0.0.0.0/0  ← Caddy braucht 80 fürs Zertifikat
  - 443 (HTTPS) — 0.0.0.0/0

## 2. DNS-Eintrag setzen

In eurer DNS-Zone einen **A-Record** anlegen:
`powerbi.eure-domain.de` → **Elastic IP der EC2**

(Vor `docker compose up` setzen, sonst schlägt die Zertifikatsausstellung fehl.)

## 3. Docker installieren (auf der EC2, per SSH)

**Amazon Linux 2023:**
```bash
sudo dnf -y install docker git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # danach einmal aus-/einloggen
# Compose-Plugin:
sudo dnf -y install docker-compose-plugin || {
  DOCKER_CONFIG=/usr/local/lib/docker/cli-plugins
  sudo mkdir -p $DOCKER_CONFIG
  sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
    -o $DOCKER_CONFIG/docker-compose
  sudo chmod +x $DOCKER_CONFIG/docker-compose
}
```
*(Ubuntu: `sudo apt update && sudo apt -y install docker.io docker-compose-v2 git`)*

## 4. Code auf die Instanz bringen

Per Git (falls im Repo) **oder** direkt kopieren vom lokalen Rechner:
```bash
# vom lokalen Rechner (Git-Bash), Projektordner:
rsync -av --exclude .venv --exclude .env -e "ssh -i euer-key.pem" \
  ./ ec2-user@<ELASTIC_IP>:~/powerbi-generator/
```
(oder `scp -r`). Danach auf der EC2 in den Ordner: `cd ~/powerbi-generator`

## 5. `.env` anlegen

```bash
cp .env.example .env
nano .env
```
Ausfüllen — alle Werte wie lokal, plus:
```
APP_DOMAIN=powerbi.eure-domain.de
REDIRECT_URI=https://powerbi.eure-domain.de/auth/callback
SESSION_SECRET=<langer, stabiler Zufallsstring>
```

## 6. Starten

```bash
docker compose up -d --build
```
Caddy holt beim ersten Start das Zertifikat (dauert ~10–30 s). Logs prüfen:
```bash
docker compose logs -f caddy   # sollte "certificate obtained" zeigen
docker compose logs -f app
```

## 7. Redirect-URI in Entra ergänzen

Entra-Portal → **App-Registrierung → powerbi-generator → Authentifizierung →
Web → Redirect URIs** → hinzufügen:
`https://powerbi.eure-domain.de/auth/callback` → speichern.

## 8. Testen

`https://powerbi.eure-domain.de/` → Microsoft-Login → App läuft. ✅

---

## Betrieb

- **Neue Version ausrollen:** Code aktualisieren (rsync/git pull) →
  `docker compose up -d --build`
- **Neustart:** `docker compose restart`
- **Stoppen:** `docker compose down` (Zertifikate bleiben im Volume erhalten)
- **Ein Container/Instanz** — „Rückgängig" & Caches liegen im RAM; für Skalierung
  später Zustand auslagern (Redis).
- **Secrets** liegen in `.env` auf der Instanz (nicht im Image, nicht im Repo).
