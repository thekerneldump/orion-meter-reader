"""Unit tests for the Orion Meter Reader service."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("orion-meter-reader.py")
SPEC = importlib.util.spec_from_file_location("orion_meter_reader", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
orion_meter_reader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = orion_meter_reader
SPEC.loader.exec_module(orion_meter_reader)

Config = orion_meter_reader.Config
ReadingStore = orion_meter_reader.ReadingStore
Receiver = orion_meter_reader.Receiver
decorate_event = orion_meter_reader.decorate_event
APP_VERSION = orion_meter_reader.APP_VERSION


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


if __name__ == "__main__":
    unittest.main()
