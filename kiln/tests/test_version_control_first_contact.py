"""An agent learns version control in its first minute with Kiln.

An agent handed a code repository already knows what a commit, a branch and
a push are, and walks a newcomer through them by doing.  Kiln keeps the same
kind of history for a physical design, and the two surfaces every agent
reads first -- the root ``SKILL.md`` and ``get_started()`` -- must teach it:
the words, the five moves an agent makes without being asked, and the real
tool behind each move.  Before this test, neither surface mentioned a
version, a branch, a release or a push at all.

Four things are pinned:

1. ``SKILL.md`` has a ``Version control`` section that teaches the five
   moves and names a tool for each.  Every tool it names exists in the live
   registry (public Kiln plus the served stubs a plain install sees), so the
   section can never teach an invented name.
2. ``get_started()`` and the skill manifest surface the same five moves and
   point at the same tools.
3. The section uses the approved vocabulary for the things a person makes
   and keeps (``docs/vocabulary.md`` in kiln-pro).  The words a person
   brings from a code host -- commit, repository -- appear only on the
   "you know it as" side of the mapping, never as Kiln's own noun.  Read
   from the kiln-pro checkout at test time; skipped, and said so, when that
   checkout is not present (public CI).
4. What the section says needs no account, and what it says needs one,
   agrees with where each tool runs on a plain install: a tool missing from
   ``pro_tool_manifest.json`` runs on the person's computer, and a tool in
   it is served, which needs a sign-in.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "SKILL.md"
SERVED_MANIFEST = REPO_ROOT / "kiln" / "src" / "kiln" / "pro_tool_manifest.json"

#: The pull-request doors a person meets on their own designs.
PULL_REQUEST_TOOLS = (
    "propose_design_change",
    "review_design_pull_request",
    "list_design_pull_requests",
    "get_design_pull_request",
)

# The five moves, and the tool each one is taught with.  The section may
# name more (a merge, an undo); these are the floor.
FIVE_MOVES: dict[str, str] = {
    "save a version": "save_design_version",
    "branch before a risky change": "create_design_branch",
    "compare before replacing": "compare_mesh_versions",
    "push to the account": "cloud_push_branch",
    "go back to a version": "rollback_design_version",
}

# Words a person brings from a code host.  Allowed only on the "you know it
# as" side of the mapping table; Kiln's own noun is never one of these.
CODE_HOST_WORDS = ("commit", "repository", "repo")


def _section(text: str, heading: str) -> str:
    """Return the body of the ``## <heading>`` section, up to the next ``## ``."""
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def _backticked_tools(body: str) -> set[str]:
    """Every ``snake_case`` name in backticks that looks like a tool name."""
    return {
        name
        for name in re.findall(r"`([a-z][a-z0-9_]+)`", body)
        if "_" in name
    }


@pytest.fixture(scope="module")
def live_tool_names() -> set[str]:
    import kiln.server as srv

    return {t.name for t in srv.mcp._tool_manager.list_tools()}


@pytest.fixture(scope="module")
def version_control_section() -> str:
    body = _section(SKILL_MD.read_text(encoding="utf-8"), "Version control")
    assert body.strip(), "SKILL.md has no '## Version control' section"
    return body


class TestSkillMdTeachesVersionControl:
    def test_names_a_real_tool_for_each_of_the_five_moves(self, version_control_section, live_tool_names):
        taught = _backticked_tools(version_control_section)
        for move, tool in FIVE_MOVES.items():
            assert tool in taught, f"the section does not teach `{tool}` for '{move}'"
        invented = sorted(taught - live_tool_names)
        assert not invented, f"SKILL.md names tools the live registry does not have: {invented}"

    def test_says_what_free_keeps_and_what_pro_adds_once_with_the_link(self, version_control_section):
        assert "https://kiln3d.com/pricing" in version_control_section
        assert "Pro" in version_control_section
        # Said once, as a fact: not a pitch repeated per move.
        assert version_control_section.count("kiln3d.com/pricing") == 1

    def test_maps_the_words_a_code_host_user_already_has(self, version_control_section):
        """A person who knows commit / branch / push must see each one mapped."""
        table_rows = [
            line for line in version_control_section.splitlines()
            if line.startswith("|") and "---" not in line
        ]
        assert len(table_rows) >= 4, "no mapping table from the code-host words to Kiln's"
        left = " ".join(row.split("|")[1].lower() for row in table_rows)
        for word in ("commit", "branch", "push"):
            assert word in left, f"the mapping never covers '{word}'"

    def test_says_each_move_to_a_person_in_plain_words(self, version_control_section):
        """One sentence per move with no history jargon in it."""
        plain = _section_lines_after(version_control_section, "plain")
        assert len(plain) >= 5, "fewer than five plain-words sentences"
        for line in plain:
            lowered = line.lower()
            for word in CODE_HOST_WORDS:
                assert word not in lowered, f"plain sentence uses '{word}': {line!r}"


def _section_lines_after(body: str, marker: str) -> list[str]:
    """Lines of the quoted sentences under the sub-heading containing *marker*."""
    lines = body.splitlines()
    out: list[str] = []
    seen = False
    for line in lines:
        if line.startswith("###") and marker in line.lower():
            seen = True
            continue
        if seen and line.startswith("###"):
            break
        if seen and line.lstrip().startswith("- "):
            out.append(line)
    return out


class TestFirstContactSurfacesPointAtTheSameTools:
    def test_get_started_surfaces_the_five_moves(self, live_tool_names):
        from kiln.server import get_started

        block = get_started().get("version_control")
        assert isinstance(block, dict), "get_started() has no version_control section"
        moves = block.get("five_moves")
        assert isinstance(moves, list) and len(moves) == 5
        blob = " ".join(moves)
        for tool in FIVE_MOVES.values():
            assert tool in blob, f"get_started's five moves never name `{tool}`"
        for name in _backticked_tools(blob) | set(re.findall(r"\b([a-z]+_[a-z0-9_]+)\(", blob)):
            assert name in live_tool_names, f"get_started names a tool that does not exist: {name}"

    def test_skill_manifest_has_the_history_workflow(self, live_tool_names):
        from kiln.skill_manifest import SkillManifest

        workflows = SkillManifest().workflows
        wf = workflows.get("version_control_history")
        assert wf, "skill manifest has no version_control_history workflow"
        blob = " ".join(wf)
        for tool in FIVE_MOVES.values():
            assert tool in blob, f"manifest workflow never names `{tool}`"
        for name in re.findall(r"\b([a-z]+_[a-z0-9_]+)\(", blob):
            assert name in live_tool_names, f"manifest names a tool that does not exist: {name}"


# ---------------------------------------------------------------------------
# Vocabulary: read from kiln-pro at test time, skipped honestly without it.
# ---------------------------------------------------------------------------

def _kiln_pro_text(rel: str) -> str | None:
    """*rel* from the kiln-pro checkout, or None when there is no checkout.

    ``KILN_PRO_REPO`` names a tree to read as it is.  A sibling checkout found
    on its own is read at ``origin/main`` (what ships), never off its working
    tree, which sits on whatever branch the last session left it on.
    """
    env = os.environ.get("KILN_PRO_REPO")
    if env:
        p = Path(env) / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None
    # A sibling checkout beside this repo, or beside the main checkout when
    # this is one of its worktrees.
    for parent in REPO_ROOT.parents:
        root = parent / "Kiln-pro"
        if not (root / ".git").exists():
            continue
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        out = subprocess.run(
            ["git", "-C", str(root), "show", f"origin/main:{rel}"],
            capture_output=True, text=True, env=clean,
        )
        return out.stdout if out.returncode == 0 else None
    return None


def _vocabulary_text() -> str | None:
    return _kiln_pro_text("docs/vocabulary.md")


def _singular(word: str) -> str:
    word = word.strip().lower()
    word = re.sub(r"(ch|sh|x)es$", r"\1", word)
    return re.sub(r"s$", "", word)


def _vocabulary_words(text: str) -> set[str]:
    """The bold terms the vocabulary defines, plus the history words it names."""
    words = {_singular(w) for w in re.findall(r"^\*\*([A-Za-z ]+)\*\*\s+[—-]", text, re.M)}
    m = re.search(r"A design has a history: ([a-z, ]+)\.", text)
    if m:
        words |= {_singular(w) for w in m.group(1).split(",")}
    return words


class TestSectionUsesTheApprovedVocabulary:
    @pytest.fixture(scope="class")
    def vocabulary(self) -> set[str]:
        text = _vocabulary_text()
        if text is None:
            pytest.skip(
                "kiln-pro checkout not found (set KILN_PRO_REPO); the vocabulary "
                "half of this test is unjudged here, not passed"
            )
        words = _vocabulary_words(text)
        assert {"design", "version", "branch", "release"} <= words, words
        return words

    def test_the_words_the_section_defines_are_vocabulary_words(self, version_control_section, vocabulary):
        """Every bold term in the section's word list is a vocabulary word."""
        defined = {w.lower() for w in re.findall(r"^- \*\*([A-Za-z ]+)\*\*", version_control_section, re.M)}
        assert defined, "the section defines no words"
        strays = sorted(w for w in defined if _singular(w) not in vocabulary and w != "push")
        assert not strays, f"the section defines words the vocabulary does not: {strays}"

    def test_kiln_never_calls_its_own_things_by_the_code_host_words(self, version_control_section, vocabulary):
        """'commit' and 'repository' may appear only on the mapping's left side."""
        for line in version_control_section.splitlines():
            lowered = line.lower()
            if line.startswith("|"):
                right = "|".join(line.split("|")[2:]).lower()
                for word in CODE_HOST_WORDS:
                    assert word not in right, f"Kiln's side of the mapping says '{word}': {line!r}"
                continue
            for word in CODE_HOST_WORDS:
                assert not re.search(rf"\b{word}\b", lowered), (
                    f"the section calls a Kiln thing a '{word}' outside the mapping: {line!r}"
                )


class TestTheFreeDesignCountIsTheOneKilnCharges:
    """SKILL.md states the free account's design count once, by hand (public
    has no source for it).  With the kiln-pro checkout present, that number
    is pinned to the constant the push really enforces; without it, unjudged
    and said so.  It stays the ONLY copy in public: get_started and the
    manifest point at the pricing page instead of restating it."""

    def test_skill_md_number_matches_the_enforced_limit(self, version_control_section):
        src = _kiln_pro_text("kiln_pro/cloud/persist_from_artifact.py")
        if src is None:
            pytest.skip("kiln-pro checkout not found (set KILN_PRO_REPO); the free design count is unjudged here")
        m = re.search(r"^FREE_CLOUD_DESIGN_LIMIT\s*=\s*(\d+)", src, re.M)
        assert m, "FREE_CLOUD_DESIGN_LIMIT not found in kiln-pro"
        stated = re.search(r"free Kiln account\s+keeps (\d+) designs", version_control_section)
        assert stated, "SKILL.md does not state how many designs a free account keeps"
        assert stated.group(1) == m.group(1), (
            f"SKILL.md says {stated.group(1)} designs; the push enforces {m.group(1)}"
        )

    def test_the_number_lives_only_in_skill_md(self):
        from kiln.server import get_started
        from kiln.skill_manifest import SkillManifest

        plans = get_started()["version_control"]["plans"]
        wf = " ".join(SkillManifest().workflows["version_control_history"])
        for blob in (plans, wf):
            assert not re.search(r"\b\d+ designs", blob), f"a second copy of the count: {blob!r}"
            assert "https://kiln3d.com/pricing" in blob


# ---------------------------------------------------------------------------
# What needs an account: the words the section uses, and the tools behind
# them, checked against where each tool runs on a plain install.
# ---------------------------------------------------------------------------

# Said to run on the person's computer with no account.
FREE_ON_THE_COMPUTER: dict[str, tuple[str, ...]] = {
    "saving versions": ("save_design_version",),
    "comparing": ("compare_mesh_versions", "diff_design_versions"),
    "rolling back": ("rollback_design_version",),
}

# Said to need a Kiln account.
NEEDS_AN_ACCOUNT: dict[str, tuple[str, ...]] = {
    "branching": ("create_design_branch",),
    "merging": ("merge_design_branches",),
    "undoing a branch change": ("undo_last_head_change",),
    "pushing": ("cloud_push_branch",),
}


def _subsection(body: str, heading: str) -> str:
    """The text under ``### <heading>`` in *body*, up to the next ``###``."""
    m = re.search(rf"^### {re.escape(heading)}\s*$(.*?)(?=^### |\Z)", body, re.M | re.S)
    return " ".join(m.group(1).split()) if m else ""


def _served_tools() -> dict[str, dict]:
    tools = json.loads(SERVED_MANIFEST.read_text(encoding="utf-8"))["tools"]
    return {t["name"]: t for t in tools}


class TestWhatNeedsAnAccountIsWhereEachToolRuns:
    @pytest.fixture(scope="class")
    def plans(self, version_control_section) -> str:
        text = _subsection(version_control_section, "Accounts and plans")
        assert text, "the section has no '### Accounts and plans' part"
        return text

    def test_the_moves_said_to_need_no_account_run_on_the_computer(self, plans):
        sentence = next((s for s in plans.split(". ") if "no account" in s), "")
        assert sentence, "the section never says what works with no account"
        served = _served_tools()
        for words in NEEDS_AN_ACCOUNT:
            assert words not in sentence.lower(), f"'{words}' is said to work with no account"
        for words, tools in FREE_ON_THE_COMPUTER.items():
            assert words in sentence.lower(), f"'{words}' is not among what works with no account"
            for tool in tools:
                assert tool not in served, (
                    f"`{tool}` is served from Kiln's servers on a plain install, "
                    f"so '{words}' needs an account"
                )

    def test_the_moves_said_to_need_an_account_are_served_and_free_to_reach(self, plans):
        sentence = next((s for s in plans.split(". ") if "need a Kiln account" in s), "")
        assert sentence, "the section never says what needs a Kiln account"
        served = _served_tools()
        for words, tools in NEEDS_AN_ACCOUNT.items():
            assert words in sentence.lower(), f"'{words}' is not among what needs an account"
            for tool in tools:
                entry = served.get(tool)
                assert entry is not None, (
                    f"`{tool}` runs on the person's computer with no account, "
                    f"yet the section says '{words}' needs one"
                )
                assert entry.get("access") in ("free", "free_banded"), (
                    f"`{tool}` is {entry.get('access')!r}: a free account cannot reach "
                    f"it, yet the section says a free account keeps the history"
                )

    def test_releases_are_said_to_come_with_pro_and_do(self, plans):
        assert "Kiln Pro" in plans and "releases" in plans
        assert _served_tools()["sign_design_release"].get("tier") == "pro"

    def test_pull_requests_are_said_on_the_band_the_server_charges(self, plans):
        """Free on the designs a free account keeps, every design on Kiln Pro:
        said in SKILL.md and get_started, and the same band the served doors carry."""
        from kiln.server import get_started

        served = _served_tools()
        for tool in PULL_REQUEST_TOOLS:
            band = served[tool].get("band") or {}
            assert served[tool].get("access") == "free_banded", tool
            assert "you keep" in band.get("free", ""), tool
            assert "every design" in band.get("pro", ""), tool
        said = get_started()["version_control"]["plans"]
        for text in (plans, " ".join(said.split())):
            sentence = next((s for s in text.split(". ") if "ull request" in s), "")
            assert sentence, "pull requests are never mentioned"
            assert "account" in sentence and "keeps" in sentence, sentence
            assert "Kiln Pro" in sentence and "every design" in sentence, sentence
