"""The first things an agent reads name the tool that carves a logo.

Putting a logo on a generated product took an agent sixteen calls: the
photo-relief generators (``generate_decorated_product``,
``generate_wall_plaque``) and the saved-recipe replay
(``apply_decoration``) all read as "the decoration tool" from their
names, and nothing in ``get_started`` or the skill manifest said which
one carves fresh content.  Both now carry a workflow that names
``decorate_surface`` as the carve tool, the ``decorate_next`` field a
generator's result hands it, and what each look-alike is for.
"""

from __future__ import annotations

from unittest.mock import patch

from kiln.server import get_started
from kiln.skill_manifest import SkillManifest

_LOOK_ALIKES = ("generate_decorated_product", "generate_wall_plaque", "apply_decoration")
_HEALTHY_SIBLINGS = {"count": 1, "pids": [111], "oldest_age": "05:44", "warning": None}


class TestGetStartedLogoDoor:
    def test_core_workflow_names_the_carve_tool_and_the_look_alikes(self):
        with patch("kiln.serve_siblings.check_serve_siblings", return_value=_HEALTHY_SIBLINGS):
            wf = get_started()["core_workflows"]["put_a_logo_or_text_on_a_product"]
        assert "decorate_surface" in wf
        assert "decorate_next" in wf
        for look_alike in _LOOK_ALIKES:
            assert look_alike in wf


class TestSkillManifestLogoDoor:
    def test_workflow_names_the_carve_tool_and_the_look_alikes(self):
        steps = SkillManifest().to_dict()["workflows"]["brand_a_product"]
        joined = " ".join(steps)
        assert "decorate_surface" in joined
        assert "decorate_next" in joined
        for look_alike in _LOOK_ALIKES:
            assert look_alike in joined
