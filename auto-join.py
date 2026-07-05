#!/usr/bin/env python3
"""
auto_join_team_battle.py

Überwacht einen bestimmten Lichess-User (den "Ersteller") auf neu erstellte
Turniere/Team-Battles und tritt automatisch mit deinem Team bei.

Voraussetzungen:
    Keine externen Pakete nötig - nutzt nur die Python-Standardbibliothek.

Konfiguration:
    Trage unten bei CONFIG deine Werte ein, oder setze die entsprechenden
    Umgebungsvariablen (empfohlen, damit der Token nicht im Code steht):

        export LICHESS_TOKEN="dein_api_token"
        export LICHESS_TEAM_ID="dein-team-id"
        export LICHESS_CREATOR="username_des_erstellers"

Der API-Token braucht den Scope "tournament:write".
Erstellen unter: https://lichess.org/account/oauth/token

Ausführen:
    python3 auto_join_team_battle.py

Das Skript läuft dauerhaft (Endlosschleife) und prüft alle POLL_INTERVAL
Sekunden, ob der Ersteller ein neues Turnier angelegt hat. Für jedes neue
Turnier wird versucht, mit dem konfigurierten Team beizutreten.

Bereits gesehene Turnier-IDs werden in SEEN_FILE gespeichert, damit bei einem
Neustart nicht erneut versucht wird, alten Turnieren beizutreten.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG - hier anpassen oder per Umgebungsvariable setzen
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("LICHESS_TOKEN", "DEIN_API_TOKEN_HIER")
TEAM_ID = os.environ.get("LICHESS_TEAM_ID", "dein-team-id")
CREATOR = os.environ.get("LICHESS_CREATOR", "username_des_erstellers")

POLL_INTERVAL = 60  # Sekunden zwischen den Checks
SEEN_FILE = Path("seen_tournaments.json")

BASE_URL = "https://lichess.org"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def get_created_tournaments(username: str) -> list:
    """Ruft die vom Nutzer erstellten Turniere ab (ndjson-Stream)."""
    url = f"{BASE_URL}/api/user/{username}/tournament/created"
    req = urllib.request.Request(url, headers=HEADERS)

    tournaments = []
    with urllib.request.urlopen(req, timeout=30) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            tournaments.append(json.loads(line))
    return tournaments


def join_tournament(tournament_id: str, team_id: str) -> bool:
    """Tritt einem Turnier mit dem angegebenen Team bei."""
    url = f"{BASE_URL}/api/tournament/{tournament_id}/join"
    data = urllib.parse.urlencode({"team": team_id}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                print(f"[OK] Turnier {tournament_id} beigetreten "
                      f"(Team: {team_id})")
                return True
            print(f"[FEHLER] Beitritt zu {tournament_id} fehlgeschlagen: "
                  f"{resp.status}")
            return False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[FEHLER] Beitritt zu {tournament_id} fehlgeschlagen: "
              f"{exc.code} {body}")
        return False
    except urllib.error.URLError as exc:
        print(f"[FEHLER] Netzwerkproblem beim Beitritt zu {tournament_id}: {exc}")
        return False


def main() -> None:
    if TOKEN == "DEIN_API_TOKEN_HIER":
        print("Bitte zuerst LICHESS_TOKEN, LICHESS_TEAM_ID und "
              "LICHESS_CREATOR setzen (Umgebungsvariablen oder im Skript).")
        return

    seen = load_seen()
    print(f"Starte Überwachung von '{CREATOR}' ... "
          f"({len(seen)} Turniere bereits bekannt)")

    while True:
        try:
            tournaments = get_created_tournaments(CREATOR)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[WARNUNG] Abfrage fehlgeschlagen: {exc}")
            time.sleep(POLL_INTERVAL)
            continue

        for t in tournaments:
            t_id = t.get("id")
            if not t_id or t_id in seen:
                continue

            # Nur Turniere, die noch nicht beendet sind, sind sinnvoll
            status = t.get("status")  # 10=created, 20=started, 30=finished
            if status == 30:
                seen.add(t_id)
                continue

            print(f"Neues Turnier gefunden: {t_id} "
                  f"(Name: {t.get('fullName', '?')})")

            success = join_tournament(t_id, TEAM_ID)
            if success:
                seen.add(t_id)
                save_seen(seen)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
