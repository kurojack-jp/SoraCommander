"""B04 SSPI mock transcript negative-case and lifecycle contract tests only.

No SSPI, Win32 DLL, credential acquisition, Named Pipe, socket, SQLite,
service, Owner prompt or process launch is permitted in this suite.
"""
from dataclasses import FrozenInstanceError, replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import unittest

from sora_commander_next.offline_b04_sspi_handshake_candidate import (
    OfflineSspiHandshakeCandidate, SspiCandidatePins, SspiStepCandidate,
    SspiCandidateDenied, REQUIRED_FINAL_ATTRIBUTES, MAX_STEPS,
)

CLIENT_NONCE = "a"*64
SERVER_NONCE = "b"*64
INSTALL = "c"*32
GENERATION = "d"*32
BROKER = "e"*32
SERVICE = "SCNProtectedLedger/localhost"
OWNER = "owner@example.test"
FLAGS = REQUIRED_FINAL_ATTRIBUTES
PINS = SspiCandidatePins(INSTALL,GENERATION,BROKER,1,CLIENT_NONCE,SERVER_NONCE,SERVICE,OWNER)


def make_step(role, ordinal, outcome, token=None, **override):
    fields = dict(role=role, ordinal=ordinal, outcome=outcome,
        output_token=(f"leg{ordinal}".encode() if token is None else token),
        returned_attributes=FLAGS if outcome=="OK" else frozenset(),
        selected_package="Kerberos" if outcome=="OK" else "",
        observed_peer_principal=(SERVICE if role=="CLIENT" else OWNER)
                                if outcome=="OK" else "",
        installation_id=INSTALL, generation=GENERATION,broker_instance_id=BROKER,
        connection_epoch=1,client_nonce=CLIENT_NONCE,server_nonce=SERVER_NONCE)
    fields.update(override)
    return SspiStepCandidate(**fields)


STEPS = (
    make_step("CLIENT",0,"CONTINUE"),
    make_step("SERVER",1,"CONTINUE"),
    make_step("CLIENT",2,"OK"),
    make_step("SERVER",3,"OK")
)


class OfflineSspiHandshakeTests(unittest.TestCase):
    def setUp(self):
        for symbol in ("ctypes.WinDLL","subprocess.Popen","os.system",
                       "socket.socket","sqlite3.connect"):
            guard=patch(symbol,side_effect=AssertionError("REAL OS OPERATION BLOCKED"),create=True)
            guard.start()
            self.addCleanup(guard.stop)
        self.moment=100
        self.session=OfflineSspiHandshakeCandidate(PINS,_clock_ms=self.clock)
    def clock(self):
        self.moment+=1
        return self.moment
    def finish(self,steps=STEPS):
        for step in steps:
            self.session.observe_step(step)
        return self.session.close_data_only()
    def denied(self, step, expected):
        with self.assertRaises(SspiCandidateDenied) as err:
            self.session.observe_step(step)
        self.assertEqual(err.exception.code, expected)
        with self.assertRaises(SspiCandidateDenied):
            self.session.observe_step(STEPS[0])
        with self.assertRaises(SspiCandidateDenied):
            self.session.close_data_only()

    def test_perfect_mock_is_still_unavailable(self):
        result=self.finish()
        self.assertEqual(result.output_state,"MOCK_CONTRACT_COMPLETE")
        self.assertEqual(result.authentication,"NOT_AUTHENTICATED")
        self.assertEqual(result.admission,"UNAVAILABLE")
        self.assertEqual(result.package_observation,"Kerberos")
        self.assertEqual(result.steps,4)
        for name in ("service_identity_os_verified","owner_identity_os_verified",
                     "signature_verified_on_connected_handle","native_sspi_context_bound",
                     "independent_publisher_anchor_verified","live_replay_protected"):
            self.assertIs(getattr(result,name),False)

    def test_digest_deterministic_for_same_injected_transcript(self):
        first=self.finish().transcript_sha256
        other=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
        for step in STEPS:other.observe_step(step)
        self.assertEqual(first,other.close_data_only().transcript_sha256)
        self.assertRegex(first,r"^[0-9a-f]{64}$")

    def test_transcript_changes_with_any_token_byte(self):
        first=self.finish().transcript_sha256
        other=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
        for index,step in enumerate(STEPS):
            other.observe_step(replace(step,output_token=b"changed") if index==1 else step)
        self.assertNotEqual(first,other.close_data_only().transcript_sha256)

    def test_transcript_digest_binds_generation_nonce_and_epoch(self):
        first = self.finish().transcript_sha256
        for field,value in (("generation","1"*32),
                            ("connection_epoch",2),
                            ("client_nonce","9"*64)):
            with self.subTest(field=field):
                pins=replace(PINS,**{field:value})
                session=OfflineSspiHandshakeCandidate(pins,_clock_ms=lambda:100)
                for st in STEPS:
                    session.observe_step(replace(st,**{field:value}))
                self.assertNotEqual(first,session.close_data_only().transcript_sha256)

    def test_no_full_opaque_tokens_retained_in_instance(self):
        self.session.observe_step(STEPS[0])
        self.assertNotIn("_last_step",vars(self.session))
        self.assertNotIn(STEPS[0].output_token,repr(vars(self.session)).encode())

    def test_review_is_immutable(self):
        observed=self.finish()
        with self.assertRaises(FrozenInstanceError):
            observed.authentication="AUTHENTICATED"

    def test_no_rearm_after_final(self):
        self.finish()
        with self.assertRaisesRegex(SspiCandidateDenied,"HANDSHAKE_TERMINAL"):
            self.session.observe_step(STEPS[0])
        with self.assertRaisesRegex(SspiCandidateDenied,"HANDSHAKE_TERMINAL"):
            self.session.close_data_only()

    def test_cannot_finish_one_sided(self):
        self.session.observe_step(STEPS[0])
        with self.assertRaisesRegex(SspiCandidateDenied,"BOTH_FINAL_CONTEXTS_REQUIRED"):
            self.session.close_data_only()

    def test_cannot_finish_before_start(self):
        with self.assertRaisesRegex(SspiCandidateDenied,"BOTH_FINAL_CONTEXTS_REQUIRED"):
            self.session.close_data_only()

    def test_out_of_order_step_rejected(self):
        self.denied(STEPS[1],"HANDSHAKE_SEQUENCE_INVALID")

    def test_duplicate_step_rejected(self):
        self.session.observe_step(STEPS[0])
        with self.assertRaisesRegex(SspiCandidateDenied,"HANDSHAKE_SEQUENCE_INVALID"):
            self.session.observe_step(STEPS[0])

    def test_wrong_turn_role_rejected(self):
        self.denied(replace(STEPS[0],role="SERVER"),"HANDSHAKE_ROLE_OR_FINAL_REPLAY")

    def test_role_after_final_rejected(self):
        self.session.observe_step(STEPS[0])
        self.session.observe_step(STEPS[1])
        self.session.observe_step(STEPS[2])
        self.session.observe_step(STEPS[3])
        self.denied(make_step("CLIENT",4,"OK"),"HANDSHAKE_ROLE_OR_FINAL_REPLAY")

    def test_no_nonsensical_status(self):
        for status in ("FAIL","SEC_I_CONTINUE_NEEDED",True,None,"ERROR"):
            with self.subTest(status=status):
                o=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
                with self.assertRaises(SspiCandidateDenied):
                    o.observe_step(replace(STEPS[0],outcome=status))

    def test_mixed_or_unpinned_package_refused(self):
        for package in ("Negotiate","NTLM","Schannel","KERBEROS","Custom","",False):
            with self.subTest(package=package):
                session=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
                session.observe_step(STEPS[0])
                session.observe_step(STEPS[1])
                with self.assertRaisesRegex(SspiCandidateDenied,"SSPI_PACKAGE_DOWNGRADE_OR_UNKNOWN"):
                    session.observe_step(replace(STEPS[2],selected_package=package))

    def test_missing_final_security_attribute_denied(self):
        for name in FLAGS:
            with self.subTest(name=name):
                session=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
                session.observe_step(STEPS[0]);session.observe_step(STEPS[1])
                with self.assertRaisesRegex(SspiCandidateDenied,"MISSING_FINAL_CONTEXT_ATTRIBUTES"):
                    session.observe_step(replace(STEPS[2],
                        returned_attributes=frozenset(FLAGS-{name})))

    def test_intermediate_flags_cannot_pass_as_final(self):
        self.session.observe_step(replace(STEPS[0],returned_attributes=FLAGS))
        self.session.observe_step(STEPS[1])
        self.session.observe_step(STEPS[2])
        self.session.observe_step(STEPS[3])
        self.assertFalse(self.session.close_data_only().native_sspi_context_bound)

    def test_intermediate_peer_identity_claim_is_rejected(self):
        self.denied(replace(STEPS[0],selected_package="Kerberos"),
                    "PREMATURE_PEER_IDENTITY_CLAIM")

    def test_wrong_peer_principal_rejected(self):
        for role,position in (("CLIENT",2),("SERVER",3)):
            with self.subTest(role=role):
                session=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
                for st in STEPS[:position]:session.observe_step(st)
                with self.assertRaisesRegex(SspiCandidateDenied,"PEER_PRINCIPAL_MISMATCH"):
                    session.observe_step(replace(STEPS[position],observed_peer_principal="unknown"))

    def test_owner_sid_is_not_guessed_from_principal_name(self):
        result=self.finish()
        self.assertIn("OWNER_SID_LOGON_SESSION_TOKEN_NOT_LINKED",result.missing_evidence)

    def test_duplicate_token_same_session_denied(self):
        self.session.observe_step(STEPS[0])
        self.denied(replace(STEPS[1],output_token=STEPS[0].output_token),
                    "DUPLICATE_HANDSHAKE_TOKEN")

    def test_oversized_token_denied(self):
        self.denied(replace(STEPS[0],output_token=b"x"*(65536+1)),
                    "OPAQUE_TOKEN_BOUNDS")

    def test_continue_without_token_denied(self):
        self.denied(replace(STEPS[0],output_token=b""),"OPAQUE_TOKEN_BOUNDS")

    def test_nonbytes_tokens_denied(self):
        for token in (bytearray(b"a"),"text",None,True):
            with self.subTest(token=token):
                session=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
                with self.assertRaises(SspiCandidateDenied):
                    session.observe_step(replace(STEPS[0],output_token=token))

    def test_generation_or_broker_change_denied(self):
        for field,value in (("installation_id","0"*32),
                            ("generation","1"*32),
                            ("broker_instance_id","2"*32),
                            ("connection_epoch",2),
                            ("client_nonce","3"*64),
                            ("server_nonce","4"*64)):
            with self.subTest(field=field):
                self2=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
                with self.assertRaisesRegex(SspiCandidateDenied,"HANDSHAKE_PIN_OR_EPOCH_MISMATCH"):
                    self2.observe_step(replace(STEPS[0],**{field:value}))

    def test_replay_from_different_connection_epoch_denied(self):
        pins=replace(PINS,connection_epoch=2)
        session=OfflineSspiHandshakeCandidate(pins,_clock_ms=lambda:100)
        with self.assertRaisesRegex(SspiCandidateDenied,"HANDSHAKE_PIN_OR_EPOCH_MISMATCH"):
            session.observe_step(STEPS[0])

    def test_clock_rollback_denied_and_latched(self):
        readings=iter((100,101,99))
        session=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:next(readings))
        session.observe_step(STEPS[0])
        with self.assertRaisesRegex(SspiCandidateDenied,"CLOCK_ROLLBACK_OR_INVALID"):
            session.observe_step(STEPS[1])
        with self.assertRaisesRegex(SspiCandidateDenied,"HANDSHAKE_TERMINAL"):
            session.close_data_only()

    def test_deadline_denied_and_latched(self):
        readings=iter((100,101,105))
        session=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:next(readings),timeout_ms=5)
        session.observe_step(STEPS[0])
        with self.assertRaisesRegex(SspiCandidateDenied,"SSPI_HANDSHAKE_TIMEOUT"):
            session.observe_step(STEPS[1])

    def test_timeout_rejects_bad_type(self):
        for val in (0,-1,5001,True,1.5,None):
            with self.subTest(val=val),self.assertRaises(SspiCandidateDenied):
                OfflineSspiHandshakeCandidate(PINS,timeout_ms=val)

    def test_pins_type_and_noncolliding_nonces(self):
        for val in (None,True,{},object()):
            with self.subTest(val=val),self.assertRaises(SspiCandidateDenied):
                OfflineSspiHandshakeCandidate(val)
        with self.assertRaisesRegex(SspiCandidateDenied,"NONCE_COLLISION"):
            replace(PINS,server_nonce=CLIENT_NONCE)

    def test_no_client_supplied_principal_override(self):
        with self.assertRaises(TypeError):
            OfflineSspiHandshakeCandidate(PINS,authenticated=True)

    def test_invalid_pin_forms(self):
        for attr,value in (
                ("installation_id","g"*32),("generation","x"*32),
                ("client_nonce","0"*63),("connection_epoch",True),
                ("service_principal","a "*80),("owner_principal","")):
            with self.subTest(attr=attr):
                with self.assertRaises(SspiCandidateDenied):
                    replace(PINS,**{attr:value})

    def test_attributes_must_be_exact_immutable_set(self):
        for val in (FLAGS, set(FLAGS),("MUTUAL_AUTH",), True, frozenset({1})):
            if val is FLAGS:continue
            with self.subTest(value=val):
                self2=OfflineSspiHandshakeCandidate(PINS,_clock_ms=lambda:100)
                with self.assertRaisesRegex(SspiCandidateDenied,"INVALID_SSPI_ATTRIBUTE_SET"):
                    self2.observe_step(replace(STEPS[0],returned_attributes=val))

    def test_service_name_does_not_equal_owner_principal(self):
        with self.assertRaisesRegex(SspiCandidateDenied,"PRINCIPALS_NOT_DISTINCT"):
            replace(PINS,owner_principal=SERVICE)

    def test_no_signing_or_transport_apis(self):
        module=(Path(__file__).resolve().parents[1]/
                "src/sora_commander_next/offline_b04_sspi_handshake_candidate.py").read_text("utf-8")
        for forbidden in ("ctypes.WinDLL", "OpenSCManager", "CreateNamedPipe", "InitializeSecurityContext(",
                          "AcceptSecurityContext(", "VerifySignature(", "subprocess.",
                          "sqlite3.connect", "broker.dispatch", "COMMITTED_FRESH"):
            self.assertNotIn(forbidden,module)

    def test_wrong_thread_cannot_use_session(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.session.observe_step,STEPS[0])
            with self.assertRaisesRegex(SspiCandidateDenied,"CROSS_THREAD_HANDSHAKE"):
                future.result()
        with self.assertRaisesRegex(SspiCandidateDenied,"HANDSHAKE_TERMINAL"):
            self.session.observe_step(STEPS[0])


if __name__=="__main__":
    unittest.main()
