"""Recovery across separate Python processes without command replay."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PYTHON=str(ROOT/'.venv'/'Scripts'/'python.exe')
SRC=str(ROOT/'src')

class SeparateProcessRecovery(unittest.TestCase):
    def test_running_becomes_interrupted_unknown_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'journal.json'
            write_code=("import sys;sys.path.insert(0,sys.argv[2]);"
                        "from sora_commander_next.session_journal import write_snapshot;"
                        "write_snapshot(sys.argv[1],[dict(session_id='c'*48,status='RUNNING',"
                        "pid=123,exit_code=None,captured_bytes=0,output_incomplete=False)])")
            load_code=("import sys,json;sys.path.insert(0,sys.argv[2]);"
                       "from sora_commander_next.jobs import JobManager;"
                       "m=JobManager(journal_path=sys.argv[1]);"
                       "print(json.dumps({'old':m.previous_sessions(),'current':m.list_sessions()}));m.close()")
            subprocess.run([PYTHON,'-c',write_code,str(path),SRC],check=True,capture_output=True,timeout=10)
            run=subprocess.run([PYTHON,'-c',load_code,str(path),SRC],check=True,capture_output=True,text=True,timeout=10)
            result=json.loads(run.stdout)
            self.assertEqual(result['old']['sessions'][0]['status'],'INTERRUPTED_UNKNOWN')
            self.assertEqual(result['current']['count'],0)
            self.assertIsNone(result['old']['sessions'][0]['exit_code'])
            self.assertNotIn('argv',path.read_text())
