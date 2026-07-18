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

    def test_save_preserves_hand_edited_latch_chunk_seconds(self):
        # The form no longer surfaces latch chunking, but a power user may set
        # latch_chunk_seconds by hand in settings.json. A form save (which omits
        # the key) must merge over the stored value, not reset it to the default.
        self.ui._settings_save({"latch_chunk_seconds": 25.0})
        saved = self.ui._settings_save({"auto_paste": False})
        self.assertEqual(saved["latch_chunk_seconds"], 25.0)
        self.assertIs(saved["latch_chunk_mode"], True)

    def test_latch_chunk_mode_is_unconditional(self):
        # Silent latch chunking has no toggle at all: a stored false (someone
        # unchecked the old toggle in a previous version) normalizes to True.
        out = self.ui._settings_normalize({"latch_chunk_mode": False})
        self.assertIs(out["latch_chunk_mode"], True)
        out = self.ui._settings_normalize({"latch_chunk_mode": "bogus"})
        self.assertIs(out["latch_chunk_mode"], True)
        out = self.ui._settings_normalize({})
        self.assertIs(out["latch_chunk_mode"], True)

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

    def test_pill_style_always_bubbles_and_form_dropdown_gone(self):
        # Bubbles is the only pill style now: any stored value is coerced to
        # "bubbles" so legacy settings files (e.g. an old "spectrogram") still
        # render bubbles, and the indicator-style dropdown is gone from the form.
        out = self.ui._settings_normalize({"pill_style": "spectrogram"})
        self.assertEqual(out["pill_style"], "bubbles")
        out = self.ui._settings_normalize({"pill_style": "bogus"})
        self.assertEqual(out["pill_style"], "bubbles")
        out = self.ui._settings_normalize({})
        self.assertEqual(out["pill_style"], "bubbles")
        self.assertNotIn('id="s-pillstyle"', self.ui.HTML)
        # The pill on/off checkbox stays.
        self.assertIn('id="s-pill"', self.ui.HTML)

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
                {"seq": first, "command": "request_ax", "arg": ""},
                {"seq": second, "command": "open_privacy_ax", "arg": ""},
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
            "choose_model", "retry_model_download", "finish_onboarding",
            "relaunch_app",
        ):
            self.assertIn(command, html)
        # The wizard polls faster than the 2s data tick while visible.
        self.assertIn("setInterval(pollOnboarding, 1000)", html)
        # Model step reuses the runtime progress bar styling.
        self.assertIn('id="ob-model-progress-fill"', html)

    def test_wizard_every_gated_step_has_escape_hatch(self):
        # A user who denies mic (or can't grant AX on a managed Mac) must
        # never be soft-locked behind the full-screen overlay: mic, AX and
        # model steps each offer "Skip for now". Skip now routes through
        # obAdvance() (which skips satisfied steps) rather than a hard-coded
        # target, but still always walks through to the try step whose own
        # skip calls obFinish() and writes the marker.
        html = self.ui.HTML
        self.assertIn("onclick=\"obAdvance('mic')\">Skip for now", html)
        self.assertIn("onclick=\"obAdvance('ax')\">Skip for now", html)
        self.assertIn("onclick=\"obAdvance('model')\">Skip for now", html)
        self.assertIn('onclick="obFinish()">Skip and finish setup', html)
        # obNextStep never returns anything past the terminal try step, so a
        # chain of skips can only ever land on try — no soft-lock.
        self.assertIn("return 'try';", html)

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
            "if (obStep === 'model' && modelReady) obScheduleAdvance('model', 900);",
            html,
        )

    def test_send_command_bridge_passes_arg(self):
        # choose_model carries its repo id in an optional "arg" field; seq
        # semantics stay identical to arg-less commands.
        self.assertIn("choose_model", self.ui._UI_COMMANDS)
        api = self.ui.API()
        res = json.loads(
            api.send_command("choose_model", "mlx-community/whisper-medium-mlx")
        )
        self.assertGreater(res["seq"], 0)
        with open(self.ui.UI_COMMAND_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["command"], "choose_model")
        self.assertEqual(raw["arg"], "mlx-community/whisper-medium-mlx")
        self.assertEqual(
            raw["pending"][-1],
            {
                "seq": res["seq"],
                "command": "choose_model",
                "arg": "mlx-community/whisper-medium-mlx",
            },
        )

    def test_wizard_model_chooser_cards_with_medium_recommended(self):
        html = self.ui.HTML
        for repo in (
            "mlx-community/whisper-tiny-mlx",
            "mlx-community/whisper-small-mlx",
            "mlx-community/whisper-medium-mlx",
            "mlx-community/whisper-large-v3-mlx",
        ):
            self.assertIn(f'data-model="{repo}"', html)
        for size in ("71 MB", "459 MB", "1.4 GB", "2.9 GB"):
            self.assertIn(size, html)
        # Medium is the preselected card and carries the Recommended badge.
        self.assertIn(
            'is-selected" role="radio" aria-checked="true" '
            'data-model="mlx-community/whisper-medium-mlx"',
            html,
        )
        self.assertIn('class="ob-model-badge">Recommended</span>', html)
        # Download button confirms the choice and sends it with its arg.
        self.assertIn('id="ob-model-download"', html)
        self.assertIn("obSend('choose_model', sel.dataset.model)", html)

    def test_wizard_model_chooser_skipped_when_model_ready(self):
        # Resumed wizard / cache already filled: the step shows the ready
        # state (or an in-flight download's progress), never the chooser.
        # The chooser only exists before any download does.
        html = self.ui.HTML
        self.assertIn(
            "const chooserVisible = !modelReady && !obModelChosen "
            "&& dlState === 'idle';",
            html,
        )
        # Download click flips to the progress view without waiting a poll,
        # and a fresh wizard run resets the choice back to Medium.
        self.assertIn("obModelChosen = true;", html)
        self.assertIn("obModelChosen = false;", html)
        self.assertIn(
            '.ob-model-card[data-model="mlx-community/whisper-medium-mlx"]',
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

    def test_wizard_skips_satisfied_steps_when_advancing(self):
        # Owner feedback: an already-satisfied step must never flash on screen.
        # Advancing (Get Started / Continue / Skip / the auto-advance timer)
        # computes the next ACTIONABLE step and skips satisfied ones in
        # passing, rather than rendering each for a moment.
        html = self.ui.HTML
        # The actionability + next-step helpers exist and gate on the runtime
        # status fields the wizard already polls.
        self.assertIn("function obStepActionable(step, s)", html)
        self.assertIn("function obNextStep(from, s)", html)
        self.assertIn("function obAdvance(from)", html)
        self.assertIn("(s.mic_status || 'unknown') !== 'granted'", html)
        self.assertIn("(s.ax_status || 'denied') !== 'granted'", html)
        # An unchosen/downloading/failed model is not ready, so it is
        # actionable; only a ready runtime model is skippable.
        self.assertIn("if (step === 'model') return s.model_ready === false;", html)
        # try is terminal — never skipped, always the fallback landing spot.
        self.assertIn("if (step === 'try' || obStepActionable(step, s)) return step;", html)
        self.assertIn("return 'try';", html)
        # The welcome button and every Continue/Skip route through obAdvance so
        # the skip is computed from the live status, not hard-coded.
        self.assertIn("onclick=\"obAdvance('welcome')\">Get Started", html)
        self.assertIn("id=\"ob-ax-continue\" onclick=\"obAdvance('ax')\"", html)
        self.assertIn("id=\"ob-model-continue\" onclick=\"obAdvance('model')\"", html)

    def test_wizard_holds_watched_satisfied_step_before_advancing(self):
        # When a VISIBLE step becomes satisfied while the user watches (they
        # grant mic/ax, the download finishes), the wizard renders the ✓ state
        # and holds ~900ms before advancing — it must not jump on the next
        # poll tick. The timer is single-fire (guards a poll racing it),
        # re-checks it is still the same step at fire, and is cancelled when
        # the user leaves the step.
        html = self.ui.HTML
        self.assertIn("function obScheduleAdvance(from, delay)", html)
        self.assertIn("if (obAdvanceTimer) return;", html)
        self.assertIn("if (obStep === from) obAdvance(from);", html)
        # Leaving a step clears the pending advance.
        self.assertIn("obAdvanceFrom = null;", html)
        # Each gated step schedules the hold from its own id at the 900ms beat.
        self.assertIn("if (obStep === 'mic') obScheduleAdvance('mic', 900);", html)
        self.assertIn("if (obStep === 'ax' && hotkey) obScheduleAdvance('ax', 900);", html)
        self.assertIn(
            "if (obStep === 'model' && modelReady) obScheduleAdvance('model', 900);",
            html,
        )

    def test_wizard_try_step_reserves_space_no_reflow(self):
        # Owner feedback: the success card / Finish button appearing after the
        # wait repainted and shifted the modal. They now fade into a reserved
        # region (visibility/opacity, not DOM growth) and the card carries a
        # stable min-height so step changes don't jump its size either.
        html = self.ui.HTML
        self.assertIn('id="ob-try-reserved"', html)
        self.assertIn('class="ob-try-reserved"', html)
        self.assertIn(".ob-try-reserved { min-height:", html)
        # The success card stays in flow and toggles via opacity/visibility.
        self.assertIn("visibility: hidden;", html)
        self.assertIn(".ob-try-card.is-visible { opacity: 1; visibility: visible; }", html)
        self.assertIn("#ob-finish.is-visible { opacity: 1; visibility: visible; }", html)
        # Finish is toggled by class, not display (which would reflow).
        self.assertIn("document.getElementById('ob-finish').classList.add('is-visible');", html)
        self.assertIn("finish.classList.remove('is-visible')", html)
        # The card floor keeps the light steps from jittering smaller.
        self.assertIn("min-height: 400px;", html)

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
