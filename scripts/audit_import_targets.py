#!/usr/bin/env python3
"""Every import of Kiln's own code names something that exists.

The 2026-03-20 change that moved the ordering code out of public Kiln left
seven of the twelve ``kiln order`` terminal commands, and ``kiln
fulfillment-materials``, importing ``kiln.fulfillment``, a package no tree
has held since.  Nothing noticed for six months, and both
reasons are general:

* The imports sit inside the command bodies.  Importing the CLI and
  collecting the tests both succeed; the break waits for someone to type the
  command.
* The commands' tests are ``skipif(not _has_fulfillment, ...)``, where the
  flag is "``import kiln.fulfillment`` succeeds".  A test that skips when a
  module is missing, over a module that is missing everywhere, skips forever,
  and a skip is a line nobody reads.

So this reads every ``import`` and ``from ... import`` of a first-party
module (one whose top package is a tree handed to :func:`scan`), wherever it
sits: at module level, in a function, behind ``try/except ImportError``
(which makes a missing module silent rather than safe).  It fails:

* ``needs_private``   a public source file imports, outside ``try/except
  ImportError``, a module or name public Kiln does not have: one only the
  private package provides, or one that exists nowhere.  Every plain install
  crashes when that line runs: the ``kiln order`` commands' shape.
* ``missing_module``  a guarded import of a module that exists in no tree, a
  fallback that is always taken (judged only where both trees are present).
* ``skips_forever``   a test skips on a first-party module that exists in no
  tree (``pytest.importorskip``, or a guarded import), so it never runs.
* ``in_process``      a public source file reaches the private package
  in-process: imports it, imports a ``kiln.*`` path only it provides, or reads
  a ``pro_features.<attr>`` off its bridge, guarded or not.

``in_process`` is the class the other three cannot see.  A guarded import
does not crash, so it passed every check here for as long as this file has
existed; but a customer's ``pip install kiln3d`` has no private package, so
the guard's fallback is what every customer gets, paying or not.  Measured
2026-10-17: about 150 of 206 such sites answered a paying customer with the
free result, silently.  Public Kiln reaches paid depth over the network
(``kiln.served_answer``), never by importing it.  Two places may still cross,
and both are named: a served bridge on ``served_answer.HOSTED_DOORS``, whose
miss falls back to Kiln's servers (:func:`served_bridges`), and the short
:data:`INFRASTRUCTURE` list.  Every other site that existed on 2026-10-17 is
in :data:`KNOWN_IN_PROCESS`, keyed by file, function and target (never a line
number, which churns); serving one removes its entry, and a stale entry fails.

A module whose top package is not a tree handed in is UNJUDGED and counted,
never passed: public Kiln's own run cannot see kiln-pro, so its imports of
kiln-pro are judged where both trees are present (kiln-pro's run of this
same script).

Findings that existed the day this landed are in :data:`KNOWN`, which may
only shrink: a known finding that is fixed is reported STALE until its entry
is deleted, and a new finding fails.

Usage::

    python3 scripts/audit_import_targets.py                  # public Kiln
    python3 scripts/audit_import_targets.py --tree kiln_pro=/path/kiln_pro
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PUBLIC_TREE = REPO / "kiln" / "src" / "kiln"
PUBLIC_TESTS = REPO / "kiln" / "tests"

#: Findings present when this check landed (2026-10-05), as
#: ``(path relative to its tree's parent, module, name)``; ``name`` is ``""``
#: for a missing module.  Shrink only.
KNOWN: frozenset[tuple[str, str, str]] = frozenset({
    # Routing a print across printers (the fleet router moved to the private
    # package); reached from the print command's routing option.
    ("kiln/src/kiln/cli/main.py", "kiln.job_router", ""),
})

#: ``in_process`` sites present when that finding landed (2026-10-17), as
#: ``(path relative to its tree's parent, function, target)``.  ``function``
#: is the dotted name of the enclosing def (``"<module>"`` at module level);
#: ``target`` is the private module, the ``kiln.*`` path only it provides, or
#: ``<bridge>.pro_features.<attr>``.  Shrink only: serve the capability, then
#: delete its line.
KNOWN_IN_PROCESS: frozenset[tuple[str, str, str]] = frozenset({
    ('kiln/src/kiln/assembly.py', '_adhesive_hint_for_joint', 'kiln_pro.adhesives'),
    ('kiln/src/kiln/assembly.py', '_calibration_view_for_clearance', 'kiln_pro.engineering.calibration_coach'),
    ('kiln/src/kiln/assembly.py', '_running_clearance_view', 'kiln_pro.engineering.running_clearance'),
    ('kiln/src/kiln/assembly.py', '_screw_hole_detail_for_joint', 'kiln_pro.fasteners_intelligence'),
    ('kiln/src/kiln/cli/main.py', '<module>', 'kiln.fulfillment.base'),
    ('kiln/src/kiln/cli/main.py', '<module>', 'kiln_pro.cli.pro_commands'),
    ('kiln/src/kiln/cli/main.py', '_order_fee_tools', 'kiln.billing'),
    ('kiln/src/kiln/cli/main.py', '_order_fee_tools', 'kiln_pro.payments.base'),
    ('kiln/src/kiln/cli/main.py', '_order_fee_tools', 'kiln_pro.payments.manager'),
    ('kiln/src/kiln/cli/main.py', '_route_printer_for_job', 'kiln.job_router'),
    ('kiln/src/kiln/cli/main.py', 'order_quote', 'kiln.billing'),
    ('kiln/src/kiln/community_outbox.py', 'ensure_senders', 'kiln_pro.bridge'),
    ('kiln/src/kiln/community_outbox.py', 'ensure_senders', 'kiln_pro.bridge.pro_features.register_community_senders'),
    ('kiln/src/kiln/decoration/managed_assets.py', '_preset_lineage', 'kiln_pro.decoration.preset_lineage'),
    ('kiln/src/kiln/design_intelligence.py', '_engineering_overlay_loaded', 'kiln_pro.hosted_intelligence'),
    ('kiln/src/kiln/design_intelligence.py', '_load_pro_overlay_if_available', 'kiln_pro.data_overlays'),
    ('kiln/src/kiln/design_intelligence.py', '_upgrade_product_name', 'kiln_pro.hosted_intelligence'),
    ('kiln/src/kiln/design_intelligence.py', 'get_material_profile', 'kiln_pro.bridge'),
    ('kiln/src/kiln/design_intelligence.py', 'get_material_profile', 'kiln_pro.bridge.pro_features.get_ingested_material_profile'),
    ('kiln/src/kiln/design_intelligence.py', 'load_pro_overlay_or_empty', 'kiln_pro.data_overlays'),
    ('kiln/src/kiln/design_reasoning.py', '_edge_policy', 'kiln_pro.bridge'),
    ('kiln/src/kiln/design_reasoning.py', '_edge_policy', 'kiln_pro.bridge.pro_features.edge_intelligence'),
    ('kiln/src/kiln/edit_lineage.py', 'Edit.__init__', 'kiln_pro.design_versions.edit_lineage'),
    ('kiln/src/kiln/edit_lineage.py', 'install', 'kiln_pro.design_versions.edit_lineage'),
    ('kiln/src/kiln/emboss_generator.py', 'generate_qr_openscad_module', 'kiln_pro.decoration.qr_openscad'),
    ('kiln/src/kiln/emergency.py', 'EmergencyCoordinator._build_ams_stop_command', 'kiln_pro.device_intelligence.bambu_ams'),
    ('kiln/src/kiln/front_door.py', '_open_session', 'kiln_pro.design_brief.explicit_attach'),
    ('kiln/src/kiln/front_door.py', '_speak', 'kiln_pro.design_brief.explicit_attach'),
    ('kiln/src/kiln/fulfillment_service.py', 'order_service', 'kiln.fulfillment'),
    ('kiln/src/kiln/generation/validation.py', '_conversion_sentence', 'kiln_pro.mesh_conversion_record'),
    ('kiln/src/kiln/heartbeat.py', '_is_pro_installed', 'kiln_pro'),
    ('kiln/src/kiln/original_design.py', 'audit_original_design', 'kiln_pro.bridge'),
    ('kiln/src/kiln/original_design.py', 'audit_original_design', 'kiln_pro.bridge.pro_features.design_brief'),
    ('kiln/src/kiln/original_design.py', 'audit_original_design', 'kiln_pro.bridge.pro_features.intent_verification'),
    ('kiln/src/kiln/original_design.py', 'audit_original_design', 'kiln_pro.bridge.pro_features.printability_overlay'),
    ('kiln/src/kiln/plate_state.py', 'plan_motion_around_plate', 'kiln_pro.bridge'),
    ('kiln/src/kiln/plugins/assembly_tools.py', '_AssemblyToolsPlugin.register.compose_assembly_parts', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/color_tools.py', '_ColorToolsPlugin.register.auto_color_by_height', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/color_tools.py', '_ColorToolsPlugin.register.auto_color_by_region', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/consumer_tools.py', '_ConsumerToolsPlugin.register.tax_estimate', 'kiln_pro.tax'),
    ('kiln/src/kiln/plugins/consumer_tools.py', '_ConsumerToolsPlugin.register.tax_jurisdiction_lookup', 'kiln_pro.tax'),
    ('kiln/src/kiln/plugins/consumer_tools.py', '_ConsumerToolsPlugin.register.tax_jurisdictions', 'kiln_pro.tax'),
    ('kiln/src/kiln/plugins/decoration_library_tools.py', '_DecorationLibraryPlugin.register.apply_decoration', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/decoration_library_tools.py', '_DecorationLibraryPlugin.register.list_decorations', 'kiln_pro.design_versions.decoration_presets'),
    ('kiln/src/kiln/plugins/decoration_library_tools.py', '_DecorationLibraryPlugin.register.save_decoration', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/decoration_library_tools.py', '_not_found_error', 'kiln_pro.decoration.decoration_lookup'),
    ('kiln/src/kiln/plugins/design_reasoning_tools.py', '_DesignReasoningToolsPlugin.register.apply_design_reinforcements', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/design_reasoning_tools.py', '_DesignReasoningToolsPlugin.register.check_print_readiness', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/design_reasoning_tools.py', '_DesignReasoningToolsPlugin.register.iterate_design', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/design_reasoning_tools.py', '_DesignReasoningToolsPlugin.register.optimize_print_orientation', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/design_reasoning_tools.py', '_DesignReasoningToolsPlugin.register.optimize_template_params', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/design_rebuild_tools.py', '_DesignRebuildPlugin.register.rebuild_design', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/design_tools.py', '_DesignToolsPlugin.register.compile_scad', 'kiln_pro.bridge'),
    ('kiln/src/kiln/plugins/design_tools.py', '_DesignToolsPlugin.register.compile_scad', 'kiln_pro.bridge.pro_features.fastener_hole_advice'),
    ('kiln/src/kiln/plugins/design_tools.py', '_DesignToolsPlugin.register.compile_scad', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/design_tools.py', '_DesignToolsPlugin.register.recommend_design_material', 'kiln_pro.skin_contact'),
    ('kiln/src/kiln/plugins/design_tools.py', '_DesignToolsPlugin.register.tweak_and_compile_scad', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.configure_sso', 'kiln.sso'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.encryption_status', 'kiln.gcode_encryption'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.manage_team_member', 'kiln.teams'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.printer_usage_summary', 'kiln_pro.billing.printer_billing'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.rotate_encryption_key', 'kiln.gcode_encryption'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.sso_exchange_code', 'kiln.sso'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.sso_login_url', 'kiln.sso'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.sso_status', 'kiln.sso'),
    ('kiln/src/kiln/plugins/enterprise_tools.py', '_EnterpriseToolsPlugin.register.uptime_report', 'kiln.uptime'),
    ('kiln/src/kiln/plugins/fleet_tools.py', '_FleetToolsPlugin.register.fleet_job_status', 'kiln.fleet_orchestrator'),
    ('kiln/src/kiln/plugins/fleet_tools.py', '_FleetToolsPlugin.register.fleet_submit_job', 'kiln.fleet_orchestrator'),
    ('kiln/src/kiln/plugins/fleet_tools.py', '_FleetToolsPlugin.register.fleet_utilization', 'kiln.fleet_orchestrator'),
    ('kiln/src/kiln/plugins/fleet_tools.py', '_FleetToolsPlugin.register.route_print_job', 'kiln.job_router'),
    ('kiln/src/kiln/plugins/fleet_tools.py', '_FleetToolsPlugin.register.route_print_job', 'kiln.placement_fleet'),
    ('kiln/src/kiln/plugins/fleet_tools.py', '_survey_plates', 'kiln.placement_fleet'),
    ('kiln/src/kiln/plugins/fulfillment_tools.py', '_FulfillmentToolsPlugin.register.fulfillment_order', 'kiln.fulfillment.intelligence'),
    ('kiln/src/kiln/plugins/fulfillment_tools.py', '_FulfillmentToolsPlugin.register.fulfillment_order', 'kiln_pro.payments.base'),
    ('kiln/src/kiln/plugins/fulfillment_tools.py', '_FulfillmentToolsPlugin.register.fulfillment_quote', 'kiln_pro.payments.base'),
    ('kiln/src/kiln/plugins/generation_ai_tools.py', '_GenerationAIToolsPlugin.register.download_generated_model', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/generation_ai_tools.py', '_GenerationAIToolsPlugin.register.generate_and_print', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/generation_ai_tools.py', '_GenerationAIToolsPlugin.register.generate_template_variations', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/generation_ai_tools.py', '_GenerationAIToolsPlugin.register.smart_generate_from_template', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/generation_tools.py', '_GenerationToolsPlugin.register.generate_model_with_provider', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/learning_tools.py', '_plate_rooms', 'kiln.placement_fleet'),
    ('kiln/src/kiln/plugins/learning_tools.py', '_recommend_calibration_overlay', 'kiln_pro.engineering.calibration_coach'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register._repair_mesh_impl', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.boolean_mesh_op', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.center_model_on_bed', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.compose_models', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.compose_part_from_primitives', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.extract_model_from_3mf', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.hollow_mesh_model', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.merge_mesh_files', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.mirror_mesh_model', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.remove_mesh_floating_regions', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.rescale_model', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.scale_mesh_to_fit', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.simplify_mesh_model', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.splice_mesh_at_z', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.split_mesh_by_component', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_MeshToolsPlugin.register.thicken_mesh_walls', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/mesh_tools.py', '_finish_edges_tool', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/printability_tools.py', '_PrintabilityToolsPlugin.register.analyze_printability', 'kiln_pro.bridge'),
    ('kiln/src/kiln/plugins/printability_tools.py', '_PrintabilityToolsPlugin.register.analyze_printability', 'kiln_pro.bridge.pro_features.fastener_hole_advice'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.analyze_print_failure_smart', 'kiln_pro.recovery.nozzle_wear_hypothesis'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.complete_print_recovery', 'kiln_pro.bridge'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.complete_print_recovery', 'kiln_pro.bridge.pro_features.recovery'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.complete_print_recovery', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.complete_print_recovery', 'kiln_pro.recovery.failure_rerouter'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.complete_print_recovery', 'kiln_pro.recovery.outcome_learning'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.plan_failure_recovery', 'kiln_pro.recovery.nozzle_wear_hypothesis'),
    ('kiln/src/kiln/plugins/recovery_tools.py', '_RecoveryToolsPlugin.register.start_print_recovery', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/slicer_tools.py', '_maybe_auto_assembly_manual', 'kiln_pro.manuals.auto_trigger'),
    ('kiln/src/kiln/plugins/slicer_tools.py', '_maybe_overlay_calibration', 'kiln_pro.bridge'),
    ('kiln/src/kiln/plugins/slicer_tools.py', '_maybe_overlay_calibration', 'kiln_pro.bridge.pro_features.record_slice_for_input'),
    ('kiln/src/kiln/plugins/slicer_tools.py', '_maybe_overlay_calibration', 'kiln_pro.engineering.calibration_coach'),
    ('kiln/src/kiln/plugins/step_tools.py', '_StepToolsPlugin.register.import_step_file', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/step_tools.py', '_StepToolsPlugin.register.import_step_file', 'kiln_pro.step_facts'),
    ('kiln/src/kiln/plugins/tier_diagnostic_tools.py', '_walk_resolution_chain', 'kiln_pro.enterprise.licensing'),
    ('kiln/src/kiln/plugins/validation_pipeline_tools.py', '_ValidationPipelinePlugin.register.prepare_ai_model_for_print', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/version_tools.py', '_VersionToolsPlugin.register.rollback_design_version', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/plugins/version_tools.py', '_VersionToolsPlugin.register.save_design_version', 'kiln_pro.bridge'),
    ('kiln/src/kiln/plugins/version_tools.py', '_VersionToolsPlugin.register.save_design_version', 'kiln_pro.bridge.pro_features.enrich_version'),
    ('kiln/src/kiln/plugins/version_tools.py', '_VersionToolsPlugin.register.save_design_version', 'kiln_pro.bridge.pro_features.register_saved_version'),
    ('kiln/src/kiln/plugins/version_tools.py', '_VersionToolsPlugin.register.save_design_version', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/print_health_monitor.py', 'PrintHealthMonitor._maybe_record_predictive_signals', 'kiln_pro.recovery.predictive'),
    ('kiln/src/kiln/printability.py', '_analysis_runner', 'kiln_pro.bridge'),
    ('kiln/src/kiln/printability.py', '_analysis_runner', 'kiln_pro.bridge.pro_features.run_mesh_analysis'),
    ('kiln/src/kiln/printability.py', '_analyze_printability_here', 'kiln_pro.bridge'),
    ('kiln/src/kiln/printability.py', '_analyze_printability_here', 'kiln_pro.bridge.pro_features.printability_overlay'),
    ('kiln/src/kiln/printability.py', '_material_physics_from_overlay', 'kiln_pro.bridge'),
    ('kiln/src/kiln/printability.py', '_material_physics_from_overlay', 'kiln_pro.bridge.pro_features.printability_overlay'),
    ('kiln/src/kiln/printability.py', '_material_wall_floor', 'kiln_pro.bridge'),
    ('kiln/src/kiln/printability.py', '_material_wall_floor', 'kiln_pro.bridge.pro_features.printability_overlay'),
    ('kiln/src/kiln/printability.py', '_tell_whoever_runs_this_machine', 'kiln_pro.bridge'),
    ('kiln/src/kiln/printability.py', '_tell_whoever_runs_this_machine', 'kiln_pro.bridge.pro_features.ops_alert'),
    ('kiln/src/kiln/printer_intelligence.py', '_overlay_for_caller', 'kiln_pro.data_overlays'),
    ('kiln/src/kiln/printers/print_gate.py', '_maybe_enrich_block', 'kiln_pro.print_fix.engine'),
    ('kiln/src/kiln/reference_compare.py', 'compare_with_goal', 'kiln_pro.design_brief.explicit_attach'),
    ('kiln/src/kiln/reference_compare.py', 'compare_with_picture', 'kiln_pro.design_brief.explicit_attach'),
    ('kiln/src/kiln/reference_compare.py', 'comparison_available', 'kiln_pro.design_brief.explicit_attach'),
    ('kiln/src/kiln/server.py', '<module>', 'kiln.billing'),
    ('kiln/src/kiln/server.py', '<module>', 'kiln.billing_alerts'),
    ('kiln/src/kiln/server.py', '<module>', 'kiln.fulfillment'),
    ('kiln/src/kiln/server.py', '<module>', 'kiln_pro'),
    ('kiln/src/kiln/server.py', '<module>', 'kiln_pro.payments.base'),
    ('kiln/src/kiln/server.py', '<module>', 'kiln_pro.payments.manager'),
    ('kiln/src/kiln/server.py', '_auto_derive_brief_id', 'kiln_pro.intent_verification'),
    ('kiln/src/kiln/server.py', '_ensure_internal_tool_plugins_registered', 'kiln_pro'),
    ('kiln/src/kiln/server.py', '_finish_decoration_result', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/server.py', '_get_fulfillment_monitor', 'kiln.fulfillment_monitor'),
    ('kiln/src/kiln/server.py', '_get_payment_mgr', 'kiln_pro.payments.circle_provider'),
    ('kiln/src/kiln/server.py', '_get_payment_mgr', 'kiln_pro.payments.stripe_provider'),
    ('kiln/src/kiln/server.py', '_pro_bridge', 'kiln_pro.bridge'),
    ('kiln/src/kiln/server.py', '_record_local_tool_call', 'kiln_pro.bridge'),
    ('kiln/src/kiln/server.py', '_record_local_tool_call', 'kiln_pro.bridge.pro_features.record_local_tool_call'),
    ('kiln/src/kiln/server.py', '_resolve_brief_context', 'kiln_pro.design_brief.context'),
    ('kiln/src/kiln/server.py', 'client_cost_report', 'kiln.project_costs'),
    ('kiln/src/kiln/server.py', 'create_project', 'kiln.project_costs'),
    ('kiln/src/kiln/server.py', 'decorate_surface', 'kiln_pro.bridge'),
    ('kiln/src/kiln/server.py', 'decorate_surface', 'kiln_pro.bridge.pro_features.wall_text'),
    ('kiln/src/kiln/server.py', 'generate_from_template', 'kiln_pro.bridge'),
    ('kiln/src/kiln/server.py', 'generate_from_template', 'kiln_pro.bridge.pro_features.fastener_hole_advice'),
    ('kiln/src/kiln/server.py', 'generate_from_template', 'kiln_pro.bridge.pro_features.intent_verification'),
    ('kiln/src/kiln/server.py', 'generate_from_template', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/server.py', 'log_project_cost', 'kiln.project_costs'),
    ('kiln/src/kiln/server.py', 'merge_stl', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/server.py', 'preflight_check', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/server.py', 'project_cost_summary', 'kiln.project_costs'),
    ('kiln/src/kiln/server.py', 'rotate_model', 'kiln_pro.plugins.git_render_tools'),
    ('kiln/src/kiln/server.py', 'smart_reprint', 'kiln_pro.intent_verification'),
    ('kiln/src/kiln/slicer_profiles.py', 'start_gcode_override_from_printer', 'kiln_pro.bridge'),
    ('kiln/src/kiln/slicer_profiles.py', 'start_gcode_override_from_printer', 'kiln_pro.bridge.pro_features.start_gcode_override'),
    ('kiln/src/kiln/start_narrative.py', '_pro_block', 'kiln_pro.bridge'),
    ('kiln/src/kiln/start_narrative.py', '_pro_block', 'kiln_pro.bridge.pro_features.device_intelligence'),
    ('kiln/src/kiln/start_narrative.py', 'start_stage_line', 'kiln_pro.bridge'),
    ('kiln/src/kiln/start_narrative.py', 'start_stage_line', 'kiln_pro.bridge.pro_features.device_intelligence'),
    ('kiln/src/kiln/store_scope.py', 'current_tier', 'kiln_pro'),
    ('kiln/src/kiln/store_scope.py', 'read_cloud_half', 'kiln_pro.bridge'),
})

#: Public modules whose JOB is to hand a name over to the private package
#: where it is installed, so their crossing is the mechanism rather than a
#: capability.  Each says why; keep this list short.
INFRASTRUCTURE: dict[str, str] = {
    "kiln.licensing": (
        "the plan every gate imports: on Kiln's server it is the private "
        "package's per-request tier, on a plain install the signed-in account's "
        "plan (kiln.account_plan); both answer per caller"
    ),
}

#: A served bridge: a module named ``_pro_<thing>_bridge`` that is on
#: ``served_answer.HOSTED_DOORS`` as ``served_answer``.  Other modules on that
#: roster (the server, a plugin) carry a served door AND other code, so their
#: crossings are judged like anyone's.
_BRIDGE_NAME = re.compile(r"(?:^|\.)_pro_\w+_bridge$")

#: Bridge attributes that ask a presence or process question, not for a
#: capability; the tier-seam check owns the second.
_BRIDGE_PROBES = frozenset({"is_available", "has_valid_license"})

#: The two first-party top packages: public Kiln, and the private package.
PUBLIC_TOP = "kiln"
PRIVATE_TOP = "kiln_pro"

_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    line: int
    module: str
    name: str
    guarded: bool
    function: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        if self.kind == "in_process":
            return (self.path, self.function, self.module)
        return (self.path, self.module, self.name)

    def __str__(self) -> str:
        if self.kind == "in_process":
            return (f"{self.kind:14s} {self.path}:{self.line}  {self.function} -> {self.module}"
                    "  (a plain install never runs this; serve it through kiln.served_answer)")
        what = f"{self.module}.{self.name}" if self.name else self.module
        how = " (inside try/except, so it fails silently)" if self.guarded else ""
        return f"{self.kind:14s} {self.path}:{self.line}  {what}{how}"


class _Index:
    """What a set of trees holds: every module, and each module's top-level names."""

    def __init__(self, trees: dict[str, Path], provided: dict[str, str] | None = None) -> None:
        self.trees = {top: Path(root) for top, root in trees.items()}
        #: Every first-party top package is in hand, so a module found in no
        #: tree exists nowhere (kiln-pro's run of this script).
        self.complete = {PUBLIC_TOP, PRIVATE_TOP} <= set(self.trees)
        #: ``kiln.*`` paths the private package registers at import time,
        #: mapped to the module that answers them (kiln_pro's compat shims).
        self.shims = dict(provided or {})
        self.files: dict[str, Path] = {}
        for top, root in self.trees.items():
            for path in root.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(root).with_suffix("")
                parts = [top, *rel.parts]
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                self.files[".".join(parts)] = path
            # A folder of modules with no __init__.py is still importable.
            for folder in [root, *[p for p in root.rglob("*") if p.is_dir()]]:
                if "__pycache__" in folder.parts:
                    continue
                dotted = ".".join([top, *folder.relative_to(root).parts])
                self.files.setdefault(dotted, folder)
        self._names: dict[str, set[str] | None] = {}
        public_root = self.trees.get(PUBLIC_TOP)
        #: Served bridges exempt from ``in_process`` (see :func:`served_bridges`).
        self.bridges = served_bridges(public_root) if public_root else frozenset()

    def judged(self, module: str) -> bool:
        return module.split(".")[0] in self.trees

    def provided(self, module: str) -> bool:
        """True when *module* exists only once the private package is
        imported: a compat shim, or anything under ``kiln_pro``."""
        for alias, real in self.shims.items():
            if module == alias or module.startswith(alias + "."):
                rest = module[len(alias):]
                return self.exists(real + rest) if self.judged(real) else True
        return module.split(".")[0] == PRIVATE_TOP and self.exists(module)

    def is_public(self, path: Path) -> bool:
        root = self.trees.get(PUBLIC_TOP)
        return root is not None and root in path.parents

    def exists(self, module: str) -> bool:
        return module in self.files

    def names(self, module: str, _depth: int = 0) -> set[str] | None:
        """Top-level names *module* defines; ``None`` when they cannot be
        known statically (a module ``__getattr__``, a folder, a parse error)."""
        if module in self._names:
            return self._names[module]
        path = self.files.get(module)
        found: set[str] | None
        if path is None or path.is_dir():
            found = None
        else:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                tree = None
            found = None if tree is None else self._top_level(tree, module, _depth)
        self._names[module] = found
        return found

    def _top_level(self, tree: ast.Module, module: str, depth: int) -> set[str] | None:
        names: set[str] = set()
        package = module if self.files[module].name == "__init__.py" else module.rpartition(".")[0]
        literals = _string_lists(tree)
        exported = _globals_loop_names(tree, literals)
        if exported is None:
            return None
        names |= exported

        def visit(body: list[ast.stmt]) -> bool:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == "__getattr__":
                        return False
                    names.add(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        for sub in ast.walk(target):
                            if isinstance(sub, ast.Name):
                                names.add(sub.id)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "*":
                            source = _absolute(node, package)
                            more = self.names(source, depth + 1) if depth < 5 and self.judged(source) else None
                            if more is None:
                                return False
                            names.update(more)
                        else:
                            names.add(alias.asname or alias.name)
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                    for sub in ast.walk(getattr(node, "target", None) or ast.Pass()):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
                    if not visit(node.body):
                        return False
                elif isinstance(node, ast.If):
                    if not visit(node.body) or not visit(node.orelse):
                        return False
                elif isinstance(node, ast.Try):
                    blocks = [node.body, node.orelse, node.finalbody]
                    blocks += [h.body for h in node.handlers]
                    if not all(visit(b) for b in blocks):
                        return False
            return True

        return names if visit(tree.body) else None


def _string_lists(tree: ast.Module) -> dict[str, list[str]]:
    """Module-level ``NAME = [...]`` / ``(...)`` lists made only of strings."""
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            items = node.value.elts
            if items and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in items):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = [e.value for e in items]
    return found


def _globals_loop_names(tree: ast.Module, literals: dict[str, list[str]]) -> set[str] | None:
    """Names a module binds through ``globals()``.

    The one shape read is a loop over a literal list of names, binding each
    through ``globals()[name] = ...`` (public Kiln's server re-exports the
    tools that moved into plugins that way).  Any other write through
    ``globals()`` means the module's names cannot be known, and ``None``
    says so, so nothing is reported against them.
    """
    names: set[str] = set()
    understood: set[int] = set()
    for loop in ast.walk(tree):
        if not (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)):
            continue
        if isinstance(loop.iter, ast.Name):
            items = literals.get(loop.iter.id)
        elif isinstance(loop.iter, (ast.List, ast.Tuple)):
            items = [e.value for e in loop.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        else:
            items = None
        for node in ast.walk(loop):
            if _globals_write(node) is not None and items is not None:
                key = _globals_write(node)
                if isinstance(key, ast.Name) and key.id == loop.target.id:
                    names.update(items)
                    understood.add(id(node))
    for node in ast.walk(tree):
        if _globals_write(node) is not None and id(node) not in understood:
            return None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and _is_globals_call(node.func.value)
        ):
            return None
    return names


def _is_globals_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "globals"


def _globals_write(node: ast.AST) -> ast.expr | None:
    """The key of a ``globals()[key] = ...`` assignment, else ``None``."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Subscript) and _is_globals_call(target.value):
                return target.slice
    return None


def _first_party(module: str) -> bool:
    return module.split(".")[0] in (PUBLIC_TOP, PRIVATE_TOP)


def _absolute(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    base = package.split(".")
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _guards_import(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(k, (ast.Name, ast.Attribute)) and (k.id if isinstance(k, ast.Name) else k.attr) in _IMPORT_ERRORS for k in kinds)


def _suppresses_import(expr: ast.expr) -> bool:
    """``contextlib.suppress(ImportError)`` (or a broader error) as a guard."""
    if not (isinstance(expr, ast.Call) and expr.args):
        return False
    func = expr.func
    named = (isinstance(func, ast.Name) and func.id == "suppress") or (
        isinstance(func, ast.Attribute) and func.attr == "suppress"
    )
    return named and any(
        isinstance(a, (ast.Name, ast.Attribute)) and (a.id if isinstance(a, ast.Name) else a.attr) in _IMPORT_ERRORS
        for a in expr.args
    )


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.AST):
    """``(node, guarded)`` for every import outside ``if TYPE_CHECKING:``."""

    def walk(node: ast.AST, guarded: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for sub in child.orelse:
                    yield from walk(sub, guarded)
                continue
            if isinstance(child, (ast.With, ast.AsyncWith)) and any(
                _suppresses_import(item.context_expr) for item in child.items
            ):
                for stmt in child.body:
                    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                        yield stmt, True
                    yield from walk(stmt, True)
                continue
            if isinstance(child, ast.Try):
                inner = guarded or any(_guards_import(h) for h in child.handlers)
                for stmt in child.body:
                    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                        yield stmt, inner
                    yield from walk(stmt, inner)
                for part in (*child.handlers, *child.orelse, *child.finalbody):
                    yield from walk(part, guarded)
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, guarded
            yield from walk(child, guarded)

    yield from walk(tree, False)


def _module_of(path: Path, index: _Index) -> str:
    for top, root in index.trees.items():
        try:
            rel = path.relative_to(root).with_suffix("")
        except ValueError:
            continue
        parts = [top, *rel.parts]
        return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    return ""


def _judge_import(
    index: _Index, path: Path, shown: str, line: int, target: str, name: str,
    guarded: bool, is_test: bool, unjudged: set[str],
) -> list[Finding]:
    """What one imported module (and name) says, or ``[]``."""
    if not _first_party(target):
        return []
    private = target.split(".")[0] == PRIVATE_TOP or (
        not index.exists(target)
        and any(target == alias or target.startswith(alias + ".") for alias in index.shims)
    )
    in_public = index.is_public(path) and not is_test
    if index.exists(target) and not private:
        if name and not index.exists(f"{target}.{name}"):
            defined = index.names(target)
            if defined is not None and name not in defined:
                if in_public and not guarded:
                    return [Finding("needs_private", shown, line, target, name, guarded)]
                # Guarded, or in a test: the private package may set the name
                # on the public module at runtime (it does, for the licence
                # manager), which no reading of public source can see.
                unjudged.add(f"{target}.{name}")
        return []
    # From here the module is not public Kiln's own: the private package
    # provides it, or nothing does.
    if in_public and not guarded:
        return [Finding("needs_private", shown, line, target, "", guarded)]
    if guarded and not is_test and not index.is_public(path):
        # The private package's own guarded imports are judged by its
        # runtime check (scripts/audit_guarded_imports.py there), which sees
        # what it sets on public modules; one owner per question.
        return []
    if not index.complete:
        unjudged.add(target)
        return []
    if not index.provided(target):
        kind = "skips_forever" if is_test and guarded else "missing_module"
        return [Finding(kind, shown, line, target, "", guarded)]
    return []


def served_bridges(public_root: Path) -> frozenset[str]:
    """The served bridges named on ``served_answer.HOSTED_DOORS``, read from
    the public tree's own file without importing it."""
    path = public_root / "served_answer.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return frozenset()
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if isinstance(target, ast.Name) and target.id == "HOSTED_DOORS" and isinstance(node.value, ast.Dict):
            found = set()
            for key, value in zip(node.value.keys, node.value.values):
                kind = value.elts[0] if isinstance(value, ast.Tuple) and value.elts else None
                if (
                    isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and isinstance(kind, ast.Constant) and kind.value == "served_answer"
                    and _BRIDGE_NAME.search(key.value)
                ):
                    found.add(key.value)
            return frozenset(found)
    return frozenset()


def _crossing_target(index: _Index, target: str) -> str | None:
    """*target* when importing it reaches the private package, else ``None``."""
    top = target.split(".")[0]
    if top == PRIVATE_TOP:
        return target
    if top == PUBLIC_TOP and not index.exists(target):
        # A kiln.* path the public tree does not hold: one only the private
        # package provides (a compat shim), or one nothing provides.
        return target
    return None


def _in_process(tree: ast.Module, path: Path, shown: str, index: _Index, module: str) -> list[Finding]:
    """Every in-process crossing in one public source file, with the dotted
    name of the def it sits in."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    bridge_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and _absolute(node, package).split(".")[0] == PRIVATE_TOP:
            for alias in node.names:
                if alias.name == "pro_features":
                    bridge_names.add(alias.asname or alias.name)
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(node: ast.AST, function: str, target: str) -> None:
        if (function, target) not in seen:
            seen.add((function, target))
            found.append(Finding("in_process", shown, node.lineno, target, "", False, function))

    def walk(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            here = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                here = [*stack, child.name]
            function = ".".join(stack) or "<module>"
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for sub in child.orelse:
                    walk(sub, stack)
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    hit = _crossing_target(index, alias.name)
                    if hit:
                        add(child, function, hit)
            elif isinstance(child, ast.ImportFrom):
                source = _absolute(child, package)
                hit = _crossing_target(index, source) if source else None
                if hit:
                    add(child, function, hit)
            elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id in bridge_names:
                if child.attr not in _BRIDGE_PROBES:
                    add(child, function, f"{PRIVATE_TOP}.bridge.pro_features.{child.attr}")
            elif (
                isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "getattr"
                and len(child.args) >= 2 and isinstance(child.args[0], ast.Name) and child.args[0].id in bridge_names
                and isinstance(child.args[1], ast.Constant) and isinstance(child.args[1].value, str)
                and child.args[1].value not in _BRIDGE_PROBES
            ):
                add(child, function, f"{PRIVATE_TOP}.bridge.pro_features.{child.args[1].value}")
            walk(child, here)

    walk(tree, [])
    return found


def _scan_file(path: Path, shown: str, index: _Index, *, is_test: bool, unjudged: set[str]) -> list[Finding]:
    """Findings for one file.  On a plain install only public Kiln exists, so
    a public file's unguarded import of anything the private package
    provides is a crash waiting for whoever walks that line."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    module = _module_of(path, index)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: list[Finding] = []
    seen: set[tuple[str, int, str, str]] = set()
    for node, guarded in _imports(tree):
        if isinstance(node, ast.Import):
            targets = [(alias.name, "") for alias in node.names]
        else:
            source = _absolute(node, package)
            targets = [(source, alias.name) for alias in node.names if alias.name != "*"] or [(source, "")]
        for target, name in targets:
            for finding in _judge_import(index, path, shown, node.lineno, target, name, guarded, is_test, unjudged):
                mark = (finding.kind, finding.line, finding.module, finding.name)
                if mark not in seen:
                    seen.add(mark)
                    found.append(finding)
    if (
        not is_test
        and index.is_public(path)
        and module not in INFRASTRUCTURE
        and module not in index.bridges
    ):
        found += _in_process(tree, path, shown, index, module)
    if is_test:
        for call in ast.walk(tree):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "importorskip"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                target = call.args[0].value
                if _first_party(target) and not index.exists(target) and not index.provided(target):
                    if index.complete:
                        found.append(Finding("skips_forever", shown, call.lineno, target, "", True))
                    else:
                        unjudged.add(target)
    return found


def scan(
    trees: dict[str, Path],
    *,
    test_dirs: list[Path] | tuple[Path, ...] = (),
    shown_from: Path | None = None,
    provided: dict[str, str] | None = None,
) -> tuple[list[Finding], set[str]]:
    """``(findings, unjudged modules)`` for every file in *trees* and *test_dirs*.

    *provided* is the private package's compat-shim map (``kiln.X`` →
    ``kiln_pro.Y``), passed by the run that has both trees.
    """
    index = _Index(trees, provided)
    unjudged: set[str] = set()
    found: list[Finding] = []

    def shown(path: Path) -> str:
        """*path* relative to the repository that holds it, so a finding
        reads the same whichever repository runs the check."""
        for parent in path.parents:
            if (parent / ".git").exists():
                return str(path.relative_to(parent))
        base = shown_from or path.anchor
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)

    for root in index.trees.values():
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                found += _scan_file(path, shown(path), index, is_test=False, unjudged=unjudged)
    for root in test_dirs:
        for path in sorted(Path(root).rglob("*.py")):
            if "__pycache__" not in path.parts and (path.name.startswith("test_") or path.name == "conftest.py"):
                found += _scan_file(path, shown(path), index, is_test=True, unjudged=unjudged)
    return found, unjudged


def read_shims(path: Path) -> dict[str, str]:
    """The private package's ``_COMPAT_SHIMS`` literal, read without importing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "_COMPAT_SHIMS" for t in targets) and isinstance(node.value, ast.Dict):
            return {
                k.value: v.value
                for k, v in zip(node.value.keys, node.value.values)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            }
    raise ValueError(f"no _COMPAT_SHIMS literal in {path}")


def judge(
    found: list[Finding],
    known: frozenset[tuple[str, str, str]],
    known_in_process: frozenset[tuple[str, str, str]] = frozenset(),
) -> tuple[list[Finding], list[tuple[str, str, str]]]:
    """``(new findings, stale known entries)``, each kind against its own list."""
    crossing = [f for f in found if f.kind == "in_process"]
    other = [f for f in found if f.kind != "in_process"]
    keys = {f.key for f in other}
    crossing_keys = {f.key for f in crossing}
    new = [f for f in other if f.key not in known] + [f for f in crossing if f.key not in known_in_process]
    stale = sorted(k for k in known if k not in keys) + sorted(k for k in known_in_process if k not in crossing_keys)
    return new, stale


def seed_in_process(found: list[Finding]) -> str:
    """The :data:`KNOWN_IN_PROCESS` body for today's crossings, one per line."""
    keys = sorted({f.key for f in found if f.kind == "in_process"})
    return "\n".join(f"    {key!r}," for key in keys)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tree", action="append", default=[], help="extra TOP=PATH tree, e.g. kiln_pro=../Kiln-pro/kiln_pro")
    parser.add_argument("--tests", action="append", default=[], help="extra test folder")
    parser.add_argument("--known-from", default="", help="Python file whose KNOWN replaces this one's")
    parser.add_argument("--shims-from", default="", help="the private package's __init__.py, read for its compat-shim map")
    parser.add_argument("--seed-in-process", action="store_true", help="print the KNOWN_IN_PROCESS body for today's crossings and exit")
    args = parser.parse_args(argv)

    trees = {"kiln": PUBLIC_TREE}
    tests = [PUBLIC_TESTS]
    for spec in args.tree:
        top, _, path = spec.partition("=")
        trees[top] = Path(path)
    tests += [Path(t) for t in args.tests]
    known, known_in_process = KNOWN, KNOWN_IN_PROCESS
    if args.known_from:
        namespace: dict = {}
        exec(compile(Path(args.known_from).read_text(encoding="utf-8"), args.known_from, "exec"), namespace)
        known = frozenset(namespace["KNOWN"])
        known_in_process = frozenset(namespace.get("KNOWN_IN_PROCESS", ()))

    shims = read_shims(Path(args.shims_from)) if args.shims_from else None
    found, unjudged = scan(trees, test_dirs=tests, shown_from=REPO, provided=shims)
    if args.seed_in_process:
        print(seed_in_process(found))
        return 0
    new, stale = judge(found, known, known_in_process)
    for finding in new:
        print(f"NEW    {finding}")
    for entry in stale:
        print(f"STALE  {entry}  — fixed; delete it from KNOWN")
    print(
        f"[import-targets] {len(found)} finding(s): {len(new)} new, {len(found) - len(new)} known, "
        f"{len(stale)} stale; {len(unjudged)} cross-tree module(s) unjudged here"
    )
    return 2 if new or stale else 0


if __name__ == "__main__":
    sys.exit(main())
