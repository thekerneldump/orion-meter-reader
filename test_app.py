"""Unit tests for the Orion Meter Reader service."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("orion-meter-reader.py")
SPEC = importlib.util.spec_from_file_location("orion_meter_reader", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
orion_meter_reader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = orion_meter_reader
SPEC.loader.exec_module(orion_meter_reader)

Config = orion_meter_reader.Config
ReadingStore = orion_meter_reader.ReadingStore
Receiver = orion_meter_reader.Receiver
RadioManager = orion_meter_reader.RadioManager
decorate_event = orion_meter_reader.decorate_event
canonical_frequency_mhz = orion_meter_reader.canonical_frequency_mhz
APP_VERSION = orion_meter_reader.APP_VERSION


class FakeProcess:
    def __init__(self):
        self.exit_code = None
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout=None):
        if self.exit_code is None:
            raise orion_meter_reader.subprocess.TimeoutExpired(
                "rtl_433", timeout
            )
        return self.exit_code

    def kill(self):
        self.exit_code = -9


class VersionTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(APP_VERSION, "0.0.1")


class ConversionTests(unittest.TestCase):
    def test_protocol_290_counter_conversion(self):
        event = decorate_event(
            {"id": 12345678, "reading": 12345, "daily_reading": 12000}
        )
        self.assertEqual(event["reading_gallons"], 1234.5)
        self.assertEqual(event["daily_reading_gallons"], 1200.0)
        self.assertEqual(event["usage_since_snapshot_gallons"], 34.5)

    def test_non_numeric_readings_are_preserved_without_conversion(self):
        event = decorate_event({"id": 12345678, "reading": "unknown"})
        self.assertEqual(event["reading"], "unknown")
        self.assertNotIn("reading_gallons", event)


class StoreTests(unittest.TestCase):
    def test_filters_and_persists_configured_meter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.jsonl"
            config = Config(
                sdr_serial="ORION",
                meter_ids=frozenset({"12345678"}),
                data_file=path,
            )
            store = ReadingStore(config)
            self.assertIsNone(store.record({"id": 87654321, "reading": 10}))
            stored = store.record(
                {"id": 12345678, "reading": 100, "daily_reading": 90}
            )
            self.assertEqual(stored["usage_since_snapshot_gallons"], 1.0)
            self.assertEqual(len(path.read_text().splitlines()), 1)
            self.assertEqual(store.get("12345678")["reading_gallons"], 10.0)

    def test_loads_latest_reading_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.jsonl"
            path.write_text(
                json.dumps({"id": 12345678, "reading": 100})
                + "\n"
                + json.dumps({"id": 12345678, "reading": 110})
                + "\n"
            )
            store = ReadingStore(
                Config(sdr_serial="ORION", data_file=path)
            )
            self.assertEqual(store.get("12345678")["reading"], 110)

    def test_retained_data_files_are_oldest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.jsonl"
            path.with_name("readings.jsonl.2").write_text("oldest\n")
            path.with_name("readings.jsonl.1").write_text("older\n")
            path.write_text("current\n")
            store = ReadingStore(
                Config(
                    sdr_serial="ORION",
                    data_file=path,
                    rotate_count=2,
                )
            )
            self.assertEqual(
                [candidate.name for candidate in store.retained_data_files()],
                ["readings.jsonl.2", "readings.jsonl.1", "readings.jsonl"],
            )

    def test_exposes_only_safe_jsonl_data_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.jsonl"
            path.write_text("current\n")
            path.with_name("readings.jsonl.1").write_text("older\n")
            path.with_name("920.9MHz.jsonl").write_text("capture\n")
            path.with_name("notes.txt").write_text("private\n")
            store = ReadingStore(Config(sdr_serial="ORION", data_file=path))

            self.assertEqual(
                [item["name"] for item in store.available_data_files()],
                ["920.9MHz.jsonl", "readings.jsonl", "readings.jsonl.1"],
            )
            self.assertEqual(
                store.resolve_data_file("920.9MHz.jsonl"),
                path.with_name("920.9MHz.jsonl").resolve(),
            )
            self.assertIsNone(store.resolve_data_file("../secret.jsonl"))
            self.assertIsNone(store.resolve_data_file("notes.txt"))


class ReceiverTests(unittest.TestCase):
    def test_serial_selector_and_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                data_file=Path(directory) / "readings.jsonl",
            )
            receiver = Receiver(config, ReadingStore(config))
            command = receiver.command()
            self.assertEqual(command[command.index("-d") + 1], ":ORION")
            self.assertEqual(command[command.index("-R") + 1], "290")
            self.assertEqual(command[command.index("-f") + 1], "905.2M")


class RadioManagerTests(unittest.TestCase):
    def test_frequency_validation(self):
        self.assertEqual(canonical_frequency_mhz(904.8), "904.8")
        self.assertEqual(canonical_frequency_mhz("924.000"), "924")
        with self.assertRaises(ValueError):
            canonical_frequency_mhz(901.9)
        with self.assertRaises(ValueError):
            canonical_frequency_mhz("not-a-frequency")

    def test_starts_retunes_and_stops_allowed_auxiliary_radio(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                data_file=Path(directory) / "readings.jsonl",
                radio_control_token="test-token",
                radio_control_serials=frozenset({"AUX1"}),
            )
            manager = RadioManager(config)
            first_process = FakeProcess()
            second_process = FakeProcess()
            with mock.patch.object(
                orion_meter_reader.subprocess,
                "Popen",
                side_effect=[first_process, second_process],
            ) as popen:
                first = manager.start(
                    "demo",
                    serial="AUX1",
                    frequency_mhz=904.8,
                )
                second = manager.start(
                    "demo",
                    serial="AUX1",
                    frequency_mhz=924.0,
                )

            self.assertEqual(first["filename"], "904.8MHz.jsonl")
            self.assertEqual(second["filename"], "924MHz.jsonl")
            self.assertTrue(first_process.terminated)
            first_command = popen.call_args_list[0].args[0]
            second_command = popen.call_args_list[1].args[0]
            self.assertEqual(first_command[first_command.index("-d") + 1], ":AUX1")
            self.assertEqual(first_command[first_command.index("-f") + 1], "904.8M")
            self.assertEqual(second_command[second_command.index("-f") + 1], "924M")
            self.assertTrue(manager.stop("demo"))
            self.assertTrue(second_process.terminated)

    def test_rejects_production_unlisted_and_unsafe_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                data_file=Path(directory) / "readings.jsonl",
                radio_control_token="test-token",
                radio_control_serials=frozenset({"AUX1"}),
            )
            manager = RadioManager(config)
            with self.assertRaisesRegex(ValueError, "production"):
                manager.start("demo", serial="ORION", frequency_mhz=910)
            with self.assertRaisesRegex(ValueError, "not allowed"):
                manager.start("demo", serial="AUX2", frequency_mhz=910)
            with self.assertRaisesRegex(ValueError, "filename"):
                manager.start(
                    "demo",
                    serial="AUX1",
                    frequency_mhz=910,
                    filename="../capture.jsonl",
                )

    def test_reports_immediate_rtl433_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                data_file=Path(directory) / "readings.jsonl",
                radio_control_token="test-token",
                radio_control_serials=frozenset({"AUX1"}),
            )
            manager = RadioManager(config)
            process = FakeProcess()
            process.exit_code = 2
            with mock.patch.object(
                orion_meter_reader.subprocess,
                "Popen",
                return_value=process,
            ):
                with self.assertRaisesRegex(RuntimeError, "status 2"):
                    manager.start(
                        "demo",
                        serial="AUX1",
                        frequency_mhz=910,
                    )


if __name__ == "__main__":
    unittest.main()
