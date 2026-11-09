import unittest
from dataclasses import replace
from sora_commander_next.broker_contract import BoundRequest, BrokerAdmission, BrokerRequestDenied


class BrokerContractTests(unittest.TestCase):
    def setUp(self):
        self.req = BoundRequest("a"*32, "C:/fixed/powershell.exe", ("-File", "fixed.ps1"),
                                "D:/fixed/repo", "b"*64, "owner-session", 1000.0, 180.0, 8192)
        self.gate = BrokerAdmission(self.req.executable, self.req.cwd, "owner-session", clock=lambda: 100.0)

    def admit(self, request=None, **changes):
        r = request or self.req
        kwargs = dict(approved_fingerprint=r.fingerprint(), current_task_sha256=r.task_sha256, owner_verified=True)
        kwargs.update(changes)
        return self.gate.admit(r, **kwargs)

    def test_approved_once(self):
        self.assertEqual(self.admit()["status"], "ACCEPTED")
        with self.assertRaises(BrokerRequestDenied):
            self.admit()

    def test_expired(self):
        with self.assertRaises(BrokerRequestDenied):
            self.admit(replace(self.req, expires_at=99.0))

    def test_owner_refused(self):
        with self.assertRaises(BrokerRequestDenied):
            self.admit(owner_verified=False)

    def test_task_changed(self):
        with self.assertRaises(BrokerRequestDenied):
            self.admit(current_task_sha256="c"*64)

    def test_approval_changed(self):
        with self.assertRaises(BrokerRequestDenied):
            self.admit(approved_fingerprint="0"*64)

    def test_scope_changed(self):
        with self.assertRaises(BrokerRequestDenied):
            self.admit(replace(self.req, cwd="D:/other"))

    def test_invalid_id(self):
        with self.assertRaises(BrokerRequestDenied):
            replace(self.req, request_id="../")

    def test_capacity(self):
        self.gate.capacity = 1
        self.admit()
        with self.assertRaises(BrokerRequestDenied):
            self.admit(replace(self.req, request_id="c"*32))

    def test_default_capacity_accepts_four_unique_ids_then_denies_fifth(self):
        for request_id in ("a"*32, "b"*32, "c"*32, "d"*32):
            with self.subTest(request_id=request_id):
                self.assertEqual(
                    self.admit(replace(self.req, request_id=request_id)),
                    {"status": "ACCEPTED", "request_id": request_id},
                )
        with self.assertRaisesRegex(BrokerRequestDenied, "^broker capacity reached$"):
            self.admit(replace(self.req, request_id="e"*32))

    def test_capacity_denial_preserves_old_id_replay_protection(self):
        for request_id in ("a"*32, "b"*32, "c"*32, "d"*32):
            self.assertEqual(
                self.admit(replace(self.req, request_id=request_id))["status"],
                "ACCEPTED",
            )
        with self.assertRaisesRegex(BrokerRequestDenied, "^broker capacity reached$"):
            self.admit(replace(self.req, request_id="e"*32))
        # A replay must be recognized as consumed, not merely denied as full.
        for replay in (self.req, replace(self.req, expires_at=1100.0)):
            with self.subTest(expires_at=replay.expires_at):
                with self.assertRaisesRegex(BrokerRequestDenied, "^request already consumed$"):
                    self.admit(replay)

    def test_completed_slot_reused_without_refunding_request_id(self):
        first = [replace(self.req, request_id=f"{i:032x}") for i in range(1, 5)]
        for req in first:
            self.assertEqual(self.admit(req)["status"], "ACCEPTED")
        with self.assertRaisesRegex(BrokerRequestDenied, "broker capacity reached"):
            self.admit(replace(self.req, request_id="a"*32))
        self.assertTrue(self.gate.note_verified_terminal_local(first[0].request_id))
        self.assertFalse(self.gate.note_verified_terminal_local(first[0].request_id))
        self.assertEqual(len(self.gate.active_ids_local()), 3)
        self.assertEqual(self.admit(replace(self.req, request_id="b"*32))["status"],
                         "ACCEPTED")
        with self.assertRaisesRegex(BrokerRequestDenied, "request already consumed"):
            self.admit(first[0])
        self.assertEqual(len(self.gate.active_ids_local()), 4)

    def test_unknown_terminal_cannot_free_any_slot(self):
        self.admit()
        with self.assertRaisesRegex(BrokerRequestDenied, "unknown admission"):
            self.gate.note_verified_terminal_local("f"*32)
        self.assertEqual(self.gate.active_ids_local(), (self.req.request_id,))

    def test_consumed_budget_exhaustion_never_evicts_spent_ids(self):
        from unittest.mock import patch
        self.admit()
        self.gate.note_verified_terminal_local(self.req.request_id)
        with patch("sora_commander_next.broker_contract._MAX_CONSUMED_REQUESTS", 1):
            with self.assertRaisesRegex(BrokerRequestDenied, "broker replay guard full"):
                self.admit(replace(self.req, request_id="f"*32))
            with self.assertRaisesRegex(BrokerRequestDenied, "request already consumed"):
                self.admit()
        self.assertEqual(len(self.gate._accepted), 1)

    def test_bad_limits(self):
        with self.assertRaises(BrokerRequestDenied):
            replace(self.req, timeout=float("nan"))


if __name__ == "__main__":
    unittest.main()
