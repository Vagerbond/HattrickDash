"""
Fetcher-Funktionen pro CHPP-Endpoint.
Jede Funktion fetcht XML, parsed es und schreibt in die DB.
Aufrufbar via CLI:  python -m chpp.fetcher --full-init
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import select

import config
from chpp.client import HattrickClient
from chpp.parser import as_bool, as_float, as_int, as_text, ensure_list, xml_to_dict
from database import session_scope, create_tables
from models.arena import ArenaSnapshot
from models.finance import FinanceSnapshot
from models.match import Match, MatchEvent, MatchLineup
from models.player import Player
from models.staff import StaffMember
from models.team import FanSnapshot, Team
from models.training import TrainingWeek
from models.transfer import Transfer
from models.youth import (
    YouthMatch,
    YouthPlayer,
    YouthScoutReport,
    YouthTeam,
    YouthTraining,
)

log = logging.getLogger(__name__)


def _today() -> date:
    return date.today()


def _parse_dt(value: Any) -> datetime | None:
    """CHPP liefert '2026-04-28 18:00:00'."""
    s = as_text(value, "")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _client() -> HattrickClient:
    return HattrickClient()


# ============================================================ teamdetails
def fetch_team_details(team_id: int | None = None) -> int:
    """Liest teamdetails + speichert Team-Stammdaten + FanSnapshot."""
    raw = _client().fetch("teamdetails")
    data = xml_to_dict(raw)
    user = data.get("User", {})
    # teamdetails.xml v1.9: HattrickData/Team direkt (kein Teams-Wrapper).
    own_or_list = data.get("Team")
    teams = ensure_list(own_or_list)
    own = teams[0] if teams else {}
    if team_id is not None:
        for t in teams:
            if as_int(t.get("TeamID")) == team_id:
                own = t
                break

    tid = as_int(own.get("TeamID")) or team_id or config.TEAM_ID
    if not tid:
        raise RuntimeError("Keine TeamID in teamdetails gefunden")

    league = own.get("League", {})
    arena = own.get("Arena", {})
    league_unit = own.get("LeagueLevelUnit") or {}
    if not isinstance(league_unit, dict):
        league_unit = {}

    with session_scope() as db:
        team = db.get(Team, tid) or Team(team_id=tid)
        team.name = as_text(own.get("TeamName"))
        team.short_name = as_text(own.get("ShortTeamName"))
        team.league_id = as_int(league.get("LeagueID"))
        team.league_name = as_text(league.get("LeagueName"))
        # League-Level kommt aus LeagueLevelUnit, nicht LeagueLevelUnitID
        team.league_level = as_int(league_unit.get("LeagueLevel"))
        team.country_id = as_int(league.get("CountryID"))
        team.country_name = as_text(league.get("CountryName"))
        team.arena_id = as_int(arena.get("ArenaID"))
        team.arena_name = as_text(arena.get("ArenaName"))
        # Trainer-Name + Skill kommen ueber den training-Endpoint
        # (wird in fetch_training_info nachgezogen).
        team.last_updated = datetime.utcnow()
        db.merge(team)

        snap = FanSnapshot(
            team_id=tid,
            snapshot_date=_today(),
            fans=as_int(own.get("FanClubSize")),
            fan_mood=as_int(own.get("FanClubMood")),
            team_spirit=as_int(own.get("TeamSpirit") or own.get("TeamSpiritLevel")),
            confidence=as_int(own.get("SelfConfidence")),
            sponsors=as_int(own.get("SponsorsPopularity")),
            youth_squad_level=as_int(own.get("YouthSquad", {}).get("YouthSquadLevel"))
            if isinstance(own.get("YouthSquad"), dict) else None,
        )
        # idempotent: alten Snapshot ersetzen
        existing = db.execute(
            select(FanSnapshot).where(
                FanSnapshot.team_id == tid, FanSnapshot.snapshot_date == _today()
            )
        ).scalar_one_or_none()
        if existing:
            for col in (
                "fans",
                "fan_mood",
                "team_spirit",
                "confidence",
                "sponsors",
                "youth_squad_level",
            ):
                setattr(existing, col, getattr(snap, col))
        else:
            db.add(snap)

    log.info("teamdetails gespeichert (TeamID %s)", tid)
    return 1


# =============================================================== players
_POSITION_MAP = {
    # PlayerNumber ist KEINE Position — Hattrick liefert hier die Trikot-
    # nummer (Default 100, sonst frei waehlbar). Echte Position kommt nur
    # ueber playerdetails.xml/BestPosition. Fallback fuer Manager, die
    # 1..11 als Trikotnummer mit Aufstellungs-Bedeutung nutzen:
    "1": "G",
    "2": "DC", "3": "DC", "4": "DC",
    "5": "DW", "6": "DW",
    "7": "M", "8": "M", "9": "M",
    "10": "W", "11": "W",
}


def _guess_position_from_skills(*, keeper: int = 0, defending: int = 0,
                                playmaking: int = 0, winger: int = 0,
                                passing: int = 0, scoring: int = 0) -> str:
    """Heuristische Best-Position aus den Skills, solange playerdetails.xml
    nicht einzeln pro Spieler geholt wird. Vereinfachte Hattrick-Position-
    Scores — gibt einen 1-2 Zeichen Position-Code zurueck (G/DC/DW/M/W/F).
    """
    if max(keeper, defending, playmaking, winger, passing, scoring) <= 0:
        return ""
    outfield_max = max(defending, playmaking, winger, passing, scoring)
    if keeper >= outfield_max + 3 or (keeper >= 8 and outfield_max <= keeper):
        return "G"
    scores = {
        "DC": 2.0 * defending + 0.5 * playmaking,
        "DW": 1.7 * defending + 0.5 * winger + 0.3 * passing,
        "M":  1.6 * playmaking + 0.6 * passing + 0.3 * defending,
        "W":  1.5 * winger + 0.7 * passing + 0.4 * defending,
        "F":  1.8 * scoring + 0.5 * passing + 0.3 * winger,
    }
    return max(scores, key=scores.get)
_SPECIALTY_MAP = {
    0: "", 1: "Schnell", 2: "Kopfball", 3: "Unberechenbar",
    4: "Technisch", 5: "Kraftvoll", 6: "Verletzungsanfällig", 8: "Unbeständig",
}


def fetch_players(team_id: int | None = None) -> int:
    """Liest Kader + speichert Player-Snapshots."""
    tid = team_id or config.TEAM_ID
    if not tid:
        raise RuntimeError("TEAM_ID nicht gesetzt")
    raw = _client().fetch("players", teamID=tid)
    data = xml_to_dict(raw)
    team = data.get("Team", {})
    # Hattrick liefert seit players.xml v1.4 als Team/PlayerList/Player —
    # Fallback auf Team/Player fuer Abwaertskompatibilitaet.
    player_list = team.get("PlayerList") or {}
    players = ensure_list(
        player_list.get("Player") if isinstance(player_list, dict) else None
    ) or ensure_list(team.get("Player"))
    today = _today()
    count = 0

    with session_scope() as db:
        for p in players:
            pid = as_int(p.get("PlayerID"))
            if not pid:
                continue
            # Single-Row-Update (Public-Version): pro player_id nur EINE Row.
            # Beim naechsten Polling wird dieselbe Row ueberschrieben — kein
            # Snapshot-Verlauf wird angelegt (CHPP-Compliance).
            # Im 'local'-Branch wird hier zusaetzlich snapshot_date als Filter
            # genutzt, sodass pro Tag eine neue Row entsteht.
            existing = db.execute(
                select(Player).where(Player.player_id == pid)
            ).scalar_one_or_none()
            row = existing or Player(player_id=pid, snapshot_date=today)
            row.snapshot_date = today
            row.team_id = tid
            # Hattrick liefert PlayerName als zusammengesetzten String —
            # FirstName/LastName/NickName gibts nur ueber playerdetails.
            full_name = as_text(p.get("PlayerName"))
            first_name = as_text(p.get("FirstName"))
            last_name = as_text(p.get("LastName"))
            if not (first_name or last_name) and full_name:
                parts = full_name.rsplit(" ", 1)
                first_name = parts[0] if len(parts) == 2 else ""
                last_name = parts[-1]
            row.first_name = first_name
            row.last_name = last_name
            row.nickname = as_text(p.get("NickName"))
            row.age_years = as_int(p.get("Age"))
            row.age_days = as_int(p.get("AgeDays"))
            row.nationality_id = as_int(p.get("CountryID"))
            row.nationality_name = as_text(p.get("CountryName") or p.get("NativeCountryName"))
            row.tsi = as_int(p.get("TSI"))
            row.salary = as_float(p.get("Salary"))

            row.skill_keeper = as_int(p.get("KeeperSkill"))
            row.skill_defending = as_int(p.get("DefenderSkill"))
            row.skill_playmaking = as_int(p.get("PlaymakerSkill"))
            row.skill_winger = as_int(p.get("WingerSkill"))
            row.skill_passing = as_int(p.get("PassingSkill"))
            row.skill_scoring = as_int(p.get("ScorerSkill"))
            row.skill_setpieces = as_int(p.get("SetPiecesSkill"))
            row.skill_experience = as_int(p.get("Experience"))
            row.skill_form = as_int(p.get("PlayerForm"))
            row.skill_stamina = as_int(p.get("StaminaSkill"))
            row.skill_loyalty = as_int(p.get("Loyalty"))
            row.motherclub_bonus = as_bool(p.get("MotherClubBonus"))

            spec = as_int(p.get("Specialty"), 0) or 0
            row.specialty = spec
            row.specialty_name = _SPECIALTY_MAP.get(spec, "")

            inj = as_int(p.get("InjuryLevel"), 0) or 0
            row.is_injured = inj > 0
            row.injury_weeks = inj if inj > 0 else 0

            cards = p.get("Cards")
            if isinstance(cards, dict):
                row.yellow_cards = as_int(cards.get("@yellow"), 0) or 0
            else:
                row.yellow_cards = as_int(cards, 0) or 0
            row.is_suspended = as_bool(p.get("Suspended"))
            row.suspension_matches = as_int(p.get("NrOfMatchesSuspended"), 0) or 0

            row.is_transfer_listed = as_bool(p.get("TransferListed"))
            row.is_national_player = as_bool(p.get("NationalTeamID"))

            # Position bestimmen: 1) Trikotnummer 1..11 wenn der Manager so
            # aufstellt, sonst 2) Heuristik aus Skills, sonst 3) "".
            number_str = as_text(p.get("PlayerNumber") or "")
            pos_from_number = _POSITION_MAP.get(number_str, "")
            row.position_code = pos_from_number or _guess_position_from_skills(
                keeper=row.skill_keeper or 0,
                defending=row.skill_defending or 0,
                playmaking=row.skill_playmaking or 0,
                winger=row.skill_winger or 0,
                passing=row.skill_passing or 0,
                scoring=row.skill_scoring or 0,
            )
            row.role_in_team = as_text(p.get("PlayerCategoryId") or p.get("PlayerCategoryID"))
            row.rating_stars = as_float(p.get("Rating"))

            if not existing:
                db.add(row)
            count += 1

    log.info("players: %d Spieler gespeichert", count)
    return count


# =============================================================== economy
def fetch_economy(team_id: int | None = None) -> int:
    """Speichert FinanceSnapshot."""
    tid = team_id or config.TEAM_ID
    raw = _client().fetch("economy", teamID=tid)
    data = xml_to_dict(raw)
    team = data.get("Team", {})
    weeks = ensure_list(team.get("CurrentWeek")) + ensure_list(team.get("NextWeek"))
    week_now = ensure_list(team.get("CurrentWeek"))
    if not week_now:
        # economy hat Income/Costs direkt im Team-Element
        cur = team
    else:
        cur = week_now[0]

    today = _today()
    with session_scope() as db:
        existing = db.execute(
            select(FinanceSnapshot).where(
                FinanceSnapshot.team_id == tid, FinanceSnapshot.snapshot_date == today
            )
        ).scalar_one_or_none()
        snap = existing or FinanceSnapshot(team_id=tid, snapshot_date=today)
        snap.season = as_int(team.get("Season"))
        snap.week_number = as_int(team.get("Week"))

        income = cur.get("Income", {}) if isinstance(cur, dict) else {}
        costs = cur.get("Costs", {}) if isinstance(cur, dict) else {}

        # Hattrick-Einheiten -> Euro per Currency-Faktor (Deutschland=10)
        cf = config.CURRENCY_FACTOR
        def _conv(v):
            return int((as_int(v, 0) or 0) / cf)

        snap.income_supporters = _conv(income.get("SupportersIncome"))
        snap.income_tickets = _conv(income.get("SponsorsBonusFromAchievements")
                                    or income.get("MatchIncome"))
        snap.income_sponsors = _conv(income.get("SponsorsIncome"))
        snap.income_interest = _conv(income.get("FinancialIncome"))
        snap.income_transfers_in = _conv(income.get("SoldPlayersIncome"))
        snap.income_other = _conv(income.get("TemporaryIncome") or income.get("Other"))
        snap.income_total = _conv(cur.get("IncomeSum") or income.get("IncomeSum"))

        snap.costs_arena = _conv(costs.get("ArenaCost"))
        snap.costs_wages = _conv(costs.get("PlayersSalary") or costs.get("Salary"))
        snap.costs_interest = _conv(costs.get("FinancialCosts"))
        snap.costs_specialists = _conv(costs.get("StaffCost"))
        snap.costs_youth = _conv(costs.get("YouthCost"))
        snap.costs_transfers_out = _conv(costs.get("BoughtPlayersCost"))
        snap.costs_other = _conv(costs.get("TemporaryCosts") or costs.get("Other"))
        snap.costs_total = _conv(cur.get("CostsSum") or costs.get("CostsSum"))

        snap.profit = (snap.income_total or 0) - (snap.costs_total or 0)
        snap.cash_balance = _conv(team.get("Cash"))
        snap.cash_next_week = _conv(team.get("CashAfterTransfer") or team.get("CashAfterWeek"))

        if not existing:
            db.add(snap)

    log.info("economy gespeichert")
    return 1


# ================================================================== arena
def fetch_arena(team_id: int | None = None) -> int:
    tid = team_id or config.TEAM_ID
    raw = _client().fetch("arenadetails", teamID=tid)
    data = xml_to_dict(raw)
    arena = data.get("Arena", {})
    cur = arena.get("CurrentCapacity", {})
    today = _today()
    with session_scope() as db:
        existing = db.execute(
            select(ArenaSnapshot).where(
                ArenaSnapshot.team_id == tid, ArenaSnapshot.snapshot_date == today
            )
        ).scalar_one_or_none()
        snap = existing or ArenaSnapshot(team_id=tid, snapshot_date=today)
        snap.arena_id = as_int(arena.get("ArenaID"))
        snap.arena_name = as_text(arena.get("ArenaName"))
        snap.terraces_capacity = as_int(cur.get("RebuiltTerraces") or cur.get("Terraces"), 0) or 0
        snap.seating_capacity = as_int(cur.get("RebuiltBasic") or cur.get("Basic"), 0) or 0
        snap.seating_roof_capacity = as_int(cur.get("RebuiltRoof") or cur.get("Roof"), 0) or 0
        snap.vip_capacity = as_int(cur.get("RebuiltVIP") or cur.get("VIP"), 0) or 0

        last = arena.get("LastMatch", {}) or {}
        snap.last_income_actual = as_int(last.get("Income"), 0) or 0
        snap.last_income_capacity = as_int(last.get("MaxIncome"), 0) or 0
        if not existing:
            db.add(snap)
    log.info("arena gespeichert")
    return 1


# ============================================================ traininginfo
def fetch_training_info(team_id: int | None = None) -> int:
    tid = team_id or config.TEAM_ID
    raw = _client().fetch("training", teamID=tid)
    data = xml_to_dict(raw)
    t = data.get("Team", {}) or {}
    # training.xml v1.3 hat KEINE Saison/Woche mehr — Fallback fuer alt.
    season = as_int(data.get("Season") or t.get("Season"))
    week = as_int(data.get("Week") or t.get("Week"))
    # TrainingLevel ist die Intensitaet (0..100), TrainingIntensity ist alt.
    intensity = as_int(t.get("TrainingLevel") or t.get("TrainingIntensity"))
    stamina = as_int(t.get("StaminaTrainingPart"))
    type_id = as_int(t.get("TrainingType"))
    type_name = _TRAINING_TYPE_MAP.get(type_id or 0, "")
    morale = as_int(t.get("Morale"))
    confidence = as_int(t.get("SelfConfidence"))
    trainer_obj = t.get("Trainer") or {}
    if not isinstance(trainer_obj, dict):
        trainer_obj = {}
    trainer_name = as_text(trainer_obj.get("TrainerName"))

    # Trainer-Name + heutige Stimmung sofort zurueckschreiben.
    with session_scope() as db:
        if trainer_name:
            team = db.get(Team, tid)
            if team:
                team.coach_name = trainer_name
        snap = db.execute(
            select(FanSnapshot).where(
                FanSnapshot.team_id == tid, FanSnapshot.snapshot_date == _today()
            )
        ).scalar_one_or_none()
        if snap is None:
            snap = FanSnapshot(team_id=tid, snapshot_date=_today())
            db.add(snap)
        if morale is not None:
            snap.team_spirit = morale
        if confidence is not None:
            snap.confidence = confidence

    if not (season and week):
        log.info(
            "training: Stimmung/Trainer aktualisiert (Saison/Woche fehlt im Schema)"
        )
        return 0

    with session_scope() as db:
        existing = db.execute(
            select(TrainingWeek).where(
                TrainingWeek.team_id == tid,
                TrainingWeek.season == season,
                TrainingWeek.week_number == week,
            )
        ).scalar_one_or_none()
        row = existing or TrainingWeek(
            team_id=tid, season=season, week_number=week, training_date=_today()
        )
        row.training_type_id = type_id
        row.training_type = type_name
        row.training_intensity = intensity or 0
        row.stamina_share = stamina or 0
        if not existing:
            db.add(row)
    log.info("traininginfo gespeichert (S%d W%d %s)", season, week, type_name)
    return 1


_TRAINING_TYPE_MAP = {
    0: "", 1: "Spielaufbau", 2: "Pässe", 3: "Flügelspiel",
    4: "Standards", 5: "Verteidigung", 6: "Torschuss",
    7: "Manndeckung Außen", 8: "Manndeckung Mitte",
    9: "Konter", 10: "Kondition", 11: "Torwarttraining",
}


# ================================================================ stafflist
_STAFF_LABELS = {
    # stafflist.xml v1.0 liefert StaffType als Integer (1..6).
    # 1=Assistant Trainer, 2=Spokesperson, 3=Press Manager,
    # 4=Form Coach, 5=Doctor, 6=Psychologist.
    # Bestaetigt aus echten Hattrick-Daten:
    "1": "Assistenztrainer",
    "2": "Sportmediziner",
    "5": "Formtrainer",
    # Restliche Slots vermutet (anpassen wenn falsch):
    "3": "Pressesprecher",
    "4": "Psychologe",
    "6": "Spielerverpflegung",
    # Backwards-compat fuer aelteres XML mit String-Type-Namen:
    "GoalkeeperCoach": "Torwarttrainer",
    "AssistantCoach": "Co-Trainer",
    "Psychologist": "Psychologe",
    "PressOfficer": "PR-Manager",
    "FinancialDirector": "Finanzberater",
    "Physiotherapist": "Physiotherapeut",
    "Doctor": "Doktor",
}


def fetch_staff(team_id: int | None = None) -> int:
    tid = team_id or config.TEAM_ID
    try:
        raw = _client().fetch("stafflist", teamID=tid, version="1.0")
    except Exception as exc:  # noqa: BLE001
        log.warning("stafflist nicht verfügbar: %s", exc)
        return 0
    data = xml_to_dict(raw)
    # stafflist.xml v1.0 schema: HattrickData/StaffList/StaffMembers/Staff[*]
    staff_root = data.get("StaffList") or data.get("Team") or {}
    members_wrap = staff_root.get("StaffMembers") if isinstance(staff_root.get("StaffMembers"), dict) else None
    if members_wrap:
        members = ensure_list(members_wrap.get("Staff"))
    else:
        members = ensure_list(staff_root.get("Staff") or staff_root.get("StaffMember"))

    today = _today()
    count = 0
    with session_scope() as db:
        # Existierende Snapshots für heute löschen, dann neu schreiben
        for old in db.execute(
            select(StaffMember).where(
                StaffMember.team_id == tid, StaffMember.snapshot_date == today
            )
        ).scalars():
            db.delete(old)

        for m in members:
            role = as_text(m.get("StaffType") or m.get("StaffTypeName"))
            db.add(
                StaffMember(
                    team_id=tid,
                    snapshot_date=today,
                    role=role,
                    role_label=_STAFF_LABELS.get(role, role),
                    name=as_text(m.get("Name")),
                    skill_level=as_int(m.get("StaffLevel")),
                    salary=as_int(m.get("Cost")),
                )
            )
            count += 1
    log.info("stafflist: %d Mitglieder", count)
    return count


# ================================================================ matchlist
def fetch_match_list(team_id: int | None = None, last_n_weeks: int = 8) -> int:
    """Holt aktuelle Spiele und persistiert Stub-Records (Details via fetch_match_details)."""
    tid = team_id or config.TEAM_ID
    raw = _client().fetch("matches", teamID=tid)
    data = xml_to_dict(raw)
    team = data.get("Team", {})
    matches = ensure_list(team.get("MatchList", {}).get("Match") if isinstance(team.get("MatchList"), dict) else team.get("Match"))

    count = 0
    with session_scope() as db:
        for m in matches:
            mid = as_int(m.get("MatchID"))
            if not mid:
                continue
            row = db.get(Match, mid) or Match(match_id=mid)
            row.match_date = _parse_dt(m.get("MatchDate"))
            row.match_type = as_text(m.get("MatchType"))
            home = m.get("HomeTeam", {}) or {}
            away = m.get("AwayTeam", {}) or {}
            row.home_team_id = as_int(home.get("HomeTeamID")) or 0
            row.home_team_name = as_text(home.get("HomeTeamName"))
            row.away_team_id = as_int(away.get("AwayTeamID")) or 0
            row.away_team_name = as_text(away.get("AwayTeamName"))
            # matches.xml v2.3: HomeGoals/AwayGoals stehen direkt unter Match.
            row.home_goals = as_int(m.get("HomeGoals"))
            row.away_goals = as_int(m.get("AwayGoals"))
            status = as_text(m.get("Status"))
            if hasattr(row, "status"):
                row.status = status
            db.merge(row)
            count += 1
    log.info("matchlist: %d Spiele", count)
    return count


# ============================================================ matchdetails
def fetch_match_details(match_id: int) -> int:
    raw = _client().fetch("matchdetails", matchEvents="true", matchID=match_id)
    data = xml_to_dict(raw)
    matchroot = data.get("Match", {})
    if not matchroot:
        return 0

    home = matchroot.get("HomeTeam", {})
    away = matchroot.get("AwayTeam", {})

    with session_scope() as db:
        row = db.get(Match, match_id) or Match(match_id=match_id)
        row.match_date = _parse_dt(matchroot.get("MatchDate"))
        row.match_type = as_text(matchroot.get("MatchType"))
        row.home_team_id = as_int(home.get("HomeTeamID")) or 0
        row.home_team_name = as_text(home.get("HomeTeamName"))
        row.away_team_id = as_int(away.get("AwayTeamID")) or 0
        row.away_team_name = as_text(away.get("AwayTeamName"))
        row.home_goals = as_int(home.get("HomeGoals"))
        row.away_goals = as_int(away.get("AwayGoals"))
        row.attendance = as_int(matchroot.get("Arena", {}).get("Attendance")) if isinstance(matchroot.get("Arena"), dict) else None
        row.weather = as_text(matchroot.get("WeatherID"))

        # Ratings
        for side, src in (("home", home), ("away", away)):
            setattr(row, f"{side}_rating_mid", as_float(src.get("RatingMidfield")))
            setattr(row, f"{side}_rating_def_left", as_float(src.get("RatingLeftDef")))
            setattr(row, f"{side}_rating_def_center", as_float(src.get("RatingMidDef")))
            setattr(row, f"{side}_rating_def_right", as_float(src.get("RatingRightDef")))
            setattr(row, f"{side}_rating_att_left", as_float(src.get("RatingLeftAtt")))
            setattr(row, f"{side}_rating_att_center", as_float(src.get("RatingMidAtt")))
            setattr(row, f"{side}_rating_att_right", as_float(src.get("RatingRightAtt")))
            setattr(row, f"{side}_tactic", as_text(src.get("TacticType")))
            setattr(row, f"{side}_tactic_skill", as_int(src.get("TacticSkill")))

        row.home_possession_total = as_int(home.get("PossessionFirstHalf"))
        row.home_possession_1h = as_int(home.get("PossessionFirstHalf"))
        row.home_possession_2h = as_int(home.get("PossessionSecondHalf"))
        row.away_possession_1h = as_int(away.get("PossessionFirstHalf"))
        row.away_possession_2h = as_int(away.get("PossessionSecondHalf"))

        db.merge(row)

        # Events – alte löschen + neu anlegen
        for old in db.execute(
            select(MatchEvent).where(MatchEvent.match_id == match_id)
        ).scalars():
            db.delete(old)

        events = ensure_list(matchroot.get("EventList", {}).get("Event") if isinstance(matchroot.get("EventList"), dict) else None)
        for e in events:
            db.add(
                MatchEvent(
                    match_id=match_id,
                    minute=as_int(e.get("Minute")),
                    event_type_id=as_int(e.get("EventTypeID")),
                    event_text=as_text(e.get("EventText")),
                    subject_team_id=as_int(e.get("SubjectTeamID")),
                    subject_player_id=as_int(e.get("SubjectPlayerID")),
                    object_player_id=as_int(e.get("ObjectPlayerID")),
                )
            )
    log.info("matchdetails gespeichert: %d", match_id)
    return 1


# =========================================================== matchlineup
def fetch_match_lineup(match_id: int, team_id: int) -> int:
    """
    Lädt die Aufstellung eines Teams für ein bestimmtes Match.
    Speichert in MatchLineup. Gibt Anzahl Spieler zurück.
    """
    from models.match import MatchLineup
    try:
        raw = _client().fetch("matchlineup", matchID=match_id, teamID=team_id)
    except Exception as exc:
        log.warning("matchlineup %s/%s fehlgeschlagen: %s", match_id, team_id, exc)
        return 0

    data = xml_to_dict(raw)
    team = data.get("Team", {})
    lineup = team.get("Lineup", {}) if isinstance(team.get("Lineup"), dict) else {}
    starting = ensure_list(lineup.get("StartingLineup", {}).get("Player") if isinstance(lineup.get("StartingLineup"), dict) else None)

    if not starting:
        # Manche Versionen: direkt unter Team.Player
        starting = ensure_list(team.get("Player"))

    count = 0
    with session_scope() as db:
        # Existierende Lineups für dieses (match, team) löschen + neu schreiben
        for old in db.execute(
            select(MatchLineup).where(
                MatchLineup.match_id == match_id, MatchLineup.team_id == team_id
            )
        ).scalars():
            db.delete(old)

        for p in starting:
            pid = as_int(p.get("PlayerID"))
            if not pid:
                continue
            db.add(MatchLineup(
                match_id=match_id,
                team_id=team_id,
                player_id=pid,
                role_id=as_int(p.get("RoleID")),
                role_name=as_text(p.get("PositionBehaviour") or p.get("RoleName") or ""),
                position_x=as_float(p.get("PositionX")),
                position_y=as_float(p.get("PositionY")),
                player_rating=as_float(p.get("Rating")),
                player_rating_end_of_match=as_float(p.get("RatingEndOfMatch")),
                is_captain=as_bool(p.get("IsCaptain")),
                is_setpieces=as_bool(p.get("IsSetPiecesTaker")),
            ))
            count += 1
    log.info("matchlineup gespeichert: match=%d team=%d players=%d", match_id, team_id, count)
    return count


def fetch_match_full(match_id: int) -> dict:
    """
    Lädt matchdetails + matchlineup für beide Teams in einem Rutsch.
    Praktisch zum „Gegner-Daten pullen" – bekommt komplettes Bild.
    """
    from models.match import Match
    from sqlalchemy.orm import Session as _Sess
    fetch_match_details(match_id)
    home_id = away_id = None
    with session_scope() as db:
        m = db.get(Match, match_id)
        if m:
            home_id, away_id = m.home_team_id, m.away_team_id
    home_count = away_count = 0
    if home_id:
        home_count = fetch_match_lineup(match_id, home_id)
    if away_id:
        away_count = fetch_match_lineup(match_id, away_id)
    return {
        "match_id": match_id,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_players": home_count,
        "away_players": away_count,
    }


def fetch_opponent_history(opponent_id: int, n_recent: int = 5) -> dict:
    """
    Pullt matchlist für einen Gegner + matchdetails+matchlineup für die letzten N Spiele.
    Damit kann predict_opponent_lineup() arbeiten.
    """
    # Match-Liste des Gegners
    raw = _client().fetch("matches", teamID=opponent_id)
    data = xml_to_dict(raw)
    team = data.get("Team", {})
    matches_xml = ensure_list(
        team.get("MatchList", {}).get("Match") if isinstance(team.get("MatchList"), dict) else team.get("Match")
    )

    # Match-Stubs persistieren
    relevant = []
    with session_scope() as db:
        for m in matches_xml:
            mid = as_int(m.get("MatchID"))
            if not mid:
                continue
            row = db.get(Match, mid) or Match(match_id=mid)
            row.match_date = _parse_dt(m.get("MatchDate"))
            row.match_type = as_text(m.get("MatchType"))
            home = m.get("HomeTeam", {}) or {}
            away = m.get("AwayTeam", {}) or {}
            row.home_team_id = as_int(home.get("HomeTeamID")) or 0
            row.home_team_name = as_text(home.get("HomeTeamName"))
            row.away_team_id = as_int(away.get("AwayTeamID")) or 0
            row.away_team_name = as_text(away.get("AwayTeamName"))
            row.home_goals = as_int(m.get("HomeGoals"))
            row.away_goals = as_int(m.get("AwayGoals"))
            db.merge(row)
            if row.home_goals is not None:  # nur gespielte Matches
                relevant.append((mid, row.match_date))

    # Sortierung jüngste zuerst, top N pullen
    relevant.sort(key=lambda x: x[1] or datetime.min, reverse=True)
    pulled = []
    for mid, _ in relevant[:n_recent]:
        try:
            r = fetch_match_full(mid)
            pulled.append(r)
        except Exception as exc:
            log.exception("Pull match_full %s fehlgeschlagen", mid)
            pulled.append({"match_id": mid, "error": str(exc)})
    return {
        "opponent_id": opponent_id,
        "matches_in_list": len(matches_xml),
        "pulled": pulled,
    }


# =========================================================== leaguedetails
def fetch_league_details(league_id: int | None = None) -> dict:
    """Liefert Ligatabelle als Dict-Liste (für API-Layer; nicht persistiert in v1)."""
    params = {}
    if league_id:
        params["leagueLevelUnitID"] = league_id
    raw = _client().fetch("leaguedetails", **params)
    data = xml_to_dict(raw)
    return data


# ============================================================ transfersTeam
def fetch_transfers_team(team_id: int | None = None) -> int:
    tid = team_id or config.TEAM_ID
    raw = _client().fetch("transfersteam", teamID=tid)
    data = xml_to_dict(raw)
    items = ensure_list(data.get("Team", {}).get("TransferList", {}).get("Transfer") if isinstance(data.get("Team", {}).get("TransferList"), dict) else None)

    count = 0
    with session_scope() as db:
        for t in items:
            tr_id = as_int(t.get("TransferID"))
            row = db.execute(
                select(Transfer).where(Transfer.transfer_id == tr_id)
            ).scalar_one_or_none() if tr_id else None
            row = row or Transfer(transfer_id=tr_id)

            buyer = t.get("Buyer", {})
            seller = t.get("Seller", {})
            buyer_id = as_int(buyer.get("BuyerTeamID"))
            seller_id = as_int(seller.get("SellerTeamID"))
            row.direction = "buy" if buyer_id == tid else "sell"
            row.own_team_id = tid
            row.other_team_id = seller_id if row.direction == "buy" else buyer_id
            row.other_team_name = as_text(
                seller.get("SellerTeamName") if row.direction == "buy" else buyer.get("BuyerTeamName")
            )
            row.deadline = _parse_dt(t.get("Deadline"))
            row.player_id = as_int(t.get("PlayerID")) or 0
            row.player_name = as_text(t.get("PlayerName"))
            row.age_years = as_int(t.get("Age"))
            row.age_days = as_int(t.get("AgeDays"))
            row.tsi = as_int(t.get("TSI"))
            row.price = as_int(t.get("Price"))
            row.currency = as_text(t.get("Currency"), "EUR")
            row.skill_keeper = as_int(t.get("KeeperSkill"))
            row.skill_defending = as_int(t.get("DefenderSkill"))
            row.skill_playmaking = as_int(t.get("PlaymakerSkill"))
            row.skill_winger = as_int(t.get("WingerSkill"))
            row.skill_passing = as_int(t.get("PassingSkill"))
            row.skill_scoring = as_int(t.get("ScorerSkill"))
            row.skill_setpieces = as_int(t.get("SetPiecesSkill"))
            db.merge(row)
            count += 1
    log.info("transfersTeam: %d Records", count)
    return count


# =============================================================== matchorder
def _matchorders_post(match_id: int, lineup_json: str, action_type: str,
                      source_system: str):
    """
    POST an matchorders. WICHTIG: der lineup-Parameter ist ein JSON-String
    (nicht XML) — Query-Params (file/actionType/matchID/sourceSystem) in der
    URL, nur lineup im Form-Body. XML im lineup-Feld lässt chppxml.ashx mit
    ASP.NET-500 abstürzen. Gibt das geparste <HattrickData>-Root zurück.
    """
    from xml.etree import ElementTree as ET
    raw = _client().post(
        "matchorders",
        data={"lineup": lineup_json},
        version="3.1",
        actionType=action_type,
        matchID=match_id,
        sourceSystem=source_system,
    )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        snippet = raw[:300].decode("utf-8", errors="replace")
        raise RuntimeError(f"CHPP-Antwort ist kein XML (HTML-Fehlerseite?): {snippet}")
    error = root.findtext("Error") or root.findtext("ErrorText")
    if error:
        code = root.findtext("ErrorCode") or "?"
        raise RuntimeError(f"CHPP-Fehler {code}: {error}")
    return root


def submit_match_order(match_id: int, lineup_json: str,
                       source_system: str = "hattrick") -> str:
    """
    WRITE-Endpoint! Aufstellung an Hattrick übermitteln.
    lineup_json ist der JSON-String aus lineup_service.build_matchorder_json.
    source_system: "hattrick" für Senior-Spiele, "youth" für Jugendspiele.
    """
    log.warning("matchorder POST für Match %d (sourceSystem=%s) wird gesendet",
                match_id, source_system)
    root = _matchorders_post(match_id, lineup_json, "setmatchorder", source_system)
    md = root.find("MatchData")
    orders_set = (md.get("OrdersSet") if md is not None else None)
    reason = (md.findtext("Reason") if md is not None else None)
    log.info("matchorder %d response: OrdersSet=%s Reason=%r", match_id, orders_set, reason)
    if md is None or str(orders_set).lower() != "true":
        raise RuntimeError(f"Hattrick lehnte ab: {reason or 'unbekannter Grund'}")
    return f"orders_set=True reason={reason or '-'}"


def predict_match_ratings(match_id: int, lineup_json: str,
                          source_system: str = "hattrick") -> dict:
    """
    READ-Dry-Run: schickt dasselbe lineup-JSON mit actionType=predictratings.
    Hattrick parst die Aufstellung serverseitig und liefert die prognostizierten
    Mannschaftsteil-Ratings — validiert das Payload OHNE die Aufstellung zu setzen.
    """
    log.info("predictratings für Match %d (sourceSystem=%s)", match_id, source_system)
    root = _matchorders_post(match_id, lineup_json, "predictratings", source_system)
    md = root.find("MatchData")
    if md is None:
        raise RuntimeError("predictratings: keine MatchData in der Antwort")
    return {
        "tactic_type": as_int(md.findtext("TacticType")),
        "tactic_skill": as_int(md.findtext("TacticSkill")),
        "midfield": as_int(md.findtext("RatingMidfield")),
        "right_def": as_int(md.findtext("RatingRightDef")),
        "mid_def": as_int(md.findtext("RatingMidDef")),
        "left_def": as_int(md.findtext("RatingLeftDef")),
        "right_att": as_int(md.findtext("RatingRightAtt")),
        "mid_att": as_int(md.findtext("RatingMidAtt")),
        "left_att": as_int(md.findtext("RatingLeftAtt")),
    }


# ==================================================================== CLI
# =============================================================== youth

def _get_youth_team_id(senior_team_id: int | None = None) -> int | None:
    """Liest YouthTeamID aus teamdetails. None wenn keine Jugend angemeldet."""
    raw = _client().fetch("teamdetails")
    data = xml_to_dict(raw)
    # teamdetails.xml v1.9: HattrickData/Team direkt (kein Teams-Wrapper).
    teams = ensure_list(data.get("Team"))
    if senior_team_id is None:
        senior_team_id = config.TEAM_ID
    for t in teams:
        if as_int(t.get("TeamID")) == senior_team_id:
            # teamdetails v1.9 exposes YouthTeamID directly under <Team>.
            ytid = as_int(t.get("YouthTeamID"))
            if ytid and ytid > 0:
                return ytid
            # Older responses nested it under <YouthTeam>.
            yt = t.get("YouthTeam") or {}
            ytid = as_int(yt.get("YouthTeamID"))
            if ytid and ytid > 0:
                return ytid
    return None


def fetch_youth_team_details(youth_team_id: int | None = None) -> int:
    """Liest youthteamdetails. Robust gegen 'keine Jugend': returnt 0."""
    if youth_team_id is None:
        youth_team_id = _get_youth_team_id()
    if not youth_team_id:
        log.info("Kein YouthTeam — Fetch übersprungen")
        return 0
    # fetch() takes the query parameters as keyword arguments; the previous
    # params={...} dict was sent as a single literal "params" query parameter,
    # so CHPP never received youthTeamID/showScoutCall.
    raw = _client().fetch("youthteamdetails",
                          youthTeamID=str(youth_team_id),
                          showScoutCall="true")
    data = xml_to_dict(raw)
    yt = data.get("Team", {}) or data.get("YouthTeam", {}) or {}
    if not yt:
        log.warning("youthteamdetails leer für %s", youth_team_id)
        return 0

    league = yt.get("YouthLeague") or yt.get("League") or {}
    coach = yt.get("Coach") or yt.get("Trainer") or {}

    with session_scope() as db:
        team = db.get(YouthTeam, youth_team_id) or YouthTeam(youth_team_id=youth_team_id)
        team.senior_team_id = as_int(yt.get("SeniorTeamID")) or config.TEAM_ID
        team.name = as_text(yt.get("YouthTeamName") or yt.get("TeamName"))
        team.short_name = as_text(yt.get("ShortTeamName"))
        team.league_id = as_int(league.get("YouthLeagueID") or league.get("LeagueID"))
        team.league_name = as_text(league.get("LeagueName") or league.get("YouthLeagueName"))
        team.league_level = as_int(league.get("LeagueLevel"))
        team.coach_name = as_text(coach.get("PlayerName")
                                  or f"{as_text(coach.get('FirstName'))} {as_text(coach.get('LastName'))}".strip())
        team.coach_skill = as_int(coach.get("CoachSkill") or coach.get("TrainerSkill"))
        team.coach_skill_name = as_text(coach.get("CoachSkillName"))
        team.scout_count = as_int(yt.get("NumberOfScouts"))
        team.last_match_date = _parse_dt(yt.get("LastMatchDate"))
        team.next_match_date = _parse_dt(yt.get("NextMatchDate"))
        team.last_updated = datetime.utcnow()
        db.merge(team)

    log.info("youthteamdetails gespeichert (YouthTeamID %s)", youth_team_id)
    return 1


def fetch_youth_players(youth_team_id: int | None = None) -> int:
    """Liest youthplayerlist mit allen Skills (gescoutet)."""
    if youth_team_id is None:
        youth_team_id = _get_youth_team_id()
    if not youth_team_id:
        log.info("Kein YouthTeam — Fetch übersprungen")
        return 0
    # Same kwargs contract as youthteamdetails; additionally ask for the v1.1
    # response, which is the shape parsed below.
    raw = _client().fetch("youthplayerlist",
                          youthTeamID=str(youth_team_id),
                          version="1.1",
                          actionType="details",
                          showScoutCall="true")
    data = xml_to_dict(raw)
    players = ensure_list(
        (data.get("PlayerList") or data.get("Players") or {}).get("YouthPlayer")
        or (data.get("PlayerList") or data.get("Players") or {}).get("Player")
        or data.get("YouthPlayer")
    )
    # CHPP youthplayerlist liefert evtl. flache Liste unter "Player"
    if not players:
        players = ensure_list(data.get("Player"))

    today = _today()
    written = 0
    with session_scope() as db:
        for p in players:
            ypid = as_int(p.get("YouthPlayerID") or p.get("PlayerID"))
            if not ypid:
                continue

            # v1.1 nests the skills under <PlayerSkills>. Values with XML
            # attributes become {"#text": ...} in xml_to_dict, so unwrap them.
            skills = p.get("PlayerSkills") or {}

            def _skill_value(value: Any) -> int | None:
                if isinstance(value, dict):
                    return as_int(value.get("#text"))
                return as_int(value)

            def _sk(prefix: str) -> tuple[int | None, int | None, bool]:
                cur = _skill_value(skills.get(f"{prefix}Skill")
                                   or skills.get(f"{prefix}SkillLevel")
                                   or p.get(f"{prefix}Skill")
                                   or p.get(f"{prefix}SkillLevel"))
                mx  = _skill_value(skills.get(f"{prefix}SkillMax")
                                   or skills.get(f"{prefix}MaxLevel")
                                   or p.get(f"{prefix}MaxLevel")
                                   or p.get(f"{prefix}SkillMax"))
                scouted = bool(mx is not None and mx > 0)
                return cur, mx, scouted

            keep_cur,  keep_mx,  keep_sc  = _sk("Keeper")
            def_cur,   def_mx,   def_sc   = _sk("Defender")
            play_cur,  play_mx,  play_sc  = _sk("Playmaker")
            wing_cur,  wing_mx,  wing_sc  = _sk("Winger")
            pass_cur,  pass_mx,  pass_sc  = _sk("Passing")
            score_cur, score_mx, score_sc = _sk("Scorer")
            sp_cur,    sp_mx,    sp_sc    = _sk("SetPieces")

            age_y = as_int(p.get("Age") or p.get("YearsOld"))
            age_d = as_int(p.get("AgeDays"))
            can_promote = (age_y is not None and age_y >= 17)

            row = YouthPlayer(
                youth_player_id = ypid,
                snapshot_date   = today,
                youth_team_id   = youth_team_id,
                first_name      = as_text(p.get("FirstName")),
                last_name       = as_text(p.get("LastName")),
                nickname        = as_text(p.get("NickName") or p.get("Nickname")),
                age_years       = age_y,
                age_days        = age_d,
                player_number   = as_int(p.get("PlayerNumber")),
                nationality_id  = as_int(p.get("CountryID")),
                nationality_name = as_text(p.get("NationalityName") or p.get("CountryName")),
                specialty       = as_int(p.get("Specialty")) or 0,
                specialty_name  = _SPECIALTY_MAP.get(as_int(p.get("Specialty")) or 0, ""),

                skill_keeper=keep_cur,    skill_defender=def_cur,
                skill_playmaker=play_cur, skill_winger=wing_cur,
                skill_passing=pass_cur,   skill_scorer=score_cur,
                skill_setpieces=sp_cur,

                max_keeper=keep_mx,    max_defender=def_mx,
                max_playmaker=play_mx, max_winger=wing_mx,
                max_passing=pass_mx,   max_scorer=score_mx,
                max_setpieces=sp_mx,

                scouted_keeper=keep_sc,    scouted_defender=def_sc,
                scouted_playmaker=play_sc, scouted_winger=wing_sc,
                scouted_passing=pass_sc,   scouted_scorer=score_sc,
                scouted_setpieces=sp_sc,

                is_promoted=False,
                is_in_squad=True,
                can_be_promoted=can_promote,
                last_match_date=_parse_dt(p.get("LastMatchDate")),
                last_match_minutes=as_int(p.get("LastMatchPlayedMinutes")),
                last_match_id=as_int(p.get("LastMatchID")),
            )

            # Single-Row-Update (Public-Version): pro youth_player_id nur EINE
            # Row. Beim naechsten Polling wird ueberschrieben — kein Skill-
            # Verlauf akkumuliert (CHPP-Compliance, gleicher Grund wie bei
            # senior players).
            existing = db.execute(
                select(YouthPlayer).where(YouthPlayer.youth_player_id == ypid)
            ).scalar_one_or_none()
            if existing:
                for col, val in row.__dict__.items():
                    if col.startswith("_") or col == "id":
                        continue
                    setattr(existing, col, val)
                existing.snapshot_date = today
            else:
                db.add(row)
            written += 1

    log.info("youthplayerlist gespeichert: %d Spieler (YouthTeamID %s)",
             written, youth_team_id)
    return written


def fetch_youth_match_list(youth_team_id: int | None = None) -> int:
    """Holt jüngste Jugend-Spiele (matches; MatchList- und Matches-Variante)."""
    if youth_team_id is None:
        youth_team_id = _get_youth_team_id()
    if not youth_team_id:
        return 0
    raw = _client().fetch("matches", isYouth="true", teamID=youth_team_id)
    data = xml_to_dict(raw)
    matches = ensure_list(
        (data.get("Team") or {}).get("MatchList", {}).get("Match")
        or (data.get("Team") or {}).get("Matches", {}).get("Match")
        or data.get("Matches", {}).get("Match")
        or data.get("Match")
    )
    written = 0
    with session_scope() as db:
        for m in matches:
            mid = as_int(m.get("MatchID") or m.get("YouthMatchID"))
            if not mid:
                continue
            home = m.get("HomeTeam") or {}
            away = m.get("AwayTeam") or {}
            row = db.get(YouthMatch, mid) or YouthMatch(youth_match_id=mid)
            row.match_date = _parse_dt(m.get("MatchDate"))
            row.match_type = as_text(m.get("MatchType")) or "LEAGUE"
            row.home_team_id = as_int(home.get("HomeTeamID") or home.get("TeamID"))
            row.away_team_id = as_int(away.get("AwayTeamID") or away.get("TeamID"))
            row.home_team_name = as_text(home.get("HomeTeamName") or home.get("TeamName"))
            row.away_team_name = as_text(away.get("AwayTeamName") or away.get("TeamName"))
            row.home_goals = as_int(m.get("HomeGoals"))
            row.away_goals = as_int(m.get("AwayGoals"))
            row.season = as_int(m.get("Season"))
            row.matchday = as_int(m.get("MatchRound") or m.get("MatchDay"))
            db.merge(row)
            written += 1
    log.info("youth matches: %d gespeichert", written)
    return written


def fetch_youth_training(youth_team_id: int | None = None) -> int:
    """Liest aktuelles Jugend-Training (Typ + Match-Kind)."""
    if youth_team_id is None:
        youth_team_id = _get_youth_team_id()
    if not youth_team_id:
        return 0
    raw = _client().fetch("training", isYouth="true", teamID=youth_team_id)
    data = xml_to_dict(raw)
    training = data.get("Training") or data.get("YouthTraining") or {}
    if not training:
        return 0
    # Aktuelle Saison + Woche aus Hattrick-Datum berechnen
    now = datetime.utcnow()
    with session_scope() as db:
        row = YouthTraining(
            youth_team_id   = youth_team_id,
            season          = as_int(training.get("Season")) or 0,
            week_number     = as_int(training.get("Week")) or now.isocalendar()[1],
            training_date   = _today(),
            training_type   = as_text(training.get("TrainingType")
                                      or training.get("TrainingName")),
            training_match_kind = as_text(training.get("TrainingMatchKind")
                                           or training.get("TrainingMatch")),
            last_updated    = now,
        )
        existing = db.execute(
            select(YouthTraining).where(
                YouthTraining.youth_team_id == youth_team_id,
                YouthTraining.season == row.season,
                YouthTraining.week_number == row.week_number,
            )
        ).scalar_one_or_none()
        if existing:
            existing.training_type = row.training_type
            existing.training_match_kind = row.training_match_kind
            existing.last_updated = now
        else:
            db.add(row)
    log.info("youth training gespeichert")
    return 1


def full_init() -> None:
    """Initialer Pull aller Daten."""
    create_tables()
    fetch_team_details()
    fetch_players()
    fetch_economy()
    fetch_arena()
    fetch_training_info()
    fetch_staff()
    fetch_match_list()
    fetch_transfers_team()
    # Youth: nur wenn vorhanden, sonst stilles Skip
    try:
        ytid = _get_youth_team_id()
        if ytid:
            fetch_youth_team_details(ytid)
            fetch_youth_players(ytid)
            fetch_youth_match_list(ytid)
            fetch_youth_training(ytid)
    except Exception:
        log.exception("Youth-Fetch im full_init fehlgeschlagen — wird ignoriert")
    log.info("Full-Init abgeschlossen")


def main() -> None:
    parser = argparse.ArgumentParser(description="HattrickDash CHPP Fetcher")
    parser.add_argument("--full-init", action="store_true", help="Alle Endpoints initial pullen")
    parser.add_argument("--endpoint", type=str, help="Einzel-Endpoint: team|players|economy|arena|training|staff|matchlist|transfers")
    parser.add_argument("--match-id", type=int, help="matchdetails für eine Match-ID")
    args = parser.parse_args()

    config.setup_logging()
    create_tables()

    if args.full_init:
        full_init()
        return
    if args.match_id:
        fetch_match_details(args.match_id)
        return
    mapping = {
        "team": fetch_team_details,
        "players": fetch_players,
        "economy": fetch_economy,
        "arena": fetch_arena,
        "training": fetch_training_info,
        "staff": fetch_staff,
        "matchlist": fetch_match_list,
        "transfers": fetch_transfers_team,
        "youth_team":     fetch_youth_team_details,
        "youth_players":  fetch_youth_players,
        "youth_matches":  fetch_youth_match_list,
        "youth_training": fetch_youth_training,
    }
    fn = mapping.get(args.endpoint or "")
    if not fn:
        parser.error("--endpoint oder --full-init oder --match-id erforderlich")
    fn()


if __name__ == "__main__":
    main()
