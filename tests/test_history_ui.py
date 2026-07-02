import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HISTORY_UI_PATH = REPO_ROOT / "history_ui.py"


def _import_history_ui():
    module_name = "history_ui_under_test"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, HISTORY_UI_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec for {HISTORY_UI_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class HistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp_state = tempfile.TemporaryDirectory()
        cls._old_state = os.environ.get("BLOOOP_STATE_DIR")
        os.environ["BLOOOP_STATE_DIR"] = cls._tmp_state.name
        cls.ui = _import_history_ui()

    @classmethod
    def tearDownClass(cls):
        if cls._old_state is None:
            os.environ.pop("BLOOOP_STATE_DIR", None)
        else:
            os.environ["BLOOOP_STATE_DIR"] = cls._old_state
        cls._tmp_state.cleanup()

    def test_paths_honor_state_dir_env(self):
        state = os.path.realpath(self._tmp_state.name)
        for path in (
            self.ui.DB_PATH,
            self.ui.SETTINGS_PATH,
            self.ui.RUNTIME_STATUS_PATH,
            self.ui.COMMAND_PATH,
        ):
            self.assertTrue(os.path.realpath(path).startswith(state), path)

    def test_settings_normalize_preserves_pill_window(self):
        out = self.ui._settings_normalize({"pill_window": False})
        self.assertIs(out["pill_window"], False)
        out = self.ui._settings_normalize({})
        self.assertIs(out["pill_window"], True)

    def test_settings_save_round_trip_keeps_pill_and_vocab(self):
        saved = self.ui._settings_save(
            {"pill_window": False, "custom_vocab": "Blooop\nAcme Widget"}
        )
        self.assertIs(saved["pill_window"], False)
        self.assertEqual(saved["custom_vocab"], ["Blooop", "Acme Widget"])
        # A later save that doesn't mention pill_window must not reset it —
        # this was a real bug: the webview save path dropped unknown keys and
        # silently flipped the pill back on.
        saved = self.ui._settings_save({"auto_paste": False})
        self.assertIs(saved["pill_window"], False)

    def test_webview_form_exposes_vocab_pill_and_clear(self):
        html = self.ui.HTML
        self.assertIn('id="s-vocab"', html)
        self.assertIn('id="s-pill"', html)
        self.assertIn('id="clear-all"', html)
        self.assertIn("clear_history", html)

    def test_command_polling_is_subsecond(self):
        # Menu-bar "Show History"/"Show Settings…" are delivered through the
        # command file; a 2s poll made those clicks feel broken.
        self.assertIn("setInterval(pollCommands, 500)", self.ui.HTML)

    def test_relative_timestamps_rerender(self):
        self.assertIn("Date.now() / 30000", self.ui.HTML)

    def test_clear_rows_helper_exists(self):
        self.assertTrue(callable(self.ui._clear_rows))

    def test_api_exposes_clear_history(self):
        self.assertTrue(callable(getattr(self.ui.API, "clear_history", None)))

    def test_pill_style_normalize_and_form(self):
        out = self.ui._settings_normalize({"pill_style": "spectrogram"})
        self.assertEqual(out["pill_style"], "spectrogram")
        out = self.ui._settings_normalize({"pill_style": "bogus"})
        self.assertEqual(out["pill_style"], "bubbles")
        self.assertIn('id="s-pillstyle"', self.ui.HTML)


if __name__ == "__main__":
    unittest.main()
