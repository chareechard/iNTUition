import os
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

            ws.create("main.c", "file", "array")
            self.assertEqual(ws.input_kinds()["main.c"], "array")
            self.assertIn("void solve(int *values, int n)", ws.read("main.c"))
            self.assertIn("scanf", ws.read("main.c"))

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

        c_source = lab.scaffold_content("main.c", "none").replace(
            "    // Write your solution here.", '    printf("logic\\n");')
        migrated_c = lab.migrate_scaffold("main.c", "array", c_source)
        self.assertIn("void solve(int *values, int n)", migrated_c)
        self.assertIn('printf("logic\\n");', migrated_c)
        self.assertIn("#include <stdlib.h>", migrated_c)
        self.assertIn('scanf("%d"', migrated_c)

    def test_c_scaffolds_are_balanced_and_carry_a_solve_and_stdin_reader(self):
        for kind in lab.INPUT_KIND_LABELS:
            source = lab.scaffold_content("main.c", kind)
            self.assertEqual(source.count("{"), source.count("}"), kind)
            self.assertIn("int main(void)", source)
            self.assertRegex(source, r"\bsolve\s*\(")
            if kind != "none":
                self.assertTrue("scanf" in source or "fgets" in source, kind)
        self.assertIn("void solve(void)", lab.scaffold_content("main.c", "none"))
        self.assertIn("int **matrix", lab.scaffold_content("main.c", "matrix"))
        self.assertIn("typedef struct", lab.scaffold_content("main.c", "graph"))
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

    def test_reorder_persists_and_survives_new_files(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            for name in ("a.py", "b.py", "c.py"):
                ws.create(name, "file")
            ws.reorder("", ["c.py", "a.py", "b.py"])
            self.assertEqual([e["name"] for e in ws.tree()], ["c.py", "a.py", "b.py"])
            # a file created afterwards lands at the end, not alphabetically
            ws.create("d.py", "file")
            self.assertEqual([e["name"] for e in ws.tree()],
                             ["c.py", "a.py", "b.py", "d.py"])
            # deleting one drops it from the order, the rest keep their places
            ws.delete("a.py")
            self.assertEqual([e["name"] for e in ws.tree()], ["c.py", "b.py", "d.py"])

    def test_reorder_is_scoped_to_one_folder(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("sub", "dir")
            for name in ("sub/x.py", "sub/y.py"):
                ws.create(name, "file")
            ws.reorder("sub", ["y.py", "x.py"])
            sub = next(e for e in ws.tree() if e["name"] == "sub")
            self.assertEqual([c["name"] for c in sub["children"]], ["y.py", "x.py"])

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

    def test_nested_folders_hold_mixed_file_types(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("docs/design", "dir")            # nested parents in one call
            ws.create("docs/design/notes.md", "file")
            ws.create("docs/data.json", "file")
            ws.create("src/main.py", "file", "array")
            docs = next(e for e in ws.tree() if e["name"] == "docs")
            self.assertEqual({c["name"] for c in docs["children"]},
                             {"design", "data.json"})
            self.assertEqual(next(c for c in docs["children"]
                                  if c["name"] == "data.json")["ext"], "json")
            # a plain document starts empty and never gets a randomized-input slot
            self.assertEqual(ws.read("docs/design/notes.md"), "")
            self.assertNotIn("docs/design/notes.md", ws.input_kinds())
            self.assertEqual(ws.input_kinds()["src/main.py"], "array")

    def test_windows_naming_rules_are_enforced(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            for bad in ("na:me.py", "pipe|.txt", "star*.md", 'quote".py',
                        "trailing.", "trailing ", "CON", "con.txt", "lpt1.log"):
                with self.assertRaises(lab.WorkspaceError):
                    ws.create(bad, "file")
            ws.create("Main.py", "file")
            with self.assertRaises(lab.WorkspaceError):
                ws.create("main.py", "file")   # case-insensitive collision
            ws.create("keep.py", "file")
            with self.assertRaises(lab.WorkspaceError):
                ws.rename("keep.py", "bad:name.py")

    def test_binary_files_are_not_opened_in_the_editor(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("blob.dat", "file")
            with open(ws.abs_path("blob.dat"), "wb") as handle:
                handle.write(b"PK\x03\x04\x00\x00binary")
            with self.assertRaises(lab.WorkspaceError):
                ws.read("blob.dat")


class TestLabRepos(unittest.TestCase):
    def test_starts_with_a_default_repository(self):
        with TemporaryDirectory() as root:
            repos = lab.LabRepos(root)
            self.assertEqual(repos.names(), [lab.DEFAULT_REPO_NAME])
            self.assertEqual(repos.default_name(), lab.DEFAULT_REPO_NAME)

    def test_migrates_a_pre_repositories_flat_workspace_once(self):
        with TemporaryDirectory() as root:
            legacy = os.path.join(root, ".intuition", "lab", "workspace")
            os.makedirs(legacy)
            with open(os.path.join(legacy, "old.py"), "w") as handle:
                handle.write("print('hi')\n")
            repos = lab.LabRepos(root)
            self.assertEqual(repos.names(), [lab.DEFAULT_REPO_NAME])
            tree = repos.workspace(lab.DEFAULT_REPO_NAME).tree()
            self.assertIn("old.py", [entry["name"] for entry in tree])
            self.assertFalse(os.path.exists(legacy))

    def test_repositories_are_isolated(self):
        with TemporaryDirectory() as root:
            repos = lab.LabRepos(root)
            repos.create("alpha")
            repos.create("beta")
            repos.workspace("alpha").create("a.py", "file")
            self.assertEqual([e["name"] for e in repos.workspace("beta").tree()], [])
            names = {r["name"] for r in repos.list()}
            self.assertEqual(names, {lab.DEFAULT_REPO_NAME, "alpha", "beta"})
            self.assertEqual(next(r["fileCount"] for r in repos.list() if r["name"] == "alpha"), 1)

    def test_rename_and_delete_and_always_keep_one(self):
        with TemporaryDirectory() as root:
            repos = lab.LabRepos(root)
            repos.create("keep")
            repos.rename("keep", "kept")
            self.assertTrue(repos.exists("kept"))
            self.assertFalse(repos.exists("keep"))
            repos.delete("kept")
            repos.delete(lab.DEFAULT_REPO_NAME)  # removing the last one recreates the default
            self.assertEqual(repos.names(), [lab.DEFAULT_REPO_NAME])

    def test_rejects_unsafe_repository_names(self):
        with TemporaryDirectory() as root:
            repos = lab.LabRepos(root)
            for bad in ("../evil", "a/b", "..", "", ".", "x" * 100):
                with self.assertRaises(lab.WorkspaceError):
                    repos.create(bad)

    def test_move_file_between_repositories_carries_metadata(self):
        with TemporaryDirectory() as root:
            repos = lab.LabRepos(root)
            repos.create("src")
            repos.create("dst")
            src = repos.workspace("src")
            src.create("array.py", "file", "array")
            src.create("other.py", "file")
            src.reorder("", ["other.py", "array.py"])

            repos.move_file("src", "array.py", "dst")

            self.assertEqual([e["name"] for e in src.tree()], ["other.py"])
            dst = repos.workspace("dst")
            self.assertEqual([e["name"] for e in dst.tree()], ["array.py"])
            self.assertEqual(dst.input_kinds().get("array.py"), "array")
            self.assertNotIn("array.py", src.input_kinds())

    def test_move_file_refuses_to_overwrite(self):
        with TemporaryDirectory() as root:
            repos = lab.LabRepos(root)
            repos.create("src")
            repos.create("dst")
            repos.workspace("src").create("main.c", "file")
            repos.workspace("dst").create("main.c", "file")
            with self.assertRaises(lab.WorkspaceError):
                repos.move_file("src", "main.c", "dst")

    def test_resolve_falls_back_to_default_for_unknown_repo(self):
        with TemporaryDirectory() as root:
            repos = lab.LabRepos(root)
            ws, name = repos.resolve("was-deleted")
            self.assertEqual(name, lab.DEFAULT_REPO_NAME)
            self.assertIsInstance(ws, lab.Workspace)


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

    def test_python_trace_frames_carry_code_by_code_context(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("run.py", "file")
            ws.write("run.py",
                     "def double(n):\n"
                     "    result = n * 2\n"
                     "    return result\n"
                     "total = 0\n"
                     "for i in range(3):\n"
                     "    total = double(i) + total\n"
                     "print(total)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "run.py", "python", "none")
            result = _wait_for(jm, job.id)
            trace = result["runtimeTrace"]
            self.assertEqual(result["status"], "exited")
            self.assertEqual(trace["version"], 3)
            frames = trace["frames"]
            self.assertTrue(frames)
            # Every frame is a step: a line, a phase, its function, an activation
            # id, a stack and locals.
            for frame in frames:
                self.assertIn(frame["phase"], ("call", "line", "return", "exception"))
                self.assertIsInstance(frame["func"], str)
                self.assertIsInstance(frame["frame"], int)
                self.assertIsInstance(frame["stack"], list)
                self.assertIsInstance(frame["locals"], dict)
            # Stepping into double() shows the parameter, then the computed local.
            in_double = [f for f in frames if f["func"] == "double"]
            self.assertTrue(any("n" in f["locals"] for f in in_double))
            self.assertTrue(any(f["locals"].get("result") == 2 for f in in_double))
            # The module frame accumulates `total` and nests double() on its stack.
            self.assertTrue(any(f["stack"][-2:] == ["<module>", "double"] for f in in_double))
            self.assertTrue(any(f["locals"].get("total") == 6
                                for f in frames if f["func"] == "<module>"))
            # Each call of double() is a distinct activation, and returns show
            # what the call produced.
            returns = [f for f in in_double if f["phase"] == "return"]
            self.assertEqual([f["ret"] for f in returns], [0, 2, 4])
            self.assertEqual(len({f["frame"] for f in returns}), 3)

    def test_python_trace_records_exception_line_and_message(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("boom.py", "file")
            ws.write("boom.py",
                     "def take(xs):\n"
                     "    return xs[9]\n"
                     "take([1, 2, 3])\n")
            jm = lab.JobManager()
            job = jm.start(ws, "boom.py", "python", "none")
            trace = _wait_for(jm, job.id)["runtimeTrace"]
            self.assertFalse(trace["verified"])
            crash = next(f for f in trace["frames"] if f["phase"] == "exception")
            self.assertEqual(crash["line"], 2)
            self.assertEqual(crash["exc"], "IndexError")
            self.assertIn("range", crash["excMsg"])

    def test_python_trace_visualises_graph_dfs(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("dfs.py", "file")
            ws.write("dfs.py",
                     "graph = {'A': ['B', 'C'], 'B': ['D'], 'C': [], 'D': []}\n"
                     "visited = set()\n"
                     "def dfs(node):\n"
                     "    visited.add(node)\n"
                     "    for nxt in graph[node]:\n"
                     "        if nxt not in visited:\n"
                     "            dfs(nxt)\n"
                     "dfs('A')\n")
            jm = lab.JobManager()
            job = jm.start(ws, "dfs.py", "python", "none")
            result = _wait_for(jm, job.id)
            trace = result["runtimeTrace"]
            self.assertEqual(result["status"], "exited")
            # An adjacency map is recognised and folded into a drawable graph.
            self.assertEqual(trace["model"]["type"], "graph")
            model = trace["model"]["initialState"]
            self.assertEqual(sorted(model["nodes"]), ["A", "B", "C", "D"])
            self.assertIn({"from": "A", "to": "B"}, model["edges"])
            # The visited set is tracked frame by frame so the canvas can light
            # up reached nodes, ending with every node reached.
            reached = [set(f["highlight"]) for f in trace["frames"] if "highlight" in f]
            self.assertTrue(reached)
            self.assertEqual(reached[-1], {"A", "B", "C", "D"})

    def test_python_trace_detects_table_and_linked_list(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("count.py", "file")
            ws.write("count.py",
                     "text = 'mississippi'\n"
                     "freq = {}\n"
                     "for ch in text:\n"
                     "    freq[ch] = freq.get(ch, 0) + 1\n"
                     "print(freq)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "count.py", "python", "none")
            trace = _wait_for(jm, job.id)["runtimeTrace"]
            self.assertEqual(trace["model"]["type"], "map")
            self.assertEqual(dict(trace["model"]["finalState"]["pairs"]),
                             {"m": 1, "i": 4, "s": 4, "p": 2})
            # The table grows one key/count at a time across the frames.
            tables = [f["state"]["pairs"] for f in trace["frames"] if f.get("k") == "map"]
            self.assertTrue(any(len(pairs) == 1 for pairs in tables))
            self.assertLess(len(tables[0]), len(tables[-1]))

        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("rev.py", "file")
            ws.write("rev.py",
                     "class Node:\n"
                     "    def __init__(self, v, nxt=None):\n"
                     "        self.val = v\n"
                     "        self.next = nxt\n"
                     "head = Node(1, Node(2, Node(3)))\n"
                     "def reverse(node):\n"
                     "    prev = None\n"
                     "    while node:\n"
                     "        node.next, prev, node = prev, node, node.next\n"
                     "    return prev\n"
                     "head = reverse(head)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "rev.py", "python", "none")
            trace = _wait_for(jm, job.id)["runtimeTrace"]
            self.assertEqual(trace["model"]["type"], "linked")
            chains = [f["state"]["cells"] for f in trace["frames"] if f.get("k") == "linked"]
            self.assertIn(["1", "2", "3"], chains)
            self.assertEqual(chains[-1], ["3", "2", "1"])

    def test_python_trace_reads_a_deque_as_a_sequence(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("bfs.py", "file")
            ws.write("bfs.py",
                     "from collections import deque\n"
                     "queue = deque([1])\n"
                     "seen = []\n"
                     "while queue:\n"
                     "    x = queue.popleft()\n"
                     "    seen.append(x)\n"
                     "    if x < 4:\n"
                     "        queue.append(x + 1)\n"
                     "        queue.append(x + 2)\n"
                     "print(seen)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "bfs.py", "python", "none")
            trace = _wait_for(jm, job.id)["runtimeTrace"]
            self.assertEqual(trace["model"]["type"], "array")
            arrays = [f["state"] for f in trace["frames"] if f.get("k") == "array"]
            # The deque is serialised as a plain list, never a repr string.
            self.assertTrue(all(isinstance(state, list) for state in arrays))
            self.assertTrue(any(len(state) > 1 for state in arrays))

    def test_python_trace_peels_a_wrapper_class_down_to_its_collection(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("stack.py", "file")
            ws.write("stack.py",
                     "class Stack:\n"
                     "    def __init__(self):\n"
                     "        self.items = []\n"
                     "        self.size = 0\n"
                     "    def push(self, v):\n"
                     "        self.items.append(v)\n"
                     "        self.size += 1\n"
                     "    def pop(self):\n"
                     "        self.size -= 1\n"
                     "        return self.items.pop()\n"
                     "s = Stack()\n"
                     "for n in [3, 1, 4, 1, 5]:\n"
                     "    s.push(n)\n"
                     "while s.size:\n"
                     "    s.pop()\n")
            jm = lab.JobManager()
            job = jm.start(ws, "stack.py", "python", "none")
            trace = _wait_for(jm, job.id)["runtimeTrace"]
            self.assertEqual(trace["model"]["type"], "array")
            arrays = [f["state"] for f in trace["frames"] if f.get("k") == "array"]
            self.assertIn([3, 1, 4, 1, 5], arrays)
            # The backing list is what animates, not "<Stack object at 0x...>".
            self.assertTrue(all(isinstance(state, list) for state in arrays))

    def test_python_trace_reads_a_dp_table_while_it_is_being_filled(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("dp.py", "file")
            ws.write("dp.py",
                     "rows, cols = 3, 4\n"
                     "dp = [None] * rows\n"
                     "for r in range(rows):\n"
                     "    dp[r] = [0] * cols\n"
                     "    for c in range(cols):\n"
                     "        dp[r][c] = r + c\n"
                     "print(dp)\n")
            jm = lab.JobManager()
            job = jm.start(ws, "dp.py", "python", "none")
            trace = _wait_for(jm, job.id)["runtimeTrace"]
            # A half-built table (some rows still None) is already a matrix.
            self.assertEqual(trace["model"]["type"], "matrix")
            matrices = [f["state"] for f in trace["frames"] if f.get("k") == "matrix"]
            self.assertTrue(any(None in state for state in matrices))

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

    @unittest.skipUnless(any(which(c) for c in lab.C_COMPILER_CANDIDATES),
                         "no local C compiler on PATH")
    def test_c_compiles_and_runs_with_generated_stdin(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("sum.c", "file", "array")
            ws.write("sum.c",
                     "#include <stdio.h>\n"
                     "#include <stdlib.h>\n"
                     "void solve(int *values, int n) {\n"
                     "    long total = 0;\n"
                     "    for (int i = 0; i < n; i++) total += values[i];\n"
                     "    printf(\"SUM %ld\\n\", total);\n"
                     "}\n"
                     "int main(void) {\n"
                     "    int cap = 16, n = 0, *values = malloc(cap * sizeof(int));\n"
                     "    while (values && scanf(\"%d\", &values[n]) == 1)\n"
                     "        if (++n == cap) values = realloc(values, (cap *= 2) * sizeof(int));\n"
                     "    solve(values, n);\n"
                     "    free(values);\n"
                     "    return 0;\n"
                     "}\n")
            jm = lab.JobManager()
            job = jm.start(ws, "sum.c", "c", "array")
            result = _wait_for(jm, job.id, timeout=30)
            self.assertEqual(result["status"], "exited")
            self.assertEqual(result["exitCode"], 0)
            expected = sum(int(token) for token in job.input_text.split())
            self.assertIn("SUM {}".format(expected),
                          [line["text"] for line in result["lines"]])
            # the hidden build dir must not leak into the visible file tree
            self.assertEqual({e["name"] for e in ws.tree()}, {"sum.c"})

    @unittest.skipUnless(any(which(c) for c in lab.C_COMPILER_CANDIDATES),
                         "no local C compiler on PATH")
    def test_c_compile_error_is_reported_not_run(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("bad.c", "file")
            ws.write("bad.c", "int main(void) { return x; }\n")
            jm = lab.JobManager()
            job = jm.start(ws, "bad.c", "c")
            result = _wait_for(jm, job.id, timeout=30)
            self.assertEqual(result["status"], "error")
            self.assertNotEqual(result["exitCode"], 0)
            self.assertTrue(any("x" in line["text"]
                                for line in result["lines"] if line["stream"] == "stderr"))

    def test_unsupported_language_rejected(self):
        with TemporaryDirectory() as root:
            ws = lab.Workspace(root)
            ws.create("a.rb", "file")
            jm = lab.JobManager()
            with self.assertRaises(lab.WorkspaceError):
                jm.start(ws, "a.rb", "ruby")


if __name__ == "__main__":
    unittest.main()
