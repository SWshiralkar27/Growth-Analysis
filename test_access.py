"""
test_access.py
Version: Step B6, v0.1.0 (2026-11-09).
Tests for the app's password check (access.py), the secret name in config.py, and a guard that the password
gate stays the first thing main() does in app.py. No Streamlit, network or secrets needed.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import ast
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import access
import config


class PasswordRequiredTests(unittest.TestCase):
    def test_a_password_is_required_only_when_some_text_is_set(self):
        self.assertTrue(access.password_required("open sesame"))
        self.assertTrue(access.password_required("  x  "))
        for unset in (None, "", "   ", "\n\t", 5, True, ["x"], {}):
            self.assertFalse(access.password_required(unset), repr(unset))


class PasswordOkTests(unittest.TestCase):
    EXPECTED = "Correct Horse Battery 42!"

    def test_the_right_password_is_accepted(self):
        self.assertTrue(access.password_ok(self.EXPECTED, self.EXPECTED))

    def test_blank_spaces_at_the_ends_are_ignored_on_both_sides(self):
        self.assertTrue(access.password_ok(self.EXPECTED + " ", self.EXPECTED))
        self.assertTrue(access.password_ok("  " + self.EXPECTED + "\n", self.EXPECTED))
        self.assertTrue(access.password_ok(self.EXPECTED, "  " + self.EXPECTED + " "))

    def test_it_is_case_sensitive_and_spaces_inside_count(self):
        self.assertFalse(access.password_ok(self.EXPECTED.lower(), self.EXPECTED))
        self.assertFalse(access.password_ok("Correct Horse  Battery 42!", self.EXPECTED))
        self.assertFalse(access.password_ok("CorrectHorseBattery42!", self.EXPECTED))

    def test_wrong_empty_and_odd_entries_are_refused(self):
        for bad in ("wrong", "", "   ", None, 0, 12345, ["x"], self.EXPECTED[:-1], self.EXPECTED + "x"):
            self.assertFalse(access.password_ok(bad, self.EXPECTED), repr(bad))

    def test_with_no_password_set_nothing_is_accepted_here(self):
        # the app does not call password_ok when no password is set (it opens as before)
        for unset in (None, "", "  "):
            self.assertFalse(access.password_ok("anything", unset))
            self.assertFalse(access.password_ok("", unset))

    def test_unicode_passwords_work_and_unencodable_text_does_not_raise(self):
        self.assertTrue(access.password_ok("p\u00e4ssw\u00f6rd \u20b9100", "p\u00e4ssw\u00f6rd \u20b9100"))
        self.assertFalse(access.password_ok("password", "p\u00e4ssword"))
        self.assertFalse(access.password_ok("bad\ud800text", self.EXPECTED))
        self.assertFalse(access.password_ok(self.EXPECTED, "bad\ud800text"))

    def test_the_comparison_is_constant_time(self):
        with mock.patch.object(access.hmac, "compare_digest", wraps=access.hmac.compare_digest) as spy:
            access.password_ok("wrong guess", self.EXPECTED)
        self.assertEqual(spy.call_count, 1)

    def test_the_password_is_never_printed(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            access.password_ok("guess", self.EXPECTED)
            access.password_required(self.EXPECTED)
        self.assertEqual(out.getvalue(), "")


class ModuleShapeTests(unittest.TestCase):
    def test_access_imports_nothing_but_the_standard_library(self):
        with open(os.path.join(HERE, "access.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported - {"__future__", "hmac", "typing"}, set())


class ConfigAndGateTests(unittest.TestCase):
    def test_the_secret_name_and_lookup(self):
        self.assertEqual(config.SECRET_ENV_NAMES["app_password"], "APP_PASSWORD")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.get_secret("app_password"))
            self.assertFalse(config.has_secret("app_password"))
        with mock.patch.dict(os.environ, {"APP_PASSWORD": "pw"}, clear=True):
            self.assertEqual(config.get_secret("app_password"), "pw")

    def test_the_gate_is_the_first_thing_main_does_in_app_py(self):
        with open(os.path.join(HERE, "app.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = main.body[0]
        self.assertIsInstance(first, ast.If)
        text = ast.unparse(first)
        self.assertIn("require_login()", text)
        self.assertIn("return", text)
        # and nothing else in app.py is called at the top level before main(), apart from set_page_config
        calls_at_top = [ast.unparse(n.value) for n in tree.body
                        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
        self.assertEqual(len(calls_at_top), 2, calls_at_top)
        self.assertTrue(calls_at_top[0].startswith("st.set_page_config"), calls_at_top)
        self.assertEqual(calls_at_top[1], "main()")


if __name__ == "__main__":
    unittest.main()
