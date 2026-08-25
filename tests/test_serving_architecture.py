"""Stage 8 guardrails for the one event-loop-native serving path."""
import ast
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVING_MODULES = (
    "app.py", "harness.py", "query_context.py", "source_clients.py",
    "ard_client.py", "llm.py", "driver.py", "resolver.py", "bq.py",
    "college.py", "fema.py", "nonprofit.py", "orgprofile.py",
    "accessor/okf_fetch.py",
)


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


class ServingArchitectureTests(unittest.TestCase):
    def trees(self):
        for relative in SERVING_MODULES:
            path = os.path.join(ROOT, relative)
            with open(path, encoding="utf-8") as source:
                yield relative, ast.parse(source.read(), filename=relative)

    def test_engine_has_one_canonical_async_entry(self):
        tree = dict(self.trees())["harness.py"]
        functions = {node.name: node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIsInstance(functions.get("run"), ast.AsyncFunctionDef)
        for deleted in ("run_async", "run_nlweb", "serve", "retrieve_for_async"):
            self.assertNotIn(deleted, functions)

    def test_async_serving_code_has_no_process_exit_or_blocking_primitives(self):
        banned_calls = {
            "time.sleep", "subprocess.run", "subprocess.call", "subprocess.Popen",
            "subprocess.check_call", "subprocess.check_output", "urllib.request.urlopen",
            "requests.get", "requests.post", "requests.request", "OpenAI", "AzureOpenAI",
            "ThreadPoolExecutor", "ThreadingHTTPServer",
        }
        failures = []
        for relative, tree in self.trees():
            for function in (node for node in ast.walk(tree)
                             if isinstance(node, ast.AsyncFunctionDef)):
                for node in ast.walk(function):
                    raised = node.exc.func if isinstance(node, ast.Raise) and isinstance(
                        node.exc, ast.Call) else (node.exc if isinstance(node, ast.Raise) else None)
                    if raised is not None and dotted(raised).endswith("SystemExit"):
                        failures.append(f"{relative}:{node.lineno} raises SystemExit")
                    if isinstance(node, ast.Call) and dotted(node.func) in banned_calls:
                        failures.append(f"{relative}:{node.lineno} calls {dotted(node.func)}")
        self.assertEqual(failures, [])

    def test_core_serving_modules_do_not_import_thread_runtime(self):
        failures = []
        for relative in (
                "app.py", "harness.py", "query_context.py", "source_clients.py", "runtime.py",
                "llm.py", "ard_client.py", "driver.py", "resolver.py", "grants.py"):
            path = os.path.join(ROOT, relative)
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), filename=relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    failures.extend(f"{relative}:{node.lineno} imports {alias.name}"
                                    for alias in node.names if alias.name in {"threading", "subprocess"})
                elif isinstance(node, ast.ImportFrom) and node.module in {
                        "threading", "subprocess", "concurrent.futures", "http.server"}:
                    failures.append(f"{relative}:{node.lineno} imports from {node.module}")
        self.assertEqual(failures, [])

    def test_launcher_uses_only_asgi_server(self):
        with open(os.path.join(ROOT, "run.sh"), encoding="utf-8") as source:
            script = source.read()
        self.assertIn("uvicorn app:app", script)
        self.assertNotIn("harness.py --serve", script)


if __name__ == "__main__":
    unittest.main()
