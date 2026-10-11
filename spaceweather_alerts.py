#!/usr/bin/env python3
"""Relay new NOAA SWPC alerts, watches, and warnings to a Meshtastic node."""

import errno
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide fcntl
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows does not provide msvcrt
    msvcrt = None

try:
    import meshtastic.tcp_interface
except ImportError as exc:  # pragma: no cover - dependency validation
    raise SystemExit("Missing dependency: meshtastic") from exc

try:
    from dotenv import find_dotenv, load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional
    def find_dotenv() -> str:
        return ""

    def load_dotenv() -> bool:
        return False


load_dotenv(find_dotenv())

SCRIPT_DIR = Path(__file__).resolve().parent
SWPC_URL = "https://services.swpc.noaa.gov/products/alerts.json"
SWPC_ALERTS_PAGE = (
    "https://orbitalradar.com/space-weather"
)
NODE_IP = os.environ.get("MESHTASTIC_NODE_IP", "192.168.0.1")

_channels_value = os.environ.get("MESHTASTIC_CHANNELS")
if _channels_value is None:
    _channels_value = os.environ.get("MESHTASTIC_CHANNEL", "2")
try:
    CHANNEL_INDEXES = tuple(
        dict.fromkeys(
            int(channel.strip())
            for channel in _channels_value.split(",")
            if channel.strip()
        )
    )
except ValueError as exc:
    raise SystemExit("Meshtastic channel indexes must be comma-separated integers.") from exc
if not CHANNEL_INDEXES or any(channel < 0 for channel in CHANNEL_INDEXES):
    raise SystemExit("Configure one or more non-negative Meshtastic channel indexes.")

try:
    MAX_MESSAGE_LENGTH = min(int(os.environ.get("MAX_MESSAGE_LENGTH", "200")), 200)
except ValueError as exc:
    raise SystemExit("MAX_MESSAGE_LENGTH must be an integer.") from exc
if MAX_MESSAGE_LENGTH < 1:
    raise SystemExit("MAX_MESSAGE_LENGTH must be greater than zero.")

try:
    BUFFER_CLEAR_TIME = max(0, int(os.environ.get("RADIO_BUFFER_CLEAR_DELAY", "6")))
    STABILIZATION_TIME = max(
        0, int(os.environ.get("INTERFACE_STABILIZATION_DELAY", "3"))
    )
    SWPC_RETRIES = max(1, int(os.environ.get("SWPC_RETRIES", "5")))
    SWPC_RETRY_DELAY = max(1, int(os.environ.get("SWPC_RETRY_DELAY", "2")))
    SWPC_INITIAL_LOOKBACK_HOURS = max(
        0, int(os.environ.get("SWPC_INITIAL_LOOKBACK_HOURS", "2"))
    )
    MAX_CACHE_ENTRIES = max(1, int(os.environ.get("MAX_CACHE_ENTRIES", "500")))
except ValueError as exc:
    raise SystemExit("Timing, retry, and cache settings must be integers.") from exc

CACHE_FILE = Path(
    os.environ.get(
        "CACHE_FILE", str(SCRIPT_DIR / "logs" / "processed_swpc_products.txt")
    )
)
LOG_FILE = Path(
    os.environ.get("LOG_FILE", str(SCRIPT_DIR / "logs" / "spaceweather_alerts.log"))
)
DEFAULT_MUTEX_PATH = (
    Path(tempfile.gettempdir()) / "meshtastic-node.lock"
    if os.name == "nt"
    else Path("/tmp/meshtastic-node.lock")
)
GLOBAL_MUTEX_LOCK = Path(os.environ.get("GLOBAL_MUTEX_LOCK", str(DEFAULT_MUTEX_PATH)))

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

global_mutex_handle = None


def acquire_global_mutex() -> bool:
    """Acquire the shared Meshtastic lock used by sibling relay scripts."""
    global global_mutex_handle

    if fcntl is None and msvcrt is None:
        logging.warning("No platform lock module is available; radio locking is disabled.")
        return True

    GLOBAL_MUTEX_LOCK.parent.mkdir(parents=True, exist_ok=True)
    global_mutex_handle = GLOBAL_MUTEX_LOCK.open("a+")
    try:
        if fcntl is not None:
            fcntl.flock(
                global_mutex_handle,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        else:
            global_mutex_handle.seek(0, os.SEEK_END)
            if global_mutex_handle.tell() == 0:
                global_mutex_handle.write("0")
                global_mutex_handle.flush()
            global_mutex_handle.seek(0)
            msvcrt.locking(global_mutex_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except (BlockingIOError, OSError) as exc:
        if not isinstance(exc, BlockingIOError) and exc.errno not in (
            errno.EACCES,
            errno.EDEADLK,
        ):
            global_mutex_handle.close()
            global_mutex_handle = None
            raise
        logging.warning("Another Meshtastic process already owns the mutex.")
        global_mutex_handle.close()
        global_mutex_handle = None
        return False

    logging.info("Global mutex acquired: %s", GLOBAL_MUTEX_LOCK)
    return True


def release_global_mutex() -> None:
    global global_mutex_handle

    if global_mutex_handle is None:
        return

    try:
        if fcntl is not None:
            fcntl.flock(global_mutex_handle, fcntl.LOCK_UN)
        elif msvcrt is not None:
            global_mutex_handle.seek(0)
            msvcrt.locking(global_mutex_handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        global_mutex_handle.close()
        global_mutex_handle = None


def fetch_swpc_products() -> list[dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "MeshtasticSpaceWeatherRelay/1.0",
    }
    last_error: requests.RequestException | None = None

    for attempt in range(1, SWPC_RETRIES + 1):
        try:
            response = requests.get(SWPC_URL, headers=headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("SWPC returned an unexpected alerts payload.")
            if not all(isinstance(item, dict) for item in payload):
                raise ValueError("SWPC alerts payload contains an invalid product.")
            logging.info("Fetched %s SWPC products on attempt %s.", len(payload), attempt)
            return payload
        except requests.RequestException as exc:
            last_error = exc
            logging.warning(
                "SWPC fetch attempt %s/%s failed: %s", attempt, SWPC_RETRIES, exc
            )
            if attempt < SWPC_RETRIES:
                time.sleep(SWPC_RETRY_DELAY * attempt)

    assert last_error is not None
    raise RuntimeError(f"SWPC fetch failed after {SWPC_RETRIES} attempts.") from last_error


def product_key(product: dict[str, Any]) -> str:
    product_id = product.get("product_id")
    issued = product.get("issue_datetime")
    if not isinstance(product_id, str) or not isinstance(issued, str):
        raise ValueError("SWPC product is missing product_id or issue_datetime.")
    return f"{product_id}|{issued}"


def load_processed_products() -> list[str]:
    if not CACHE_FILE.exists():
        return []
    return [
        line.strip()
        for line in CACHE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def save_processed_products(keys: list[str]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    contents = "".join(f"{key}\n" for key in keys[-MAX_CACHE_ENTRIES:])
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=CACHE_FILE.parent,
        delete=False,
    ) as temporary:
        temporary.write(contents)
        temporary_path = Path(temporary.name)
    temporary_path.replace(CACHE_FILE)


def _issued_label(value: str) -> str:
    try:
        issued = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return issued.astimezone(timezone.utc).strftime("%b %d %H:%MZ")


def _is_within_initial_lookback(
    product: dict[str, Any], now: datetime
) -> bool:
    issued_value = product.get("issue_datetime")
    if not isinstance(issued_value, str):
        raise ValueError("SWPC product is missing issue_datetime.")
    try:
        issued = datetime.fromisoformat(issued_value.replace("Z", "+00:00"))
    except ValueError:
        logging.warning(
            "Could not parse issue_datetime %r; retaining the product.", issued_value
        )
        return True
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return issued.astimezone(timezone.utc) >= now - timedelta(
        hours=SWPC_INITIAL_LOOKBACK_HOURS
    )


def _title_and_type(message: str) -> tuple[str, str] | None:
    for line in message.splitlines():
        match = re.match(r"^\s*(ALERT|WATCH|WARNING):\s*(.+?)\s*$", line, re.I)
        if match:
            return match.group(1).upper(), match.group(2)
    return None


def _scale_label(message: str) -> str:
    match = re.search(
        r"NOAA Scale:\s*((?:G|R|S)\s*[1-5]\s*-\s*[^\r\n]+)",
        message,
        re.I,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _detail_line(message: str) -> str:
    preferred_labels = (
        "Threshold Reached:",
        "Valid From:",
        "Begin Time:",
        "Maximum Time:",
    )
    for label in preferred_labels:
        match = re.search(
            rf"^\s*{re.escape(label)}\s*(.+?)\s*$",
            message,
            re.I | re.M,
        )
        if match:
            if label == "Threshold Reached:":
                threshold_time = re.sub(r"^\d{4}\s+", "", match.group(1))
                return f"Threshold {threshold_time}"
            return f"{label[:-1]} {match.group(1)}"

    impact = re.search(
        r"^\s*Potential Impacts:\s*(.+?)\s*$", message, re.I | re.M
    )
    if impact:
        return impact.group(1)
    return ""


def _fit_text(text: str, char_budget: int, byte_budget: int) -> str:
    if char_budget <= 0 or byte_budget <= 0:
        return ""
    if len(text) <= char_budget and len(text.encode("utf-8")) <= byte_budget:
        return text

    suffix = "..." if char_budget >= 3 and byte_budget >= 3 else ""
    char_budget -= len(suffix)
    byte_budget -= len(suffix.encode("utf-8"))
    kept: list[str] = []
    byte_count = 0
    for character in text:
        char_bytes = len(character.encode("utf-8"))
        if len(kept) + 1 > char_budget or byte_count + char_bytes > byte_budget:
            break
        kept.append(character)
        byte_count += char_bytes
    return f"{''.join(kept).rstrip()}{suffix}"


def _truncate_detail(prefix: str, detail: str, suffix: str = "") -> str:
    """Fit the complete payload under both the character and UTF-8 byte limits."""
    if not detail:
        return f"{prefix}{suffix}"

    separator = "\nⓘ "
    full_prefix = f"{prefix}{separator}"
    char_budget = MAX_MESSAGE_LENGTH - len(full_prefix) - len(suffix)
    byte_budget = (
        MAX_MESSAGE_LENGTH
        - len(full_prefix.encode("utf-8"))
        - len(suffix.encode("utf-8"))
    )
    if char_budget <= 0 or byte_budget <= 0:
        return f"{prefix}{suffix}"

    detail = " ".join(detail.split())
    if len(detail) <= char_budget and len(detail.encode("utf-8")) <= byte_budget:
        return f"{full_prefix}{detail}{suffix}"

    return f"{full_prefix}{_fit_text(detail, char_budget, byte_budget)}{suffix}"


def format_product(product: dict[str, Any]) -> str | None:
    """Format one SWPC ALERT/WATCH/WARNING product in a compact multiline style."""
    message = product.get("message")
    if not isinstance(message, str):
        raise ValueError("SWPC product message must be text.")
    classified = _title_and_type(message)
    if classified is None:
        return None

    kind, title = classified
    scale = _scale_label(message)
    strong_scale = re.search(r"\b(?:G|R|S)\s*[3-5]\b", scale, re.I)
    emoji = "🚨" if strong_scale else "⚠️"
    issued = product.get("issue_datetime")
    if not isinstance(issued, str):
        raise ValueError("SWPC product is missing issue_datetime.")

    if scale:
        scale = _fit_text(scale, 36, 36)
    metadata = f"\n📅 {_issued_label(issued)}"
    if scale:
        metadata = f"\n☀️💨 {scale}{metadata}"
    link_suffix = f"\n🔗 [noaa.gov]({SWPC_ALERTS_PAGE})"
    heading = f"{emoji} SWPC {kind}:\n"f"⚡{title}"
    heading = _fit_text(
        heading,
        MAX_MESSAGE_LENGTH - len(metadata) - len(link_suffix),
        MAX_MESSAGE_LENGTH
        - len(metadata.encode("utf-8"))
        - len(link_suffix.encode("utf-8")),
    )
    prefix = f"{heading}{metadata}"

    detail = _detail_line(message)
    formatted = _truncate_detail(prefix, detail, link_suffix)
    if (
        len(formatted) > MAX_MESSAGE_LENGTH
        or len(formatted.encode("utf-8")) > MAX_MESSAGE_LENGTH
    ):
        raise AssertionError("Formatted message exceeds its payload limit.")
    return formatted


def main() -> int:
    if not acquire_global_mutex():
        return 0

    interface = None
    try:
        products = fetch_swpc_products()
        is_initial_run = not CACHE_FILE.exists()
        processed = load_processed_products()
        processed_set = set(processed)
        pending: list[tuple[str, str]] = []
        now = datetime.now(timezone.utc)

        for product in products:
            key = product_key(product)
            if key in processed_set:
                continue
            message = format_product(product)
            processed_set.add(key)
            if message is None:
                processed.append(key)
                continue
            if is_initial_run and not _is_within_initial_lookback(product, now):
                processed.append(key)
                logging.info("Ignoring historical SWPC product on initial run: %s", key)
                continue
            pending.append((key, message))

        save_processed_products(processed)
        if not pending:
            logging.info("No new SWPC alerts, watches, or warnings.")
            return 0

        interface = meshtastic.tcp_interface.TCPInterface(hostname=NODE_IP)
        time.sleep(STABILIZATION_TIME)

        for key, message in reversed(pending):
            logging.info(
                "Broadcasting %s (%s characters) to channels %s.",
                key,
                len(message),
                CHANNEL_INDEXES,
            )
            for channel in CHANNEL_INDEXES:
                interface.sendText(text=message, channelIndex=channel)
                time.sleep(BUFFER_CLEAR_TIME)
            processed.append(key)
            processed_set.add(key)
            save_processed_products(processed)

        return 0
    except Exception:
        logging.exception("Space Weather alert relay failed.")
        return 1
    finally:
        try:
            if interface is not None:
                interface.close()
        finally:
            release_global_mutex()


if __name__ == "__main__":
    sys.exit(main())
