import time
import unittest
from shutil import which
from tempfile import TemporaryDirectory

from ntu_learn_downloader import lab


def _wait_for(job_manager, job_id, timeout=10):
    deadline = time.time() + timeout
    result = job_manager.output_since(job_id, 0)
    while result["status"] == "running" and time.time() < deadline:
        time.sleep(0.1)
        result = job_manager.output_since(job_id, 0)
    return result


class TestWorkspace(unittest.TestCase):
    def test_create_write_read_delete_rename(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("a.py", "file")
            ws.write("a.py", "print(1)\n")
            self.assertEqual(ws.read("a.py"), "print(1)\n")
            ws.create("sub", "dir")
            ws.create("sub/b.py", "file")
            tree = ws.tree()
            names = {entry["name"] for entry in tree}
            self.assertEqual(names, {"a.py", "sub"})
            sub = next(e for e in tree if e["name"] == "sub")
            self.assertEqual([c["name"] for c in sub["children"]], ["b.py"])
            ws.rename("a.py", "renamed.py")
            self.assertTrue(any(e["name"] == "renamed.py" for e in ws.tree()))
            ws.delete("renamed.py")
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
