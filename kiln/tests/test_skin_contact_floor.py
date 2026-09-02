"""Public skin-contact safety floor — the always-free, offline caution.

The kiln-pro sector carries the cited depth; this public floor is what reaches
EVERY install (offline, free, pip) so a worn-against-skin make never ships
without the honest caution.  These tests pin the floor's presence, its
free-only shape (nothing from the overlay reaches the public record even
when it is merged in on a server), and the wearable detector.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiln
import kiln.design_intelligence as di

_DATA = Path(kiln.__file__).parent / "data" / "design_knowledge"

# Fields that are FREE floor (public); anything else belongs to the overlay.
_FREE_KEYS = {
    "display_name", "base_state", "concern_level", "concern_basis", "safety_floor",
}
_OVERLAY_ONLY_KEYS = {
    "business_depth", "enterprise_depth", "exposure", "_provenance_internal",
    "remediation", "compliance", "standards_reference",
}


@pytest.fixture(autouse=True)
def _fresh_kb():
    di._reset_knowledge_base()
    yield
    di._reset_knowledge_base()


# ---------------------------------------------------------------------------
# The public file: present, well-formed, and FREE-ONLY (leak proof on disk)
# ---------------------------------------------------------------------------

def test_skin_contact_json_ships_and_is_free_only():
    path = _DATA / "skin_contact.json"
    assert path.is_file(), "public skin_contact safety floor must ship on every disk"
    raw = json.loads(path.read_text(encoding="utf-8"))
    materials = {k: v for k, v in raw.items() if not k.startswith("_")}
    assert len(materials) >= 12, "the floor should cover the common FDM families"
    for mid, rec in materials.items():
        # No overlay-only field is ever written into the PUBLIC file.
        leaked = _OVERLAY_ONLY_KEYS & set(rec.keys())
        assert not leaked, f"overlay-only field in public skin_contact.json[{mid}]: {leaked}"
        floor = rec.get("safety_floor") or {}
        assert floor.get("honesty_note"), f"{mid} missing honesty_note"
        assert floor.get("refer_to_medical"), f"{mid} missing refer_to_medical"


def test_public_file_never_affirms_skin_safe():
    raw = json.loads((_DATA / "skin_contact.json").read_text(encoding="utf-8"))
    # The prime directive is present and explicitly refuses an affirmative verdict.
    directive = (raw.get("_meta") or {}).get("prime_directive", "").lower()
    assert "no affirmative" in directive and "caution" in directive
    # The honesty spine ("no 3D-printed part is skin-safe ...") is present, and
    # every material carries a real honesty note — the negation, never a green light.
    blob = json.dumps(raw).lower()
    assert "no 3d-printed" in blob and "skin-safe" in blob
    for mid, rec in raw.items():
        if mid.startswith("_"):
            continue
        assert (rec.get("safety_floor") or {}).get("honesty_note"), f"{mid} missing honesty_note"


# ---------------------------------------------------------------------------
# The accessor: free floor only, even after the server-side overlay merge
# ---------------------------------------------------------------------------

def test_get_skin_contact_floor_is_free_only_after_merge():
    # _get_kb()._load() runs the pro-overlay merge if kiln-pro is importable;
    # the raw record may then carry overlay fields, but the accessor must
    # expose free fields ONLY.
    floor = di.get_skin_contact_floor("pla")
    assert floor is not None
    d = floor.to_dict()
    assert _OVERLAY_ONLY_KEYS.isdisjoint(d.keys()), f"overlay field leaked through accessor: {d.keys()}"
    assert floor.has_engineering_data() is False
    assert floor.honesty_note and floor.refer_to_medical


def test_get_skin_contact_floor_per_material_honesty():
    # ABS carries a named chemistry-of-concern hazard at the floor.
    abs_floor = di.get_skin_contact_floor("abs")
    assert abs_floor is not None
    assert abs_floor.concern_level  # a granularity signal, always free
    assert any("acrylonitrile" in h.lower() or "styrene" in h.lower()
               for h in abs_floor.named_hazards)


def test_no_material_named_is_none():
    """Nothing to warn about is still nothing to say.

    An empty material is the caller having no material at all — distinct from
    a material we do not recognise, which now gets the generic floor below.
    """
    assert di.get_skin_contact_floor("") is None
    assert di.get_skin_contact_floor("   ") is None


def test_unknown_material_gets_the_floor_never_silence():
    """An unrecognised material must NOT return nothing.

    This reverses the previous contract deliberately.  Returning ``None``
    made the caller skip the whole skin-contact block, and a worn part with
    no warning reads as "no concern" when the truth is "untested" — the one
    direction a safety floor must never fail in.  Thirteen materials shipped
    in v1.2.0 were silent this way.
    """
    floor = di.get_skin_contact_floor("unobtanium")
    assert floor is not None
    assert floor.is_uncharacterized
    assert floor.honesty_note, "the generic floor must still say something real"
    assert floor.refer_to_medical
    # Untested is stated as untested — never as a clearance.
    assert "clearance" in floor.honesty_note.lower()
    assert floor.concern_level == "untested"


def test_non_material_key_never_yields_a_pseudo_record():
    """A merged non-material key (e.g. a standards cross-reference) is not a
    floor: it may only ever fall through to the generic floor, never be
    dressed up as a record about itself."""
    floor = di.get_skin_contact_floor("standards_reference")
    assert floor is not None and floor.is_uncharacterized


def test_filled_grades_never_answer_softer_than_the_filler_family():
    """A filled grade must never carry LESS concern than its filler's record.

    cf_pla resolving to plain PLA understated a fibre hazard on a material
    shipped in v1.2.0.  The guard is deliberately written against the
    ANSWER rather than the mechanism: these grades were served by
    inheritance from the fibre record until 2026-07-25 and by curated
    records of their own afterwards, and the property that matters —
    never softer than the filler — has to hold either way.
    """
    rank = {"untested": 0, "elevated": 1, "high": 2, "not_applicable": 3}
    fibre = di.get_skin_contact_floor("cf_nylon")
    assert fibre is not None

    for mid in ("cf_pla", "cf_petg", "petg_cf", "pet_cf"):
        floor = di.get_skin_contact_floor(mid)
        assert floor is not None, mid
        assert not floor.is_uncharacterized, mid
        assert rank[floor.concern_level] >= rank[fibre.concern_level], (
            f"{mid} answers {floor.concern_level!r} against the fibre record's "
            f"{fibre.concern_level!r} — a fibre-filled grade must not be the "
            "softer answer"
        )

    wood_fill = di.get_skin_contact_floor("wood_fill")
    wood = di.get_skin_contact_floor("wood_pla")
    assert wood is not None and wood_fill is not None
    assert rank[wood.concern_level] >= rank[wood_fill.concern_level]


def test_exact_records_are_never_overridden_by_inheritance():
    for mid in ("pla", "abs", "tpu", "peek", "pva"):
        floor = di.get_skin_contact_floor(mid)
        assert floor is not None and floor.inherited_from is None, mid
        assert not floor.is_uncharacterized, mid


def test_floor_works_without_kiln_pro(monkeypatch):
    """Free-tier regression: the floor is pure public — it must resolve with no
    kiln-pro on the path (the whole point: the caution reaches offline users)."""
    import builtins
    real_import = builtins.__import__

    def _no_kiln_pro(name, *args, **kwargs):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError("simulated: kiln-pro not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_kiln_pro)
    di._reset_knowledge_base()
    floor = di.get_skin_contact_floor("petg")
    assert floor is not None
    assert floor.honesty_note and floor.refer_to_medical
    assert floor.has_engineering_data() is False


# ---------------------------------------------------------------------------
# Wearable-intent detection (offline, profile-driven, negative-guarded)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("a ring worn daily", True),
    ("watch band in tpu", True),
    ("a bracelet", True),
    ("sunglasses frame", True),
    ("a napkin ring holder", False),
    ("a band saw jig", False),
    ("a watch stand", False),
    ("an earbud case", False),
    ("a coaster", False),
    ("", False),
])
def test_use_case_implies_skin_contact(text, expected):
    assert di.use_case_implies_skin_contact(text) is expected


def test_against_skin_profile_present_with_guards():
    prof = di._get_kb().requirements.get("against_skin")
    assert prof is not None, "against_skin functional-requirement profile must ship"
    assert prof.get("triggers"), "the profile drives offline detection"
    assert prof.get("trigger_exclusions"), "the negative-context guard keeps it credible"
    assert prof.get("caution"), "the free caution must ride on the profile"


def test_match_requirements_surfaces_against_skin_with_live_caution():
    sets = di.match_requirements("a ring worn daily")
    skin = next((s for s in sets if s.requirement_id == "against_skin"), None)
    assert skin is not None, "the constraint matcher must catch a worn item"
    # The caution is LIVE (reaches the constraint set), not dead data.
    assert skin.caution and "skin-safe" in skin.caution.lower()


def test_match_requirements_suppresses_homographs():
    # A napkin ring is not worn — the exclusion guard keeps the generic
    # constraint matcher from crying wolf here too (not just the make path).
    sets = di.match_requirements("a napkin ring holder")
    assert "against_skin" not in {s.requirement_id for s in sets}
    # And an unrelated make never trips it.
    assert "against_skin" not in {
        s.requirement_id for s in di.match_requirements("a plain coaster")
    }
