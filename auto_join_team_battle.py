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

    WICHTIG: Es werden ausschließlich Team-Battle-Turniere berücksichtigt.
    Normale Arena-Turniere (ohne Team-Battle-Modus) werden komplett
    ignoriert - auch nicht in seen_tournaments.json eingetragen -, da man
    dort nicht "mit einem Team" beitreten kann.

    Optional: TEAM_KEYWORDS erlaubt es, ein Team nur bei einer bestimmten
    Zeitkontrolle beitreten zu lassen (ultrabullet/bullet/blitz/rapid/
    classical) - basierend auf der tatsächlichen Bedenkzeit des Turniers,
    nicht auf dessen Namen. Teams ohne Eintrag in TEAM_KEYWORDS treten immer
    bei, unabhängig von der Zeitkontrolle.

Token erstellen unter: https://lichess.org/account/oauth/token

Ausführen (einmaliger Durchlauf):
    python3 auto_join_team_battle.py

Für jedes Turnier wird pro Team einzeln gespeichert, ob der Beitritt schon
erfolgreich war (in SEEN_FILE). Schlägt ein Beitritt fehl (z.B. Rate Limit),
wird beim nächsten Lauf NUR für die noch fehlenden Teams erneut versucht -
bereits erfolgreiche Team-Beitritte werden nicht wiederholt. Damit das
zwischen GitHub-Action-Läufen erhalten bleibt, committed der Workflow diese
Datei nach jedem Lauf zurück ins Repo (siehe Workflow-Datei).

Sobald Lichess mit HTTP 429 (Rate Limit) antwortet, bricht das Skript SOFORT
komplett ab (kein Warten/Retry innerhalb des Laufs), speichert aber vorher
alles bisher Erledigte. Der nächste geplante GitHub-Actions-Lauf (Cron,
z.B. alle 15 Minuten) übernimmt dann den Rest automatisch.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG - hier anpassen oder per Umgebungsvariable setzen
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("LICHESS_TOKEN", "DEIN_API_TOKEN_HIER")

# Mapping: Ersteller-Username -> Liste der Teams, mit denen bei dessen
# Turnieren beigetreten werden soll.
CREATOR_TEAMS = {
    "seyed111": ["darkonblitz-dob", "darkonteams"],
    "DarkOnCrack": ["darkonblitz-dob", "german11", "darkonteams"],
    "Nathanael01": ["DarkOnUltra", "DarkOnTeams"],
    "FRCCENTER": ["DarkOnVariants"],
    "Gouravprithyani": ["DarkOnBlitz-dob", "german11"],
    "shadow_ghost66": ["Darkonblitz-dob", "german11"],
    "Arseniy_Rybasov": ["DarkOnUltra", "DarkOnTeams"],
    "Experimentator1": ["DarkOnBullet", "DarkOnBlitz-dob", "DarkOnTeams"],
    "Jeffforever": ["DarkOnBlitz-dob", "darkonswiss-dos", "darkonrapid", "german11", "darkonclassical", "darkonleagues", "darkonteams"],
    "kombinator02": ["DarkOnRapid", "DarkOnTeams"],
    "Sy_Idus": ["german11", "DarkOnBullt", "DarkOnTeams"],
    "Gloria1959": ["DarkOnClassical", "DarkOnTeams"],
    "Lichess": ["DarkOnVariants"],
    "Ezrg94": ["DarkOnBlitz-dob", "DarkOnRapid", "DarkOnBullt", "DarkOnTeams"],
    "M_milan2015": ["DarkOnRapid"],
    "Kurt_rohrer56": ["DarkOnClassical"],
    "Abyin2000": ["DarkOnRapid", "DarkOnClassical"],
    "jorgeeespinoza": ["DarkOnBullt", "DarkOnClassical", "DarkOnRapid"],
    "Alexander_Savchenko": ["DarkOnRapid"],
}

# Mapping: Team-ID -> erforderliche Geschwindigkeits-Kategorie (basierend auf
# der Zeitkontrolle des Turniers, genau wie Lichess selbst klassifiziert:
# ultrabullet / bullet / blitz / rapid / classical).
# Team-IDs, die hier NICHT auftauchen, treten immer bei, unabhängig von der
# Zeitkontrolle.
TEAM_KEYWORDS = {
    "darkonultra": "ultrabullet",
    "darkonbullet": "bullet",
    "darkonblitz-dob": "blitz",
    "darkonrapid": "rapid",
    "darkonclassical": "classical",
    # darkonswiss, darkonteams, darkonvariants -> keine Einschränkung
}


def classify_speed(clock: dict) -> str:
    """
    Klassifiziert die Zeitkontrolle eines Turniers genau wie Lichess selbst:
    geschätzte Spieldauer = limit (Sekunden) + 40 * increment (Sekunden).
    """
    limit = clock.get("limit")
    increment = clock.get("increment", 0)
    if limit is None:
        return "unbekannt"

    estimate = limit + 40 * increment

    if estimate < 30:
        return "ultrabullet"
    if estimate < 180:
        return "bullet"
    if estimate < 480:
        return "blitz"
    if estimate < 1500:
        return "rapid"
    return "classical"

SEEN_FILE = Path("seen_tournaments.json")

# Ersteller, die nicht bei jedem Lauf neu abgefragt werden sollen, sondern
# nur alle X Tage (z.B. weil sie extrem viele Turniere anlegen und das
# Rate-Limit / die Laufzeit unnötig belasten). Mapping: Username (klein-
# geschrieben) -> Mindestabstand in Tagen zwischen zwei Abfragen.
REDUCED_CHECK_INTERVAL_DAYS = {
    "jeffforever": 2,
}

# Meta-Key in seen_tournaments.json, unter dem die letzten Check-Zeitpunkte
# pro Ersteller gespeichert werden.
LAST_CHECKED_KEY = "_last_checked"

# Team-IDs und Usernamen zur Sicherheit auf Kleinbuchstaben normalisieren
# (Lichess-IDs sind intern case-insensitive, aber so gehen wir auf Nummer
# sicher und vermeiden inkonsistente Schreibweisen).
CREATOR_TEAMS = {
    creator.lower(): [team_id.lower() for team_id in teams]
    for creator, teams in CREATOR_TEAMS.items()
}

BASE_URL = "https://lichess.org"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class RateLimitError(Exception):
    """Wird ausgelöst, wenn Lichess mit HTTP 429 (Rate Limit) antwortet."""


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

    try:
        tournaments = []
        with urllib.request.urlopen(req, timeout=30) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                tournaments.append(json.loads(line))
        return tournaments
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitError(f"Rate Limit beim Abfragen von '{username}'") from exc
        raise


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
        if exc.code == 429:
            raise RateLimitError(
                f"Rate Limit beim Beitritt zu Turnier {tournament_id} "
                f"(Team '{team_id}')"
            ) from exc
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
    grand_skipped_arenas = 0

    for creator, team_ids in CREATOR_TEAMS.items():
        print()
        print(f"### Ersteller: '{creator}' -> Teams: {', '.join(team_ids)}")

        interval_days = REDUCED_CHECK_INTERVAL_DAYS.get(creator)
        if interval_days is not None:
            last_checked_raw = seen.get(LAST_CHECKED_KEY, {}).get(creator)
            if last_checked_raw:
                try:
                    last_checked = datetime.fromisoformat(last_checked_raw)
                    if datetime.now(timezone.utc) - last_checked < timedelta(days=interval_days):
                        remaining = timedelta(days=interval_days) - (datetime.now(timezone.utc) - last_checked)
                        print(f"  Übersprungen - '{creator}' wird nur alle {interval_days} Tage "
                              f"geprüft (letzter Check: {last_checked_raw}, "
                              f"noch ca. {remaining} bis zum nächsten Check).")
                        continue
                except ValueError:
                    pass  # kaputter/fehlender Zeitstempel -> normal weitermachen

        try:
            tournaments = get_created_tournaments(creator)
        except RateLimitError as exc:
            print(f"[RATE LIMIT] {exc}")
            print("Breche Skript sofort ab. Nächster Versuch beim nächsten "
                  "geplanten Lauf (z.B. in 15 Minuten).")
            save_seen(seen)
            return
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[WARNUNG] Abfrage für '{creator}' fehlgeschlagen: {exc}")
            continue

        print(f"{len(tournaments)} Turnier(e) insgesamt von '{creator}' gefunden.")

        if interval_days is not None:
            seen.setdefault(LAST_CHECKED_KEY, {})[creator] = datetime.now(timezone.utc).isoformat()

        for t in tournaments:
            t_id = t.get("id")
            if not t_id:
                continue

            # Nur Team-Battles berücksichtigen - normale Arena-Turniere
            # (ohne "teamBattle"-Feld im Turnier-Objekt) werden komplett
            # ignoriert, da man dort nicht "mit einem Team" beitritt.
            if not t.get("teamBattle"):
                grand_skipped_arenas += 1
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
            speed = classify_speed(clock) if clock else "unbekannt"
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

            # Teams anhand der Zeitkontrolle filtern: ein Team wird nur
            # versucht, wenn kein Keyword gesetzt ist ODER das Keyword zur
            # erkannten Geschwindigkeit passt.
            battle_teams = set()
            team_battle_info = t.get("teamBattle")
            if isinstance(team_battle_info, dict):
                teams_field = team_battle_info.get("teams", [])
                if isinstance(teams_field, dict):
                    battle_teams = {tid.lower() for tid in teams_field.keys()}
                elif isinstance(teams_field, list):
                    battle_teams = {tid.lower() for tid in teams_field}

            applicable_teams = []
            skipped_teams = []
            not_in_battle = []
            for tid in missing_teams:
                if battle_teams and tid not in battle_teams:
                    # Team ist bei diesem Team-Battle gar nicht als
                    # teilnehmendes Team registriert -> Beitritt würde immer
                    # mit HTTP 400 "Missing team" fehlschlagen. Kein Retry
                    # sinnvoll, also als erledigt markieren.
                    not_in_battle.append(tid)
                    continue
                required_speed = TEAM_KEYWORDS.get(tid)
                if required_speed is None or required_speed == speed:
                    applicable_teams.append(tid)
                else:
                    skipped_teams.append((tid, required_speed))

            print()
            print(f"Turnier: {name}")
            print(f"  Ersteller:     {creator}")
            print(f"  ID:            {t_id}")
            print(f"  Link:          {url}")
            print(f"  Variante:      {variant}, Zeitkontrolle: {clock_str} "
                  f"(Kategorie: {speed})")
            print(f"  Start:         {starts_at}")
            print(f"  Teilnehmer:    {nb_players}")
            print(f"  Status:        {'läuft bereits' if status == 20 else 'noch nicht gestartet'}")
            if joined_teams:
                print(f"  Bereits beigetreten: {', '.join(sorted(joined_teams))}")
            if skipped_teams:
                skip_str = ", ".join(f"{tid} (erwartet: {req})" for tid, req in skipped_teams)
                print(f"  Übersprungen (Zeitkontrolle passt nicht): {skip_str}")
            if not_in_battle:
                print(f"  Nicht Teil dieses Team-Battles (kein Beitritt möglich): "
                      f"{', '.join(not_in_battle)}")
                # Als erledigt markieren, damit nicht jeden Run erneut ein
                # aussichtsloser Join-Versuch (HTTP 400 "Missing team")
                # unternommen wird.
                joined_teams.update(not_in_battle)

            if not applicable_teams:
                print("  Kein passendes Team für diese Zeitkontrolle - "
                      "kein Beitritt in diesem Lauf.")
                entry["joined_teams"] = sorted(joined_teams)
                seen[t_id] = entry
                continue

            print(f"  Trete bei mit {len(applicable_teams)} Team(s): "
                  f"{', '.join(applicable_teams)}")

            for team_id in applicable_teams:
                try:
                    success = join_tournament(t_id, team_id)
                except RateLimitError as exc:
                    print(f"[RATE LIMIT] {exc}")
                    print("Breche Skript sofort ab. Nächster Versuch beim "
                          "nächsten geplanten Lauf (z.B. in 15 Minuten).")
                    entry["joined_teams"] = sorted(joined_teams)
                    seen[t_id] = entry
                    save_seen(seen)
                    return

                if success:
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
    print(f"  Arenas ignoriert (keine Team-Battles): {grand_skipped_arenas}")
    print("=" * 60)


if __name__ == "__main__":
    main()
