# PRTG-OPCUA-Server

Dieses Python-Skript liest zyklisch Sensoren aus PRTG (über die v2-API), spiegelt sie als OPC-UA-Objekte und stellt ihre wichtigsten Statuswerte für externe Systeme bereit.

## Funktionsumfang
- Authentifiziert sich per Token beim PRTG-Server und filtert Sensoren anhand eines `device_tag`.
- Legt für jedes Gerät einen eigenen OPC-UA-Node mit Status-Byte an; Sensoren erscheinen als Knoten mit Status, Meldung und `uptimetotal`.
- Aggregiert den kritischsten Sensorstatus zu einem Byte (`StatusSummary`), sodass ein OPC-UA-Client sofort den wichtigsten Zustand erkennt.
- Schreibt strukturierte Logs in eine rotierende Logdatei zur Fehleranalyse.

## Voraussetzungen
1. Python 3.11+ auf Windows Server (für den Servicebetrieb via NSSM getestet, funktioniert ebenso auf Linux).
2. Pakete: `requests`, `opcua`, `urllib3`.
3. Netzwerkzugriff auf den PRTG-Server sowie auf den OPC-UA-Port, der in `config.ini` definiert ist.

## Installation & Betrieb
1. Repository nach `C:\Program Files (x86)\Scripte\PRTG_OPCUAServer` kopieren.
2. Abhängigkeiten installieren:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install requests opcua urllib3
   ```
3. `config.ini` anpassen (siehe Abschnitt unten) und darauf achten, dass der angegebene Logpfad existiert.
4. Testlauf direkt im Terminal:
   ```powershell
   python prtg_opcua_service.py
   ```
5. Dauerbetrieb als Windows-Dienst via NSSM:
   ```powershell
   nssm install PRTG_OPCUA_Service "C:\Program Files (x86)\Scripte\PRTG_OPCUAServer\.venv\Scripts\python.exe" "C:\Program Files (x86)\Scripte\PRTG_OPCUAServer\prtg_opcua_service.py"
   nssm set PRTG_OPCUA_Service AppDirectory "C:\Program Files (x86)\Scripte\PRTG_OPCUAServer"
   nssm set PRTG_OPCUA_Service AppStdout "C:\Program Files (x86)\Scripte\PRTG_OPCUAServer\ServiceLogs\stdout.log"
   nssm set PRTG_OPCUA_Service AppStderr "C:\Program Files (x86)\Scripte\PRTG_OPCUAServer\ServiceLogs\stderr.log"
   nssm start PRTG_OPCUA_Service
   ```

## Aufbau der `config.ini`

| Sektion | Schlüssel | Beschreibung |
|---------|-----------|--------------|
| `[prtg]` | `base_url` | PRTG-Server inkl. Port, z. B. `https://localhost:1616`. |
|         | `username` / `password` | API-Benutzer mit Zugriff auf alle relevanten Geräte. Passwort nach der Inbetriebnahme ändern. |
|         | `device_tag` | PRTG-Tag, das auf Geräten/Sensoren liegt und als Filter dient. |
| `[opcua]` | `endpoint` | OPC-UA-Endpoint, an dem der lokale Server lauscht (z. B. `opc.tcp://0.0.0.0:4840`). |
|         | `namespace_uri` | Eindeutiger Namespace für alle Nodes; sollte organisationstypisch sein. |
| `[service]` | `poll_interval` | Intervall in Sekunden, wie oft Sensoren neu geladen werden. |
| `[logging]` | `log_file` | Vollständiger Pfad zur Logdatei. Pfad muss vorab existieren. |
|         | `log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` oder `CRITICAL`. |
|         | `max_bytes` / `backup_count` | Steuern die rotierende Logdatei: maximale Größe einer Datei und Anzahl der Backups. |

## OPC-UA-Nodestruktur
- Geräte werden als `Device_<ID>_<Name>` angelegt. `make_safe_name` entfernt Sonderzeichen und Leerzeichen, damit Nodes OPC-UA-konforme Namen erhalten.
- Jedes Gerät besitzt die Variable `StatusSummary` (Byte). Das Script bestimmt den kritischsten Sensorstatus anhand der priorisierten Statusliste.
- Sensoren erscheinen als `Sensor_<ID>_<Name>` unterhalb des Geräts und stellen drei Variablen bereit:
  - `status`: Byte mit demselben Mapping wie in `STATUS_BYTE_MAP`.
  - `message`: Aktuelle Meldung von PRTG.
  - `uptimetotal`: Ganzzahliger Wert der bisher aufgezeichneten Laufzeit in Minuten.

## Logging & Fehlersuche
- Logeinträge befinden sich standardmäßig unter `ServiceLogs/prtg_opcua.log`. Dank `RotatingFileHandler` bleiben auch ältere Dateien erhalten.
- Bei API- oder OPC-UA-Problemen erscheinen Fehlermeldungen inklusive Traceback. `DEBUG` hilft beim initialen Troubleshooting, sollte in der Produktion aber wieder reduziert werden.
- Die Konsolenausgabe des ersten Durchlaufs listet alle erkannten Geräte/Sensoren samt vollständigem OPC-UA-Pfad auf – praktisch zum Einrichten von Subscriptions im Client.

## Wartungshinweise
- Zertifikate: Da der API-Aufruf aktuell `verify=False` nutzt, akzeptiert das Skript selbstsignierte PRTG-Zertifikate. In Produktionsumgebungen empfiehlt es sich, stattdessen offizielle Zertifikate und `verify=True` zu verwenden.
- Passwörter regelmäßig ändern (auch im Dienstkonto).
- Bei Schemaänderungen in PRTG genügt es, `device_tag` neu zu setzen oder Sensoren neu zu taggen; OPC-UA-Nodes werden beim nächsten Poll automatisch erweitert.
