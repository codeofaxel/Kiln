"""Tests for kiln.teams."""

from __future__ import annotations

import pytest

from kiln.teams import TeamError, TeamManager


class TestTeamManagerBasics:
    def test_empty_team(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        assert mgr.member_count() == 0
        assert mgr.list_members() == []

    def test_add_member(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        member = mgr.add_member("alice@test.com", role="engineer", tier="enterprise")
        assert member.email == "alice@test.com"
        assert member.role == "engineer"
        assert member.active is True

    def test_add_duplicate_raises(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        mgr.add_member("alice@test.com", tier="enterprise")
        with pytest.raises(TeamError, match="already a team member"):
            mgr.add_member("alice@test.com", tier="enterprise")

    def test_invalid_role_raises(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        with pytest.raises(TeamError, match="Invalid role"):
            mgr.add_member("alice@test.com", role="superuser", tier="enterprise")

    def test_remove_member(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        mgr.add_member("alice@test.com", tier="enterprise")
        assert mgr.remove_member("alice@test.com") is True
        assert mgr.member_count() == 0

    def test_remove_nonexistent(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        assert mgr.remove_member("nobody@test.com") is False

    def test_set_member_role(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        mgr.add_member("alice@test.com", role="engineer", tier="enterprise")
        updated = mgr.set_member_role("alice@test.com", "admin")
        assert updated.role == "admin"

    def test_set_role_invalid_raises(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        mgr.add_member("alice@test.com", tier="enterprise")
        with pytest.raises(TeamError, match="Invalid role"):
            mgr.set_member_role("alice@test.com", "superuser")


class TestSeatLimits:
    def test_business_limit_enforced(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        for i in range(5):
            mgr.add_member(f"user{i}@test.com", tier="business")
        with pytest.raises(TeamError, match="Seat limit"):
            mgr.add_member("user5@test.com", tier="business")

    def test_enterprise_unlimited(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        for i in range(20):
            mgr.add_member(f"user{i}@test.com", tier="enterprise")
        assert mgr.member_count() == 20


class TestPersistence:
    def test_survives_reload(self, tmp_path):
        team_file = tmp_path / "team.json"
        mgr1 = TeamManager(team_file=team_file)
        mgr1.add_member("alice@test.com", role="admin", tier="enterprise")
        mgr1.add_member("bob@test.com", role="operator", tier="enterprise")

        mgr2 = TeamManager(team_file=team_file)
        assert mgr2.member_count() == 2
        assert mgr2.get_member("alice@test.com").role == "admin"

    def test_seat_status(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        mgr.add_member("alice@test.com", tier="enterprise")
        status = mgr.seat_status("enterprise")
        assert status["used"] == 1
        assert status["limit"] is None

    def test_seat_status_business(self, tmp_path):
        mgr = TeamManager(team_file=tmp_path / "team.json")
        mgr.add_member("alice@test.com", tier="business")
        status = mgr.seat_status("business")
        assert status["used"] == 1
        assert status["limit"] == 5
        assert status["available"] == 4
