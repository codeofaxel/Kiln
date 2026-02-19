"""Team seat management for Business and Enterprise tiers.

Provides team creation, member invites, and role assignment.
Business tier: up to 5 seats. Enterprise tier: unlimited seats.

Team data is persisted to ``~/.kiln/team.json``.

Usage::

    from kiln.teams import TeamManager

    mgr = TeamManager()
    mgr.add_member("alice@openmind.org", role="engineer")
    mgr.list_members()
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TEAM_DIR = Path.home() / ".kiln"
_TEAM_FILE = _TEAM_DIR / "team.json"

# Seat limits by tier.
TIER_SEAT_LIMITS: dict[str, int | None] = {
    "free": 1,
    "pro": 1,
    "business": 5,
    "enterprise": None,  # Unlimited
}


@dataclass
class TeamMember:
    """A member of a Kiln team.

    Attributes:
        email: Member's email address (unique identifier).
        role: One of ``"admin"``, ``"engineer"``, ``"operator"``.
        invited_at: Unix timestamp when the member was added.
        last_active_at: Unix timestamp of last activity, or ``None``.
        active: Whether the member is currently active.
    """

    email: str
    role: str = "engineer"
    invited_at: float = field(default_factory=time.time)
    last_active_at: float | None = None
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TeamError(Exception):
    """Raised for team management failures."""

    pass


class TeamManager:
    """Manages team members and seat limits.

    Persists team data to ``~/.kiln/team.json``.
    """

    def __init__(self, *, team_file: Path | None = None) -> None:
        self._team_file = team_file or _TEAM_FILE
        self._members: dict[str, TeamMember] = {}
        self._load()

    def _load(self) -> None:
        """Load team data from disk."""
        if not self._team_file.exists():
            return
        try:
            data = json.loads(self._team_file.read_text(encoding="utf-8"))
            for email, member_data in data.get("members", {}).items():
                self._members[email] = TeamMember(
                    email=email,
                    role=member_data.get("role", "engineer"),
                    invited_at=member_data.get("invited_at", 0),
                    last_active_at=member_data.get("last_active_at"),
                    active=member_data.get("active", True),
                )
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load team data: %s", exc)

    def _save(self) -> None:
        """Persist team data to disk."""
        self._team_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "members": {
                email: member.to_dict()
                for email, member in self._members.items()
            },
            "updated_at": time.time(),
        }
        self._team_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add_member(
        self,
        email: str,
        *,
        role: str = "engineer",
        tier: str = "enterprise",
    ) -> TeamMember:
        """Add a team member.

        Args:
            email: Member's email address.
            role: Role to assign (admin, engineer, operator).
            tier: Current license tier (for seat limit enforcement).

        Returns:
            The created :class:`TeamMember`.

        Raises:
            TeamError: If seat limit is exceeded or email is already a member.
        """
        if email in self._members and self._members[email].active:
            raise TeamError(f"{email} is already a team member")

        if role not in ("admin", "engineer", "operator"):
            raise TeamError(f"Invalid role: {role!r}. Must be admin, engineer, or operator.")

        # Check seat limit
        limit = TIER_SEAT_LIMITS.get(tier)
        if limit is not None:
            active_count = sum(1 for m in self._members.values() if m.active)
            if active_count >= limit:
                raise TeamError(
                    f"Seat limit reached ({active_count}/{limit} for {tier} tier). "
                    f"Upgrade to add more team members."
                )

        member = TeamMember(email=email, role=role)
        self._members[email] = member
        self._save()
        logger.info("Added team member %s (role=%s)", email, role)
        return member

    def remove_member(self, email: str) -> bool:
        """Deactivate a team member.

        Args:
            email: Member's email to remove.

        Returns:
            ``True`` if the member was found and deactivated.
        """
        member = self._members.get(email)
        if member is None:
            return False
        member.active = False
        self._save()
        logger.info("Removed team member %s", email)
        return True

    def set_member_role(self, email: str, role: str) -> TeamMember:
        """Change a member's role.

        Args:
            email: Member's email.
            role: New role (admin, engineer, operator).

        Returns:
            Updated :class:`TeamMember`.

        Raises:
            TeamError: If member not found or role is invalid.
        """
        if role not in ("admin", "engineer", "operator"):
            raise TeamError(f"Invalid role: {role!r}. Must be admin, engineer, or operator.")

        member = self._members.get(email)
        if member is None or not member.active:
            raise TeamError(f"No active member with email {email!r}")

        member.role = role
        self._save()
        logger.info("Updated role for %s to %s", email, role)
        return member

    def list_members(self, *, include_inactive: bool = False) -> list[TeamMember]:
        """Return all team members.

        Args:
            include_inactive: Include deactivated members.

        Returns:
            List of :class:`TeamMember` objects.
        """
        members = list(self._members.values())
        if not include_inactive:
            members = [m for m in members if m.active]
        return sorted(members, key=lambda m: m.email)

    def member_count(self) -> int:
        """Return count of active members."""
        return sum(1 for m in self._members.values() if m.active)

    def get_member(self, email: str) -> TeamMember | None:
        """Look up a member by email."""
        member = self._members.get(email)
        if member and member.active:
            return member
        return None

    def seat_status(self, tier: str = "enterprise") -> dict[str, Any]:
        """Return current seat usage vs limit.

        Args:
            tier: License tier name.

        Returns:
            Dict with ``used``, ``limit``, and ``available``.
        """
        limit = TIER_SEAT_LIMITS.get(tier)
        used = self.member_count()
        return {
            "used": used,
            "limit": limit,  # None means unlimited
            "available": None if limit is None else max(0, limit - used),
            "tier": tier,
        }
