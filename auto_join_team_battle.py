#!/usr/bin/env python3
"""
auto_join_team_battle.py

Prüft EINMAL, ob ein bestimmter Lichess-User (der "Ersteller") ein neues
Turnier/Team-Battle angelegt hat, und tritt automatisch mit deinem Team bei.

Gedacht für den Einsatz in einer GitHub Action mit Cron-Trigger (z.B. alle
15 Minuten) statt als Dauer-Loop - siehe .github/workflows/auto-join.yml.

Voraussetzungen:
    Keine externen Pakete nötig - nutzt nur die Python-Standardbibliothek.

Konfiguration (Umgebungsvariablen, z.B. als GitHub Secrets):
        LICHESS_TOKEN    -> API-Token mit Scope "tournament:write"
        LICHESS_TEAM_ID  -> Team-Slug aus der lichess.org/team/<slug> URL
        LICHESS_CREATOR  -> Username des Erstellers der Team-Battles

Token erstellen unter: https://lichess.org/account/oauth/token

Ausführen (einmaliger Durchlauf):
    python3 auto_join_team_battle.py

Bereits gesehene Turnier-IDs werden in SEEN_FILE gespeichert. Damit das
zwischen GitHub-Action-Läufen erhalten bleibt, committed der Workflow diese
Datei nach jedem Lauf zurück ins Repo (siehe Workflow-Datei).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG - hier anpassen oder per Umgebungsvariable setzen
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("LICHESS_TOKEN", "DEIN_API_TOKEN_HIER")
TEAM_ID = "darkonrapid"
CREATOR = "m_milan2015"

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
    print(f"Prüfe Turniere von '{CREATOR}' ... "
          f"({len(seen)} Turniere bereits bekannt)")

    try:
        tournaments = get_created_tournaments(CREATOR)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"[WARNUNG] Abfrage fehlgeschlagen: {exc}")
        return

    new_count = 0
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
            new_count += 1

    save_seen(seen)
    print(f"Fertig. {new_count} neue(s) Turnier(e) beigetreten.")


if __name__ == "__main__":
    main()
