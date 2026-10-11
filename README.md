# NOAA Space Weather Alerts for Meshtastic

Poll NOAA's Space Weather Prediction Center (SWPC) public alerts feed and relay
new **ALERT**, **WATCH**, and **WARNING** products to a Meshtastic node over TCP.

The script defaults to node `192.168.50.49` and channel index `2`. Messages use
a compact, multiline format with severity emoji, NOAA scale (when available),
issue time, a short detail, and a link to the official SWPC alerts page. Each
message is kept within both 200 characters and 200 UTF-8 bytes.

## Features

- Reads the public SWPC feed at
  [`services.swpc.noaa.gov/products/alerts.json`](https://services.swpc.noaa.gov/products/alerts.json).
- Sends only products containing an `ALERT:`, `WATCH:`, or `WARNING:` line;
  routine summaries are ignored.
- Supports one channel or several channel indexes on the same node.
- Deduplicates products by product ID and issue time using a local cache.
- On first run, sends only products from the configured initial lookback window
  (two hours by default), recording older products without broadcasting them.
- Coordinates access to the radio using a shared lock file when the platform
  supports file locking.
- Logs activity and errors to a file.

## Requirements

- Python 3.10 or later.
- A Meshtastic node reachable over TCP with the channel indexes configured on
  that node.
- Network access to the SWPC feed.

## Install

Clone or copy this directory, then install dependencies in a virtual environment.

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### Linux / macOS

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` for your node and channel setup. The script loads `.env` when
`python-dotenv` is installed; environment variables set by the process take
precedence over the example defaults. Never commit `.env`.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `MESHTASTIC_NODE_IP` | `192.168.0.1` | Host name or IP address of the Meshtastic TCP node. |
| `MESHTASTIC_CHANNELS` | `2` | Comma-separated channel indexes, e.g. `2,4`. Duplicate indexes are removed. |
| `MESHTASTIC_CHANNEL` | `2` | Single-channel compatibility setting; used only if `MESHTASTIC_CHANNELS` is unset. |
| `MAX_MESSAGE_LENGTH` | `200` | Desired payload ceiling. The script always caps it at 200; smaller values shorten the title and detail. Set it large enough to retain the message header. |
| `INTERFACE_STABILIZATION_DELAY` | `3` | Seconds to wait after connecting to the node. |
| `RADIO_BUFFER_CLEAR_DELAY` | `6` | Seconds between channel sends. |
| `SWPC_RETRIES` | `5` | Maximum attempts for HTTP/network errors while fetching the feed. |
| `SWPC_RETRY_DELAY` | `2` | Base delay in seconds for HTTP/network retries; attempts use a linearly increasing delay. |
| `SWPC_INITIAL_LOOKBACK_HOURS` | `2` | On first run, only send products this recent; older products are cached without sending. |
| `MAX_CACHE_ENTRIES` | `500` | Maximum product keys retained in the deduplication cache. |
| `CACHE_FILE` | `logs/processed_swpc_products.txt` under the script directory | Path to the processed-product cache. |
| `LOG_FILE` | `logs/spaceweather_alerts.log` under the script directory | Path to the runtime log. |
| `GLOBAL_MUTEX_LOCK` | Windows: system temp directory; POSIX: `/tmp/meshtastic-node.lock` | Optional lock-file path shared with other Meshtastic senders. |

Channel indexes refer to channels already configured on the node. This script
does not configure channels or create them.

## Run

From this directory:

```sh
python spaceweather_alerts.py
```

The process exits with status `0` after a successful run or when another sender
owns the mutex. It exits with status `1` when fetching, connecting, or sending
fails; see the configured log for details.

For periodic polling, run it every few minutes with a scheduler. The script
acquires `/tmp/meshtastic-node.lock` internally on POSIX systems, so do **not**
wrap it in another `flock` using the same lock file. Run it from the project
directory so `.env` is discovered and relative paths are predictable.

For the other Meshtastic cron jobs listed in
[`crontab.example`](./crontab.example), Space Weather runs at minute `5, 10,
..., 55`:

```cron
5-55/5 * * * * cd /scripts/spaceweather && /usr/bin/timeout 240 /usr/bin/python3 /scripts/spaceweather/spaceweather_alerts.py >> /scripts/logs/spaceweather_alerts_cron.log 2>&1
```

This deliberately skips the `:00` run each hour to avoid starting at the same
time as an hourly weather-report job. There is consequently one ten-minute
polling interval per hour (from `:55` to `:05`). To poll at exactly five-minute
intervals instead, use `*/5` and accept the top-of-hour lock contention; if
another process owns the mutex when this script starts, this run exits and the
next cron run will poll again.

All senders must use the **same lock file on the same machine** for the lock to
serialize access to the radio. The POSIX default is
`/tmp/meshtastic-node.lock`; set `GLOBAL_MUTEX_LOCK` in `.env` only if you need
to match a different path already used by your other scripts. For jobs wrapped
in `flock`, use that same path. Those jobs wait only for their configured
`flock -w` interval, then may skip the run if the lock remains busy. Locking
prevents concurrent sends; it does not deduplicate a notification between
different scripts.

The example schedule assigns the frequent jobs different minute offsets. Some
occasional jobs (such as NASA NEO) may still share a minute with a frequent job;
the shared lock serializes access. Review the per-job logs for lock contention
or missed runs and adjust the offsets or wait duration as needed.

On Windows, create a Task Scheduler task that runs `.venv\Scripts\python.exe`
with `spaceweather_alerts.py` as its argument, and set the task's **Start in**
directory to this project folder. The script uses a Windows file lock in the
system temporary directory by default; other Windows senders must use the same
locking mechanism and lock file to coordinate with it.

## Message example

```text
🚨 SWPC ALERT:
⚡Geomagnetic K-index of 7
🔥💨 G3 - Strong
📅 Oct 10 19:27Z
ⓘ Threshold Oct 10 1926 UTC
🔗 [noaa.gov](https://www.swpc.noaa.gov/products/alerts-watches-and-warnings)
```

Long titles and details are shortened to fit the configured maximum. The limit
also accounts for the UTF-8 byte length of emoji and other non-ASCII characters.
The link points to the general SWPC alerts page; individual feed products do not
provide a stable per-product web URL.

## Deduplication and delivery

Each feed product is identified by its product ID and issue time. Processed
products are stored in `CACHE_FILE`; keep that file between runs to prevent
repeated broadcasts. If it is removed, the first-run lookback is applied again.

When sending to multiple channels, each channel receives the same message in
sequence. A product is marked processed only after all configured channel sends
return successfully. If sending fails partway through, a later run may resend
the product to channels that already accepted it; Meshtastic's TCP send call
does not confirm delivery to every receiving radio.

The script locks the Meshtastic interface itself. Do not wrap it in another
lock using the same `GLOBAL_MUTEX_LOCK` path. When installed alongside other
senders, ensure each one uses the same lock path and a compatible file-locking
mechanism.

## Logs and runtime files

By default, the script creates a `logs/` directory beside the script and keeps
the log and processed-product cache there. Runtime files and local configuration
are excluded from Git by `.gitignore`.

## Security

Keep local configuration and network details private, and restrict the node's
TCP interface to trusted devices. See [SECURITY.md](./SECURITY.md) for
vulnerability reporting and operational security guidance.

## License

This project is licensed under the [MIT License](./LICENSE).
