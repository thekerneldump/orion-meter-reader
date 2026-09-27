#!/usr/bin/env python3
"""Receive Badger ORION packets and expose readings through a small HTTP API."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

APP_VERSION = "0.0.1"


def utc_now() -> str:
    """Return a timezone-aware ISO timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


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
            rtl433_extra_args=tuple(
                shlex.split(os.getenv("RTL433_EXTRA_ARGS", ""))
            ),
        )


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

    def record(self, raw_event: dict[str, Any]) -> dict[str, Any] | None:
        event_id = meter_id(raw_event)
        if not event_id:
            return None
        if self.config.meter_ids and event_id not in self.config.meter_ids:
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

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return dict(self.latest)

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.lock:
            value = self.latest.get(event_id)
            return None if value is None else dict(value)

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

    def command(self) -> list[str]:
        """Build the rtl_433 command line."""
        return [
            self.config.rtl433_bin,
            "-d",
            f":{self.config.sdr_serial}",
            "-R",
            "290",
            "-f",
            self.config.frequency,
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

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            print("Starting rtl_433 receiver", flush=True)
            try:
                process = subprocess.Popen(
                    self.command(),
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

            return_code = process.wait()
            with self.lock:
                self.process = None
                self.running = False
                if not self.stop_event.is_set():
                    self.restart_count += 1
                    self.last_error = f"rtl_433 exited with status {return_code}"
            if not self.stop_event.is_set():
                print(f"{self.last_error}; restarting in 5 seconds", flush=True)
                self.stop_event.wait(5)

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
            }

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self.thread.join(timeout=6)


class AppServer(ThreadingHTTPServer):
    """HTTP server carrying shared application state."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: ReadingStore, receiver: Receiver):
        super().__init__(address, RequestHandler)
        self.store = store
        self.receiver = receiver


class RequestHandler(BaseHTTPRequestHandler):
    """Serve health, current readings, and history."""

    server: AppServer

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
                    ],
                },
            )
            return
        if path == "/healthz":
            status = self.server.receiver.status()
            self._json(200 if status["running"] else 503, status)
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
        self._json(404, {"error": "Not found"})

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"http {format_string % args}", flush=True)


def main() -> None:
    """Run the receiver and HTTP server until interrupted."""
    config = Config.from_env()
    store = ReadingStore(config)
    receiver = Receiver(config, store)
    server = AppServer((config.http_host, config.http_port), store, receiver)
    stopping = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        print(f"Received signal {signum}; stopping", flush=True)
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    server.timeout = 0.5
    receiver.start()
    print(f"HTTP server listening on {config.http_host}:{config.http_port}", flush=True)
    try:
        while not stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        receiver.stop()


if __name__ == "__main__":
    main()
