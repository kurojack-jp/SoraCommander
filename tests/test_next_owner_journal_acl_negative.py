"""Existing-file ACL must fail closed on a disposable Owner-only folder."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.stdio_main import owner_journal_path
from sora_commander_next.session_journal import write_snapshot

@unittest.skipUnless(os.name=="nt","Windows ACL tests")
class FileAclNegativeTests(unittest.TestCase):
    def test_extra_file_ace_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/"protected"
            directory.mkdir()
            owner=subprocess.run(["whoami","/user"],capture_output=True,text=True,check=True).stdout
            import ctypes
            # Use icacls only on the new disposable directory, never on real Owner storage.
            sid=owner.split("S-1-5-21-")[1].split()[0]
            sid="S-1-5-21-"+sid
            def acl(*args):
                return subprocess.run(["icacls",str(directory),*args],capture_output=True,text=True)
            self.assertEqual(acl("/inheritance:r").returncode,0)
            for principal in ("*"+sid,"*S-1-5-18","*S-1-5-32-544"):
                self.assertEqual(acl("/grant:r",principal+":(OI)(CI)F").returncode,0)
            journal=directory/"owner-session-journal.json"
            # Owner storage requires explicit protected stage/lock ACLs.
            # Plain write_snapshot (unprotected DEV mode) must not pass this check.
            write_snapshot(journal, [], protected_stage=True)
            self.assertEqual(owner_journal_path(storage_override=directory),journal)
            bad=subprocess.run(["icacls",str(journal),"/grant","*S-1-5-11:(R)"],capture_output=True,text=True)
            self.assertEqual(bad.returncode,0,bad.stderr)
            with self.assertRaises(RuntimeError):
                owner_journal_path(storage_override=directory)
