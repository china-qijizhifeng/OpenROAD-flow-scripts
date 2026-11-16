#!/usr/bin/env python3
"""Tests of util/async_reports_merge.py: the end-of-flow join (--wait) and the metrics merge of ORFS_ASYNC_REPORTS=1."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "util", "async_reports_merge.py"
)

STEP = '{\n\t"cts__flow__warnings__count": 0,\n\t"cts__orfs_async__slot:4_1_cts-01-cts_final": 0,\n\t"cts__flow__warnings__type_count": 0\n}'
JOB = '{\n\t"cts__timing__setup__ws": 0.5,\n\t"cts__flow__warnings__count": 0,\n\t"cts__flow__warnings__type_count": 0\n}'
MERGED = '{\n\t"cts__flow__warnings__count": 0,\n\t"cts__timing__setup__ws": 0.5,\n\t"cts__flow__warnings__type_count": 0\n}'
NAME = "4_1_cts-01-cts_final"


class TestAsyncReportsMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = os.path.join(self.tmp.name, "async_reports")
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.ad)
        os.makedirs(self.logs)
        self.write(os.path.join(self.logs, "4_1_cts.json"), STEP)
        self.write(
            os.path.join(self.ad, NAME + ".params.tcl"), "set ::async(kind) report\n"
        )
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.tmp.cleanup()

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def finish_job(self, status):
        self.write(os.path.join(self.ad, NAME + ".json"), JOB)
        self.write(
            os.path.join(self.ad, NAME + ".log"),
            "ORFS_ASYNC_JOB section_begin\nORFS_ASYNC_JOB section_end\n",
        )
        self.write(os.path.join(self.ad, NAME + ".exit"), f"{status}\n")

    def run_merge(self, *extra):
        return subprocess.run(
            [sys.executable, SCRIPT, *extra, self.ad, self.logs],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def step_json(self):
        with open(os.path.join(self.logs, "4_1_cts.json")) as f:
            return f.read()

    def test_wait_for_running_job_then_merge(self):
        # a live launcher: the join waits until the job writes its exit status
        launcher = subprocess.Popen(["sleep", "30"])
        self.procs.append(launcher)
        self.write(os.path.join(self.ad, NAME + ".pid"), f"{launcher.pid}\n")
        t0 = time.time()
        merge = subprocess.Popen(
            [sys.executable, SCRIPT, "--wait", self.ad, self.logs],
            stdout=subprocess.PIPE,
            text=True,
        )
        time.sleep(1.5)
        self.assertIsNone(merge.poll())
        self.finish_job(0)
        out, _ = merge.communicate(timeout=60)
        self.assertEqual(merge.returncode, 0, out)
        self.assertGreater(time.time() - t0, 1.5)
        self.assertIn("ASYNC_JOIN", out)
        self.assertIn("jobs=1 failed=0", out)
        self.assertEqual(self.step_json(), MERGED)
        # a second run finds no placeholders and leaves the file as it is
        again = self.run_merge("--wait")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(self.step_json(), MERGED)

    def test_failed_job_fails(self):
        self.finish_job(1)
        r = self.run_merge("--wait")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"ASYNC_JOB_FAILED {NAME} exit=1", r.stdout)
        self.assertIn("failed=1", r.stdout)

    def test_launcher_gone_without_status_fails(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.write(os.path.join(self.ad, NAME + ".pid"), f"{gone.pid}\n")
        r = self.run_merge("--wait")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("launcher gone without an exit status", r.stdout)
        self.assertIn(f"cts__orfs_async__slot:{NAME}", self.step_json())

    def test_no_job_directory(self):
        r = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--wait",
                os.path.join(self.tmp.name, "none"),
                self.logs,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.step_json(), STEP)


if __name__ == "__main__":
    unittest.main()
