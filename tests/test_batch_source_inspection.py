"""Read-only multi-file source inspection and existing-file comparison."""
import asyncio
import sys
from pathlib import Path
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary
import unittest

from sora_commander_next.file_reads import (
    ReadBoundaryError, compare_text_files, read_many_text_files,
)
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import _validate_arguments, build_server


class BatchSourceTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(inherited_test_temp())).resolve()
        # Option B: production _check denies the canonical temp/ fixture ancestor.
        # alias.txt is admitted so the real post-admission st_nlink check runs.
        self.enterContext(mock_read_boundary(self.root,("old.py","new.py","alias.txt")))
        self.before=self.root/"old.py"
        self.after=self.root/"new.py"
        self.before.write_bytes(b"one\ntwo\nthree\n")
        self.after.write_bytes("one\nTWO 日本語\nthree\n".encode("utf-8"))

    def test_hardlinked_files_are_denied_for_batch_and_comparison(self):
        import os
        with inherited_test_temp() as outside:
            foreign = Path(outside) / "private.txt"
            foreign.write_bytes(b"private")
            alias = self.root / "alias.txt"
            os.link(foreign, alias)
            try:
                for operation in (
                    lambda: read_many_text_files([str(alias)], (str(self.root),)),
                    lambda: compare_text_files(str(self.before), str(alias), (str(self.root),)),
                ):
                    with self.assertRaises(ReadBoundaryError):
                        operation()
            finally:
                # Remove only this test's alias before either guarded cleanup.
                if not isinstance(sys.exception(), PermissionError):
                    alias.unlink()

    def test_invalid_utf8_is_denied_for_batch_and_comparison(self):
        self.after.write_bytes(b"\xff")
        with self.assertRaises(ReadBoundaryError):
            read_many_text_files([str(self.before), str(self.after)], (str(self.root),))
        with self.assertRaises(ReadBoundaryError):
            compare_text_files(str(self.before), str(self.after), (str(self.root),))

    def test_batch_and_comparison_reject_swap_after_read(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        real_open = Path.open
        for operation in (
            lambda: read_many_text_files([str(self.before)], (str(self.root),)),
            lambda: compare_text_files(str(self.before), str(self.after), (str(self.root),)),
        ):
            backup = self.root / "saved.txt"
            @contextmanager
            def swapped(path, *args, **kwargs):
                with real_open(path, *args, **kwargs) as stream:
                    yield stream
                if path == self.before and args == ("rb",):
                    path.rename(backup)
                    with real_open(path, "wb") as stream:
                        stream.write(b"replacement")
            with patch.object(Path, "open", swapped), self.assertRaises(ReadBoundaryError):
                operation()
            self.before.unlink()
            backup.rename(self.before)

    def test_two_files_in_single_request_with_hashes(self):
        result=read_many_text_files([str(self.before),str(self.after)],(str(self.root),))
        self.assertEqual(result["count"],2)
        self.assertEqual([f["content"] for f in result["files"]],
                         ["one\ntwo\nthree\n","one\nTWO 日本語\nthree\n"])
        self.assertEqual(len(result["files"][0]["sha256"]),64)

    def test_compare_two_existing_files_no_writes(self):
        original=self.before.read_bytes()
        proposed=self.after.read_bytes()
        result=compare_text_files(str(self.before),str(self.after),(str(self.root),))
        self.assertTrue(result["changed"])
        self.assertIn("-two",result["diff"])
        self.assertIn("+TWO 日本語",result["diff"])
        self.assertFalse(result["diff_truncated"])
        self.assertEqual(self.before.read_bytes(),original)
        self.assertEqual(self.after.read_bytes(),proposed)
        self.assertFalse(compare_text_files(str(self.before),str(self.before),
                                             (str(self.root),))["changed"])

    def test_invalid_batch_and_diff_are_fail_closed(self):
        bad_batches=([],[str(self.before)]*2,
                     [str(self.before)]*9,[str(self.before),42],None)
        for bad in bad_batches:
            with self.subTest(bad=bad),self.assertRaises(ReadBoundaryError):
                read_many_text_files(bad,(str(self.root),))
        for limit in (0,8193,True,"20"):
            with self.subTest(limit=limit),self.assertRaises(ReadBoundaryError):
                read_many_text_files([str(self.before)],(str(self.root),),
                                     max_bytes_per_file=limit)
        for context in (-1,9,True,1.5):
            with self.subTest(context=context),self.assertRaises(ReadBoundaryError):
                compare_text_files(str(self.before),str(self.after),
                                   (str(self.root),),context_lines=context)

    def test_outside_scope_and_file_bound_are_rejected(self):
        with inherited_test_temp() as other:
            outside=Path(other)/"private.txt"
            outside.write_text("hidden",encoding="utf-8")
            with self.assertRaises(ReadBoundaryError):
                read_many_text_files([str(self.before),str(outside)],(str(self.root),))
            with self.assertRaises(ReadBoundaryError):
                compare_text_files(str(self.before),str(outside),(str(self.root),))
        self.before.write_bytes(b"x"*32769)
        with self.assertRaises(ReadBoundaryError):
            compare_text_files(str(self.before),str(self.after),(str(self.root),))
        with self.assertRaises(ReadBoundaryError):
            read_many_text_files([str(self.before)],(str(self.root),))

    def test_both_tools_registered_in_real_mcp_sdk(self):
        h=Host(policy=Policy(roots=(str(self.root),)))
        try:
            sdk=build_server(h)
            for name,args in (
                ("read_many_text_files",{"paths":[str(self.before),str(self.after)]}),
                ("compare_text_files",{"before_path":str(self.before),
                                       "after_path":str(self.after)}),
            ):
                with self.subTest(name=name):
                    _,data=asyncio.run(sdk.call_tool(name,args))
                    self.assertIs(type(data),dict)
                    self.assertTrue(data)
            for name,args in (
                ("read_many_text_files",{"paths":[str(self.before),str(self.before)]}),
                ("read_many_text_files",{"paths":[str(self.before)],"max_bytes_per_file":True}),
                ("compare_text_files",{"before_path":str(self.before),
                                       "after_path":str(self.after),"context_lines":True}),
            ):
                with self.subTest(name=name),self.assertRaises(ValueError):
                    _validate_arguments(name,args)
        finally:
            h.jobs.close()

    def test_default_host_cannot_read_batch_or_diff(self):
        from mcp.server.fastmcp.exceptions import ToolError
        h=Host()
        try:
            sdk=build_server(h)
            for name,args in (
                ("read_many_text_files",{"paths":[str(self.before)]}),
                ("compare_text_files",{"before_path":str(self.before),
                                       "after_path":str(self.after)}),
            ):
                with self.subTest(name=name),self.assertRaises(ToolError):
                    asyncio.run(sdk.call_tool(name,args))
        finally:
            h.jobs.close()


if __name__=="__main__":
    unittest.main()
