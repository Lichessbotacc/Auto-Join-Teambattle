#!/usr/bin/env python3
"""
auto_join_team_battle.py

Prüft EINMAL, ob bestimmte Lichess-User (die "Ersteller") ein neues
Turnier/Team-Battle angelegt haben, und tritt automatisch mit den jeweils
zugeordneten Teams bei.

Gedacht für den Einsatz in einer GitHub Action mit Cron-Trigger (z.B. alle
15 Minuten) statt als Dauer-Loop - siehe .github/workflows/auto-join.yml.

Voraussetzungen:
    Keine externen Pakete nötig - nutzt nur die Python-Standardbibliothek.

Konfiguration:
    LICHESS_TOKEN als Umgebungsvariable/GitHub Secret setzen
    (Scope "tournament:write").
    CREATOR_TEAMS direkt unten im CONFIG-Block eintragen: ein Dictionary,
    das jeden Ersteller-Username auf eine Liste von Team-Slugs abbildet.
    Beispiel:
        CREATOR_TEAMS = {
            "username1": ["DarkOnRapid"],
            "username2": ["DarkOnSwiss", "DarkOnTeams"],
        }
    Für jeden Ersteller wird nach dessen Turnieren gesucht, und für jedes
    neue Turnier wird mit allen zugeordneten Teams beigetreten (sofern dein
    Account Mitglied in diesen Teams ist).

Token erstellen unter: https://lichess.org/account/oauth/token

Ausführen (einmaliger Durchlauf):
    python3 auto_join_team_battle.py

Für jedes Turnier wird pro Team einzeln gespeichert, ob der Beitritt schon
erfolgreich war (in SEEN_FILE). Schlägt ein Beitritt fehl (z.B. Rate Limit),
wird beim nächsten Lauf NUR für die noch fehlenden Teams erneut versucht -
bereits erfolgreiche Team-Beitritte werden nicht wiederholt. Damit das
zwischen GitHub-Action-Läufen erhalten bleibt, committed der Workflow diese
Datei nach jedem Lauf zurück ins Repo (siehe Workflow-Datei).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG - hier anpassen oder per Umgebungsvariable setzen
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("LICHESS_TOKEN", "DEIN_API_TOKEN_HIER")

# Mapping: Ersteller-Username -> Liste der Teams, mit denen bei dessen
# Turnieren beigetreten werden soll.
CREATOR_TEAMS = {
    "seyed111": ["darkonblitz-dob", "darkonteams],
    "Nathanael01": ["DarkOnUltra", "DarkOnTeams"],
    "FRCCENTER": ["DarkOnVariants"],
}

SEEN_FILE = Path("seen_tournaments.json")

# Team-IDs und Usernamen zur Sicherheit auf Kleinbuchstaben normalisieren
# (Lichess-IDs sind intern case-insensitive, aber so gehen wir auf Nummer
# sicher und vermeiden inkonsistente Schreibweisen).
CREATOR_TEAMS = {
    creator.lower(): [team_id.lower() for team_id in teams]
    for creator, teams in CREATOR_TEAMS.items()
}

BASE_URL = "https://lichess.org"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def load_seen() -> dict:
    """
    Struktur: {
        tournament_id: {
            "finished": bool,
            "joined_teams": [team_id, ...]   # bereits erfolgreich beigetretene Teams
        }
    }
    """
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


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
    team_id = team_id.lower()
    url = f"{BASE_URL}/api/tournament/{tournament_id}/join"
    data = urllib.parse.urlencode({"team": team_id}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                print(f"  [OK]     Team '{team_id}' ist beigetreten.")
                return True
            print(f"  [FEHLER] Team '{team_id}' -> HTTP {resp.status}")
            return False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"  [FEHLER] Team '{team_id}' -> HTTP {exc.code}: {body}")
        return False
    except urllib.error.URLError as exc:
        print(f"  [FEHLER] Team '{team_id}' -> Netzwerkproblem: {exc}")
        return False


def format_starts_at(t: dict) -> str:
    """Formatiert den Startzeitpunkt eines Turniers menschenlesbar (lokale Zeit)."""
    ms = t.get("startsAt")
    if not ms:
        return "unbekannt"
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
        return dt.strftime("%d.%m.%Y %H:%M %Z")
    except (TypeError, ValueError, OSError):
        return "unbekannt"


def main() -> None:
    if TOKEN == "DEIN_API_TOKEN_HIER":
        print("Bitte zuerst LICHESS_TOKEN setzen (Umgebungsvariable/Secret).")
        return

    seen = load_seen()
    print(f"Bereits bekannte Turniere insgesamt: {len(seen)}")
    print(f"Konfigurierte Ersteller: {len(CREATOR_TEAMS)}")
    print("=" * 60)

    grand_new_joins = 0
    grand_join_ok = 0
    grand_join_fail = 0
    grand_already_done = 0
    grand_already_finished = 0

    for creator, team_ids in CREATOR_TEAMS.items():
        print()
        print(f"### Ersteller: '{creator}' -> Teams: {', '.join(team_ids)}")

        try:
            tournaments = get_created_tournaments(creator)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[WARNUNG] Abfrage für '{creator}' fehlgeschlagen: {exc}")
            continue

        print(f"{len(tournaments)} Turnier(e) insgesamt von '{creator}' gefunden.")

        for t in tournaments:
            t_id = t.get("id")
            if not t_id:
                continue

            entry = seen.get(t_id, {"finished": False, "joined_teams": []})
            joined_teams = set(entry.get("joined_teams", []))

            # Teams, die für dieses Turnier noch fehlen
            missing_teams = [tid for tid in team_ids if tid not in joined_teams]

            if entry.get("finished"):
                grand_already_finished += 1
                continue

            if not missing_teams:
                # Für dieses Turnier sind bereits alle Teams beigetreten
                grand_already_done += 1
                continue

            status = t.get("status")  # 10=created, 20=started, 30=finished
            name = t.get("fullName", "?")
            variant = t.get("variant", {}).get("name", "?") if isinstance(t.get("variant"), dict) else "?"
            clock = t.get("clock", {})
            clock_str = f"{clock.get('limit', '?')}+{clock.get('increment', '?')}" if clock else "?"
            nb_players = t.get("nbPlayers", "?")
            starts_at = format_starts_at(t)
            url = f"{BASE_URL}/tournament/{t_id}"

            if status == 30:
                print(f"[ÜBERSPRUNGEN] {name} ({t_id}) ist bereits beendet "
                      f"(fehlende Teams werden nicht mehr versucht: "
                      f"{', '.join(missing_teams)}).")
                entry["finished"] = True
                seen[t_id] = entry
                grand_already_finished += 1
                continue

            print()
            print(f"Turnier: {name}")
            print(f"  Ersteller:     {creator}")
            print(f"  ID:            {t_id}")
            print(f"  Link:          {url}")
            print(f"  Variante:      {variant}, Zeitkontrolle: {clock_str}")
            print(f"  Start:         {starts_at}")
            print(f"  Teilnehmer:    {nb_players}")
            print(f"  Status:        {'läuft bereits' if status == 20 else 'noch nicht gestartet'}")
            if joined_teams:
                print(f"  Bereits beigetreten: {', '.join(sorted(joined_teams))}")
            print(f"  Noch beizutreten mit {len(missing_teams)} Team(s): "
                  f"{', '.join(missing_teams)}")

            for team_id in missing_teams:
                if join_tournament(t_id, team_id):
                    joined_teams.add(team_id)
                    grand_join_ok += 1
                    grand_new_joins += 1
                else:
                    grand_join_fail += 1

            entry["joined_teams"] = sorted(joined_teams)
            seen[t_id] = entry

    save_seen(seen)

    print()
    print("=" * 60)
    print("Gesamt-Zusammenfassung:")
    print(f"  Neue erfolgreiche Team-Beitritte: {grand_new_joins}")
    print(f"  Team-Beitritte erfolgreich (gesamt): {grand_join_ok}")
    print(f"  Team-Beitritte fehlgeschlagen:    {grand_join_fail}")
    print(f"  Turniere komplett (übersprungen): {grand_already_done}")
    print(f"  Turniere beendet (übersprungen):  {grand_already_finished}")
    print("=" * 60)


if __name__ == "__main__":
    main()
