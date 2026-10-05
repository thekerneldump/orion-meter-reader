"""Unit tests for the Orion Meter Reader service."""

import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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
AppServer = orion_meter_reader.AppServer
decorate_event = orion_meter_reader.decorate_event
canonical_frequency_mhz = orion_meter_reader.canonical_frequency_mhz
APP_VERSION = orion_meter_reader.APP_VERSION


class FakeProcess:
    def __init__(self, output=""):
        self.exit_code = None
        self.terminated = False
        self.stdout = io.StringIO(output)

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


class ConfigTests(unittest.TestCase):
    def test_none_disables_extra_rtl433_arguments(self):
        with mock.patch.dict(
            orion_meter_reader.os.environ,
            {"SDR_SERIAL": "ORION", "RTL433_EXTRA_ARGS": "none"},
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.rtl433_extra_args, ())

    def test_loads_silence_seek_configuration(self):
        with mock.patch.dict(
            orion_meter_reader.os.environ,
            {
                "SDR_SERIAL": "ORION",
                "AUTO_SEEK_ENABLED": "true",
                "AUTO_SEEK_SILENCE_SECONDS": "180",
                "AUTO_SEEK_FREQUENCIES_MHZ": "904.8,910.0,922.4",
            },
            clear=True,
        ):
            config = Config.from_env()
        self.assertTrue(config.auto_seek_enabled)
        self.assertEqual(config.auto_seek_silence_seconds, 180)
        self.assertEqual(
            config.auto_seek_frequencies_mhz,
            (904.8, 910.0, 922.4),
        )


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

    def test_recenters_after_consistent_matching_packets(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                frequency="921.2M",
                meter_ids=frozenset({"12345678"}),
                data_file=Path(directory) / "readings.jsonl",
                auto_recenter_enabled=True,
                auto_recenter_threshold_mhz=0.35,
                auto_recenter_min_packets=5,
                auto_recenter_window_seconds=300,
                auto_recenter_cooldown_seconds=900,
            )
            receiver = Receiver(config, ReadingStore(config))
            adjustment = None
            for index in range(5):
                adjustment = receiver.observe_frequency(
                    {
                        "id": 12345678,
                        "freq1": 921.48 + index * 0.001,
                        "freq2": 921.68 + index * 0.001,
                    },
                    now=float(index),
                )

            self.assertIsNotNone(adjustment)
            self.assertEqual(adjustment["old_frequency_mhz"], 921.2)
            self.assertEqual(adjustment["new_frequency_mhz"], 921.6)
            self.assertNotIn("id", adjustment)
            command = receiver.command()
            self.assertEqual(command[command.index("-f") + 1], "921.6M")

    def test_authenticated_retune_requests_immediate_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                frequency="921.2M",
                data_file=Path(directory) / "readings.jsonl",
            )
            receiver = Receiver(config, ReadingStore(config))
            process = FakeProcess()
            receiver.process = process
            receiver.running = True

            result = receiver.retune(922.4)

            self.assertTrue(result["changed"])
            self.assertTrue(result["restart_requested"])
            self.assertFalse(result["persistent"])
            self.assertTrue(process.terminated)
            self.assertEqual(receiver.current_frequency_mhz, 922.4)
            command = receiver.command()
            self.assertEqual(command[command.index("-f") + 1], "922.4M")
            self.assertNotIn("serial", result)

    def test_seeks_next_center_after_three_minutes_without_packets(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                frequency="922.4M",
                meter_ids=frozenset({"12345678"}),
                data_file=Path(directory) / "readings.jsonl",
                auto_seek_enabled=True,
                auto_seek_silence_seconds=180,
                auto_seek_frequencies_mhz=(904.8, 906.4, 922.4, 924.0),
                auto_recenter_enabled=True,
            )
            receiver = Receiver(config, ReadingStore(config))
            process = FakeProcess()
            receiver.process = process
            receiver.running = True
            receiver.last_matching_packet_monotonic = 0

            self.assertIsNone(receiver.seek_if_silent(now=179))
            adjustment = receiver.seek_if_silent(now=180)

            self.assertIsNotNone(adjustment)
            self.assertEqual(adjustment["frequency_mhz"], 924.0)
            self.assertTrue(process.terminated)
            self.assertIsNotNone(receiver.last_seek_at)
            self.assertIsNone(receiver.last_recenter_monotonic)
            command = receiver.command()
            self.assertEqual(command[command.index("-f") + 1], "924M")

    def test_recenter_ignores_unconfigured_meter(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                frequency="921.2M",
                meter_ids=frozenset({"12345678"}),
                data_file=Path(directory) / "readings.jsonl",
                auto_recenter_enabled=True,
                auto_recenter_min_packets=3,
            )
            receiver = Receiver(config, ReadingStore(config))
            for index in range(5):
                adjustment = receiver.observe_frequency(
                    {
                        "id": 87654321,
                        "freq1": 922.0,
                        "freq2": 922.2,
                    },
                    now=float(index),
                )
                self.assertIsNone(adjustment)
            self.assertEqual(receiver.current_frequency_mhz, 921.2)

    def test_recenter_is_disabled_for_frequency_hopping_receiver(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                meter_ids=frozenset({"12345678"}),
                data_file=Path(directory) / "readings.jsonl",
                rtl433_extra_args=("-f", "921.2M", "-H", "5"),
                auto_recenter_enabled=True,
            )
            receiver = Receiver(config, ReadingStore(config))
            self.assertFalse(receiver.auto_recenter_active)
            self.assertIn("frequency hopping", receiver.auto_recenter_disabled_reason)


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

    def test_starts_multi_frequency_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                data_file=Path(directory) / "readings.jsonl",
                radio_control_token="test-token",
                radio_control_serials=frozenset({"AUX1"}),
            )
            manager = RadioManager(config)
            process = FakeProcess()
            with mock.patch.object(
                orion_meter_reader.subprocess,
                "Popen",
                return_value=process,
            ) as popen:
                status = manager.start(
                    "scanner",
                    serial="AUX1",
                    frequencies_mhz=[905.2, 910.0, 921.2],
                    hop_seconds=120,
                    filename="discovery.jsonl",
                )

            command = popen.call_args.args[0]
            self.assertEqual(
                [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "-f"
                ],
                ["905.2M", "910M", "921.2M"],
            )
            self.assertEqual(command[command.index("-H") + 1], "120")
            self.assertIsNone(status["frequency_mhz"])
            self.assertEqual(status["frequencies_mhz"], [905.2, 910.0, 921.2])
            self.assertEqual(status["hop_seconds"], 120)

    def test_rejects_invalid_sweep_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                data_file=Path(directory) / "readings.jsonl",
                radio_control_token="test-token",
                radio_control_serials=frozenset({"AUX1"}),
            )
            manager = RadioManager(config)
            with self.assertRaisesRegex(ValueError, "2 through 32"):
                manager.start(
                    "scanner",
                    serial="AUX1",
                    frequencies_mhz=[905.2],
                    hop_seconds=120,
                )
            with self.assertRaisesRegex(ValueError, "between 5 and 3600"):
                manager.start(
                    "scanner",
                    serial="AUX1",
                    frequencies_mhz=[905.2, 910.0],
                    hop_seconds=1,
                )

    def test_publishes_only_allowlisted_auxiliary_meter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.jsonl"
            config = Config(
                sdr_serial="ORION",
                meter_ids=frozenset({"11111111"}),
                data_file=path,
                radio_control_token="test-token",
                radio_control_serials=frozenset({"AUX1"}),
            )
            store = ReadingStore(config)
            raw_events = "".join(
                json.dumps(event) + "\n"
                for event in (
                    {"id": 22222222, "reading": 100},
                    {"id": 33333333, "reading": 200},
                )
            )
            process = FakeProcess(raw_events)
            manager = RadioManager(config, store)
            with mock.patch.object(
                orion_meter_reader.subprocess,
                "Popen",
                return_value=process,
            ):
                status = manager.start(
                    "scanner",
                    serial="AUX1",
                    frequency_mhz=916.4,
                    filename="discovery.jsonl",
                    publish_meter_ids=[22222222],
                )

            manager.captures["scanner"].output_thread.join(timeout=2)
            self.assertEqual(status["published_meter_count"], 1)
            self.assertNotIn("publish_meter_ids", status)
            self.assertIn("22222222", store.snapshot())
            self.assertNotIn("33333333", store.snapshot())
            self.assertEqual(len(path.read_text().splitlines()), 1)
            self.assertEqual(
                len((Path(directory) / "discovery.jsonl").read_text().splitlines()),
                2,
            )

    def test_rejects_invalid_publish_meter_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                data_file=Path(directory) / "readings.jsonl",
                radio_control_token="test-token",
                radio_control_serials=frozenset({"AUX1"}),
            )
            manager = RadioManager(config)
            with self.assertRaisesRegex(ValueError, "JSON array"):
                manager.start(
                    "scanner",
                    serial="AUX1",
                    frequency_mhz=916.4,
                    publish_meter_ids="22222222",
                )


class ApiTests(unittest.TestCase):
    def test_production_retune_needs_token_but_not_auxiliary_serials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                sdr_serial="ORION",
                frequency="921.2M",
                data_file=Path(directory) / "readings.jsonl",
                radio_control_token="test-token",
            )
            store = ReadingStore(config)
            receiver = Receiver(config, store)
            server = AppServer(
                ("127.0.0.1", 0),
                store,
                receiver,
                RadioManager(config),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/api/receiver"
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps({"frequency_mhz": 922.4}).encode(),
                    headers={
                        "Authorization": "Bearer test-token",
                        "Content-Type": "application/json",
                    },
                    method="PUT",
                )
                with urllib.request.urlopen(request) as response:
                    payload = json.load(response)
                self.assertTrue(payload["changed"])
                self.assertEqual(payload["frequency_mhz"], 922.4)
                self.assertEqual(receiver.current_frequency_mhz, 922.4)

                unauthorized = urllib.request.Request(
                    url,
                    data=json.dumps({"frequency_mhz": 923.0}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(unauthorized)
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
