"""Synthetic, no OS authentication/spawn/real path identity tests."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from hashlib import sha256
import json
import os
import re
import sys
import unittest

from sora_commander_next.commands import Command
from sora_commander_next.broker_launch_fence import _fingerprint as fence_fp
from sora_commander_next.jobs import _unknown_command_fingerprint
from offline_operation_intent_contract_model import OperationIntent
from offline_semantic_command_binding_model import (
    SemanticBindingEvidence, compare_claimed_binding_data_only,
    derive_semantic_binding_data_only,
)

OP = "a" * 32
REQ = "b" * 32
REQ2 = "c" * 32


class SemanticBindingTests(unittest.TestCase):
    def setUp(self):
        self.command = Command(
            executable=sys.executable, argv=("--version",),
            cwd=str(Path(__file__).resolve().parents[1]),
            request_id=REQ, timeout=1.0, output_limit=1024,
        )

    def binding(self, command=None):
        result = derive_semantic_binding_data_only(
            self.command if command is None else command)
        self.assertEqual(result.scope, "LEXICAL_ONLY_DATA_ONLY")
        self.assertEqual(result.authority, "NOT_AUTHENTICATED")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertEqual(result.os_acceptance, "NOT_OS_ACCEPTED")
        self.assertRegex(result.digest, r"^[0-9a-f]{64}$")
        return result

    def test_same_semantic_command_new_request_id_has_same_binding(self):
        a = self.binding()
        b = self.binding(replace(self.command, request_id=REQ2))
        self.assertEqual(a.digest, b.digest)

    def test_timeout_and_output_budget_do_not_change_semantic_binding(self):
        ref = self.binding().digest
        for overrides in ({"timeout": 3.5}, {"output_limit": 8192},
                          {"timeout": 3.5, "output_limit": 8192}):
            with self.subTest(overrides=overrides):
                self.assertEqual(ref, self.binding(
                    replace(self.command, **overrides)).digest)

    def test_command_variation_in_executable_argv_cwd_changes_binding(self):
        ref = self.binding().digest
        changed = (
            replace(self.command, executable=sys.executable + ".other"),
            replace(self.command, argv=("-V",)),
            replace(self.command, cwd=str(Path(self.command.cwd).parent)),
            replace(self.command, argv=("a", "b")),
            replace(self.command, argv=("a b",)),
        )
        for alternate in changed:
            with self.subTest(alt=alternate):
                self.assertNotEqual(ref, self.binding(alternate).digest)

    def test_argv_boundaries_are_not_ambiguous(self):
        a = self.binding(replace(self.command, argv=("ab", "c")))
        b = self.binding(replace(self.command, argv=("a", "bc")))
        self.assertNotEqual(a.digest, b.digest)

    def test_unicode_and_json_delimiter_arguments(self):
        a = self.binding(replace(self.command, argv=("日本語", "a,b", "x\\y")))
        b = self.binding(replace(self.command, argv=("日本語a,b", "x\\y")))
        self.assertNotEqual(a.digest, b.digest)

    def test_fence_attempt_fp_does_not_equal_semantic_fp(self):
        first = fence_fp(self.command)
        second = fence_fp(replace(self.command, request_id=REQ2))
        self.assertNotEqual(first, second)
        self.assertEqual(self.binding().digest, self.binding(
            replace(self.command, request_id=REQ2)).digest)

    def test_jobmanager_unknown_fp_is_bytes_and_lexical_scope_only(self):
        first = _unknown_command_fingerprint(self.command)
        second = _unknown_command_fingerprint(
            replace(self.command, request_id=REQ2))
        self.assertIs(type(first), bytes)
        self.assertEqual(first, second)
        # These are two unrelated, deliberately domain-distinct formats.
        self.assertNotEqual(first.hex(), self.binding().digest)

    def test_untrusted_matching_claim_does_not_promote_authority(self):
        evidence = self.binding()
        self.assertEqual(compare_claimed_binding_data_only(
            self.command, evidence.digest), (True, "NOT_AUTHENTICATED"))
        self.assertEqual(compare_claimed_binding_data_only(
            self.command, "0" * 64), (False, "NOT_AUTHENTICATED"))

    def test_wrong_claim_types_and_hex_syntax_rejected(self):
        for v in (True, None, 1, [], {}, "A" * 64, "f" * 63,
                  "g" * 64, "f" * 64 + "\n"):
            with self.subTest(v=v):
                self.assertEqual(compare_claimed_binding_data_only(
                    self.command, v), (False, "NOT_AUTHENTICATED"))

    def test_caller_may_mint_new_operation_id_same_command_limitation(self):
        digest = self.binding().digest
        old = OperationIntent(OP, digest).reserve_data_only(REQ)
        forged_new = OperationIntent("d" * 32, digest)
        # Same digest, two independent in-memory aggregates:
        # no trusted issuer, no cross-operation uniqueness.
        self.assertNotEqual(old.operation_id, forged_new.operation_id)
        self.assertEqual(old.command_binding, forged_new.command_binding)
        self.assertEqual(forged_new.inspect_data_only(), "NEW_INERT_DATA_ONLY")
        self.assertEqual(old.inspect_data_only(),
                         "HOLD_RESERVED_UNKNOWN_DATA_ONLY")

    def test_digest_cannot_be_used_as_approval(self):
        result = self.binding()
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            result.admission = "SPAWN_ALLOWED"
        with self.assertRaises(TypeError):
            replace(result, authority="OS_AUTHENTICATED")

    def test_exact_command_type_required(self):
        with self.assertRaises(ValueError):
            derive_semantic_binding_data_only({"executable": sys.executable})

    def test_revalidated_forged_invalid_request_id(self):
        # Dataclasses can be forged in Python; checks repeat at use boundary.
        for v in (True, "", "G" * 32, "a" * 31, "a" * 33):
            with self.subTest(v=v):
                fake = replace(self.command, request_id=REQ)
                object.__setattr__(fake, "request_id", v)
                with self.assertRaises(ValueError):
                    derive_semantic_binding_data_only(fake)

    def test_invalid_mutated_arguments_and_limits(self):
        for attr, value in (
            ("argv", ["--version"]), ("argv", (True,)), ("argv", ("a\0b",)),
            ("timeout", float("nan")), ("timeout", True),
            ("output_limit", 1), ("output_limit", True),
        ):
            with self.subTest(attr=attr, value=value):
                fake = replace(self.command, request_id=REQ)
                object.__setattr__(fake, attr, value)
                with self.assertRaises(ValueError):
                    derive_semantic_binding_data_only(fake)

    @unittest.skipUnless(os.name == "nt", "Windows lexical paths only")
    def test_windows_lexical_case_separator_and_dot_segments(self):
        def forged(executable, cwd):
            request = object.__new__(Command)
            for key, value in (
                ("executable", executable), ("cwd", cwd),
                ("argv", ("--version",)), ("request_id", REQ),
                ("timeout", 1.0), ("output_limit", 1024),
            ):
                object.__setattr__(request, key, value)
            return request
        variant = forged(r"C:\PROJECTS\Bin\RUN.EXE", r"C:\TEMP\Dir\..\Work")
        canonical = forged(r"c:\projects\bin\run.exe", r"c:\temp\work")
        self.assertEqual(self.binding(variant).digest,
                         self.binding(canonical).digest)
        # Lexical matching is NOT a reparse/8.3/OS file identity proof.

    @unittest.skipUnless(os.name == "nt", "Windows known vector only")
    def test_windows_static_golden_hash_vector(self):
        request = object.__new__(Command)
        for key, value in (
            ("executable", r"C:\Program Files\Sora\tool.exe"),
            ("cwd", r"C:\Work\Task"),
            ("argv", ("a,b", "日本語", "--flag")),
            ("request_id", REQ), ("timeout", 1.0),
            ("output_limit", 1024),
        ):
            object.__setattr__(request, key, value)
        # Independently fixed Windows normcase + sorted compact ASCII JSON
        # + domain-separation SHA256; NOT computed via model under test.
        self.assertEqual(self.binding(request).digest,
                         "d2740115743a82b6d168f50938d9214d"
                         "5ff38cd82a41fee35af9201680f40b9e")

    def test_no_automatic_domain_upgrade_or_runtime_auth(self):
        result = self.binding()
        self.assertEqual(result.os_acceptance, "NOT_OS_ACCEPTED")
        self.assertFalse(hasattr(result, "launch"))
        self.assertFalse(hasattr(result, "owner_approved"))
