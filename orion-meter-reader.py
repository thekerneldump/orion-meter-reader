#!/usr/bin/env python3
"""Receive Badger ORION packets and expose readings through a small HTTP API."""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import shlex
import signal
import statistics
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

APP_VERSION = "0.0.1"
DATA_FILE_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl(?:\.\d+)?$"
)
RADIO_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SDR_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RADIO_STATE_FILE_NAME = "managed-radios.json"
RADIO_STATE_VERSION = 1
MIN_CONTROL_FREQUENCY_MHZ = 902.0
MAX_CONTROL_FREQUENCY_MHZ = 928.0
DEFAULT_AUTO_SEEK_FREQUENCIES_MHZ = (
    904.8,
    906.4,
    908.0,
    909.6,
    911.2,
    912.8,
    914.4,
    916.0,
    917.6,
    919.2,
    920.8,
    922.4,
    924.0,
)


def utc_now() -> str:
    """Return a timezone-aware ISO timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


def env_float(name: str, default: float) -> float:
    value = os.getenv(name, str(default))
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {value!r}") from exc


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be true or false, got {value!r}")


def env_frequency_list(
    name: str,
    default: tuple[float, ...],
) -> tuple[float, ...]:
    """Load and validate a comma-separated list of MHz center frequencies."""
    raw_values = os.getenv(name, ",".join(str(value) for value in default))
    values: list[float] = []
    for raw_value in raw_values.split(","):
        raw_value = raw_value.strip()
        if not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise SystemExit(
                f"{name} must contain comma-separated numbers"
            ) from exc
        if not math.isfinite(value):
            raise SystemExit(f"{name} values must be finite")
        if not MIN_CONTROL_FREQUENCY_MHZ <= value <= MAX_CONTROL_FREQUENCY_MHZ:
            raise SystemExit(
                f"{name} values must be between {MIN_CONTROL_FREQUENCY_MHZ:g} "
                f"and {MAX_CONTROL_FREQUENCY_MHZ:g}"
            )
        values.append(value)
    if len(set(values)) < 2:
        raise SystemExit(f"{name} must contain at least two unique frequencies")
    if len(values) != len(set(values)):
        raise SystemExit(f"{name} cannot contain duplicate frequencies")
    return tuple(values)


@dataclass(frozen=True)
class Config:
    """Application configuration."""

    sdr_serial: str
    frequency: str = "905.2M"
    sample_rate: str = "1600k"
    gain: str = "0"
    meter_ids: frozenset[str] = frozenset()
    data_file: Path = Path("/data/readings.jsonl")
    max_jsonl_bytes: int = 100 * 1024 * 1024
    rotate_count: int = 4
    http_host: str = "0.0.0.0"
    http_port: int = 8083
    rtl433_bin: str = "rtl_433"
    rtl433_extra_args: tuple[str, ...] = ()
    radio_control_token: str = ""
    radio_control_serials: frozenset[str] = frozenset()
    auto_recenter_enabled: bool = False
    auto_recenter_threshold_mhz: float = 0.35
    auto_recenter_min_packets: int = 5
    auto_recenter_window_seconds: int = 300
    auto_recenter_cooldown_seconds: int = 900
    auto_seek_enabled: bool = False
    auto_seek_silence_seconds: int = 180
    auto_seek_frequencies_mhz: tuple[float, ...] = (
        DEFAULT_AUTO_SEEK_FREQUENCIES_MHZ
    )

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        serial = os.getenv("SDR_SERIAL", "").strip().removeprefix(":")
        if not serial:
            raise SystemExit(
                "SDR_SERIAL is required. Set it to the unique serial of the Orion dongle."
            )

        meter_ids = frozenset(
            item.strip()
            for item in os.getenv("METER_IDS", "").split(",")
            if item.strip()
        )
        radio_control_serials = frozenset(
            item.strip().removeprefix(":")
            for item in os.getenv("RADIO_CONTROL_SERIALS", "").split(",")
            if item.strip().removeprefix(":")
        )
        return cls(
            sdr_serial=serial,
            frequency=os.getenv("FREQUENCY", "905.2M"),
            sample_rate=os.getenv("SAMPLE_RATE", "1600k"),
            gain=os.getenv("GAIN", "0"),
            meter_ids=meter_ids,
            data_file=Path(os.getenv("DATA_FILE", "/data/readings.jsonl")),
            max_jsonl_bytes=env_int("MAX_JSONL_BYTES", 100 * 1024 * 1024),
            rotate_count=env_int("JSONL_ROTATE_COUNT", 4),
            http_host=os.getenv("HTTP_HOST", "0.0.0.0"),
            http_port=env_int("HTTP_PORT", 8083),
            rtl433_bin=os.getenv("RTL433_BIN", "rtl_433"),
            rtl433_extra_args=(
                ()
                if os.getenv("RTL433_EXTRA_ARGS", "").strip().lower()
                in {"none", "off"}
                else tuple(shlex.split(os.getenv("RTL433_EXTRA_ARGS", "")))
            ),
            radio_control_token=os.getenv("RADIO_CONTROL_TOKEN", "").strip(),
            radio_control_serials=radio_control_serials,
            auto_recenter_enabled=env_bool("AUTO_RECENTER_ENABLED"),
            auto_recenter_threshold_mhz=env_float(
                "AUTO_RECENTER_THRESHOLD_MHZ", 0.35
            ),
            auto_recenter_min_packets=env_int(
                "AUTO_RECENTER_MIN_PACKETS", 5
            ),
            auto_recenter_window_seconds=env_int(
                "AUTO_RECENTER_WINDOW_SECONDS", 300
            ),
            auto_recenter_cooldown_seconds=env_int(
                "AUTO_RECENTER_COOLDOWN_SECONDS", 900
            ),
            auto_seek_enabled=env_bool("AUTO_SEEK_ENABLED"),
            auto_seek_silence_seconds=env_int(
                "AUTO_SEEK_SILENCE_SECONDS", 180
            ),
            auto_seek_frequencies_mhz=env_frequency_list(
                "AUTO_SEEK_FREQUENCIES_MHZ",
                DEFAULT_AUTO_SEEK_FREQUENCIES_MHZ,
            ),
        )


def canonical_frequency_mhz(value: Any) -> str:
    """Validate and normalize an auxiliary capture frequency."""
    if isinstance(value, bool):
        raise ValueError("frequency_mhz must be a number")
    try:
        frequency = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("frequency_mhz must be a number") from exc
    if not math.isfinite(frequency):
        raise ValueError("frequency_mhz must be finite")
    if not MIN_CONTROL_FREQUENCY_MHZ <= frequency <= MAX_CONTROL_FREQUENCY_MHZ:
        raise ValueError(
            f"frequency_mhz must be between {MIN_CONTROL_FREQUENCY_MHZ:g} "
            f"and {MAX_CONTROL_FREQUENCY_MHZ:g}"
        )
    return f"{frequency:.3f}".rstrip("0").rstrip(".")


def canonical_gain(value: Any) -> str:
    """Validate and normalize an rtl_433 gain value."""
    if isinstance(value, bool):
        raise ValueError("gain must be a number")
    try:
        gain = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("gain must be a number") from exc
    if not math.isfinite(gain) or not 0 <= gain <= 100:
        raise ValueError("gain must be between 0 and 100")
    return f"{gain:.1f}".rstrip("0").rstrip(".")


def configured_frequency_mhz(value: str) -> float:
    """Parse an rtl_433 frequency such as 921.2M into MHz."""
    normalized = value.strip().lower()
    if normalized.endswith("mhz"):
        normalized = normalized[:-3]
    elif normalized.endswith("m"):
        normalized = normalized[:-1]
    return float(canonical_frequency_mhz(normalized))


def meter_id(event: dict[str, Any]) -> str | None:
    """Return an event's endpoint ID as a string."""
    value = event.get("id")
    return None if value is None else str(value)


def decorate_event(event: dict[str, Any]) -> dict[str, Any]:
    """Preserve rtl_433 data and add protocol-290 gallon values."""
    output = dict(event)
    output["ingested_at"] = utc_now()

    reading = event.get("reading")
    snapshot = event.get("daily_reading")
    reading_is_number = isinstance(reading, (int, float)) and not isinstance(
        reading, bool
    )
    snapshot_is_number = isinstance(snapshot, (int, float)) and not isinstance(
        snapshot, bool
    )

    if reading_is_number:
        output["reading_gallons"] = round(reading / 10, 1)
    if snapshot_is_number:
        output["daily_reading_gallons"] = round(snapshot / 10, 1)
    if reading_is_number and snapshot_is_number:
        output["usage_since_snapshot_gallons"] = round(
            (reading - snapshot) / 10, 1
        )
    return output


class ReadingStore:
    """Persist readings and retain the latest event per endpoint."""

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.latest: dict[str, dict[str, Any]] = {}
        self.events_written = 0
        self.config.data_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.config.data_file.exists():
            return
        try:
            with self.config.data_file.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_id = meter_id(event)
                    if event_id:
                        self.latest[event_id] = event
        except OSError as exc:
            print(f"Could not load existing readings: {exc}", flush=True)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        path = self.config.data_file
        if self.config.max_jsonl_bytes <= 0 or not path.exists():
            return
        if path.stat().st_size + incoming_bytes <= self.config.max_jsonl_bytes:
            return
        if self.config.rotate_count <= 0:
            path.unlink(missing_ok=True)
            return

        path.with_name(f"{path.name}.{self.config.rotate_count}").unlink(
            missing_ok=True
        )
        for index in range(self.config.rotate_count - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))

    def _persist(self, raw_event: dict[str, Any]) -> dict[str, Any] | None:
        event_id = meter_id(raw_event)
        if not event_id:
            return None

        event = decorate_event(raw_event)
        line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
        encoded_size = len(line.encode("utf-8"))
        with self.lock:
            self._rotate_if_needed(encoded_size)
            with self.config.data_file.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
            self.latest[event_id] = event
            self.events_written += 1
        return event

    def record(self, raw_event: dict[str, Any]) -> dict[str, Any] | None:
        """Record an event accepted by the production meter filter."""
        event_id = meter_id(raw_event)
        if not event_id:
            return None
        if self.config.meter_ids and event_id not in self.config.meter_ids:
            return None
        return self._persist(raw_event)

    def record_selected(
        self,
        raw_event: dict[str, Any],
        allowed_meter_ids: frozenset[str],
    ) -> dict[str, Any] | None:
        """Record an auxiliary event only when explicitly allowlisted."""
        event_id = meter_id(raw_event)
        if not event_id or event_id not in allowed_meter_ids:
            return None
        return self._persist(raw_event)

    def record_unfiltered(
        self,
        raw_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Record an auxiliary meter event without the production allowlist."""
        return self._persist(raw_event)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return dict(self.latest)

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.lock:
            value = self.latest.get(event_id)
            return None if value is None else dict(value)

    def retained_data_files(self) -> list[Path]:
        """Return retained JSONL files from oldest to newest."""
        path = self.config.data_file
        with self.lock:
            paths = [
                path.with_name(f"{path.name}.{index}")
                for index in range(self.config.rotate_count, 0, -1)
            ]
            paths.append(path)
            return [candidate for candidate in paths if candidate.exists()]

    def available_data_files(self) -> list[dict[str, Any]]:
        """Return safe JSONL files available from the data directory."""
        directory = self.config.data_file.parent.resolve()
        files: list[dict[str, Any]] = []
        with self.lock:
            try:
                candidates = list(directory.iterdir())
            except OSError:
                return []
            for candidate in candidates:
                if not DATA_FILE_NAME_PATTERN.fullmatch(candidate.name):
                    continue
                if candidate.is_symlink():
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                    if resolved.parent != directory or not resolved.is_file():
                        continue
                    size = resolved.stat().st_size
                except OSError:
                    continue
                files.append(
                    {
                        "name": candidate.name,
                        "bytes": size,
                        "url": f"/files/{candidate.name}",
                    }
                )
        return sorted(files, key=lambda item: item["name"])

    def resolve_data_file(self, name: str) -> Path | None:
        """Resolve an allowed JSONL filename without permitting traversal."""
        if not DATA_FILE_NAME_PATTERN.fullmatch(name):
            return None
        directory = self.config.data_file.parent.resolve()
        candidate = directory / name
        if candidate.is_symlink():
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != directory or not resolved.is_file():
            return None
        return resolved

    def history(self, event_id: str | None, limit: int) -> list[dict[str, Any]]:
        results: deque[dict[str, Any]] = deque(maxlen=limit)
        with self.lock:
            if not self.config.data_file.exists():
                return []
            with self.config.data_file.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event_id is None or meter_id(event) == event_id:
                        results.append(event)
        return list(results)


class Receiver:
    """Run and supervise rtl_433."""

    def __init__(self, config: Config, store: ReadingStore):
        self.config = config
        self.store = store
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.thread = threading.Thread(target=self._run, name="rtl433", daemon=True)
        self.running = False
        self.started_at: str | None = None
        self.last_packet_at: str | None = None
        self.last_error: str | None = None
        self.restart_count = 0
        self.decode_errors = 0
        self.write_errors = 0
        self.current_frequency_mhz = configured_frequency_mhz(config.frequency)
        self.frequency_generation = 0
        self.restart_event = threading.Event()
        self.frequency_observations: deque[tuple[float, float]] = deque()
        self.last_recenter_monotonic: float | None = None
        self.last_recenter_at: str | None = None
        self.last_adjustment_at: str | None = None
        self.last_matching_packet_monotonic = time.monotonic()
        self.last_seek_at: str | None = None
        self.auto_recenter_disabled_reason: str | None = None
        self.auto_seek_disabled_reason: str | None = None
        self.seek_thread = threading.Thread(
            target=self._seek_loop,
            name="rtl433-seek",
            daemon=True,
        )
        if config.auto_recenter_enabled:
            if len(config.meter_ids) != 1:
                self.auto_recenter_disabled_reason = (
                    "AUTO_RECENTER_ENABLED requires exactly one METER_IDS value"
                )
            elif "-f" in config.rtl433_extra_args or "-H" in config.rtl433_extra_args:
                self.auto_recenter_disabled_reason = (
                    "automatic recentering cannot be combined with rtl_433 frequency hopping"
                )
            elif config.auto_recenter_threshold_mhz <= 0:
                self.auto_recenter_disabled_reason = (
                    "AUTO_RECENTER_THRESHOLD_MHZ must be greater than zero"
                )
            elif config.auto_recenter_min_packets < 3:
                self.auto_recenter_disabled_reason = (
                    "AUTO_RECENTER_MIN_PACKETS must be at least 3"
                )
            elif config.auto_recenter_window_seconds <= 0:
                self.auto_recenter_disabled_reason = (
                    "AUTO_RECENTER_WINDOW_SECONDS must be greater than zero"
                )
            elif config.auto_recenter_cooldown_seconds < 0:
                self.auto_recenter_disabled_reason = (
                    "AUTO_RECENTER_COOLDOWN_SECONDS cannot be negative"
                )
        if config.auto_seek_enabled:
            if len(config.meter_ids) != 1:
                self.auto_seek_disabled_reason = (
                    "AUTO_SEEK_ENABLED requires exactly one METER_IDS value"
                )
            elif "-f" in config.rtl433_extra_args or "-H" in config.rtl433_extra_args:
                self.auto_seek_disabled_reason = (
                    "automatic seeking cannot be combined with rtl_433 frequency hopping"
                )
            elif config.auto_seek_silence_seconds < 15:
                self.auto_seek_disabled_reason = (
                    "AUTO_SEEK_SILENCE_SECONDS must be at least 15"
                )

    @property
    def auto_recenter_active(self) -> bool:
        return (
            self.config.auto_recenter_enabled
            and self.auto_recenter_disabled_reason is None
        )

    @property
    def auto_seek_active(self) -> bool:
        return self.config.auto_seek_enabled and self.auto_seek_disabled_reason is None

    def command(self) -> list[str]:
        """Build the rtl_433 command line."""
        with self.lock:
            frequency = self.current_frequency_mhz
        return [
            self.config.rtl433_bin,
            "-d",
            f":{self.config.sdr_serial}",
            "-R",
            "290",
            "-f",
            f"{canonical_frequency_mhz(frequency)}M",
            "-s",
            self.config.sample_rate,
            "-g",
            self.config.gain,
            "-M",
            "time:iso",
            "-M",
            "protocol",
            "-M",
            "level",
            *self.config.rtl433_extra_args,
            "-F",
            "json",
        ]

    def observe_frequency(
        self,
        event: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Return a safe recenter action after consistent matching packets."""
        if not self.auto_recenter_active:
            return None
        if meter_id(event) not in self.config.meter_ids:
            return None

        freq1 = event.get("freq1")
        freq2 = event.get("freq2")
        if (
            not isinstance(freq1, (int, float))
            or isinstance(freq1, bool)
            or not isinstance(freq2, (int, float))
            or isinstance(freq2, bool)
        ):
            return None
        midpoint = (float(freq1) + float(freq2)) / 2
        if not MIN_CONTROL_FREQUENCY_MHZ <= midpoint <= MAX_CONTROL_FREQUENCY_MHZ:
            return None

        observed_at = time.monotonic() if now is None else now
        with self.lock:
            self.frequency_observations.append((observed_at, midpoint))
            cutoff = observed_at - self.config.auto_recenter_window_seconds
            while (
                self.frequency_observations
                and self.frequency_observations[0][0] < cutoff
            ):
                self.frequency_observations.popleft()

            if len(self.frequency_observations) < self.config.auto_recenter_min_packets:
                return None
            if (
                self.last_recenter_monotonic is not None
                and observed_at - self.last_recenter_monotonic
                < self.config.auto_recenter_cooldown_seconds
            ):
                return None

            observed_midpoint = statistics.median(
                value for _, value in self.frequency_observations
            )
            old_frequency = self.current_frequency_mhz
            if (
                abs(observed_midpoint - old_frequency)
                < self.config.auto_recenter_threshold_mhz
            ):
                return None

            new_frequency = round(observed_midpoint, 1)
            if not MIN_CONTROL_FREQUENCY_MHZ <= new_frequency <= MAX_CONTROL_FREQUENCY_MHZ:
                return None
            self.current_frequency_mhz = new_frequency
            self.frequency_generation += 1
            self.last_recenter_monotonic = observed_at
            self.last_recenter_at = utc_now()
            self.last_adjustment_at = self.last_recenter_at
            packet_count = len(self.frequency_observations)
            self.frequency_observations.clear()
            return {
                "action": "recenter",
                "receiver": "production",
                "old_frequency_mhz": old_frequency,
                "new_frequency_mhz": new_frequency,
                "observed_midpoint_mhz": round(observed_midpoint, 3),
                "packet_count": packet_count,
                "reason": "rolling packet midpoint exceeded configured threshold",
            }

    def retune(
        self,
        frequency_mhz: Any,
        *,
        reason: str = "authenticated radio-control API",
        now: float | None = None,
        apply_recenter_cooldown: bool = True,
    ) -> dict[str, Any]:
        """Retune the production receiver and request an immediate restart."""
        new_frequency = float(canonical_frequency_mhz(frequency_mhz))
        adjusted_at = time.monotonic() if now is None else now
        with self.lock:
            old_frequency = self.current_frequency_mhz
            if new_frequency == old_frequency:
                return {
                    "receiver": "production",
                    "changed": False,
                    "frequency_mhz": new_frequency,
                    "restart_requested": False,
                    "persistent": False,
                }

            self.current_frequency_mhz = new_frequency
            self.frequency_generation += 1
            self.frequency_observations.clear()
            if apply_recenter_cooldown:
                self.last_recenter_monotonic = adjusted_at
            self.last_adjustment_at = utc_now()
            self.last_matching_packet_monotonic = adjusted_at
            process = self.process
            restart_requested = process is not None and process.poll() is None
            self.restart_event.set()

        adjustment = {
            "action": "retune",
            "receiver": "production",
            "old_frequency_mhz": old_frequency,
            "new_frequency_mhz": new_frequency,
            "reason": reason,
        }
        print(
            "radio_adjustment " + json.dumps(adjustment, sort_keys=True),
            flush=True,
        )
        if restart_requested:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        return {
            "receiver": "production",
            "changed": True,
            "frequency_mhz": new_frequency,
            "restart_requested": restart_requested,
            "persistent": False,
        }

    def seek_if_silent(self, *, now: float | None = None) -> dict[str, Any] | None:
        """Move to the next configured center after sustained packet silence."""
        if not self.auto_seek_active:
            return None
        observed_at = time.monotonic() if now is None else now
        with self.lock:
            silence_seconds = observed_at - self.last_matching_packet_monotonic
            if silence_seconds < self.config.auto_seek_silence_seconds:
                return None
            frequencies = self.config.auto_seek_frequencies_mhz
            nearest_index = min(
                range(len(frequencies)),
                key=lambda index: abs(
                    frequencies[index] - self.current_frequency_mhz
                ),
            )
            next_frequency = frequencies[(nearest_index + 1) % len(frequencies)]
            self.last_matching_packet_monotonic = observed_at
            self.last_seek_at = utc_now()
            self.last_recenter_monotonic = None
        return self.retune(
            next_frequency,
            reason=(
                "no matching packets for "
                f"{self.config.auto_seek_silence_seconds} seconds"
            ),
            now=observed_at,
            apply_recenter_cooldown=False,
        )

    def _seek_loop(self) -> None:
        interval = min(5.0, max(1.0, self.config.auto_seek_silence_seconds / 4))
        while not self.stop_event.wait(interval):
            self.seek_if_silent()

    def start(self) -> None:
        if self.auto_recenter_disabled_reason:
            print(
                f"Automatic recentering disabled: {self.auto_recenter_disabled_reason}",
                flush=True,
            )
        if self.auto_seek_disabled_reason:
            print(
                f"Automatic seeking disabled: {self.auto_seek_disabled_reason}",
                flush=True,
            )
        with self.lock:
            self.last_matching_packet_monotonic = time.monotonic()
        self.thread.start()
        if self.auto_seek_active:
            self.seek_thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                self.restart_event.clear()
                frequency_generation = self.frequency_generation
                command = self.command()
                starting_frequency = self.current_frequency_mhz
            print(
                "Starting rtl_433 receiver "
                f"at {canonical_frequency_mhz(starting_frequency)} MHz",
                flush=True,
            )
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                with self.lock:
                    self.last_error = f"Could not start rtl_433: {exc}"
                    self.running = False
                print(self.last_error, flush=True)
                self.stop_event.wait(10)
                continue

            with self.lock:
                self.process = process
                self.running = True
                self.started_at = utc_now()
                self.last_error = None
                stale_command = self.frequency_generation != frequency_generation
            if stale_command:
                process.terminate()

            planned_recenter: dict[str, Any] | None = None
            assert process.stdout is not None
            for line in process.stdout:
                if self.stop_event.is_set():
                    break
                try:
                    raw_event = json.loads(line)
                    if not isinstance(raw_event, dict):
                        raise ValueError("decoded JSON is not an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    with self.lock:
                        self.decode_errors += 1
                        self.last_error = f"Invalid rtl_433 output: {exc}"
                    continue

                try:
                    event = self.store.record(raw_event)
                except OSError as exc:
                    with self.lock:
                        self.write_errors += 1
                        self.last_error = f"Could not persist reading: {exc}"
                    print(self.last_error, flush=True)
                    continue
                if event is not None:
                    with self.lock:
                        self.last_packet_at = utc_now()
                        self.last_matching_packet_monotonic = time.monotonic()
                    planned_recenter = self.observe_frequency(raw_event)
                    if planned_recenter is not None:
                        print(
                            "radio_adjustment "
                            + json.dumps(planned_recenter, sort_keys=True),
                            flush=True,
                        )
                        process.terminate()
                        break

            return_code = process.wait()
            with self.lock:
                retune_requested = self.frequency_generation != frequency_generation
                self.process = None
                self.running = False
                if planned_recenter is not None or retune_requested:
                    self.last_error = None
                elif not self.stop_event.is_set():
                    self.restart_count += 1
                    self.last_error = f"rtl_433 exited with status {return_code}"
            if planned_recenter is not None or retune_requested:
                continue
            if not self.stop_event.is_set():
                print(f"{self.last_error}; restarting in 5 seconds", flush=True)
                self.restart_event.wait(5)

    def status(self) -> dict[str, Any]:
        """Return receiver health without exposing configuration identifiers."""
        with self.lock:
            return {
                "version": APP_VERSION,
                "running": self.running,
                "started_at": self.started_at,
                "last_packet_at": self.last_packet_at,
                "last_error": self.last_error,
                "restart_count": self.restart_count,
                "decode_errors": self.decode_errors,
                "write_errors": self.write_errors,
                "events_written_this_run": self.store.events_written,
                "frequency_mhz": self.current_frequency_mhz,
                "auto_recenter_enabled": self.auto_recenter_active,
                "auto_recenter_disabled_reason": self.auto_recenter_disabled_reason,
                "last_recenter_at": self.last_recenter_at,
                "last_adjustment_at": self.last_adjustment_at,
                "auto_seek_enabled": self.auto_seek_active,
                "auto_seek_disabled_reason": self.auto_seek_disabled_reason,
                "auto_seek_silence_seconds": self.config.auto_seek_silence_seconds,
                "last_seek_at": self.last_seek_at,
            }

    def stop(self) -> None:
        self.stop_event.set()
        self.restart_event.set()
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self.thread.join(timeout=6)
        if self.seek_thread.is_alive():
            self.seek_thread.join(timeout=2)


@dataclass
class ManagedCapture:
    """An auxiliary rtl_433 capture process started through the API."""

    name: str
    serial: str
    frequencies_mhz: tuple[str, ...]
    hop_seconds: int | None
    gain: str
    filename: str
    started_at: str
    process: subprocess.Popen[Any]
    output_thread: threading.Thread
    publish_meter_ids: frozenset[str]
    publish_all: bool
    persistent: bool

    def persistent_config(self) -> dict[str, Any]:
        """Return the non-secret configuration needed to restore this radio."""
        config: dict[str, Any] = {
            "serial": self.serial,
            "hop_seconds": self.hop_seconds,
            "gain": float(self.gain),
            "filename": self.filename,
            "publish_meter_ids": sorted(self.publish_meter_ids),
            "publish_all": self.publish_all,
            "persistent": True,
        }
        if len(self.frequencies_mhz) == 1:
            config["frequency_mhz"] = float(self.frequencies_mhz[0])
        else:
            config["frequencies_mhz"] = [
                float(value) for value in self.frequencies_mhz
            ]
        return config

    def status(self) -> dict[str, Any]:
        exit_code = self.process.poll()
        return {
            "name": self.name,
            "frequency_mhz": (
                float(self.frequencies_mhz[0])
                if len(self.frequencies_mhz) == 1
                else None
            ),
            "frequencies_mhz": [
                float(value) for value in self.frequencies_mhz
            ],
            "hop_seconds": self.hop_seconds,
            "filename": self.filename,
            "started_at": self.started_at,
            "running": exit_code is None,
            "exit_code": exit_code,
            "publish_mode": (
                "all"
                if self.publish_all
                else "allowlist" if self.publish_meter_ids else "none"
            ),
            "published_meter_count": (
                None if self.publish_all else len(self.publish_meter_ids)
            ),
            "persistent": self.persistent,
        }


class RadioManager:
    """Safely manage auxiliary SDR capture processes."""

    def __init__(self, config: Config, store: ReadingStore | None = None):
        self.config = config
        self.store = store
        self.data_directory = config.data_file.parent.resolve()
        self.state_file = self.data_directory / RADIO_STATE_FILE_NAME
        self.lock = threading.RLock()
        self.captures: dict[str, ManagedCapture] = {}
        self.persistent_configs: dict[str, dict[str, Any]] = {}
        self._load_persistent_configs()

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.radio_control_token
            and self.config.radio_control_serials
        )

    def _validate_name(self, name: str) -> None:
        if not RADIO_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                "radio name must start with a lowercase letter and contain only "
                "lowercase letters, numbers, underscores, or hyphens"
            )

    def _validate_serial(self, serial: Any) -> str:
        if not isinstance(serial, str):
            raise ValueError("serial must be a string")
        serial = serial.strip().removeprefix(":")
        if not SDR_SERIAL_PATTERN.fullmatch(serial):
            raise ValueError("serial contains unsupported characters")
        if serial == self.config.sdr_serial:
            raise ValueError("the production receiver cannot be retuned")
        if serial not in self.config.radio_control_serials:
            raise ValueError("serial is not allowed by RADIO_CONTROL_SERIALS")
        return serial

    def _capture_path(self, filename: Any, frequency_mhz: str) -> Path:
        if filename is None:
            filename = f"{frequency_mhz}MHz.jsonl"
        if not isinstance(filename, str) or not DATA_FILE_NAME_PATTERN.fullmatch(
            filename
        ):
            raise ValueError("filename must be a simple .jsonl filename")
        candidate = self.data_directory / filename
        if candidate.is_symlink():
            raise ValueError("filename cannot refer to a symbolic link")
        if candidate.exists() and not candidate.is_file():
            raise ValueError("filename does not refer to a regular file")
        return candidate

    def _validate_frequencies(
        self,
        frequency_mhz: Any,
        frequencies_mhz: Any,
    ) -> tuple[str, ...]:
        if frequencies_mhz is None:
            return (canonical_frequency_mhz(frequency_mhz),)
        if frequency_mhz is not None:
            raise ValueError(
                "provide frequency_mhz or frequencies_mhz, not both"
            )
        if not isinstance(frequencies_mhz, list):
            raise ValueError("frequencies_mhz must be a JSON array")
        if not 2 <= len(frequencies_mhz) <= 32:
            raise ValueError("frequencies_mhz must contain 2 through 32 values")
        normalized = tuple(
            canonical_frequency_mhz(value) for value in frequencies_mhz
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("frequencies_mhz cannot contain duplicates")
        return normalized

    def _validate_hop_seconds(
        self,
        value: Any,
        *,
        sweeping: bool,
    ) -> int | None:
        if not sweeping:
            if value is not None:
                raise ValueError(
                    "hop_seconds is only valid with frequencies_mhz"
                )
            return None
        if isinstance(value, bool):
            raise ValueError("hop_seconds must be an integer")
        try:
            seconds = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("hop_seconds must be an integer") from exc
        if not 5 <= seconds <= 3600:
            raise ValueError("hop_seconds must be between 5 and 3600")
        return seconds

    def _validate_publish_meter_ids(self, value: Any) -> frozenset[str]:
        if value is None:
            return frozenset()
        if not isinstance(value, list):
            raise ValueError("publish_meter_ids must be a JSON array")
        if len(value) > 32:
            raise ValueError("publish_meter_ids cannot contain more than 32 values")
        meter_ids: list[str] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                raise ValueError("publish_meter_ids values must be meter IDs")
            event_id = str(item).strip()
            if not event_id.isdigit() or len(event_id) > 32:
                raise ValueError("publish_meter_ids values must be numeric meter IDs")
            meter_ids.append(event_id)
        if len(meter_ids) != len(set(meter_ids)):
            raise ValueError("publish_meter_ids cannot contain duplicates")
        return frozenset(meter_ids)

    def _validate_boolean(
        self,
        value: Any,
        name: str,
        *,
        default: bool,
    ) -> bool:
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value

    def _load_persistent_configs(self) -> None:
        """Load desired auxiliary radios without starting them yet."""
        if not self.state_file.exists():
            return
        if self.state_file.is_symlink():
            print(
                f"Ignoring symbolic-link radio state file {self.state_file}",
                flush=True,
            )
            return
        try:
            if self.state_file.stat().st_size > 64 * 1024:
                raise ValueError("radio state file is too large")
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != RADIO_STATE_VERSION
                or not isinstance(payload.get("radios"), dict)
            ):
                raise ValueError("unsupported radio state format")
            for name, radio in payload["radios"].items():
                if isinstance(name, str) and isinstance(radio, dict):
                    self.persistent_configs[name] = dict(radio)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Could not load persistent radio state: {exc}", flush=True)

    def _write_persistent_configs_locked(self) -> None:
        """Atomically save desired auxiliary radios in the data volume."""
        self.data_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(f".{self.state_file.name}.tmp")
        payload = {
            "version": RADIO_STATE_VERSION,
            "radios": {
                name: self.persistent_configs[name]
                for name in sorted(self.persistent_configs)
            },
        }
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(self.state_file)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(
                f"could not persist auxiliary radio configuration: {exc}"
            ) from exc

    def _consume_output(
        self,
        name: str,
        process: subprocess.Popen[str],
        path: Path,
        publish_meter_ids: frozenset[str],
        publish_all: bool,
    ) -> None:
        """Tee auxiliary JSON to disk and optionally publish meter events."""
        assert process.stdout is not None
        try:
            with path.open("a", encoding="utf-8") as output:
                for line in process.stdout:
                    output.write(line)
                    output.flush()
                    if (
                        self.store is None
                        or not publish_all and not publish_meter_ids
                    ):
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                        if publish_all:
                            self.store.record_unfiltered(event)
                        else:
                            self.store.record_selected(
                                event,
                                publish_meter_ids,
                            )
                    except json.JSONDecodeError:
                        continue
                    except OSError as exc:
                        print(
                            f"Could not publish auxiliary reading from {name}: {exc}",
                            flush=True,
                        )
        except OSError as exc:
            print(f"Could not write auxiliary capture {name}: {exc}", flush=True)

    def _stop_locked(self, name: str) -> bool:
        capture = self.captures.pop(name, None)
        if capture is None:
            return False
        process = capture.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        capture.output_thread.join(timeout=2)
        return True

    def start(
        self,
        name: str,
        *,
        serial: Any,
        frequency_mhz: Any = None,
        frequencies_mhz: Any = None,
        hop_seconds: Any = None,
        gain: Any = None,
        filename: Any = None,
        publish_meter_ids: Any = None,
        publish_all: Any = False,
        persistent: Any = True,
        _restoring: bool = False,
    ) -> dict[str, Any]:
        """Start or retune a named auxiliary receiver."""
        self._validate_name(name)
        safe_serial = self._validate_serial(serial)
        safe_frequencies = self._validate_frequencies(
            frequency_mhz, frequencies_mhz
        )
        safe_frequency = safe_frequencies[0]
        safe_hop_seconds = self._validate_hop_seconds(
            hop_seconds,
            sweeping=len(safe_frequencies) > 1,
        )
        safe_gain = canonical_gain(
            self.config.gain if gain is None else gain
        )
        safe_publish_meter_ids = self._validate_publish_meter_ids(
            publish_meter_ids
        )
        safe_publish_all = self._validate_boolean(
            publish_all,
            "publish_all",
            default=False,
        )
        safe_persistent = self._validate_boolean(
            persistent,
            "persistent",
            default=True,
        )
        if safe_publish_all and safe_publish_meter_ids:
            raise ValueError(
                "publish_all and publish_meter_ids cannot be used together"
            )
        path = self._capture_path(filename, safe_frequency)
        command = [
            self.config.rtl433_bin,
            "-d",
            f":{safe_serial}",
            "-R",
            "290",
        ]
        for frequency in safe_frequencies:
            command.extend(("-f", f"{frequency}M"))
        command.extend([
            "-s",
            self.config.sample_rate,
            "-g",
            safe_gain,
        ])
        if safe_hop_seconds is not None:
            command.extend(("-H", str(safe_hop_seconds)))
        command.extend([
            "-M",
            "time:iso",
            "-M",
            "protocol",
            "-M",
            "level",
            "-F",
            "json",
        ])

        with self.lock:
            for capture_name, capture in self.captures.items():
                if capture_name != name and capture.serial == safe_serial:
                    raise ValueError(
                        f"serial is already managed by radio {capture_name!r}"
                    )
            previous = self.captures.get(name)
            previous_persistent = self.persistent_configs.get(name)
            old_frequencies = (
                [float(value) for value in previous.frequencies_mhz]
                if previous is not None
                else None
            )
            self._stop_locked(name)
            self.data_directory.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("a", encoding="utf-8"):
                    pass
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                raise RuntimeError(f"could not start rtl_433: {exc}") from exc

            output_thread = threading.Thread(
                target=self._consume_output,
                args=(
                    name,
                    process,
                    path,
                    safe_publish_meter_ids,
                    safe_publish_all,
                ),
                name=f"rtl433-{name}",
                daemon=True,
            )
            output_thread.start()

            try:
                return_code = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            else:
                output_thread.join(timeout=2)
                raise RuntimeError(
                    f"rtl_433 exited immediately with status {return_code}; "
                    "check the container logs"
                )

            capture = ManagedCapture(
                name=name,
                serial=safe_serial,
                frequencies_mhz=safe_frequencies,
                hop_seconds=safe_hop_seconds,
                gain=safe_gain,
                filename=path.name,
                started_at=utc_now(),
                process=process,
                output_thread=output_thread,
                publish_meter_ids=safe_publish_meter_ids,
                publish_all=safe_publish_all,
                persistent=safe_persistent,
            )
            self.captures[name] = capture
            if safe_persistent:
                self.persistent_configs[name] = capture.persistent_config()
            else:
                self.persistent_configs.pop(name, None)
            if not _restoring:
                try:
                    self._write_persistent_configs_locked()
                except RuntimeError:
                    self._stop_locked(name)
                    if previous_persistent is None:
                        self.persistent_configs.pop(name, None)
                    else:
                        self.persistent_configs[name] = previous_persistent
                    raise
            print(
                "radio_adjustment "
                + json.dumps(
                    {
                        "action": "retune" if previous is not None else "start",
                        "receiver": name,
                        "old_frequencies_mhz": old_frequencies,
                        "new_frequencies_mhz": [
                            float(value) for value in safe_frequencies
                        ],
                        "hop_seconds": safe_hop_seconds,
                        "publish_mode": (
                            "all"
                            if safe_publish_all
                            else (
                                "allowlist"
                                if safe_publish_meter_ids
                                else "none"
                            )
                        ),
                        "published_meter_count": (
                            None
                            if safe_publish_all
                            else len(safe_publish_meter_ids)
                        ),
                        "persistent": safe_persistent,
                        "reason": (
                            "persistent startup restore"
                            if _restoring
                            else "authenticated radio-control API"
                        ),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return capture.status()

    def stop(self, name: str) -> bool:
        self._validate_name(name)
        with self.lock:
            stopped = self._stop_locked(name)
            forgotten = self.persistent_configs.pop(name, None) is not None
            if forgotten:
                self._write_persistent_configs_locked()
            return stopped or forgotten

    def restore_persistent(self) -> None:
        """Restore saved auxiliary radios after the production receiver starts."""
        if not self.enabled or not self.persistent_configs:
            return
        restored = 0
        for name, radio in list(self.persistent_configs.items()):
            try:
                self.start(name, _restoring=True, **radio)
                restored += 1
            except (RuntimeError, ValueError) as exc:
                print(
                    f"Could not restore persistent radio {name}: {exc}",
                    flush=True,
                )
        print(
            f"Restored {restored} persistent auxiliary radio(s)",
            flush=True,
        )

    def statuses(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                self.captures[name].status()
                for name in sorted(self.captures)
            ]

    def stop_all(self) -> None:
        with self.lock:
            for name in list(self.captures):
                self._stop_locked(name)


class AppServer(ThreadingHTTPServer):
    """HTTP server carrying shared application state."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: ReadingStore,
        receiver: Receiver,
        radio_manager: RadioManager,
    ):
        super().__init__(address, RequestHandler)
        self.store = store
        self.receiver = receiver
        self.radio_manager = radio_manager


class RequestHandler(BaseHTTPRequestHandler):
    """Serve health, current readings, and history."""

    server: AppServer

    def _json(
        self,
        status: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _require_radio_control(self, *, require_auxiliary: bool = False) -> bool:
        manager = self.server.radio_manager
        if not manager.config.radio_control_token:
            self._json(
                503,
                {
                    "error": "Radio control is disabled. Configure "
                    "RADIO_CONTROL_TOKEN."
                },
            )
            return False
        if require_auxiliary and not manager.config.radio_control_serials:
            self._json(
                503,
                {
                    "error": "Auxiliary radio control is disabled. Configure "
                    "RADIO_CONTROL_SERIALS."
                },
            )
            return False
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {manager.config.radio_control_token}"
        if not hmac.compare_digest(authorization, expected):
            self._json(
                401,
                {"error": "Authentication required"},
                {"WWW-Authenticate": "Bearer"},
            )
            return False
        return True

    def _request_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if not 0 < length <= 8192:
            raise ValueError("JSON request body must be between 1 and 8192 bytes")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _jsonl(self) -> None:
        path = self.server.store.config.data_file
        with self.server.store.lock:
            if not path.exists():
                self._json(404, {"error": "No readings have been recorded yet"})
                return
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    self.wfile.write(chunk)

    def _all_jsonl(self) -> None:
        with self.server.store.lock:
            paths = self.server.store.retained_data_files()
            if not paths:
                self._json(404, {"error": "No readings have been recorded yet"})
                return

            size = sum(candidate.stat().st_size for candidate in paths)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for candidate in paths:
                with candidate.open("rb") as handle:
                    while chunk := handle.read(64 * 1024):
                        self.wfile.write(chunk)

    def _data_file(self, path: Path) -> None:
        """Stream a fixed snapshot of a JSONL file that may still be growing."""
        try:
            handle = path.open("rb")
        except OSError:
            self._json(404, {"error": "Data file not found"})
            return

        with handle:
            size = os.fstat(handle.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            remaining = size
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._json(
                200,
                {
                    "service": "orion-meter-reader",
                    "version": APP_VERSION,
                    "endpoints": [
                        "/healthz",
                        "/api/readings",
                        "/api/readings/{meter_id}",
                        "/api/history?id={meter_id}&limit=100",
                        "/api/readings.jsonl",
                        "/readings",
                        "/files",
                        "/files/{filename}",
                        "/api/receiver",
                        "/api/radios",
                        "/api/radios/{name}",
                    ],
                },
            )
            return
        if path == "/healthz":
            status = self.server.receiver.status()
            self._json(200 if status["running"] else 503, status)
            return
        if path == "/api/receiver":
            if not self._require_radio_control():
                return
            self._json(200, self.server.receiver.status())
            return
        if path == "/api/readings":
            self._json(200, self.server.store.snapshot())
            return
        if path.startswith("/api/readings/"):
            event_id = path.removeprefix("/api/readings/")
            event = self.server.store.get(event_id)
            self._json(200, event) if event else self._json(
                404, {"error": "Unknown meter ID"}
            )
            return
        if path == "/api/history":
            query = parse_qs(parsed.query)
            event_id = query.get("id", [None])[0]
            try:
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 5000)
            except ValueError:
                self._json(400, {"error": "limit must be an integer"})
                return
            self._json(200, self.server.store.history(event_id, limit))
            return
        if path == "/api/readings.jsonl":
            self._jsonl()
            return
        if path == "/readings":
            self._all_jsonl()
            return
        if path == "/files":
            files = self.server.store.available_data_files()
            self._json(200, {"count": len(files), "files": files})
            return
        if path.startswith("/files/"):
            filename = path.removeprefix("/files/")
            data_file = self.server.store.resolve_data_file(filename)
            if data_file is None:
                self._json(404, {"error": "Data file not found"})
                return
            self._data_file(data_file)
            return
        if path == "/api/radios":
            if not self._require_radio_control(require_auxiliary=True):
                return
            radios = self.server.radio_manager.statuses()
            self._json(200, {"count": len(radios), "radios": radios})
            return
        self._json(404, {"error": "Not found"})

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = (urlparse(self.path).path.rstrip("/") or "/")
        if path == "/api/receiver":
            if not self._require_radio_control():
                return
            try:
                payload = self._request_json()
                status = self.server.receiver.retune(
                    payload.get("frequency_mhz")
                )
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, status)
            return
        if not path.startswith("/api/radios/"):
            self._json(404, {"error": "Not found"})
            return
        if not self._require_radio_control(require_auxiliary=True):
            return
        name = path.removeprefix("/api/radios/")
        try:
            payload = self._request_json()
            status = self.server.radio_manager.start(
                name,
                serial=payload.get("serial"),
                frequency_mhz=payload.get("frequency_mhz"),
                frequencies_mhz=payload.get("frequencies_mhz"),
                hop_seconds=payload.get("hop_seconds"),
                gain=payload.get("gain"),
                filename=payload.get("filename"),
                publish_meter_ids=payload.get("publish_meter_ids"),
                publish_all=payload.get("publish_all"),
                persistent=payload.get("persistent"),
            )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._json(500, {"error": str(exc)})
            return
        self._json(200, status)

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = (urlparse(self.path).path.rstrip("/") or "/")
        if not path.startswith("/api/radios/"):
            self._json(404, {"error": "Not found"})
            return
        if not self._require_radio_control(require_auxiliary=True):
            return
        name = path.removeprefix("/api/radios/")
        try:
            stopped = self.server.radio_manager.stop(name)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        if not stopped:
            self._json(404, {"error": "Unknown managed radio"})
            return
        self._json(200, {"name": name, "stopped": True})

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"http {format_string % args}", flush=True)


def main() -> None:
    """Run the receiver and HTTP server until interrupted."""
    config = Config.from_env()
    store = ReadingStore(config)
    receiver = Receiver(config, store)
    radio_manager = RadioManager(config, store)
    server = AppServer(
        (config.http_host, config.http_port),
        store,
        receiver,
        radio_manager,
    )
    stopping = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        print(f"Received signal {signum}; stopping", flush=True)
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    server.timeout = 0.5
    receiver.start()
    radio_manager.restore_persistent()
    print(f"HTTP server listening on {config.http_host}:{config.http_port}", flush=True)
    try:
        while not stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        radio_manager.stop_all()
        receiver.stop()


if __name__ == "__main__":
    main()
