import random
import time
import unittest
from shutil import which
from tempfile import TemporaryDirectory

from intuition import lab


def _wait_for(job_manager, job_id, timeout=10):
    deadline = time.time() + timeout
    result = job_manager.output_since(job_id, 0)
    while result["status"] == "running" and time.time() < deadline:
        time.sleep(0.1)
        result = job_manager.output_since(job_id, 0)
    return result


class TestWorkspace(unittest.TestCase):
    def test_new_files_get_input_aware_scaffolds(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("solution.py", "file")
            self.assertIn("def solve():", ws.read("solution.py"))

            ws.create("array.py", "file", "array")
            self.assertEqual(ws.input_kinds()["array.py"], "array")
            self.assertIn("def solve(values):", ws.read("array.py"))
            self.assertIn("sys.stdin.read()", ws.read("array.py"))

            ws.create("Main.java", "file", "matrix")
            self.assertIn("static void solve(int[][] matrix)", ws.read("Main.java"))
            self.assertIn("parseMatrix", ws.read("Main.java"))

    def test_random_input_has_bounded_supported_shapes(self):
        rng = random.Random(7)
        for kind in lab.INPUT_KIND_LABELS:
            sample = lab.generate_input(kind, rng)
            self.assertEqual(sample["kind"], kind)
            if kind == "none":
                self.assertEqual(sample["text"], "")
            else:
                self.assertTrue(sample["text"])
                self.assertTrue(sample["preview"])

    def test_selecting_input_kind_migrates_scaffold_and_keeps_logic(self):
        python_source = lab.scaffold_content("solution.py", "none").replace(
            "    pass", "    return 42")
        migrated_python = lab.migrate_scaffold("solution.py", "array", python_source)
        self.assertIn("def solve(values):", migrated_python)
        self.assertIn("return 42", migrated_python)
        self.assertIn("sys.stdin.read()", migrated_python)
        compile(migrated_python, "solution.py", "exec")

        java_source = lab.scaffold_content("Main.java", "none").replace(
            "        // Write your solution here.",
            '        System.out.println("logic");')
        migrated_java = lab.migrate_scaffold("Main.java", "array", java_source)
        self.assertIn("static void solve(int[] values)", migrated_java)
        self.assertIn('System.out.println("logic");', migrated_java)
        self.assertIn("parseArray", migrated_java)
    def test_create_write_read_delete_rename(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("a.py", "file")
            ws.write("a.py", "print(1)\n")
            ws.set_input_kind("a.py", "array")
            self.assertEqual(ws.read("a.py"), "print(1)\n")
            self.assertEqual(ws.input_kinds()["a.py"], "array")
            ws.create("sub", "dir")
            ws.create("sub/b.py", "file")
            tree = ws.tree()
            names = {entry["name"] for entry in tree}
            self.assertEqual(names, {"a.py", "sub"})
            sub = next(e for e in tree if e["name"] == "sub")
            self.assertEqual([c["name"] for c in sub["children"]], ["b.py"])
            ws.rename("a.py", "renamed.py")
            self.assertTrue(any(e["name"] == "renamed.py" for e in ws.tree()))
            self.assertEqual(ws.input_kinds()["renamed.py"], "array")
            ws.delete("renamed.py")
            self.assertNotIn("renamed.py", ws.input_kinds())
            self.assertEqual([e["name"] for e in ws.tree()], ["sub"])

    def test_rejects_path_traversal(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            with self.assertRaises(lab.WorkspaceError):
                ws.read("../outside.txt")
            with self.assertRaises(lab.WorkspaceError):
                ws.write("../../evil.py", "x")
            with self.assertRaises(lab.WorkspaceError):
                ws.create("..", "dir")

    def test_create_rejects_existing_path(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("a.py", "file")
            with self.assertRaises(lab.WorkspaceError):
                ws.create("a.py", "file")


class TestJobManager(unittest.TestCase):
    def test_run_feeds_selected_random_input_to_stdin(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("echo.py", "file", "array")
            ws.write("echo.py", "import sys\nprint(sys.stdin.read().strip())\n")
            jm = lab.JobManager()
            job = jm.start(ws, "echo.py", "python", "array")
            result = _wait_for(jm, job.id)
            self.assertEqual(result["status"], "exited")
            self.assertEqual(job.input_kind, "array")
            self.assertNotEqual(job.input_preview, "(none)")
            self.assertIn(job.input_preview, [line["text"] for line in result["lines"]])


    def test_python_run_returns_verified_runtime_trace(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("sort.py", "file", "array")
            ws.write("sort.py",
                     "import sys\n"
                     "values = [int(x) for x in sys.stdin.read().split()]\n"
                     "values.sort()\n"
                     "print('RESULT', values)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "sort.py", "python", "array")
            result = _wait_for(jm, job.id)
            trace = result["runtimeTrace"]
            self.assertEqual(result["status"], "exited")
            self.assertEqual(trace["exitCode"], 0)
            self.assertTrue(trace["verified"])
            self.assertEqual(trace["input"], job.input_text)
            self.assertEqual(trace["inputPreview"], job.input_preview)
            self.assertEqual(trace["sourceHash"], job.source_hash)
            self.assertEqual(trace["inputKind"], "array")
            self.assertEqual(trace["model"]["type"], "array")
            self.assertTrue(trace["frames"])
            self.assertIn("RESULT", trace["stdout"])
            expected = sorted(int(token) for token in job.input_text.split())
            self.assertEqual(trace["model"]["finalState"], expected)

    def test_python_run_streams_output_and_exits_cleanly(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("hello.py", "file")
            ws.write("hello.py", "print(2 + 2)\nprint('hi')\n")
            jm = lab.JobManager()
            job = jm.start(ws, "hello.py", "python")
            result = _wait_for(jm, job.id)
            self.assertEqual(result["status"], "exited")
            self.assertEqual(result["exitCode"], 0)
            texts = [line["text"] for line in result["lines"]]
            self.assertIn("4", texts)
            self.assertIn("hi", texts)

    def test_second_run_rejected_while_one_is_active(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("loop.py", "file")
            ws.write("loop.py", "import time\nwhile True:\n    time.sleep(0.05)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "loop.py", "python")
            try:
                with self.assertRaises(lab.WorkspaceError):
                    jm.start(ws, "loop.py", "python")
            finally:
                jm.kill(job.id)
                _wait_for(jm, job.id)

    def test_kill_stops_a_running_process(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("loop.py", "file")
            ws.write("loop.py", "while True:\n    pass\n")
            jm = lab.JobManager()
            job = jm.start(ws, "loop.py", "python")
            time.sleep(0.3)
            self.assertTrue(jm.kill(job.id))
            result = _wait_for(jm, job.id)
            self.assertEqual(result["status"], "killed")
            # kill() flips status before the OS has necessarily finished
            # tearing the process down - on Windows that can briefly leave
            # its cwd handle open, which would race the TemporaryDirectory
            # cleanup below. Give it a moment to actually exit.
            job_obj = jm.get(job.id)
            if job_obj.process is not None:
                job_obj.process.wait(timeout=5)

    def test_kill_drains_output_before_reporting_finished(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("output.py", "file")
            ws.write("output.py",
                     "import time\n"
                     "print('before stop', flush=True)\n"
                     "time.sleep(10)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "output.py", "python")
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                    line["text"] == "before stop"
                    for line in (jm.output_since(job.id, 0) or {}).get("lines", [])):
                time.sleep(0.05)
            self.assertTrue(jm.kill(job.id))
            result = _wait_for(jm, job.id)
            self.assertEqual(result["status"], "killed")
            self.assertIn("before stop", [line["text"] for line in result["lines"]])

    @unittest.skipUnless(which("javac") and which("java"), "no local JDK on PATH")
    def test_java_compiles_and_runs(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("Main.java", "file")
            ws.write("Main.java",
                     "public class Main {\n"
                     "    public static void main(String[] args) {\n"
                     "        System.out.println(\"java ok\");\n"
                     "    }\n"
                     "}\n")
            jm = lab.JobManager()
            job = jm.start(ws, "Main.java", "java")
            result = _wait_for(jm, job.id, timeout=30)
            self.assertEqual(result["status"], "exited")
            self.assertEqual(result["exitCode"], 0)
            self.assertIn("java ok", [line["text"] for line in result["lines"]])

    def test_unsupported_language_rejected(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("a.rb", "file")
            jm = lab.JobManager()
            with self.assertRaises(lab.WorkspaceError):
                jm.start(ws, "a.rb", "ruby")


if __name__ == "__main__":
    unittest.main()
