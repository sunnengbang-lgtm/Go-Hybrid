"""Offline CLI regressions for the differential harness, using a fake builder.

These exercise harness decisions, not Hybrid compiler correctness.
Run: python3 -m unittest discover -s tools -p test_differential_loop.py -v
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


HARNESS = pathlib.Path(__file__).with_name("differential_loop.py")


class RuntimeHazardTests(unittest.TestCase):
    def run_case(self, stderr="", stdout="", exit_code=2,
                 hazard="concurrent map writes (Go fatal, pre-registered)"):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            corpus = root / "corpus"
            corpus.mkdir()
            source = "// EXPECTED-VERDICT: ACCEPT\nfn main() {}\n"
            if hazard is not None:
                source += f"// RUNTIME-HAZARD: {hazard}\n"
            (corpus / "01-case.hy").write_text(source, encoding="utf-8")
            program = (f"#!{sys.executable}\nimport sys\n"
                       f"sys.stderr.write({stderr!r})\n"
                       f"sys.stdout.write({stdout!r})\n"
                       f"sys.exit({exit_code})\n")
            builder = root / "hyb"
            builder.write_text(
                f"#!{sys.executable}\nimport pathlib\n"
                f"p = pathlib.Path('a.out')\np.write_text({program!r})\n"
                "p.chmod(0o755)\n", encoding="utf-8")
            builder.chmod(0o755)
            return subprocess.run(
                [sys.executable, str(HARNESS), "--bin", str(builder),
                 "--corpus", str(corpus)],
                capture_output=True, text=True, timeout=10,
            )

    def assert_result(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual("FULL AGREEMENT" in result.stdout, code == 0)

    def test_registered_fatal_is_allowed(self):
        self.assert_result(self.run_case(stderr="fatal error: concurrent map writes\n"), 0)

    def test_registered_fatal_before_long_stack_is_allowed(self):
        self.assert_result(self.run_case(
            stderr="fatal error: concurrent map writes\n" + "stack frame\n" * 100), 0)

    def test_unrelated_panic_fails(self):
        self.assert_result(self.run_case(stderr="panic: runtime error: index out of range\n"), 1)

    def test_unrelated_fatal_fails(self):
        self.assert_result(self.run_case(stderr="fatal error: all goroutines are asleep - deadlock!\n"), 1)

    def test_hazard_words_in_stdout_do_not_allow_unrelated_failure(self):
        self.assert_result(self.run_case(
            stdout="concurrent map writes", stderr="panic: unexpected nil pointer\n"), 1)

    def test_unregistered_fatal_fails(self):
        self.assert_result(self.run_case(
            stderr="fatal error: concurrent map writes\n", hazard=None), 1)

    def test_empty_normalized_hazard_does_not_match(self):
        self.assert_result(self.run_case(
            stderr="panic: unexpected nil pointer\n", hazard="(explanation only)"), 1)

    def test_successful_program_is_allowed(self):
        self.assert_result(self.run_case(exit_code=0), 0)


class CorpusPathTests(unittest.TestCase):
    def run_case(self, relative_corpus, relative_bin=False):
        spec = importlib.util.spec_from_file_location("differential", HARNESS)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary).resolve()
            corpus = root / "corpus with spaces"
            corpus.mkdir()
            source = corpus / "01-case.hy"
            source.write_text(
                "// EXPECTED-VERDICT: ACCEPT\nfn main() {}\n", encoding="utf-8")
            builder = root / "hyb"
            builder.touch()
            workspaces = []

            def fake_run(cmd, **kwargs):
                cwd = pathlib.Path(kwargs["cwd"])
                self.assertNotEqual(cwd, root)
                if cmd[1:2] == ["build"]:
                    self.assertEqual(pathlib.Path(cmd[0]), builder)
                    # Model the builder opening its input from the subprocess
                    # cwd, rather than assuming arguments are already absolute.
                    if not (cwd / cmd[2]).is_file():
                        return subprocess.CompletedProcess(
                            cmd, 1, "", f"source not found: {cmd[2]}")
                    (cwd / "a.out").touch()
                    workspaces.append(cwd)
                else:
                    self.assertEqual(cmd, [str(workspaces[-1] / "a.out")])
                return subprocess.CompletedProcess(cmd, 0, "", "")

            args = [str(HARNESS), "--bin",
                    builder.name if relative_bin else str(builder),
                    "--corpus", corpus.name if relative_corpus else str(corpus)]
            previous_cwd = os.getcwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with patch.object(sys, "argv", args), \
                     patch.object(harness.subprocess, "run", side_effect=fake_run) as run, \
                     contextlib.redirect_stdout(output):
                    status = harness.main()
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(status, 0, output.getvalue())
            self.assertIn("differential: FULL AGREEMENT", output.getvalue())
            self.assertEqual(run.call_count, 2)

    def test_relative_corpus_builds_and_runs(self):
        self.run_case(relative_corpus=True)

    def test_absolute_corpus_still_builds_and_runs(self):
        self.run_case(relative_corpus=False)

    def test_relative_builder_and_corpus_build_and_run(self):
        self.run_case(relative_corpus=True, relative_bin=True)


if __name__ == "__main__":
    unittest.main()
