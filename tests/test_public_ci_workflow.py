"""Check the public Windows CI workflow in either repository layout."""

from pathlib import Path
import re
import unittest


class PublicCIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        source = root / "docs/public_release/github/workflows/tests.yml"
        published = root / ".github/workflows/tests.yml"
        path = source if source.is_file() else published
        cls.workflow = path.read_text(encoding="utf-8")

    def top_level_mapping(self, key):
        lines = self.workflow.splitlines()
        starts = [index for index, line in enumerate(lines) if line.startswith(key + ":")]
        self.assertEqual(len(starts), 1, f"Expected one top-level {key} mapping")
        start = starts[0]
        self.assertEqual(lines[start].rstrip(), key + ":")
        entries = []
        for line in lines[start + 1:]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" "):
                break
            entries.append(line.rstrip())
        return entries

    def test_only_manual_and_pull_request_triggers(self):
        self.assertCountEqual(
            self.top_level_mapping("on"), ["  workflow_dispatch:", "  pull_request:"]
        )

    def test_permissions_only_allow_contents_read(self):
        self.assertEqual(self.top_level_mapping("permissions"), ["  contents: read"])

    def test_job_timeout_is_25_minutes(self):
        self.assertEqual(re.findall(r"(?m)^    timeout-minutes: (.+)$", self.workflow), ["25"])

    def test_no_secret_references(self):
        self.assertNotRegex(self.workflow, r"(?i)\bsecrets\s*(?:\.|\[)")

    def test_checkout_does_not_persist_credentials(self):
        self.assertRegex(
            self.workflow,
            r"(?m)^      - uses: actions/checkout@\S+[^\n]*\n"
            r"        with:\n          persist-credentials: false$",
        )

    def test_public_modules_run_through_dialog_guard(self):
        self.assertRegex(
            self.workflow,
            r'(?m)^      - name: Run the public test modules with the dialog guard\n'
            r'(?:        #[^\n]*\n)*        env:\n'
            r'          SCN_HOSTED_CI: "1"\n        run: \|$',
        )
        self.assertEqual(self.workflow.count("SCN_HOSTED_CI"), 1)
        self.assertRegex(
            self.workflow,
            r"(?m)^ +\.\\\.venv\\Scripts\\python\.exe -B tests\\"
            r"run_with_real_dialog_guard\.py @modules$",
        )

    def test_hosted_ci_marker_is_absent_from_src_and_tools(self):
        root = Path(__file__).resolve().parents[1]
        for directory in ("src", "tools"):
            scan_root = root / directory
            self.assertTrue(scan_root.is_dir())
            matches = [
                path.relative_to(root) for path in sorted(scan_root.rglob("*"))
                if path.is_file() and b"SCN_HOSTED_CI" in path.read_bytes()
            ]
            self.assertEqual(matches, [])


class PublicCIActionPinTests(unittest.TestCase):
    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        root = Path(__file__).resolve().parents[1]
        source = root / "docs/public_release/github/workflows/tests.yml"
        path = source if source.is_file() else root / ".github/workflows/tests.yml"
        uses = re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", path.read_text(encoding="utf-8"))
        self.assertTrue(uses)
        for reference in uses:
            with self.subTest(reference=reference):
                self.assertRegex(reference, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
