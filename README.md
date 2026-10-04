# Orion Meter Reader

Orion Meter Reader uses an RTL-SDR and `rtl_433` to receive Badger ORION water
meter broadcasts. It stores decoded packets as JSON Lines and provides a small
local HTTP API suitable for Home Assistant or other LAN services.

The Docker image builds `rtl_433` 25.12 for the host architecture and is intended
for a Linux Docker host such as a Raspberry Pi. Docker Desktop does not provide
normal USB passthrough for this setup.

## Requirements

- A Linux host with Docker Engine and Docker Compose
- A compatible RTL-SDR dedicated to meter reception
- An antenna suitable for the local ORION frequency band
- Host access to `/dev/bus/usb`

The reading and file APIs have no authentication. Keep the service on a trusted
network and do not expose its port to the public internet. The optional
state-changing radio-control API is disabled by default and requires a bearer
token plus an explicit SDR serial allowlist.

## Assign a unique RTL-SDR serial

Device numbers such as `0` and `1` can change after a reboot. Assign the meter
receiver a unique EEPROM serial and configure the container to select that serial.
This also prevents another SDR service from accidentally opening the same dongle.

Install the host utility if needed:

```sh
sudo apt update
sudo apt install rtl-sdr
```

Stop every container or process that may be using an RTL-SDR, then list the
available radios:

```sh
docker ps
sudo rtl_eeprom
```

Inspect each device by index:

```sh
sudo rtl_eeprom -d 0
sudo rtl_eeprom -d 1
```

If the dongles already have different serials, choose the one dedicated to meter
reception and use its existing serial. If two radios have the same factory serial,
physically unplug every RTL-SDR except the meter receiver before changing it. With
only the intended device connected, assign a short unique serial:

```sh
sudo rtl_eeprom -d 0 -s ORION
```

Confirm the write when prompted. Then unplug and reconnect the dongle; an EEPROM
change does not take effect until USB power is cycled. Verify the result:

```sh
sudo rtl_eeprom -d 0
```

Changing the wrong radio may prevent an existing SDR container from finding its
device. Physically identify and isolate the target before writing its EEPROM.

## Configure

Copy the example environment file and create the persistent data directory:

```sh
cp .env.example .env
mkdir -p data
```

Edit `.env` and set `SDR_SERIAL` to the exact EEPROM serial, without a leading
colon:

```dotenv
SDR_SERIAL=ORION
```

`METER_IDS` is an optional comma-separated allowlist. Leave it empty while
discovering endpoints, then add only the IDs you intend to retain:

```dotenv
METER_IDS=
```

The defaults scan the North American 900 MHz ISM band because ORION endpoints can
transmit on changing frequencies:

```dotenv
FREQUENCY=905.2M
SAMPLE_RATE=1600k
RTL433_EXTRA_ARGS=-f 906.8M -f 908.4M -f 910.0M -f 911.6M -f 913.2M -f 914.8M -f 916.4M -f 918.0M -f 919.6M -f 921.2M -f 922.8M -f 924.4M -H 5
```

`FREQUENCY` supplies the first `-f` option; `RTL433_EXTRA_ARGS` supplies the
remaining center frequencies and the five-second hop interval. If a fixed center
frequency is more reliable at your location, replace `FREQUENCY` with that value
and set `RTL433_EXTRA_ARGS=none`. An empty or unset value retains the default
frequency list so existing single-radio scanners keep working after an update.
Gain `0` selects automatic gain; a supported fixed gain can be configured with
`GAIN`.

### Automatic fixed-center recentering

Automatic recentering can follow slow movement of a fixed receiver's decoded
channel. It is opt-in, requires exactly one configured `METER_IDS` value, and is
disabled whenever `RTL433_EXTRA_ARGS` contains rtl_433 frequency-hopping options.
It does not discover a completely silent channel; use a separate scanning radio
for discovery.

For a fixed production receiver, configure:

```dotenv
FREQUENCY=921.2M
RTL433_EXTRA_ARGS=none
AUTO_RECENTER_ENABLED=true
AUTO_RECENTER_THRESHOLD_MHZ=0.35
AUTO_RECENTER_MIN_PACKETS=5
AUTO_RECENTER_WINDOW_SECONDS=300
AUTO_RECENTER_COOLDOWN_SECONDS=900
```

After the required number of matching packets moves beyond the threshold, the
service rounds their median channel midpoint to 0.1 MHz, restarts only the
`rtl_433` child process at that center, and enforces the cooldown. Every automatic
or API-requested adjustment writes a `radio_adjustment` JSON object to the
container log. Meter identifiers are not included in adjustment log entries.

## Build and run

Check the rendered configuration, then build and start the service:

```sh
docker compose config
docker compose up -d --build
docker compose logs -f orion-meter-reader
```

The service uses `restart: unless-stopped`, so it starts again with Docker after a
host reboot.

## Verify the dongle inside Docker

If the receiver cannot open the radio, stop the service and run a short raw sample
test:

```sh
docker compose stop orion-meter-reader
timeout 10s docker compose run --rm --no-deps \
  --entrypoint rtl_sdr orion-meter-reader \
  -d ORION -f 907200000 -s 1600000 /dev/null
docker compose start orion-meter-reader
```

For `rtl_sdr`, pass the serial as `-d ORION`. The application uses the
`rtl_433` serial form `-d :ORION` internally.

## HTTP API

The default host port is `8083`. Replace `<reader-host>` and `<meter-id>` in these
examples:

```sh
curl http://<reader-host>:8083/healthz
curl http://<reader-host>:8083/api/readings
curl http://<reader-host>:8083/api/readings/<meter-id>
curl 'http://<reader-host>:8083/api/history?id=<meter-id>&limit=100'
curl -O http://<reader-host>:8083/api/readings.jsonl
curl http://<reader-host>:8083/readings
curl http://<reader-host>:8083/files
curl -O http://<reader-host>:8083/files/<capture-name>.jsonl
```

Files are retained in `data/readings.jsonl` and rotated at 100 MiB by default.
`/api/readings.jsonl` returns only the active file. `/readings` returns all
retained JSON Lines history in chronological order: the oldest rotated archive
first and the active file last. Raw records contain meter identifiers, so keep
these endpoints on a trusted network.

`/files` lists every safe JSONL capture in the mounted `data` directory, including
standalone frequency-test files. Download one with `/files/<filename>`. Only
simple JSONL filenames are exposed; directory traversal, symbolic links, hidden
files, and other file types are rejected.
Protocol 290 counters are tenths of a gallon. API objects retain the original
`rtl_433` fields and add:

- `reading_gallons`: cumulative meter reading
- `daily_reading_gallons`: the endpoint's snapshot counter
- `usage_since_snapshot_gallons`: difference between those values
- `ingested_at`: time the service accepted the packet

The endpoint snapshot may not occur at civil midnight, so
`usage_since_snapshot_gallons` should not automatically be treated as calendar-day
usage.

## Auxiliary radio control API

For frequency-hopping experiments, the service can safely start, retune, inspect,
and stop auxiliary RTL-SDR capture processes. It does not accept shell commands,
it only permits frequencies from 902 through 928 MHz, and it refuses to retune
the production receiver configured by `SDR_SERIAL`.

Create a strong token, then add the token and the permitted auxiliary SDR serials
to `.env`:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```dotenv
RADIO_CONTROL_TOKEN=<generated-token>
RADIO_CONTROL_SERIALS=AUX1
```

Recreate the service after changing `.env`:

```sh
docker compose up -d --build --force-recreate orion-meter-reader
```

In Postman, use an `Authorization` header with type **Bearer Token**. Start a
named capture with `PUT /api/radios/demo` and a JSON body:

```json
{
  "serial": "AUX1",
  "frequency_mhz": 904.8,
  "gain": 70
}
```

The default output filename is derived from the frequency, such as
`904.8MHz.jsonl`. An optional `filename` property may specify another simple
`.jsonl` filename. Sending another `PUT` to the same named radio stops its old
process, retunes it, and appends to the new frequency's file.

Each start or retune also writes a `radio_adjustment` entry to the container log
with the previous center, new center, and adjustment reason.

An auxiliary receiver can instead sweep several centers in one persistent
`rtl_433` process. Supply `frequencies_mhz` instead of `frequency_mhz`, plus a
hop interval from 5 through 3600 seconds:

```json
{
  "serial": "AUX1",
  "frequencies_mhz": [905.2, 906.8, 910.0, 911.6, 913.2],
  "hop_seconds": 120,
  "gain": 70,
  "filename": "discovery.jsonl"
}
```

The status response reports the complete frequency list and hop interval. Each
decoded packet retains rtl_433's measured `freq1` and `freq2`, allowing discovery
results to be grouped by their actual channel midpoint.

Inspect or stop managed captures:

```text
GET /api/radios
DELETE /api/radios/demo
```

Managed capture files immediately appear in `GET /files`. Auxiliary captures
stop when the main service container stops and are not automatically restarted
after a container restart.

## Troubleshooting

- **`No matching devices found`**: confirm the configured serial exactly matches
  `rtl_eeprom`; do not include the leading colon in `.env`.
- **`usb_claim_interface error -6`**: another process owns the dongle. Stop other
  SDR containers and ensure each one selects a different serial.
- **`Permission denied: /data/readings.jsonl`**: ensure `data` exists and is
  writable by Docker. On a normal rootful Docker installation,
  `sudo chown -R root:root data` corrects ownership.
- **No decoded packets**: check the antenna and `/healthz`, inspect the logs, try a
  fixed center frequency, and compare automatic gain with a supported fixed gain.
  Unchanged readings in repeated packets are normal.
- **DVB kernel driver conflict**: blacklist the host's RTL2832 DVB modules if they
  repeatedly reclaim the receiver, then reboot.

Run the unit tests on any host with Python 3.12 or newer:

```sh
python3 -m unittest -v
```
