"""Team-Übersicht: Stammdaten, Stimmungs-History, Trainerstab."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from chpp.client import HattrickClient
from chpp.parser import as_int, as_text, ensure_list, xml_to_dict
from models.staff import StaffMember
from models.team import FanSnapshot, Team


def get_team_overview(db: Session, team_id: int | None = None) -> dict[str, Any]:
    tid = team_id or config.TEAM_ID
    if not tid:
        return {}

    team = db.get(Team, tid)
    if not team:
        return {}

    all_snaps = db.execute(
        select(FanSnapshot)
        .where(FanSnapshot.team_id == tid)
        .order_by(FanSnapshot.snapshot_date.desc())
    ).scalars().all()
    cur, prev = (list(all_snaps) + [None, None])[:2]
    earliest_date = all_snaps[-1].snapshot_date.isoformat() if all_snaps else None
    latest_date = all_snaps[0].snapshot_date.isoformat() if all_snaps else None

    def diff(field: str) -> int | None:
        if cur is None or prev is None:
            return None
        a = getattr(cur, field) or 0
        b = getattr(prev, field) or 0
        return a - b

    return {
        "team_id": team.team_id,
        "name": team.name,
        "short_name": team.short_name,
        "league_id": team.league_id,
        "league_name": team.league_name,
        "league_level": team.league_level,
        "country_name": team.country_name,
        "arena_id": team.arena_id,
        "arena_name": team.arena_name,
        "coach_name": team.coach_name,
        "coach_skill": team.coach_skill,
        "coach_leadership": team.coach_leadership,
        "current": _snap_to_dict(cur),
        "deltas": {
            "fans": diff("fans"),
            "fan_mood": diff("fan_mood"),
            "team_spirit": diff("team_spirit"),
            "confidence": diff("confidence"),
            "sponsors": diff("sponsors"),
        },
        "last_updated": team.last_updated.isoformat() if team.last_updated else None,
        "snapshot_count": len(all_snaps),
        "earliest_snapshot": earliest_date,
        "latest_snapshot": latest_date,
    }


def _snap_to_dict(s: FanSnapshot | None) -> dict | None:
    if s is None:
        return None
    return {
        "snapshot_date": s.snapshot_date.isoformat(),
        "fans": s.fans,
        "fan_mood": s.fan_mood,
        "team_spirit": s.team_spirit,
        "confidence": s.confidence,
        "sponsors": s.sponsors,
        "youth_squad_level": s.youth_squad_level,
        "training_intensity": s.training_intensity,
        "stamina_share": s.stamina_share,
        "training_type": s.training_type,
    }


def get_staff(db: Session, team_id: int | None = None) -> list[dict]:
    tid = team_id or config.TEAM_ID
    rows = db.execute(
        select(StaffMember)
        .where(StaffMember.team_id == tid)
        .order_by(StaffMember.snapshot_date.desc(), StaffMember.role)
    ).scalars().all()
    if not rows:
        return []
    latest_date = rows[0].snapshot_date
    return [
        {
            "role": r.role,
            "role_label": r.role_label,
            "name": r.name,
            "skill_level": r.skill_level,
            "salary": r.salary,
        }
        for r in rows
        if r.snapshot_date == latest_date
    ]


# ------------------------------------------------ Live-Aufruf für Ligatabelle
def get_league_table(league_id: int | None = None) -> list[dict]:
    """Live von CHPP – wird nicht persistiert, sondern jedes Mal frisch geholt."""
    params = {}
    if league_id:
        params["leagueLevelUnitID"] = league_id
    raw = HattrickClient().fetch("leaguedetails", **params)
    data = xml_to_dict(raw)
    # leaguedetails v1.2+ returns the <Team> elements directly under
    # HattrickData; keep the old LeagueLevelUnit/Teams wrapper as fallback.
    league_unit = data.get("LeagueLevelUnit")
    teams = ensure_list(
        data.get("Team")
        or (league_unit.get("Teams", {}).get("Team")
            if isinstance(league_unit, dict) else None)
    )
    out = []
    for rank, t in enumerate(teams, start=1):
        out.append({
            "rank": as_int(t.get("Position")) or rank,
            "team_id": as_int(t.get("TeamID")),
            "team_name": as_text(t.get("TeamName")),
            "matches": as_int(t.get("Matches")),
            "wins": as_int(t.get("Won")),
            "draws": as_int(t.get("Draws")),
            "losses": as_int(t.get("Lost")),
            "goals_for": as_int(t.get("GoalsFor")),
            "goals_against": as_int(t.get("GoalsAgainst")),
            "goals_diff": (as_int(t.get("GoalsFor"), 0) or 0) - (as_int(t.get("GoalsAgainst"), 0) or 0),
            "points": as_int(t.get("Points")),
        })
    return out
