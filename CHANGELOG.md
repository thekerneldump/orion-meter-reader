# Changelog

## Unreleased

- Add `/readings` to stream all retained JSON Lines history, including rotated
  archives.
- Add an optional authenticated API for starting, retuning, inspecting, and
  stopping allowlisted auxiliary SDR capture radios.

## 0.0.1 - 2026-09-27

Initial release.

- Receive Badger ORION protocol 290 packets with a serial-selected RTL-SDR.
- Scan configurable center frequencies with `rtl_433`.
- Persist current and historical readings as JSON Lines.
- Expose health, current-reading, history, and JSONL HTTP endpoints.
- Convert protocol 290 counters to gallons.
- Run as a restartable Docker Compose service.
