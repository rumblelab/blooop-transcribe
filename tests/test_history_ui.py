import importlib.util
import json
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

    def test_mic_sensitivity_normalize_and_form(self):
        out = self.ui._settings_normalize({"mic_sensitivity": "high"})
        self.assertEqual(out["mic_sensitivity"], "high")
        out = self.ui._settings_normalize({"mic_sensitivity": "low"})
        self.assertEqual(out["mic_sensitivity"], "low")
        out = self.ui._settings_normalize({"mic_sensitivity": "bogus"})
        self.assertEqual(out["mic_sensitivity"], "normal")
        out = self.ui._settings_normalize({})
        self.assertEqual(out["mic_sensitivity"], "normal")

        self.assertIn('id="s-sensitivity"', self.ui.HTML)
        self.assertIn("mic_sensitivity: document.getElementById('s-sensitivity').value", self.ui.HTML)
        self.assertIn("s-sensitivity", self.ui.HTML)

    def test_mic_sensitivity_save_round_trip_keeps_value(self):
        saved = self.ui._settings_save({"mic_sensitivity": "low"})
        self.assertEqual(saved["mic_sensitivity"], "low")
        # A later save that doesn't mention mic_sensitivity must not reset it,
        # mirroring the pill_window regression test above.
        saved = self.ui._settings_save({"auto_paste": False})
        self.assertEqual(saved["mic_sensitivity"], "low")

    def test_ui_command_path_honors_state_dir(self):
        state = os.path.realpath(self._tmp_state.name)
        self.assertTrue(os.path.realpath(self.ui.UI_COMMAND_PATH).startswith(state))
        self.assertEqual(os.path.basename(self.ui.UI_COMMAND_PATH), "ui_command.json")

    def test_send_command_writes_sequenced_allowlisted_commands(self):
        api = self.ui.API()
        first = json.loads(api.send_command("request_mic"))
        second = json.loads(api.send_command("finish_onboarding"))
        self.assertGreater(first["seq"], 0)
        self.assertEqual(second["seq"], first["seq"] + 1)
        with open(self.ui.UI_COMMAND_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["command"], "finish_onboarding")
        self.assertEqual(raw["seq"], second["seq"])
        # Unknown commands are refused client-side: seq 0, nothing written.
        refused = json.loads(api.send_command("rm_rf_slash"))
        self.assertEqual(refused["seq"], 0)
        with open(self.ui.UI_COMMAND_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["command"], "finish_onboarding")

    def test_send_command_keeps_pending_queue(self):
        # Two clicks within one 500ms main-process poll must both survive:
        # the file carries a short pending queue, not a single slot.
        api = self.ui.API()
        first = json.loads(api.send_command("request_ax"))["seq"]
        second = json.loads(api.send_command("open_privacy_ax"))["seq"]
        with open(self.ui.UI_COMMAND_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["seq"], second)
        self.assertEqual(
            raw["pending"][-2:],
            [
                {"seq": first, "command": "request_ax"},
                {"seq": second, "command": "open_privacy_ax"},
            ],
        )
        self.assertLessEqual(len(raw["pending"]), self.ui._UI_COMMAND_PENDING_MAX)

    def test_command_state_includes_onboarding_seq(self):
        state = self.ui._command_state_load()
        self.assertIn("onboarding_seq", state)
        self.assertIn("raise_seq", state)
        self.assertIn("settings_seq", state)

    def test_wizard_markup_and_bridge_present(self):
        html = self.ui.HTML
        self.assertIn('id="onboarding"', html)
        for step in ("welcome", "mic", "ax", "model", "try"):
            self.assertIn(f'id="ob-step-{step}"', html)
        self.assertIn("send_command", html)
        for command in (
            "request_mic", "request_ax", "open_privacy_mic", "open_privacy_ax",
            "retry_model_download", "finish_onboarding", "relaunch_app",
        ):
            self.assertIn(command, html)
        # The wizard polls faster than the 2s data tick while visible.
        self.assertIn("setInterval(pollOnboarding, 1000)", html)
        # Model step reuses the runtime progress bar styling.
        self.assertIn('id="ob-model-progress-fill"', html)

    def test_wizard_every_gated_step_has_escape_hatch(self):
        # A user who denies mic (or can't grant AX on a managed Mac) must
        # never be soft-locked behind the full-screen overlay: mic, AX and
        # model steps each offer "Skip for now", walking through to the try
        # step whose own skip calls obFinish() and writes the marker.
        html = self.ui.HTML
        self.assertIn("onclick=\"obSetStep('ax')\">Skip for now", html)
        self.assertIn("onclick=\"obSetStep('model')\">Skip for now", html)
        self.assertIn("onclick=\"obSetStep('try')\">Skip for now", html)
        self.assertIn('onclick="obFinish()">Skip and finish setup', html)

    def test_settings_offers_run_setup_again(self):
        html = self.ui.HTML
        self.assertIn('id="run-setup-again"', html)
        self.assertIn("restart_onboarding", html)

    def test_wizard_finish_suppresses_stale_reopen(self):
        # Finish/Skip hides the overlay optimistically; the next 1s poll can
        # still read onboarding_active:true and must not flash the wizard
        # back at the Welcome step. The flag clears when the backend
        # confirms (active false) or a restart bumps the onboarding seq.
        html = self.ui.HTML
        self.assertIn("let obFinishSent = false;", html)
        self.assertIn("obFinishSent = true;", html)
        self.assertIn("if (!active) obFinishSent = false;", html)
        self.assertIn("if (obFinishSent) { obShow(false); return; }", html)
        # Restart path re-arms even if active:false was never observed.
        seq_branch = html[html.index("if (o > lastOnboardingSeq)"):]
        self.assertIn("obFinishSent = false;", seq_branch[:400])

    def test_wizard_model_step_driven_by_runtime_readiness(self):
        # The download fields are global and may describe a model SWITCH
        # (reachable via "Run setup again"): a ready runtime model must show
        # ready and allow advance regardless of stale switch-download state,
        # and Retry only surfaces while the runtime model is actually
        # missing.
        html = self.ui.HTML
        self.assertIn("const modelReady = s.model_ready !== false;", html)
        self.assertIn("mRetry.style.display = (failed && !modelReady)", html)
        self.assertIn("mCont.style.display = modelReady", html)
        self.assertIn(
            "if (obStep === 'model' && modelReady) obScheduleAdvance('try', 900);",
            html,
        )

    def test_wizard_try_step_credits_latch_takes_via_baseline(self):
        # Latch-mode rows are created at session START and updated in place
        # to ok — a created_at cutoff missed any take spanning the step
        # open. The step snapshots {id -> status} when it opens and credits
        # rows that are new or flipped to ok since.
        html = self.ui.HTML
        self.assertIn("obTryBaseline", html)
        self.assertIn("obTryCaptureBaseline", html)
        self.assertIn("before === undefined || before !== 'ok'", html)
        self.assertNotIn("obTryOpenedAt", html)
        self.assertNotIn("created_at || ''", html.split("function obCheckTryIt")[1].split("}")[0])

    def test_boot_hides_app_until_first_wizard_decision(self):
        # Fresh installs must not flash the normal history UI for the ~1s the
        # js bridge takes to boot; a bounded failsafe guarantees the page can
        # never stay permanently blank.
        html = self.ui.HTML
        self.assertIn('<body class="is-booting">', html)
        self.assertIn("body.is-booting > .header", html)
        self.assertIn("body.is-booting > .cards", html)
        self.assertIn("setTimeout(bootReveal, 2500);", html)
        self.assertIn("bootReveal();", html)


if __name__ == "__main__":
    unittest.main()
