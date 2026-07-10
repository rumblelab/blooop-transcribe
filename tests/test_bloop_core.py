import importlib.util
import inspect
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
BLOOP_PATH = REPO_ROOT / "bloop.py"


def _install_stub_modules():
    if "numpy" not in sys.modules:
        sys.modules["numpy"] = types.ModuleType("numpy")

    if "sounddevice" not in sys.modules:
        sd = types.ModuleType("sounddevice")

        class _DummyInputStream:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

        sd.InputStream = _DummyInputStream
        sd.query_devices = lambda kind=None: {"name": "stub-input"}
        sys.modules["sounddevice"] = sd

    if "soundfile" not in sys.modules:
        sf = types.ModuleType("soundfile")
        sf.write = lambda *args, **kwargs: None
        sys.modules["soundfile"] = sf

    if "pyperclip" not in sys.modules:
        pc = types.ModuleType("pyperclip")
        pc.copy = lambda text: None
        sys.modules["pyperclip"] = pc

    if "pynput" not in sys.modules or "pynput.keyboard" not in sys.modules:
        pynput_mod = types.ModuleType("pynput")
        keyboard_mod = types.ModuleType("pynput.keyboard")

        class _DummyKey:
            cmd_r = "cmd_r"
            alt_r = "alt_r"
            shift_r = "shift_r"
            cmd = "cmd"

        class _PressedCtx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _DummyController:
            def pressed(self, _key):
                return _PressedCtx()

            def press(self, _key):
                return None

            def release(self, _key):
                return None

        class _DummyListener:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def join(self):
                return None

        keyboard_mod.Key = _DummyKey
        keyboard_mod.Controller = _DummyController
        keyboard_mod.Listener = _DummyListener

        pynput_mod.keyboard = keyboard_mod
        sys.modules["pynput"] = pynput_mod
        sys.modules["pynput.keyboard"] = keyboard_mod


def _import_bloop_module():
    module_name = "bloop_under_test"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, BLOOP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec for {BLOOP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class BloopCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp_home = tempfile.TemporaryDirectory()
        cls._old_home = os.environ.get("HOME")
        os.environ["HOME"] = cls._tmp_home.name
        _install_stub_modules()
        cls.bloop = _import_bloop_module()

    @classmethod
    def tearDownClass(cls):
        if cls._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = cls._old_home
        cls._tmp_home.cleanup()

    def test_settings_normalize_clamps_and_filters(self):
        out = self.bloop._settings_normalize(
            {
                "model": "   ",
                "auto_paste": "yes",
                "latch_chunk_mode": False,
                "latch_chunk_seconds": "9999",
                "silence_trim_preset": "aggressive",
                "hotkey": "right_option",
                "custom_vocab": [" Blooop ", "Acme Widget", "blooop", ""],
            }
        )
        self.assertEqual(out["model"], self.bloop.DEFAULT_MODEL)
        self.assertEqual(out["auto_paste"], True)
        self.assertEqual(out["latch_chunk_mode"], False)
        self.assertEqual(out["latch_chunk_seconds"], 60.0)
        self.assertEqual(out["silence_trim_preset"], "aggressive")
        self.assertEqual(out["hotkey"], "right_option")
        self.assertEqual(out["custom_vocab"], ["Blooop", "Acme Widget"])

    def test_settings_save_and_load_round_trip(self):
        saved, path, _mtime = self.bloop._settings_save(
            {
                "model": "mlx-community/whisper-tiny-mlx",
                "hotkey": "right_shift",
                "latch_chunk_seconds": 2.4,
                "auto_paste": False,
                "custom_vocab": ["Blooop", "Codex"],
            }
        )
        self.assertTrue(Path(path).exists())
        loaded, loaded_path, _ = self.bloop._settings_load()
        self.assertEqual(path, loaded_path)
        self.assertEqual(saved["model"], "mlx-community/whisper-tiny-mlx")
        self.assertEqual(saved["hotkey"], "right_shift")
        self.assertEqual(saved["latch_chunk_seconds"], 2.4)
        self.assertEqual(saved["auto_paste"], False)
        self.assertEqual(saved["custom_vocab"], ["Blooop", "Codex"])
        self.assertEqual(loaded["model"], "mlx-community/whisper-tiny-mlx")
        self.assertEqual(loaded["hotkey"], "right_shift")
        self.assertEqual(loaded["custom_vocab"], ["Blooop", "Codex"])

    def test_parse_cli(self):
        opts = self.bloop._parse_cli(["--history", "12", "--recopy-last"])
        self.assertEqual(opts["show_history"], True)
        self.assertEqual(opts["history_limit"], 12)
        self.assertEqual(opts["recopy_last"], True)
        self.assertEqual(opts["help"], False)

        opts = self.bloop._parse_cli(["-psn_0_12345", "--history"])
        self.assertEqual(opts["show_history"], True)
        self.assertEqual(opts["history_limit"], self.bloop.HISTORY_SHOW_DEF)

        with self.assertRaises(ValueError):
            self.bloop._parse_cli(["--history", "abc"])

    def test_frozen_macos_uses_in_process_mlx_probe(self):
        old_frozen = getattr(self.bloop.sys, "frozen", None)
        had_frozen = hasattr(self.bloop.sys, "frozen")
        old_platform = self.bloop.sys.platform
        try:
            self.bloop.sys.frozen = True
            self.bloop.sys.platform = "darwin"
            self.assertEqual(self.bloop._supports_subprocess_mlx_probe(), False)
        finally:
            self.bloop.sys.platform = old_platform
            if had_frozen:
                self.bloop.sys.frozen = old_frozen
            else:
                delattr(self.bloop.sys, "frozen")

    def test_self_exec_command_non_frozen_includes_script(self):
        cmd = self.bloop._self_exec_command(["--probe-mlx"])
        self.assertGreaterEqual(len(cmd), 3)
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("bloop.py"))
        self.assertEqual(cmd[2], "--probe-mlx")

    def test_state_dir_defaults_under_home_when_writable(self):
        expected_prefix = str(Path(os.environ["HOME"]) / ".bloop_flow")
        self.assertTrue(str(self.bloop.STATE_DIR).startswith(expected_prefix))

    def test_history_summary(self):
        summary, truncated = self.bloop._history_summary(
            "Sentence one. Sentence two. Sentence three.",
            "",
            max_chars=25,
        )
        self.assertLessEqual(len(summary), 25)
        self.assertEqual(truncated, True)

        empty_summary, empty_truncated = self.bloop._history_summary("", "", max_chars=10)
        self.assertEqual(empty_summary, "-")
        self.assertEqual(empty_truncated, False)

    def test_history_panel_uses_summary_text_only(self):
        src = inspect.getsource(self.bloop.HistoryPanel._refresh_rows)
        self.assertIn("preview, _truncated = _history_summary(", src)

    def test_prewarm_has_tmpfile_cleanup(self):
        src = inspect.getsource(self.bloop.BloopFlow._prewarm)
        self.assertIn("tmp = None", src)
        self.assertIn("if tmp is not None", src)

    def test_custom_vocab_prompt_contains_exact_spellings(self):
        prompt = self.bloop._custom_vocab_initial_prompt(["Blooop", "Codex"])
        self.assertIn("Blooop", prompt)
        self.assertIn("Codex", prompt)
        self.assertIn("exact spellings", prompt)

    def test_hallucination_filter_existing_rules_still_apply(self):
        self.assertTrue(self.bloop._looks_like_hallucination("Thanks for watching!"))
        self.assertTrue(self.bloop._looks_like_hallucination("help help help"))
        self.assertFalse(self.bloop._looks_like_hallucination("this is a normal sentence"))
        self.assertFalse(self.bloop._looks_like_hallucination(None))
        self.assertFalse(self.bloop._looks_like_hallucination("   "))

    def test_phrase_loop_truncated_to_legitimate_prefix(self):
        # The real-world miss: a long valid dictation followed by a Whisper
        # repetition loop on a multi-word phrase. The legitimate prefix must
        # survive; the loop must go.
        prefix = (
            "I spent the morning reworking the history panel and honestly "
            "the new layout is much calmer"
        )
        loop = " ".join(["the history panel made me feel like"] * 30)
        self.assertEqual(
            self.bloop._truncate_phrase_loops(f"{prefix} {loop}"), prefix
        )

    def test_phrase_loop_dominating_text_is_hallucination(self):
        loop_only = " ".join(["the history panel made me feel like"] * 30)
        self.assertEqual(self.bloop._truncate_phrase_loops(loop_only), "")
        self.assertTrue(self.bloop._looks_like_hallucination(loop_only))

    def test_phrase_loop_requires_five_consecutive_repeats(self):
        # Someone intentionally repeating a phrase a few times is real
        # dictation; four consecutive copies must pass through untouched.
        quad = " ".join(["i really mean it"] * 4)
        self.assertEqual(self.bloop._truncate_phrase_loops(quad), quad)
        self.assertFalse(self.bloop._looks_like_hallucination(quad))
        quint = " ".join(["i really mean it"] * 5)
        self.assertEqual(self.bloop._truncate_phrase_loops(quint), "")
        self.assertTrue(self.bloop._looks_like_hallucination(quint))

    def test_phrase_loop_matches_despite_punctuation_and_case(self):
        # Whisper loops drift in casing and punctuation between repeats;
        # comparison has to ignore both.
        prefix = "Note to self about the release"
        loop = (
            "The history panel made me feel like, "
            "the history panel made me feel like. " * 3
        ).strip()
        self.assertEqual(
            self.bloop._truncate_phrase_loops(f"{prefix} {loop}"), prefix
        )

    def test_phrase_loop_ngram_length_bounds(self):
        # 2-word and 12-word repeating units are detected; the scan stops at
        # 12 so a 13-word unit repeated five times is (deliberately) left
        # alone.
        pair_loop = ("now then " * 6).strip()
        self.assertEqual(self.bloop._truncate_phrase_loops(pair_loop), "")
        twelve = (
            "alpha bravo charlie delta echo foxtrot "
            "golf hotel india juliet kilo lima"
        )
        self.assertEqual(self.bloop._truncate_phrase_loops(" ".join([twelve] * 4)), "")
        thirteen = twelve + " mike"
        loop13 = " ".join([thirteen] * 5)
        self.assertEqual(self.bloop._truncate_phrase_loops(loop13), loop13)

    def test_phrase_loop_long_units_trip_at_four_repeats(self):
        # Verbatim 4x repetition of a 6+ word clause is not natural dictation,
        # so long units trip one repeat sooner than short phrases (which keep
        # the conservative threshold of five — see the quad test above).
        prefix = "and that was the last slide"
        long_loop = " ".join(["the history panel made me feel like"] * 4)
        self.assertEqual(
            self.bloop._truncate_phrase_loops(f"{prefix} {long_loop}"), prefix
        )
        short = " ".join(["i really mean it"] * 4)
        self.assertEqual(self.bloop._truncate_phrase_loops(short), short)

    def test_hallucination_blacklist_ignores_trailing_punctuation(self):
        self.assertTrue(self.bloop._looks_like_hallucination("Thanks for watching!!"))
        self.assertTrue(self.bloop._looks_like_hallucination("Thank you."))
        self.assertTrue(self.bloop._looks_like_hallucination("You."))
        self.assertTrue(
            self.bloop._looks_like_hallucination("Subtitles by the Amara.org community")
        )
        self.assertTrue(self.bloop._looks_like_hallucination("Please like and subscribe."))
        # Whole-utterance matching only: real sentences that merely contain a
        # blacklisted phrase pass through.
        self.assertFalse(
            self.bloop._looks_like_hallucination("thanks for watching the demo")
        )

    def test_whisper_uses_temperature_fallback_ladder(self):
        # compression_ratio/logprob thresholds only flag a segment for
        # re-decode at the NEXT temperature in the tuple; a scalar 0.0 means
        # no fallback exists and looping segments are kept verbatim.
        temps = self.bloop.WHISPER_TEMPERATURE
        self.assertIsInstance(temps, tuple)
        self.assertGreater(len(temps), 1)
        self.assertEqual(temps[0], 0.0)
        self.assertEqual(sorted(temps), list(temps))

    def test_phrase_loop_detected_across_chunk_join(self):
        # Latch mode concatenates per-chunk pieces with spaces. A loop split
        # across a chunk boundary can stay under the threshold in each piece
        # while the assembled text crosses it — the final-text pass in
        # _transcribe_locked exists for exactly this case.
        phrase = "the history panel made me feel like"
        piece_a = "so the design review went fine " + " ".join([phrase] * 2)
        piece_b = " ".join([phrase] * 3)
        self.assertEqual(self.bloop._truncate_phrase_loops(piece_a), piece_a)
        self.assertEqual(self.bloop._truncate_phrase_loops(piece_b), piece_b)
        self.assertEqual(
            self.bloop._truncate_phrase_loops(f"{piece_a} {piece_b}"),
            "so the design review went fine",
        )

    def test_phrase_loop_ignores_normal_dictation(self):
        text = (
            "Today I want to talk about the release plan, then the testing "
            "story, and then the open questions. The release plan is mostly "
            "settled but the testing story still needs a volunteer, and the "
            "open questions are honestly the hard part of the whole thing."
        )
        self.assertEqual(self.bloop._truncate_phrase_loops(text), text)
        self.assertFalse(self.bloop._looks_like_hallucination(text))
        self.assertEqual(self.bloop._truncate_phrase_loops(""), "")

    def test_transcribe_paths_apply_loop_filter(self):
        # Per-chunk: loops are decode artifacts inside one Whisper window, so
        # each chunk is filtered before it enters the latch session. Assembled:
        # the cross-chunk safety net runs on the final session text.
        src = inspect.getsource(self.bloop.BloopFlow._transcribe_audio)
        self.assertIn("_truncate_phrase_loops(", src)
        src = inspect.getsource(self.bloop.BloopFlow._transcribe_locked)
        self.assertIn("_truncate_phrase_loops(", src)

    def test_overlay_uses_nonactivating_mac_style(self):
        src = inspect.getsource(self.bloop._macos_configure_nonactivating_overlay)
        self.assertIn("MacWindowStyle", src)
        self.assertIn('"noActivates"', src)
        # hideOnSuspend must not be passed to MacWindowStyle — it would hide
        # the pill whenever Blooop isn't the active app, which is always the
        # case while the user is transcribing into another app. The word may
        # still appear in explanatory comments; guard only against the
        # combined style string that would re-enable the behavior.
        self.assertNotIn("noActivates hideOnSuspend", src)
        self.assertNotIn('"hideOnSuspend"', src)

    def test_overlay_joins_all_spaces_and_fullscreen(self):
        src = inspect.getsource(self.bloop._macos_configure_nonactivating_overlay)
        self.assertIn("join_all_spaces=True", src)
        self.assertIn("fullscreen_auxiliary=True", src)
        self.assertIn("stationary=True", src)
        # move_to_active_space conflicts with join_all_spaces on macOS; dropped.
        self.assertNotIn("move_to_active_space=True", src)

    def test_overlay_raises_window_without_grabbing_mouse(self):
        src = inspect.getsource(self.bloop._macos_prepare_overlay_window)
        self.assertIn("target.setIgnoresMouseEvents_(True)", src)
        self.assertIn("target.orderFrontRegardless()", src)

    def test_indicator_state_labels_are_short_and_explicit(self):
        self.assertEqual(self.bloop._indicator_state_label("idle"), "")
        self.assertEqual(self.bloop._indicator_state_label("recording"), "REC")
        self.assertEqual(self.bloop._indicator_state_label("transcribing"), "TXT")

    def test_visualizer_reapplies_overlay_focus_guards_on_show(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer._tick)
        self.assertIn("self._drain_ui_callbacks()", src)
        self.assertIn("self._set_overlay_visible(True)", src)
        self.assertNotIn("self.root.deiconify()", src)

    def test_visualizer_uses_top_right_status_chip_layout(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer.__init__)
        self.assertIn("self.STATUS_W", src)
        self.assertIn("x  = max(self.EDGE_MARGIN_X, sw - self.W - self.EDGE_MARGIN_X)", src)
        self.assertIn("self._overlay = tk.Toplevel(self.root)", src)

    def test_visualizer_uses_opaque_overlay_surface_for_visibility(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer.__init__)
        self.assertIn("self._canvas_bg = self.BG_OUTER", src)
        self.assertIn("self._window.configure(bg=self.BG_OUTER)", src)
        self.assertNotIn("systemTransparent", src)
        self.assertNotIn('wm_attributes("-transparent"', src)

    def test_visualizer_draws_recording_and_transcribing_labels(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer._draw)
        self.assertIn('status_label = "TXT"', src)
        self.assertIn('status_label = "REC"', src)

    def test_visualizer_primes_overlay_once_then_uses_alpha_toggles(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer._prime_overlay_window)
        self.assertIn('self._window.attributes("-alpha", self.HIDDEN_ALPHA)', src)
        self.assertIn("self._window.deiconify()", src)
        src = inspect.getsource(self.bloop.WaveformVisualizer._set_overlay_visible)
        self.assertIn('self._window.attributes("-topmost", bool(visible))', src)
        self.assertIn('self._window.attributes("-alpha", alpha)', src)
        self.assertIn("_macos_prepare_overlay_window(self._window, front=True)", src)

    def test_visualizer_pins_overlay_to_screen_corner(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer._reposition_overlay)
        # The overlay is anchored to the top-right of the primary screen and
        # must NOT track the pointer — that behavior read as broken and
        # interacted badly with Spaces on macOS.
        self.assertIn("self.root.winfo_screenwidth()", src)
        self.assertIn('self._window.geometry(f"{self.W}x{self.H}+{int(x)}+{int(y)}")', src)
        self.assertNotIn("winfo_pointerx()", src)
        self.assertNotIn("winfo_pointery()", src)

    def test_visualizer_mode_changes_post_back_to_ui_queue(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer._notify_mode)
        self.assertIn("self.post_to_ui(lambda: cb(mode))", src)

    def test_hotkey_callbacks_are_marshaled_to_visualizer_queue(self):
        src = inspect.getsource(self.bloop.BloopFlow.run)
        self.assertIn("schedule=self._viz.post_to_ui", src)
        self.assertIn("self._viz.post_to_ui(cb)", src)
        self.assertNotIn("schedule=lambda cb: tk_root.after(0, cb)", src)

    def test_pynput_callbacks_are_marshaled_to_visualizer_queue(self):
        src = inspect.getsource(self.bloop.BloopFlow._on_press)
        self.assertIn("self._viz.post_to_ui(self._handle_ptt_press)", src)
        src = inspect.getsource(self.bloop.BloopFlow._on_release)
        self.assertIn("self._viz.post_to_ui(self._handle_ptt_release)", src)

    def test_ptt_delay_prefers_tk_scheduler(self):
        src = inspect.getsource(self.bloop.BloopFlow._schedule_start_timer)
        self.assertIn("self._viz.root.after(", src)
        self.assertIn('self._start_timer = ("tk", token)', src)
        self.assertIn("threading.Timer(", src)

    def test_history_window_moves_to_active_space(self):
        src = inspect.getsource(self.bloop._macos_configure_history_window)
        self.assertIn("move_to_active_space=True", src)
        self.assertIn("fullscreen_auxiliary=True", src)

    def test_history_panel_stays_visible_when_app_deactivates(self):
        src = inspect.getsource(self.bloop.HistoryPanel.on_app_active_change)
        self.assertIn("if active:", src)
        self.assertNotIn("self._win.withdraw()", src)

    def test_app_activate_reopens_hidden_history_panel(self):
        src = inspect.getsource(self.bloop.BloopFlow._on_app_activate)
        self.assertIn("self._history_panel._is_visible()", src)
        self.assertIn("self._history_panel.show()", src)

    def test_spec_uses_dock_app_mode(self):
        spec_text = (REPO_ROOT / "Blooop.spec").read_text(encoding="utf-8")
        self.assertIn('"LSUIElement": False', spec_text)

    def test_pill_window_defaults_to_on_via_settings(self):
        # The floating pill is the primary recording indicator (the menu bar
        # icon is hidden by macOS's menu-bar overflow on Ventura+ and the Dock
        # badge is invisible when the Dock is auto-hidden). Default lives in
        # the user-facing settings schema so it can be flipped from the UI;
        # the BLOOOP_PILL_WINDOW env var still wins when explicitly set.
        defaults = self.bloop._settings_defaults()
        self.assertIs(defaults["pill_window"], True)
        src = inspect.getsource(self.bloop.BloopFlow.__init__)
        self.assertIn('_boot_settings.get("pill_window"', src)

    def test_menu_bar_shows_explicit_indicator_labels(self):
        src = inspect.getsource(self.bloop._BloopMenuBarController._apply_visuals)
        self.assertIn("button.setImagePosition_(NSImageLeft if label else NSImageOnly)", src)
        self.assertIn('button.setTitle_(f" {label}" if label else "")', src)

    def test_menu_bar_has_live_level_pulse_while_recording(self):
        # The recording-state indicator draws a red dot whose radius scales
        # with live audio RMS so the user can see the mic is actually
        # picking up their voice. Bars/waveforms would be illegible at
        # 16pt menu bar size; the oscillating dot is unambiguous.
        cls = self.bloop._BloopMenuBarController
        self.assertTrue(hasattr(cls, "set_level"))
        self.assertTrue(hasattr(cls, "_renderRecordingImage_"))
        # Level is piped from the viz tick loop through an on_level callback.
        src = inspect.getsource(self.bloop.WaveformVisualizer._tick)
        self.assertIn("self._on_level_change(latest)", src)
        src = inspect.getsource(self.bloop.BloopFlow.run)
        self.assertIn("self._viz.set_on_level_change(_on_viz_level)", src)

    def test_viz_mode_updates_dock_badge(self):
        src = inspect.getsource(self.bloop.BloopFlow.run)
        self.assertIn("_macos_set_dock_badge(_indicator_state_label(mode))", src)
        self.assertIn('self._viz.set_on_mode_change(_on_viz_mode)', src)

    def test_history_panel_click_binding_and_custom_vocab_editor(self):
        src = inspect.getsource(self.bloop.HistoryPanel._build_window)
        self.assertIn("<ButtonRelease-1>", src)
        self.assertIn("Custom Vocabulary", src)
        self.assertIn("self._custom_vocab_text = vocab_text", src)

    def test_history_panel_settings_start_collapsed(self):
        src = inspect.getsource(self.bloop.HistoryPanel._build_window)
        self.assertIn('text="Show Settings"', src)
        self.assertIn("self._settings_frame = settings", src)
        self.assertIn("self._settings_frame.grid_remove()", src)

    def test_history_panel_save_controls_follow_dirty_state(self):
        src = inspect.getsource(self.bloop.HistoryPanel._update_settings_controls)
        self.assertIn('toggle_text += " *"', src)
        self.assertIn("Save Changes", src)
        self.assertIn("No Changes", src)
        self.assertIn('self._save_btn.state(["disabled"])', src)

    def test_history_panel_preserves_unsaved_settings_on_show(self):
        src = inspect.getsource(self.bloop.HistoryPanel.show)
        self.assertIn("if not self._settings_dirty:", src)

    def test_stream_teardown_is_offloaded_with_breadcrumbs(self):
        # 2026-06-10: the first tap-to-stop after a fresh install killed the
        # app with no .ips and no faulthandler dump; the standalone log cut
        # exactly at the native CoreAudio teardown in stop_recording.
        # Pa_StopStream can also block indefinitely, which freezes the Tk
        # main thread into a force-quit. Teardown therefore runs on a
        # disposable daemon thread and writes durable begin/end breadcrumbs
        # to the issues log ("begin" without "end" = teardown died).
        src = inspect.getsource(self.bloop.BloopFlow._close_stream)
        self.assertIn("threading.Thread", src)
        self.assertIn("daemon=True", src)
        self.assertIn("audio_stream_close_begin", src)
        self.assertIn("audio_stream_close_end", src)
        # PortAudio re-init must not yank the library out from under an
        # in-flight async teardown.
        src = inspect.getsource(self.bloop.BloopFlow._reset_portaudio)
        self.assertIn("self._stream_close_lock", src)

    def test_wedged_teardown_triggers_guarded_self_relaunch(self):
        # 2026-06-25 + 2026-07-02: Pa_StopStream deadlocked against CoreAudio's
        # IO thread (HAL-vs-AudioUnit mutex inversion; sample saved in
        # ~/.bloop_flow/issues/20260702-deadlock-sample.txt). The HAL client is
        # poisoned for the process lifetime, so the next Pa_OpenStream froze
        # the main thread — an artifact-free "crash". Guards required:
        # a watchdog on every teardown, an open path that never runs while a
        # teardown is in flight, and a self-relaunch that first drains queued
        # transcription so the final recording still pastes.
        src = inspect.getsource(self.bloop.BloopFlow._close_stream)
        self.assertIn("_watchdog", src)
        self.assertIn("_declare_audio_wedge", src)
        src = inspect.getsource(self.bloop.BloopFlow._ensure_stream)
        self.assertIn("self._teardown_idle.wait", src)
        self.assertIn("_declare_audio_wedge", src)
        src = inspect.getsource(self.bloop.BloopFlow._relaunch_after_wedge)
        self.assertIn("self._drain_transcription", src)
        self.assertIn("self._relaunch_self", src)
        src = inspect.getsource(self.bloop.BloopFlow._drain_transcription)
        self.assertIn("_transcribe_queue.empty", src)
        self.assertIn("self.recording", src)
        src = inspect.getsource(self.bloop.BloopFlow._relaunch_self)
        self.assertIn("_release_single_instance_lock", src)
        self.assertIn("os._exit(0)", src)
        # Normal quit must never race the watchdog into relaunching the app
        # the user just closed.
        src = inspect.getsource(self.bloop.BloopFlow._declare_audio_wedge)
        self.assertIn("self._quitting", src)
        src = inspect.getsource(self.bloop.BloopFlow._quit)
        self.assertIn("self._quitting = True", src)

    def test_double_tap_latch_survives_deferred_recording_start(self):
        # Real taps land right at the PTT_START_DELAY_MS boundary (observed
        # held_dur 0.103–0.187s vs the 110ms deferred start), so whenever the
        # start timer fired mid-tap, `not self.recording` voided the first tap
        # and the latch gesture became a coin flip. Tap classification must be
        # by held duration alone — a finished push-to-talk hold is far longer
        # than DOUBLE_TAP_MS, so duration still prevents accidental latch.
        src = inspect.getsource(self.bloop.BloopFlow._handle_ptt_release)
        self.assertIn(
            "self._last_was_tap = held <= (DOUBLE_TAP_MS / 1000)", src
        )
        self.assertNotIn("and not self.recording", src)
        # The tap window must comfortably clear the deferred-start delay.
        self.assertGreaterEqual(
            self.bloop.DOUBLE_TAP_MS, self.bloop.PTT_START_DELAY_MS * 3
        )

    def test_model_download_offers_relaunch_dialog(self):
        # A model change only applies on relaunch, and that used to be a
        # console-log footnote. Download completion must surface a native
        # relaunch offer, drain in-flight transcription before restarting,
        # and treat dialog timeout ("gave up") as Later — never a surprise
        # relaunch on silence.
        src = inspect.getsource(self.bloop.BloopFlow._model_download_loop)
        self.assertIn("_offer_model_relaunch", src)
        src = inspect.getsource(self.bloop.BloopFlow._offer_model_relaunch)
        self.assertIn("Relaunch Now", src)
        self.assertIn("giving up after", src)
        self.assertIn("self._drain_transcription", src)
        self.assertIn("self._relaunch_self", src)

    def test_stop_recording_releases_input_stream(self):
        # The mic stream must not outlive the recording session. A stream left
        # open for hours goes stale across sleep/wake and device swaps, and
        # closing/reopening it later was the long-uptime native-crash path —
        # it also kept the macOS orange mic indicator lit while idle. The
        # actual close now happens after the stop-grace tail, in
        # _finalize_stop_locked, but stop_recording is still what schedules
        # it (via a timer running _finalize_stop) so the stream is guaranteed
        # to be released shortly after every stop.
        src = inspect.getsource(self.bloop.BloopFlow._finalize_stop_locked)
        self.assertIn("self._close_stream()", src)
        src = inspect.getsource(self.bloop.BloopFlow.stop_recording)
        self.assertIn("threading.Timer(STOP_GRACE_TAIL_SEC, self._finalize_stop)", src)
        self.assertIn("timer.start()", src)

    def test_stop_grace_tail_constant_is_a_short_reasonable_delay(self):
        # STOP_GRACE_TAIL_SEC is the whole fix for end-of-speech truncation:
        # long enough for CoreAudio's already-buffered tail (or a trailing
        # word) to land, short enough that "stop" still feels instant.
        self.assertGreater(self.bloop.STOP_GRACE_TAIL_SEC, 0.2)
        self.assertLess(self.bloop.STOP_GRACE_TAIL_SEC, 0.5)

    def test_stop_recording_defers_snapshot_and_flag_flip_to_finalize(self):
        # stop_recording must NOT flip `recording` False or snapshot
        # `frames` itself anymore -- that used to happen immediately and cut
        # off whatever the user was still saying (or whatever CoreAudio had
        # not yet delivered). It only marks intent (_stop_pending) and
        # schedules the real work, on a grace timer, in _finalize_stop.
        src = inspect.getsource(self.bloop.BloopFlow.stop_recording)
        self.assertIn("self._stop_pending = True", src)
        self.assertNotIn("self.recording = False", src)
        self.assertNotIn("self.frames[:]", src)
        self.assertNotIn("self.frames    = []", src)
        self.assertIn("threading.Timer(STOP_GRACE_TAIL_SEC, self._finalize_stop)", src)

    def test_stop_recording_returns_early_on_double_stop(self):
        # A second stop hitting while the grace timer is already pending
        # (e.g. a repeated hotkey release) must be a no-op rather than
        # scheduling a second finalize or resetting the grace window.
        src = inspect.getsource(self.bloop.BloopFlow.stop_recording)
        self.assertIn("if not self.recording or self._stop_pending:", src)
        self.assertIn("return", src)

    def test_restart_during_grace_flushes_previous_take_first(self):
        # A fast stop -> talk-again must not be swallowed by the still-true
        # `recording` flag during the grace tail: start_recording flushes
        # (finalizes) the previous take unconditionally before deciding
        # whether it's already recording.
        src = inspect.getsource(self.bloop.BloopFlow.start_recording)
        finalize_idx = src.index("self._finalize_stop()")
        recording_check_idx = src.index("if self.recording:")
        self.assertLess(finalize_idx, recording_check_idx)

        # The deferred-start and double-tap-latch paths read `recording=True`
        # during a pending grace stop as "not really recording", or a quick
        # stop->talk-again during the grace window would be dropped.
        src = inspect.getsource(self.bloop.BloopFlow._delayed_start_if_held)
        self.assertIn("self._stop_pending or not self.recording", src)
        src = inspect.getsource(self.bloop.BloopFlow._handle_ptt_press)
        self.assertIn("if self._stop_pending or not self.recording:", src)

    def test_finalize_stop_serializes_and_delegates_to_locked_impl(self):
        # The grace timer firing and a restart cutting the grace short race
        # on the same finalize path; _finalize_stop must serialize through
        # _stop_finalize_lock before running the actual (idempotent) work in
        # _finalize_stop_locked.
        src = inspect.getsource(self.bloop.BloopFlow._finalize_stop)
        self.assertIn("with self._stop_finalize_lock:", src)
        self.assertIn("self._finalize_stop_locked()", src)

        src = inspect.getsource(self.bloop.BloopFlow._finalize_stop_locked)
        self.assertIn("if not self._stop_pending:", src)
        self.assertIn("self.recording = False", src)
        self.assertIn("frames = self.frames[:]", src)
        self.assertIn("self._close_stream()", src)
        self.assertIn("self._queue_transcribe(", src)

    def test_finalize_stop_flushes_frames_added_during_grace_tail(self):
        # Behavioral: this is the actual bug being fixed. A frame delivered
        # by _audio_callback after stop_recording() returns (but before the
        # grace timer fires) must still reach the transcriber -- it must not
        # be dropped just because stop_recording already ran.
        bloop = self.bloop
        flow = object.__new__(bloop.BloopFlow)
        flow._lock = threading.Lock()
        flow.recording = True
        flow.frames = ["frame-before-stop"]
        flow._stop_pending = False
        flow._stop_grace_timer = None
        flow._stop_finalize_lock = threading.Lock()
        flow._chunk_stop = threading.Event()
        flow._start_timer = None
        flow._viz = None
        flow._active_paste_target = "com.example.test"
        flow._stream_rate = bloop.SAMPLE_RATE
        flow._session_lock = threading.Lock()
        flow._latch_session_id = None

        queued = []
        flow._queue_transcribe = lambda **kwargs: queued.append(kwargs)
        flow._close_stream = lambda: None

        captured = {}

        class _FakeTimer:
            def __init__(self, interval, function, *a, **kw):
                captured["interval"] = interval
                captured["function"] = function
                self.daemon = False

            def start(self):
                captured["started"] = True

            def cancel(self):
                captured["cancelled"] = True

        real_threading = bloop.threading
        fake_threading = types.SimpleNamespace(
            **{
                name: getattr(real_threading, name)
                for name in dir(real_threading)
                if not name.startswith("__")
            }
        )
        fake_threading.Timer = _FakeTimer
        bloop.threading = fake_threading
        try:
            flow.stop_recording()
            # stop_recording must not have snapshotted/cleared frames itself.
            self.assertTrue(flow.recording)
            self.assertTrue(flow._stop_pending)
            self.assertEqual(flow.frames, ["frame-before-stop"])
            self.assertEqual(captured["interval"], bloop.STOP_GRACE_TAIL_SEC)

            # A frame lands (as _audio_callback would append it) during the
            # grace window, before the timer fires.
            flow.frames.append("frame-during-grace")

            # Fire the captured timer callback -- this is _finalize_stop.
            captured["function"]()
        finally:
            bloop.threading = real_threading

        self.assertFalse(flow.recording)
        self.assertFalse(flow._stop_pending)
        self.assertEqual(len(queued), 1)
        self.assertEqual(
            queued[0]["frames"], ["frame-before-stop", "frame-during-grace"]
        )
        self.assertEqual(queued[0]["paste_target"], "com.example.test")

    def test_start_recording_surfaces_stream_open_failure(self):
        src = inspect.getsource(self.bloop.BloopFlow.start_recording)
        self.assertIn("self._ensure_stream()", src)
        self.assertIn("Microphone unavailable", src)

    def test_ensure_stream_retries_after_portaudio_reset(self):
        src = inspect.getsource(self.bloop.BloopFlow._ensure_stream)
        self.assertIn("self._reset_portaudio()", src)
        self.assertIn("self._open_input_stream()", src)

    def test_whisper_warmup_serializes_with_transcribe_worker(self):
        # Warmup inference runs on the Tk main thread while the worker thread
        # is already live; without the lock a recording finished mid-warmup
        # put two threads inside MLX/Metal at once (intermittent abort).
        src = inspect.getsource(self.bloop.BloopFlow._prepare_whisper_runtime)
        self.assertIn("self._tx_lock.acquire()", src)
        self.assertIn("self._tx_lock.release()", src)

    def test_release_transcribe_memory_prefers_modern_clear_cache(self):
        src = inspect.getsource(self.bloop.BloopFlow._release_transcribe_memory)
        self.assertIn('getattr(mx, "clear_cache", None)', src)

    def test_native_crash_tracebacks_enabled(self):
        import faulthandler

        self.assertTrue(faulthandler.is_enabled())

    def test_history_command_file_follows_state_dir(self):
        self.assertEqual(
            self.bloop.HISTORY_COMMAND_FILE,
            os.path.join(self.bloop.STATE_DIR, "history_command.json"),
        )

    def test_webview_history_panel_inherits_state_dir(self):
        src = inspect.getsource(self.bloop.WebviewHistoryPanel._spawn)
        self.assertIn('env["BLOOOP_STATE_DIR"] = STATE_DIR', src)

    def test_visualizer_idles_slower_when_hidden(self):
        src = inspect.getsource(self.bloop.WaveformVisualizer._tick_interval_ms)
        self.assertIn("self.IDLE_FPS", src)
        self.assertGreater(
            self.bloop.WaveformVisualizer.FPS,
            self.bloop.WaveformVisualizer.IDLE_FPS,
        )

    def test_overlay_front_assert_uses_appkit_not_tk_topmost(self):
        # Tk's -topmost resets the NSWindow level below fullscreen windows,
        # which made the pill vanish on maximized (fullscreen-Space) apps.
        # While visible, z-order must be reasserted through AppKit; Tk topmost
        # is only the non-macOS / lookup-failed fallback.
        src = inspect.getsource(self.bloop.WaveformVisualizer._tick)
        self.assertIn("_macos_prepare_overlay_window(self._window, front=True)", src)
        src_show = inspect.getsource(self.bloop.WaveformVisualizer._set_overlay_visible)
        self.assertIn("if not appkit_managed:", src_show)

    def test_pill_uses_fable_palette_and_asterisk(self):
        viz = self.bloop.WaveformVisualizer
        self.assertEqual(viz.CORAL, "#d97757")
        self.assertEqual(len(viz.RAY_PATTERN), 8)
        src = inspect.getsource(viz._draw)
        self.assertIn("self._draw_asterisk(", src)

    def test_native_pill_uses_nonactivating_panel(self):
        # scripts/overlay_probe.py proved plain NSWindows (all Tk can make)
        # are never composited onto fullscreen Spaces while nonactivating
        # NSPanels always are. The pill must stay an NSPanel.
        src = inspect.getsource(self.bloop._create_pill_panel)
        self.assertIn("NSWindowStyleMaskNonactivatingPanel", src)
        self.assertIn("NSWindowCollectionBehaviorFullScreenAuxiliary", src)
        self.assertIn("NSWindowCollectionBehaviorCanJoinAllSpaces", src)
        src = inspect.getsource(self.bloop.WaveformVisualizer.__init__)
        self.assertIn("_create_pill_panel", src)

    def test_pill_panel_follows_active_screen(self):
        # On multi-display setups the pill was hard-pinned to screens[0], so
        # it only ever appeared on the primary monitor. It must target the
        # screen hosting the focused (frontmost) window, with the pointer's
        # screen as fallback, and re-pick the screen every time it is shown.
        src = inspect.getsource(self.bloop._position_pill_panel)
        self.assertIn("_pill_target_screen(force=force)", src)
        src = inspect.getsource(self.bloop._pill_target_screen)
        self.assertIn("_cg_frontmost_window_bounds()", src)
        self.assertIn("NSEvent.mouseLocation()", src)
        src = inspect.getsource(self.bloop.WaveformVisualizer._set_overlay_visible)
        self.assertIn(
            "_position_pill_panel(self._native_panel, self.W, self.H, force=True)", src
        )

    def test_screen_containing_appkit_point(self):
        def _screen(x, y, w, h):
            frame = types.SimpleNamespace(
                origin=types.SimpleNamespace(x=x, y=y),
                size=types.SimpleNamespace(width=w, height=h),
            )
            return types.SimpleNamespace(frame=lambda f=frame: f)

        # Laptop primary + external above-right, AppKit (bottom-left) coords.
        primary = _screen(0, 0, 1512, 982)
        external = _screen(1512, 120, 2560, 1440)
        screens = [primary, external]
        contains = self.bloop._screen_containing_appkit_point
        self.assertIs(contains(100, 100, screens), primary)
        self.assertIs(contains(2000, 800, screens), external)
        self.assertIsNone(contains(-50, 50, screens))

    def test_pill_tick_recycles_panel_when_server_drops_it(self):
        # "Sometimes the pill doesn't overlay" — orderFrontRegardless alone
        # can't detect that the window server silently dropped the panel from
        # the active Space. The tick must ask the server (kCGWindowIsOnscreen),
        # recycle the window order when dropped, and breadcrumb the episode.
        src = inspect.getsource(self.bloop.WaveformVisualizer._tick)
        self.assertIn("_pill_panel_is_composited(self._native_panel) is False", src)
        self.assertIn("self._native_panel.orderOut_(None)", src)
        self.assertIn('"pill_not_composited"', src)
        src = inspect.getsource(self.bloop._pill_panel_is_composited)
        self.assertIn("kCGWindowIsOnscreen", src)

    def test_pill_tick_escalates_to_rebuild_when_recycle_fails(self):
        # Observed 2026-07-07..09: once the server drops the panel, the
        # orderOut/orderFront recycle never restores it — every session logs
        # pill_not_composited and the pill stays invisible. The tick must
        # escalate to a full panel rebuild (fresh window number), bounded per
        # visibility episode, and reset the counters when the pill is shown.
        src = inspect.getsource(self.bloop.WaveformVisualizer._tick)
        self.assertIn("self._pill_drop_streak", src)
        self.assertIn("self._rebuild_native_panel()", src)
        self.assertIn("PILL_MAX_REBUILDS_PER_EPISODE", src)
        src = inspect.getsource(self.bloop.WaveformVisualizer._rebuild_native_panel)
        self.assertIn("_create_pill_panel", src)
        self.assertIn('"pill_panel_rebuilt"', src)
        # Create-then-swap: a failed create must leave the old panel alive.
        self.assertLess(src.index("_create_pill_panel"), src.index("old = self._native_panel"))
        src = inspect.getsource(self.bloop.WaveformVisualizer._set_overlay_visible)
        self.assertIn("self._pill_drop_streak = 0", src)
        self.assertIn("self._pill_rebuilds = 0", src)

    def test_prompt_echo_filtered(self):
        prompt = self.bloop._custom_vocab_initial_prompt(["Blooop"])
        # The observed confabulation: silent chunk -> near-verbatim echo of
        # the prompt's instruction tail ("the" for "these").
        self.assertTrue(self.bloop._looks_like_prompt_echo(
            "Use the exact spellings when they match the spoken audio.", prompt))
        self.assertTrue(self.bloop._looks_like_prompt_echo(
            "Use these exact spellings when they match the spoken audio.", prompt))
        self.assertTrue(self.bloop._looks_like_prompt_echo(
            "Preferred spellings, product names, and proper nouns.", prompt))
        # Real dictation, vocab words, and degenerate inputs pass through.
        self.assertFalse(self.bloop._looks_like_prompt_echo(
            "Let's grab lunch after the meeting tomorrow.", prompt))
        self.assertFalse(self.bloop._looks_like_prompt_echo("Blooop", prompt))
        self.assertFalse(self.bloop._looks_like_prompt_echo("", prompt))
        self.assertFalse(self.bloop._looks_like_prompt_echo(None, prompt))
        self.assertFalse(self.bloop._looks_like_prompt_echo("anything at all here", None))

    def test_segment_confidence_filter(self):
        prompt = self.bloop._custom_vocab_initial_prompt(["Blooop"])
        real = " So let's wire up the new settings panel."
        result = {
            "text": "So let's wire up the new settings panel. Use the exact "
                    "spellings when they match the spoken audio. random noise words",
            "segments": [
                {"text": real, "no_speech_prob": 0.02, "avg_logprob": -0.25},
                # Confident prompt echo: survives whisper's built-in gate
                # (logprob above -0.8) but must be dropped here.
                {"text": " Use the exact spellings when they match the spoken audio.",
                 "no_speech_prob": 0.90, "avg_logprob": -0.30},
                # Sounded like silence AND decoded poorly.
                {"text": " random noise words",
                 "no_speech_prob": 0.85, "avg_logprob": -0.90},
            ],
        }
        text, dropped = self.bloop._filter_result_segments(result, prompt)
        self.assertEqual(text, real.strip())
        self.assertEqual(len(dropped), 2)

    def test_segment_filter_untouched_when_clean(self):
        # No drops -> the exact whole-result text comes back (no re-join
        # artifacts), and malformed segment payloads fail open.
        result = {
            "text": "Hello there.  General Kenobi.",
            "segments": [
                {"text": " Hello there. ", "no_speech_prob": 0.01, "avg_logprob": -0.2},
                {"text": " General Kenobi.", "no_speech_prob": 0.02, "avg_logprob": -0.3},
            ],
        }
        text, dropped = self.bloop._filter_result_segments(result, None)
        self.assertEqual(text, "Hello there.  General Kenobi.")
        self.assertEqual(dropped, [])
        text, dropped = self.bloop._filter_result_segments(
            {"text": "fallback", "segments": ["not-a-dict"]}, None)
        self.assertEqual(text, "fallback")
        self.assertEqual(dropped, [])
        text, dropped = self.bloop._filter_result_segments({"text": "fallback"}, None)
        self.assertEqual(text, "fallback")
        self.assertEqual(dropped, [])

    def test_transcribe_audio_applies_segment_and_echo_filters(self):
        src = inspect.getsource(self.bloop.BloopFlow._transcribe_audio)
        self.assertIn("text, dropped_segments = _filter_result_segments(", src)
        self.assertIn("result,", src)
        self.assertIn("initial_prompt,", src)
        self.assertIn("audio=model_audio,", src)
        self.assertIn("sample_rate=SAMPLE_RATE,", src)
        self.assertIn("_looks_like_prompt_echo(text, initial_prompt)", src)
        self.assertIn('"segments_filtered"', src)

    def test_pill_style_setting_round_trip(self):
        out = self.bloop._settings_normalize({"pill_style": "spectrogram"})
        self.assertEqual(out["pill_style"], "spectrogram")
        out = self.bloop._settings_normalize({"pill_style": "nonsense"})
        self.assertEqual(out["pill_style"], "bubbles")
        self.assertIn("bubbles", self.bloop.PILL_STYLE_DIMS)
        self.assertIn("spectrogram", self.bloop.PILL_STYLE_DIMS)

    def test_menu_bar_level_repaints_only_on_bucket_change(self):
        src = inspect.getsource(self.bloop._BloopMenuBarController.set_level)
        self.assertIn("_bloop_level_bucket", src)
        # PyObjC registers camelCase methods as python_selector objects;
        # unwrap to the underlying function for inspect.
        cached = self.bloop._BloopMenuBarController._cachedRecordingImage
        src = inspect.getsource(getattr(cached, "callable", cached))
        self.assertIn("_bloop_image_cache", src)

    def test_silence_trim_pad_tail_constant_exists_and_is_asymmetric(self):
        # The tail truncation bug also came from a symmetric silence-trim
        # pad: the trailing edge got the same (short) pad as the leading
        # edge, so a soft/trailing word right at the detected voice boundary
        # got clipped. The tail pad must exist and be at least as generous
        # as the head pad.
        self.assertTrue(hasattr(self.bloop, "SILENCE_TRIM_PAD_TAIL_MS"))
        self.assertGreaterEqual(
            self.bloop.SILENCE_TRIM_PAD_TAIL_MS, self.bloop.SILENCE_TRIM_PAD_MS
        )

    def test_silence_presets_all_define_pad_tail_ms(self):
        for name, cfg in self.bloop.SILENCE_PRESETS.items():
            self.assertIn("pad_tail_ms", cfg, f"preset {name!r} missing pad_tail_ms")
            self.assertGreaterEqual(
                cfg["pad_tail_ms"], cfg["pad_ms"],
                f"preset {name!r} tail pad shorter than head pad",
            )

    def test_apply_silence_preset_sets_tail_pad_global(self):
        src = inspect.getsource(self.bloop._apply_silence_preset)
        self.assertIn("global SILENCE_TRIM_PAD_TAIL_MS", src)
        # Strict lookup, matching every other preset field: a preset missing
        # pad_tail_ms should fail loudly, not silently inherit the previous
        # preset's tail pad.
        self.assertIn('int(cfg["pad_tail_ms"])', src)

        self.bloop._apply_silence_preset("aggressive")
        try:
            self.assertEqual(
                self.bloop.SILENCE_TRIM_PAD_TAIL_MS,
                self.bloop.SILENCE_PRESETS["aggressive"]["pad_tail_ms"],
            )
        finally:
            self.bloop._apply_silence_preset(self.bloop.DEFAULT_SILENCE_PRESET)

    def test_trim_silence_uses_asymmetric_pad_for_end_index(self):
        src = inspect.getsource(self.bloop.BloopFlow._trim_silence)
        self.assertIn(
            "pad_tail = int(src_rate * (SILENCE_TRIM_PAD_TAIL_MS / 1000.0))", src
        )
        self.assertIn("end = min(len(audio), last * hop + frame + pad_tail)", src)
        # The head pad must still use the (shorter) head constant, not the
        # tail constant, or the asymmetry silently collapses.
        self.assertIn("pad = int(src_rate * (SILENCE_TRIM_PAD_MS / 1000.0))", src)
        self.assertIn("start = max(0, first * hop - pad)", src)

    def test_whisper_tail_pad_constant_is_positive(self):
        # Even after silence trimming, Whisper's own attention window can
        # clip the very last phoneme of an utterance ending right at frame
        # zero of a hard cut. WHISPER_TAIL_PAD_SEC appends silence so the
        # model never sees audio ending abruptly at the last word.
        self.assertGreater(self.bloop.WHISPER_TAIL_PAD_SEC, 0)

    def test_transcribe_audio_pads_tail_after_resample(self):
        src = inspect.getsource(self.bloop.BloopFlow._transcribe_audio)
        resample_idx = src.index("self._resample_audio(")
        pad_idx = src.index("WHISPER_TAIL_PAD_SEC > 0")
        self.assertLess(resample_idx, pad_idx)
        self.assertIn(
            "np.zeros(int(SAMPLE_RATE * WHISPER_TAIL_PAD_SEC), dtype=\"float32\")",
            src,
        )
        # The padded array must be what actually gets written to the wav
        # fed to Whisper, not a discarded local.
        concat_idx = src.index("model_audio = np.concatenate(")
        write_idx = src.index("sf.write(tmp, model_audio, SAMPLE_RATE)")
        self.assertLess(concat_idx, write_idx)

    def test_mic_sensitivity_in_settings_defaults(self):
        defaults = self.bloop._settings_defaults()
        self.assertEqual(defaults["mic_sensitivity"], self.bloop.DEFAULT_MIC_SENSITIVITY)
        self.assertEqual(self.bloop.DEFAULT_MIC_SENSITIVITY, "normal")

    def test_mic_sensitivity_normalize_invalid_falls_back_and_valid_round_trips(self):
        out = self.bloop._settings_normalize({"mic_sensitivity": "bogus"})
        self.assertEqual(out["mic_sensitivity"], "normal")

        out = self.bloop._settings_normalize({"mic_sensitivity": "high"})
        self.assertEqual(out["mic_sensitivity"], "high")

        out = self.bloop._settings_normalize({"mic_sensitivity": "low"})
        self.assertEqual(out["mic_sensitivity"], "low")

        # Missing key entirely also falls back to the default.
        out = self.bloop._settings_normalize({})
        self.assertEqual(out["mic_sensitivity"], "normal")

    def test_apply_runtime_settings_applies_silence_preset_before_mic_sensitivity(self):
        # The offsets in _apply_mic_sensitivity mutate the base values that
        # _apply_silence_preset just set; running them in the other order
        # would compound the offset across every settings reload instead of
        # freshly layering it on the preset's base value each time.
        src = inspect.getsource(self.bloop._apply_runtime_settings)
        silence_idx = src.index("_apply_silence_preset(")
        sensitivity_idx = src.index("_apply_mic_sensitivity(")
        self.assertLess(silence_idx, sensitivity_idx)

    def test_apply_mic_sensitivity_high_offsets_compose_on_fresh_base(self):
        try:
            self.bloop._apply_silence_preset("normal")
            self.bloop._apply_mic_sensitivity("high")
            self.assertEqual(self.bloop.WHISPER_SEGMENT_NO_SPEECH_PROB, 0.80)
            self.assertEqual(self.bloop.SILENCE_TRIM_DBFS, -48.0)
        finally:
            self.bloop._apply_silence_preset(self.bloop.DEFAULT_SILENCE_PRESET)
            self.bloop._apply_mic_sensitivity(self.bloop.DEFAULT_MIC_SENSITIVITY)

    def test_mic_sensitivity_presets_all_keys_and_directionality(self):
        presets = self.bloop.MIC_SENSITIVITY_PRESETS
        for key in ("high", "normal", "low"):
            self.assertIn(key, presets)

        high = presets["high"]
        normal = presets["normal"]
        low = presets["low"]

        # "high" favors picking up quiet speech: looser (more permissive)
        # gates than normal, and a negative dbfs offset (lower trim
        # threshold = trims less aggressively).
        self.assertGreater(high["seg_no_speech"], normal["seg_no_speech"])
        self.assertLess(high["dbfs_offset"], 0.0)

        # "low" favors suppressing noise in loud rooms: the reverse.
        self.assertLess(low["seg_no_speech"], normal["seg_no_speech"])
        self.assertGreater(low["dbfs_offset"], 0.0)

    def test_auto_gain_constants(self):
        self.assertTrue(self.bloop.AUTO_GAIN_ENABLED)
        self.assertGreater(self.bloop.AUTO_GAIN_MAX, 1.0)
        self.assertLess(self.bloop.AUTO_GAIN_PEAK_CEILING, 1.0)

    def test_transcribe_audio_calls_auto_gain_after_min_duration_before_trim(self):
        src = inspect.getsource(self.bloop.BloopFlow._transcribe_audio)
        min_duration_idx = src.index("if duration < MIN_DURATION:")
        rms_idx = src.index("raw_rms = float(")
        auto_gain_idx = src.index("audio, _agc_gain = _auto_gain(audio, src_rate)")
        trim_idx = src.index("work_audio, voiced_duration = self._trim_silence(")

        # raw_rms/raw_peak measure the untouched mic signal, so they must be
        # computed before the gain boost is applied.
        self.assertLess(rms_idx, auto_gain_idx)
        # The MIN_DURATION short-circuit must happen before any processing.
        self.assertLess(min_duration_idx, auto_gain_idx)
        # Auto-gain must run before silence trimming sees the audio.
        self.assertLess(auto_gain_idx, trim_idx)

    def test_filter_result_segments_energy_rescue_keeps_voiced_segment(self):
        result = {
            "text": "quiet real speech",
            "segments": [
                {
                    "text": "quiet real speech",
                    "no_speech_prob": 0.9,
                    "avg_logprob": -0.9,
                    "start": 0.0,
                    "end": 1.0,
                },
            ],
        }

        # Old behavior preserved: with no audio supplied, the gate drops the
        # segment exactly as before this feature existed.
        text, dropped = self.bloop._filter_result_segments(result, None)
        self.assertEqual(text, "")
        self.assertEqual(len(dropped), 1)

        orig_voiced = self.bloop._segment_sounds_voiced
        orig_issues_append = self.bloop._issues_append
        captured = []

        def fake_segment_sounds_voiced(*_args, **_kwargs):
            return True

        def fake_issues_append(*args, **kwargs):
            captured.append((args, kwargs))

        self.bloop._segment_sounds_voiced = fake_segment_sounds_voiced
        self.bloop._issues_append = fake_issues_append
        try:
            text2, dropped2 = self.bloop._filter_result_segments(
                result, None, audio=object(), sample_rate=16000
            )
        finally:
            self.bloop._segment_sounds_voiced = orig_voiced
            self.bloop._issues_append = orig_issues_append

        self.assertEqual(text2, "quiet real speech")
        self.assertEqual(dropped2, [])
        self.assertTrue(captured, "expected a segment_kept_voiced breadcrumb")
        self.assertEqual(captured[0][0][0], "segment_kept_voiced")


if __name__ == "__main__":
    unittest.main()
