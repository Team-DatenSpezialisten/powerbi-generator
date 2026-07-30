# Addison-Gesamtmodell – Deploy & Test

Kurzanleitung zum Ausrollen der Addison-Anbindung (Variante C) auf Produktion
(`pbigen.ai-gutachten.com`) und zum Testen durch Kollegen.

## Was das Feature macht

Beim App-Start wird automatisch ein Power-BI-Semantic-Model **„Addison Gesamtmodell"**
angelegt, das **1:1 dem Addison-DB-Schema entspricht**: alle Tabellen, alle Mandanten,
und alle Beziehungen direkt aus den in der DB deklarierten Foreign Keys – **ohne KI**
(kein LLM sieht Daten oder Schema). Das Anlegen ist idempotent: existiert das Modell
schon, passiert nichts.

Zusätzlich gibt es den Tab **„🗃️ Aus Addison/SQL"** für KI-gestützte Einzelmodelle
(ein Mandant, per Prompt gewählte Tabellen).

## Deploy auf Produktion

> `redeploy.ps1` deployt aus der lokalen Arbeitskopie (scp `app/` + `requirements.txt`)
> und baut das Image neu. **`.env` wird NICHT mitsynchronisiert** – neue Variablen
> müssen manuell auf der EC2 ergänzt werden.

1. **`.env` auf der EC2 ergänzen** (`ec2-user@63.186.71.33:~/powerbi-generator/.env`):
   ```
   ADDISON_SQL_SERVER=18.196.22.233
   ADDISON_SQL_PORT=1433
   ADDISON_SQL_DATABASE=AlleMandanten
   ADDISON_SQL_USER=<login>
   ADDISON_SQL_PASSWORD=<passwort>
   ADDISON_MODEL_NAME=Addison Gesamtmodell
   ADDISON_AUTO_MODEL=true
   ```
2. **Deployen** (im Projektordner, lokal): `.\redeploy.ps1`
   `docker compose up -d --build` liest die geänderte `.env` neu ein (nur `restart`
   würde das **nicht** tun). `pymssql` kommt über `requirements.txt` als fertiges
   Wheel ins Image – keine zusätzlichen apt-Pakete nötig.
3. **Prüfen**: https://pbigen.ai-gutachten.com/ – einloggen, im Tab „Dashboard
   erstellen" muss das Dataset **„Addison Gesamtmodell"** in der Auswahl stehen.

## Einmalige Schritte pro Modell (kein Deploy)

- **Gateway-Zuordnung**: Ein neues Modell muss in den Dataset-Einstellungen dem
  On-premises-Gateway **`Addison-EC2`** zugeordnet werden (Gateway- und
  Cloudverbindungen → Datenquelle zuordnen → Übernehmen), sonst scheitert der
  Refresh mit `DMTS_DatasourceHasNoCredentialError`. Für das aktuell live liegende
  „Addison Gesamtmodell" ist das bereits erledigt und ein voller Refresh gelaufen.
- **Refresh** danach über die Dataset-Aktualisierung. Voller Import aller Mandanten
  (~4 Mio Zeilen) dauert auf der aktuellen Testkapazität ~17 Min.

## Firewall-Hinweis

- Das **Gesamtmodell** braucht zur Laufzeit **keine** DB-Verbindung von der App-EC2
  (Power BI holt die Daten über das Gateway). Kollegen können damit sofort testen.
- Der **KI-Tab „Aus Addison/SQL"** ruft die DB direkt von der App-EC2 ab
  (Mandantenliste, Schema). Dafür muss die App-EC2-IP (`63.186.71.33`) am
  Addison-SQL-Server (Port 1433) freigeschaltet sein – sonst zeigt nur dieser Tab
  einen Fehler, alles andere läuft.

## So testen Kollegen

1. Auf https://pbigen.ai-gutachten.com/ mit dem Datenspezialisten-Entra-Konto einloggen.
2. Tab **„Dashboard erstellen"** → Dataset **„Addison Gesamtmodell"** wählen.
3. In natürlicher Sprache ein Dashboard/eine Kennzahl beschreiben → wird gebaut.

## Bekannte offene Punkte

- SQL-Login ist noch `sa` – auf read-only `db_datareader` umstellen und Passwort rotieren.
- Datums-Bereinigung (< 01.03.1900 → null) verhindert Query-Folding → große Tabellen
  laden langsamer. Bei Bedarf später auf SQL-Ebene verschieben.
- Multi-Mandant-Modell: v1 mischt alle Mandanten in einem Modell (Beziehungen über
  globale Schlüssel WjId/DlsId/KostId/MandantId). Ein Mandanten-Slicer/Composite-Key
  ist noch nicht gebaut.
