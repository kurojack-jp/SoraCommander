"""Offline, deterministic inventory for a *candidate* Windows code bundle.

This is NOT signing, protected installation, or a runtime security boundary.
Verification needs an expected manifest SHA256 supplied from a separately
trusted channel. The verifier itself must also be trusted/protected.
Nothing in this module launches a process, changes Windows ACLs, or registers
an MCP endpoint, Task or service.
"""
import hashlib
import hmac
import json
from pathlib import Path
import re
import stat
from .plugin_icon import inspect_icon_png, MAX_ICON_BYTES

FORMAT = "sora-commander-next-stage-v1"
PACKAGE = "sora_commander_next"
MANIFEST = "manifest.json"
ASSETS = "assets"
PLUGIN_ICON = "plugin-icon.png"
MAX_FILE_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 64 * 1024

# These exact worktree-only candidates may be absent in a clean checkout.
# Presence never promotes them into the runtime inventory.
OPTIONAL_DEV_ONLY_MODULES = frozenset((
    "owner_locale_frame_display_candidate.py",
    "owner_locale_settings_gui_candidate.py",
    "owner_review_locale_settings_candidate.py",
))
OPTIONAL_DEV_ONLY_ASSET_SHA256 = {
    "settings_en.dev.json": "ecb852c881b893cdcec6c7821a0f7dcb374ed7f0ce3a841356d0855b6aab35f9",
    "settings_ja.dev.json": "f2116548507d196d141fb2bcc0e79ea8213fafdc34ae64050c3cbe252c145275",
}

# Explicit development-only modules. Every source .py must be classified;
# these files are NEVER copied into release candidates, signed or imported by
# the runtime bundle. When a v2 feature is production-ready, promote it into
# MODULES through a separately reviewed release inventory change.
DEV_ONLY_MODULES = frozenset((
    "cosmetic_settings_catalog.py",
    "owner_command_caution.py",
    "owner_command_dialog_localized.py",  # inactive until a reviewed broker cutover
    "owner_localized_broker_dev_20261104.py",  # unconnected adapter; no Host imports it
    "owner_file_review_localized_dev_20261104.py",  # display data only; no write path
    "owner_file_review_gate_dev_20261105.py",  # unconnected; no Host imports it
    "owner_file_dialog_localized.py",  # unconnected child window; no write path
    "owner_appdata_locale_store_dev_20261104.py",  # in-memory fake only; no filesystem
    "owner_review_file_view.py",
    "owner_review_i18n.py",
    "owner_review_presentation_candidate.py",
    "owner_review_isolated_candidate_loader.py",
    "owner_review_request_binding_candidate.py",
    "owner_review_file_binding_candidate.py",
    "dev_request_observability.py",  # isolated DEV-only observer
    "dev_rdc_compat.py",  # personal Owner-reviewed cooperative DEV operations only
    "offline_g1_ledger_v2.py",  # syntax/chain DATA_ONLY; no service signature or trusted floor
    "offline_codex_exact_file_evidence.py",  # explicit read-only file SHA observations; never CAS
    "offline_codex_task_acceptance.py",  # separate CLI lifecycle from human-reviewed task evidence
    "offline_exact_source_scope.py",  # MVP4 exact-file data-only grant proposals; no writer
    "offline_native_windows_source_probe.py",  # Win32 read-handle diagnostic only, no writer
    "offline_windows_cas_readiness.py",  # explicit fail-closed strong CAS checklist, no writer
    "offline_source_patch_candidate.py",  # bounded data-only preview, no write authority
    "offline_b04_composite_review_candidate.py",  # data-only composite, no authority
    "offline_b04_message_integrity_candidate.py",  # in-memory test-only HMAC, never auth
    "offline_b04_pipe_binding_candidate.py",  # observation only; cannot authenticate
    "offline_b04_trust_evidence_candidate.py",  # review-only observations; never auth
    "offline_b04_p1363_der_candidate.py",  # strict format conversion; no auth
    "offline_b04_signed_challenge_candidate.py",  # standalone P-256 math only; no install key trust
    "offline_b04_sspi_handshake_candidate.py",  # mock mutual-auth transcript, never authority
    "service_pipe_handle_role_candidate.py",  # server-only PID ABI and client-side hard deny; no trust
    "service_scm_retained_bridge_candidate.py",  # held process+service mock observation, NOT auth
    "service_scm_query_abi_candidate.py",  # B04 explicit fake-DLL Win32 ABI, not executed or trusted
    "offline_release_tool_payload.py",  # release helper hash/portability data only; no install
    "service_scm_sampling_candidate.py",  # B04 same-handle data-only SCM mock; no Win32/permit
    "service_auth_candidate.py",  # typed comparisons never authenticate
    "service_spent_ledger_candidate.py",  # injected SQL only; native acceptance absent
    "source_handle_verifier_candidate.py",  # dry run only; existing-source commit denied
    "broker_recovery_candidate.py",  # retained runtime seam; synthetic manager only
    "service_native_prerequisites.py",  # B04 evidence only; never authentication
    "protected_ledger_client.py",  # B01 wire candidate; no accepted channel/permit
    "source_review.py",  # D06 complete DEV workflow; native CAS not accepted
    "development_broker.py",  # injected integration only; never product authority
    "owner_broker_service.py",  # G2 wire dispatch; no listener, not connected
    "owner_broker_pipe.py",  # B04 Host-Broker pipe; nothing starts it
    "owner_broker_dev.py",  # G2 personal DEV tool parity; not product
    "owner_broker_main.py",  # G2 DEV Broker process; started by hand
    "owner_launch_ledger.py",  # B01 SQLite launch record; Owner-level only
    "owner_broker_client_main.py",  # G2 DEV forwarding Host
    "approval_v2_broker_prepare_offline.py",
    "approval_v2_contract.py",
    "approval_v2_flow_offline.py",
    "approval_v2_ledger.py",
    "approval_v2_owner_local.py",
    "approval_v2_policy.py",
    "owner_approval_dialog_v2.py",
    "dev_job_review_preview_offline.py",
    "dev_job_scope_offline.py",
    "offline_grant_contract.py",
    "offline_b01_full_binding.py",  # U2-b volatile contract, no runtime authority
    "offline_b01_execution_profile.py",  # U2-c asserted recipe, no launch authority
    "offline_b01_service_fixture.py",  # U2-d isolated data-only DB, NEVER runtime
    "offline_b01_broker_actor.py",  # U2-e synthetic state transitions, no spawn
    "offline_b01_handoff_harness.py",  # U2-f data-only composition, no authority
    "offline_b01_receipt_adjudication.py",  # U2-g offline reply boundary, no authority
    "offline_b04_service_identity_probe.py",  # B04-a SCM/process observation, NOT_AUTHENTICATED
    "offline_ledger_lifecycle_contract.py",
    "offline_output_spool.py",  # D08 data-only dev pilot, not approved runtime
    "offline_live_command_snapshot.py",
    "offline_owner_bridge.py",
    "offline_owner_choice.py",
    "offline_owner_transaction.py",
    "offline_review_intake.py",
    "offline_review_queue.py",
    "offline_broker_result_wire.py",
    "offline_broker_result_reader.py",
    "offline_broker_lifecycle.py",
    "offline_broker_singleton.py",
    "offline_broker_process_probe.py",
    "offline_broker_owner_probe.py",
    "offline_broker_owner_process_probe.py",
    "offline_broker_process_evidence.py",
    "offline_broker_install_acl_audit.py",
    "offline_machine_install_plan.py",
    "offline_broker_runtime_process.py",
    "offline_owner_authority.py",
)) | OPTIONAL_DEV_ONLY_MODULES

# Explicit inventory of modules for Phase 9F. A new imported module must be
# reviewed and added here; an unexpected *.py fails rather than being copied.
MODULES = tuple(sorted((
    "owner_storage.py",  # create-only startup provisioning, imported by stdio_main
    "allowed_roots.py",  # startup-only local roots; imported by stdio_main
    "git_push_guard.py",  # refused pushes; imported by policy.py
    "approval_visual_theme.py",
    "owner_review_renderer.py",
    "broker_host.py",  # transport only; product factory remains unavailable
    "product_composition.py",  # opt-in fail-closed factory, no trust promotion
    "__init__.py",
    "activity.py",  # data-only observation store; no command content, no launch
    "approval.py",
    "broker_contract.py",
    "broker_dispatch.py",
    "broker_deadline.py",
    "broker_frames.py",
    "broker_launch_fence.py",
    "broker_owner_lease.py",
    "broker_owner_process.py",
    "broker_protocol.py",
    "broker_runtime.py",
    "broker_transport_gate.py",
    "commands.py",
    "cli_monitor_bootstrap.py",
    "codex_jobs.py",
    "extensions.py",
    "file_reads.py", "git_readonly.py", "image_reads.py",
    "file_edits.py",
    "file_review_outcome.py",
    "no_answer.py",
    "edit_review_pilot.py",
    "host.py",
    "host_supervisor.py",
    "http_local.py",
    "interactive_hooks.py",  # release-safe DEV interactive hook slot; imports nothing local
    "job_status_snapshot.py",
    "jobs.py",
    "recovery_overview.py",  # read-only manual recovery guidance; no replay
    "safe_command_identity.py",  # bounded non-secret exact command identification
    "local_health.py",
    "mcp_adapter.py",
    "owner_approval_broker.py",
    "owner_dialog_integrity.py",
    "owner_ui_language.py",
    "task_presets.py",
    "ogf_cue.py",
    "owner_approval_dialog.py",
    "owner_ui.py",
    "owner_review_dispatch.py",
    "owner_stage_acl.py",
    "performance_baseline.py",
    "plugin_icon.py",
    "policy.py",
    "release_inventory.py",
    "release_broker_bom_receipt.py",
    "release_broker_bom_files_audit.py",
    "release_broker_import_audit.py",
    "release_layout.py",
    "release_anchor_review.py",
    "release_environment.py",
    "release_hash_lock.py",
    "release_journal_recovery.py",
    "release_preflight.py",
    "release_pilot_review.py",
    "release_receipt.py",
    "release_runtime_attestation.py",
    "release_windows_signature_audit.py",
    "release_sandbox.py",
    "release_slots.py",
    "session_journal.py",
    "offline_result_archive.py",
    "scoped_dev_edits.py",
    "release_update.py",
    "release_wheelhouse.py",
    "release_wheel_payload_audit.py",
    "release_trust_policy.py",
    "release_trust_store_plan.py",
    "stdio_main.py",
    "windows_job.py",
    "windows_owner.py",
    "windows_pipe_client.py",
    "windows_pipe_context.py",
    "windows_pipe_listener.py",
    "windows_pipe_io.py",
    "windows_pipe_connect.py",
    "windows_pipe_lifetime.py",
    "windows_pipe_transfer.py",
    "windows_owner_controller.py",
    "windows_session_events.py",
    "windows_session_lock.py",
    "worker.py",
)))


class CandidateIntegrityError(ValueError):
    """Candidate manifest/content/path invalid; never treat as installable."""


def _regular(path):
    p = Path(path)
    if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
        raise CandidateIntegrityError("symbolic link or junction not permitted")
    try:
        mode = p.lstat().st_mode
    except OSError as exc:
        raise CandidateIntegrityError("missing or unreadable staged file") from exc
    if not stat.S_ISREG(mode):
        raise CandidateIntegrityError("not an ordinary file")
    return p


def _plain_dir(path):
    p = Path(path)
    if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
        raise CandidateIntegrityError("linked directory not permitted")
    if not p.is_dir():
        raise CandidateIntegrityError("expected directory is missing")
    return p


def _bounded_bytes(path, maximum=MAX_FILE_BYTES):
    p = _regular(path)
    if p.stat().st_size > maximum:
        raise CandidateIntegrityError("file exceeds bounded inventory size")
    try:
        with p.open("rb") as f:
            data = f.read(maximum + 1)
    except OSError as exc:
        raise CandidateIntegrityError("file read failed") from exc
    if len(data) > maximum:
        raise CandidateIntegrityError("file exceeds bounded inventory size")
    return data


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _manifest_bytes(records):
    manifest = {"format": FORMAT, "package": PACKAGE, "files": records}
    return (json.dumps(manifest, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":")) + "\n").encode("ascii")


def _entries(directory):
    try:
        return set(p.name for p in directory.iterdir())
    except OSError as exc:
        raise CandidateIntegrityError("directory enumeration failed") from exc


def _package_sources(source):
    source = _plain_dir(source)
    # DEV-only, SHA-pinned bilingual presentation resources are not shipped
    # until the Owner UI localization release gate is separately accepted.
    locale_dir = source / "i18n"
    if locale_dir.is_symlink():
        raise CandidateIntegrityError("linked DEV locale directory")
    if locale_dir.exists():
        if not locale_dir.is_dir() or _entries(locale_dir) != {"en.json", "ja.json", "caution_en.json", "caution_ja.json"}:
            raise CandidateIntegrityError("unexpected DEV locale resources")
        for locale_name in ("en.json", "ja.json", "caution_en.json", "caution_ja.json"):
            item = locale_dir / locale_name
            if item.is_symlink() or not item.is_file() or item.stat().st_size > 8192:
                raise CandidateIntegrityError("invalid DEV locale resource")
    names = _entries(source)
    present_assets = names & OPTIONAL_DEV_ONLY_ASSET_SHA256.keys()
    if present_assets:
        if present_assets != OPTIONAL_DEV_ONLY_ASSET_SHA256.keys():
            raise CandidateIntegrityError("incomplete DEV settings resources")
        for name, expected_sha in OPTIONAL_DEV_ONLY_ASSET_SHA256.items():
            item = source / name
            info = item.lstat()
            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise CandidateIntegrityError("linked DEV settings resource")
            data = _bounded_bytes(item, 8192)
            if not hmac.compare_digest(_sha(data), expected_sha):
                raise CandidateIntegrityError("DEV settings resource SHA256 mismatch")
        names.difference_update(present_assets)
    for name in names & OPTIONAL_DEV_ONLY_MODULES:
        item = source / name
        if getattr(item.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise CandidateIntegrityError("linked DEV locale module")
        _regular(item)
    if locale_dir.exists():
        names.discard("i18n")
    if "__pycache__" in names:
        # Python test activity creates bytecode caches. They are not bundled.
        names.remove("__pycache__")
    # Source is a development tree, while the signed candidate is only the
    # reviewed runtime inventory. Unknown source code is still a hard failure.
    if (set(MODULES) & DEV_ONLY_MODULES
            or names - OPTIONAL_DEV_ONLY_MODULES !=
            set(MODULES) | (DEV_ONLY_MODULES - OPTIONAL_DEV_ONLY_MODULES)):
        raise CandidateIntegrityError("unexpected or missing source modules")
    return {name: _bounded_bytes(source / name) for name in MODULES}


def make_candidate(source_package, new_destination, *, approved_icon_png=None):
    """Create a NEW candidate dir only. Never install/replace an existing dir.

    Source is a checked Python package directory. Destination MUST NOT exist.
    Returned digest must be retained out of band by a trusted release owner.
    """
    source = _package_sources(source_package)
    icon = None
    if approved_icon_png is not None:
        icon = _bounded_bytes(approved_icon_png, MAX_ICON_BYTES)
        inspect_icon_png(icon)
    dest = Path(new_destination)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError("refusing to overwrite any existing destination")
    _plain_dir(dest.parent)
    dest.mkdir(exist_ok=False)
    pkg = dest / PACKAGE
    pkg.mkdir(exist_ok=False)
    records = []
    for name in MODULES:
        data = source[name]
        (pkg / name).write_bytes(data)
        records.append({"path": f"{PACKAGE}/{name}",
                        "bytes": len(data), "sha256": _sha(data)})
    if icon is not None:
        asset = dest / ASSETS
        asset.mkdir(exist_ok=False)
        (asset / PLUGIN_ICON).write_bytes(icon)
        records.append({"path": f"{ASSETS}/{PLUGIN_ICON}",
                        "bytes": len(icon), "sha256": _sha(icon)})
    manifest = _manifest_bytes(records)
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise CandidateIntegrityError("inventory too large")
    (dest / MANIFEST).write_bytes(manifest)
    return _sha(manifest)


def _unique_object(pairs):
    obj = {}
    for name, value in pairs:
        if name in obj:
            raise CandidateIntegrityError("duplicate JSON key")
        obj[name] = value
    return obj


def verify_candidate(directory, *, trusted_manifest_sha256):
    """Fail closed against a separately supplied, exact expected digest.

    WARNING: No self-attestation. Same-user tampering of the verifier,
    expected value, executing interpreter, or post-check files is out of
    scope. Never use this check to silently elevate a deployment to trusted.
    """
    if type(trusted_manifest_sha256) is not str or not re.fullmatch(
            r"[0-9a-f]{64}", trusted_manifest_sha256):
        raise CandidateIntegrityError("independently trusted manifest SHA256 required")
    root = _plain_dir(directory)
    root_names = _entries(root)
    with_icon = ASSETS in root_names
    expected_root = {PACKAGE, MANIFEST} | ({ASSETS} if with_icon else set())
    if root_names != expected_root:
        raise CandidateIntegrityError("extra or missing candidate root entries")
    pkg = _plain_dir(root / PACKAGE)
    if _entries(pkg) != set(MODULES):
        raise CandidateIntegrityError("extra or missing candidate modules")
    if with_icon and _entries(_plain_dir(root / ASSETS)) != {PLUGIN_ICON}:
        raise CandidateIntegrityError("unexpected candidate branding asset")
    raw = _bounded_bytes(root / MANIFEST, MAX_MANIFEST_BYTES)
    if not hmac.compare_digest(_sha(raw), trusted_manifest_sha256):
        raise CandidateIntegrityError("manifest digest differs from trusted input")
    try:
        parsed = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CandidateIntegrityError("malformed manifest") from exc
    if (type(parsed) is not dict or set(parsed) != {"format", "package", "files"}
            or parsed["format"] != FORMAT or parsed["package"] != PACKAGE
            or type(parsed["files"]) is not list
            or len(parsed["files"]) != len(MODULES) + (1 if with_icon else 0)):
        raise CandidateIntegrityError("invalid inventory schema")
    records = []
    for name, record in zip(MODULES, parsed["files"]):
        if (type(record) is not dict
                or set(record) != {"path", "bytes", "sha256"}
                or type(record["path"]) is not str
                or record["path"] != f"{PACKAGE}/{name}"
                or type(record["bytes"]) is not int
                or not 0 <= record["bytes"] <= MAX_FILE_BYTES
                or type(record["sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise CandidateIntegrityError("invalid inventory entry")
        data = _bounded_bytes(pkg / name)
        if len(data) != record["bytes"] or not hmac.compare_digest(
                _sha(data), record["sha256"]):
            raise CandidateIntegrityError("candidate file differs from inventory")
        records.append(record)
    if with_icon:
        record = parsed["files"][-1]
        if (type(record) is not dict
                or set(record) != {"path", "bytes", "sha256"}
                or record["path"] != f"{ASSETS}/{PLUGIN_ICON}"
                or type(record["bytes"]) is not int
                or not 0 <= record["bytes"] <= MAX_ICON_BYTES
                or type(record["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None):
            raise CandidateIntegrityError("invalid signed icon entry")
        icon = _bounded_bytes(root / ASSETS / PLUGIN_ICON, MAX_ICON_BYTES)
        inspect_icon_png(icon)
        if len(icon) != record["bytes"] or not hmac.compare_digest(
                _sha(icon), record["sha256"]):
            raise CandidateIntegrityError("signed plugin icon does not match")
        records.append(record)
    if raw != _manifest_bytes(records):
        raise CandidateIntegrityError("noncanonical manifest")
    return True
