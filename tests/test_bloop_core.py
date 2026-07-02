import importlib.util
import inspect
import os
from pathlib import Path
import sys
import tempfile
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
        # it also kept the macOS orange mic indicator lit while idle.
        src = inspect.getsource(self.bloop.BloopFlow.stop_recording)
        self.assertIn("self._close_stream()", src)

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


if __name__ == "__main__":
    unittest.main()
