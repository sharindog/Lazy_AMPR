import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

import utils.state as state_module
from core.lz4_packer import loose_paths_from_listing, parse_pack_result, run_lz4_pack
from gui.game_card import GameCard
from gui.main_window import MainWindow
from gui.one_shot_page import OneShotPage
from utils.process_manager import ExtractWorker, ProfileWorker
from utils.state import DEFAULT_SETTINGS


def _game(root, title_id="PPSA00001"):
    (root / "sce_sys").mkdir(parents=True)
    (root / "sce_sys" / "param.json").write_text(
        f'{{"titleId": "{title_id}", "contentVersion": "01.000.000"}}', encoding="utf-8"
    )
    return root


class PackOutputParsingTests(unittest.TestCase):
    def test_pack_result_survives_braces_in_paths_and_progress_lines(self):
        lines = [
            "[pack  10%] planning: files 1/2, current=data/{a}.bin, elapsed 00:00:01",
            "{",
            '"files_total": 2,',
            '"loose_paths": [',
            '"sce_sys/{weird}.txt",',
            '"boot.pkg"',
            "],",
            '"warnings": []',
            "}",
        ]
        result = parse_pack_result(lines)
        self.assertEqual(result["loose_paths"], ["sce_sys/{weird}.txt", "boot.pkg"])

    def test_pack_result_is_none_without_json(self):
        self.assertIsNone(parse_pack_result(["[pack 100%] complete", "error: boom"]))

    def test_listing_yields_only_loose_relative_paths(self):
        lines = [
            '[{"path": "/app0/assets/a.bin", "packed": true},',
            ' {"path": "/app0/boot.pkg", "packed": false}]',
        ]
        self.assertEqual(loose_paths_from_listing(lines), {"boot.pkg"})


class PackPipelineRegressionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lazy_ampr_regression_"))
        self.source = _game(self.root / "game")
        (self.source / "assets").mkdir()
        (self.source / "assets" / "world.bin").write_bytes(b"compressible\n" * 50000)
        self.settings = {"lz4_level": 1, "auto_loose_large": False}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_brace_in_a_file_name_keeps_unlisted_loose_files(self):
        # boot.pkg is loose only through default_action, not SAFETY_EXCLUSIONS,
        # so it is lost if the pack's loose_paths cannot be read.
        (self.source / "boot.pkg").write_bytes(b"loose by default")
        (self.source / "sce_sys" / "{weird}.txt").write_text("x", encoding="utf-8")
        output = self.root / "out"
        logs = []
        run_lz4_pack(self.source, output, self.settings, workers=1,
                     progress_callback=logs.append)
        self.assertEqual((output / "boot.pkg").read_bytes(), b"loose by default")
        self.assertTrue((output / "sce_sys" / "{weird}.txt").is_file())
        self.assertFalse(any("falling back to SAFETY_EXCLUSIONS" in line for line in logs))
        self.assertFalse(any('"loose_paths"' in line for line in logs))

    def test_rerun_into_same_output_replaces_stale_loose_files(self):
        output = self.root / "out"
        run_lz4_pack(self.source, output, self.settings, workers=1)
        param = self.source / "sce_sys" / "param.json"
        param.write_text('{"titleId": "PPSA00001", "contentVersion": "01.010.000"}',
                         encoding="utf-8")
        future = time.time() + 60
        os.utime(param, (future, future))
        # Diagnostics fail the build if a stale loose file or index is kept.
        run_lz4_pack(self.source, output, self.settings, workers=1)
        self.assertEqual((output / "sce_sys" / "param.json").read_bytes(), param.read_bytes())


class AutoTomlMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.toml_dir = root / "toml_profiles"
        patches = [
            patch.object(state_module, "DATA_DIR", root),
            patch.object(state_module, "TOML_DIR", self.toml_dir),
            patch.object(state_module, "STATE_FILE", root / "state.json"),
            patch.object(state_module, "BUNDLE_ROOT", root / "bundle"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.state = state_module.State()
        for name in ("astrobot.toml", "spiderman_milesmorales.toml", "spiderman.toml"):
            (self.toml_dir / name).write_text("[pack]\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_and_most_specific_matches_win(self):
        self.assertEqual(self.state.auto_toml_for("PPSA01234", "ASTRO BOT"), "astrobot.toml")
        self.assertEqual(
            self.state.auto_toml_for("", "Marvel's Spider-Man: Miles Morales"),
            "spiderman_milesmorales.toml",
        )

    def test_short_names_and_placeholders_do_not_match(self):
        self.assertIsNone(self.state.auto_toml_for("Unknown", "Bot", "Unknown"))
        self.assertIsNone(self.state.auto_toml_for("", ""))


class WorkerRegressionTests(unittest.TestCase):
    def test_extract_worker_does_not_shadow_qthread_finished(self):
        # deleteLater is connected to QThread.finished; a custom signal of the
        # same name fired from run() would delete a still-running thread.
        self.assertIs(ExtractWorker.finished, QThread.finished)

    def test_profile_name_never_defaults_to_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = ProfileWorker(temporary, temporary, "Unknown", "My Game")
            names = []
            worker.profile_finished.connect(lambda ok, name, message: names.append(name))
            with patch("utils.process_manager.generate_trace_profile"):
                worker.run()
        self.assertEqual(names, ["my_game.toml"])


class FakeState:
    def __init__(self):
        self.settings = dict(DEFAULT_SETTINGS)
        self.games = {}

    def save(self):
        pass

    def tomls(self):
        return []

    def auto_toml_for(self, *keys):
        return None

    def games_using(self, name):
        return []

    def upsert_game(self, path, **values):
        entry = self.games.setdefault(str(path), {"path": str(path)})
        entry.update(values)
        return entry

    def link_toml(self, path, name, source="auto"):
        self.upsert_game(path, toml=name, toml_src=source)


class UiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_one_shot_forgets_previous_game_config_and_traces(self):
        page = OneShotPage()
        page.set_folder(_game(self.root / "first", "PPSA00001"))
        page.custom_config_path = self.root / "first.toml"
        page.traces_dir = self.root / "traces"
        page.apply_link("first.toml", False)
        page.set_folder(_game(self.root / "second", "PPSA00002"))
        self.assertIsNone(page.custom_config_path)
        self.assertIsNone(page.traces_dir)
        self.assertIsNone(page.toml_name)
        self.assertEqual(page.cfg_lbl.text(), "Auto-detect from Toml list")

    def test_one_shot_rejects_a_new_input_while_processing(self):
        page = OneShotPage()
        first = _game(self.root / "first", "PPSA00001")
        page.load_input(first)
        page.set_running_state(True)
        self.assertFalse(page.load_input(_game(self.root / "second", "PPSA00002")))
        self.assertEqual(page.game_dir, first)
        page.set_running_state(False)
        self.assertTrue(page.load_input(self.root / "second"))

    def test_toml_list_change_keeps_a_config_imported_on_the_card(self):
        class AutoMatchingState(FakeState):
            def auto_toml_for(self, *keys):
                return "auto.toml"

        with patch("gui.main_window.State", AutoMatchingState):
            window = MainWindow()
        game = _game(self.root / "game")
        card = GameCard({"title": "Game", "title_id": "PPSA00001"})
        manual = self.root / "manual.toml"
        card.custom_config_path, card.toml_name, card.toml_auto = manual, manual.name, False
        window.state.upsert_game(game, toml="other.toml", toml_src="auto")
        window.batch.cards[str(game)] = card
        window._relink_all()
        self.assertEqual(card.custom_config_path, manual)
        window.close()


if __name__ == "__main__":
    unittest.main()
