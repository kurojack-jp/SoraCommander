import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from sora_commander_next.broker_contract import BoundRequest, BrokerAdmission, BrokerRequestDenied


class BrokerEdgeTests(unittest.TestCase):
    def setUp(self):
        self.r = BoundRequest("a"*32, "C:/fixed.exe", (), "D:/repo", "b"*64,
                              "owner", 1000.0, 30.0, 8192)
        self.g = BrokerAdmission(self.r.executable, self.r.cwd, "owner", clock=lambda: 100.0)

    def accept(self, **changes):
        kw = dict(approved_fingerprint=self.r.fingerprint(),
                  current_task_sha256=self.r.task_sha256, owner_verified=True)
        kw.update(changes)
        return self.g.admit(self.r, **kw)

    def test_invalid_digest_type(self):
        with self.assertRaises(BrokerRequestDenied):
            self.accept(current_task_sha256=None)

    def test_invalid_approval_type(self):
        with self.assertRaises(BrokerRequestDenied):
            self.accept(approved_fingerprint=1)

    def test_owner_true_only(self):
        with self.assertRaises(BrokerRequestDenied):
            self.accept(owner_verified=1)

    def test_invalid_owner_binding_nul(self):
        with self.assertRaises(BrokerRequestDenied):
            replace(self.r, owner_binding="owner\0x")

    def test_invalid_path_nul(self):
        with self.assertRaises(BrokerRequestDenied):
            replace(self.r, cwd="D:/repo\0other")

    def test_invalid_expiry_bool(self):
        with self.assertRaises(BrokerRequestDenied):
            replace(self.r, expires_at=True)

    def test_clock_nan(self):
        self.g.clock = lambda: float("nan")
        with self.assertRaises(BrokerRequestDenied):
            self.accept()

    def test_concurrent_gate_one_use(self):
        def attempt(_):
            try:
                self.accept()
                return True
            except BrokerRequestDenied:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(24)))
        self.assertEqual(sum(results), 1)


if __name__ == "__main__":
    unittest.main()
