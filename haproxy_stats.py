#!/usr/bin/env python3
"""Print a summary of frontend and backend HAProxy statistics.

Setup and run:
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python haproxy_stats.py
"""

import base64
import csv
import io
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from threading import Event
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pymongo import MongoClient
from pymongo.errors import PyMongoError

STATS_URL = os.environ.get("STATS_URL", '["http://localhost:8080/stats;csv"]')
USERNAME = os.environ.get("USERNAME", "admin")
PASSWORD = os.environ.get("PASSWORD", "admin")

MONGODB_URI = os.environ.get(
    "MONGODB_URI",
    "mongodb://admin:admin@localhost:27017/?authSource=admin",
)
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "hastats")
MONGODB_COLLECTION = os.environ.get("MONGODB_COLLECTION", "ldap_lb")

REQUEST_TIMEOUT = 10
POLL_INTERVAL_SECONDS = 10

SUMMARY_FIELDS = (
    ("Sessions", "scur", "stot"),
    ("Bytes in", "bin", None),
    ("Bytes out", "bout", None),
    ("Req errors", "ereq", None),
    ("Conn errors", "econ", None),
    ("Resp errors", "eresp", None),
)


def as_int(value):
    """Convert a CSV field to an integer, treating empty values as zero."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_proxy_stats(csv_text):
    """Return only frontend and backend rows from HAProxy's stats CSV."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return []

    # HAProxy prefixes its CSV header with a comment marker ("# ").
    reader.fieldnames = [name.strip().lstrip("#").strip() for name in reader.fieldnames]

    rows = []
    for row in reader:
        if not row:
            continue
        normalized = {
            (key or "").strip().lstrip("#").strip(): (value or "").strip()
            for key, value in row.items()
        }
        if normalized.get("svname") in ("FRONTEND", "BACKEND"):
            rows.append(normalized)
    return rows


def format_value(row, field, total_field=None):
    value = f"{as_int(row.get(field)):,}"
    if total_field:
        total = f"{as_int(row.get(total_field)):,}"
        return f"{value}/{total}"
    return value


def build_documents(rows, server, observed_at):
    """Build MongoDB documents from frontend and backend stats rows."""
    return [
        {
            "ISODate": observed_at,
            "server": server,
            "proxy": row.get("pxname", ""),
            "type": row.get("svname", ""),
            "status": row.get("status") or "unknown",
            "sessions": {
                "current": as_int(row.get("scur")),
                "total": as_int(row.get("stot")),
            },
            "bytes_in": as_int(row.get("bin")),
            "bytes_out": as_int(row.get("bout")),
            "errors": {
                "requests": as_int(row.get("ereq")),
                "connections": as_int(row.get("econ")),
                "responses": as_int(row.get("eresp")),
            },
        }
        for row in rows
    ]


def build_failure_document(url, error, observed_at):
    """Build a MongoDB event document for an unreachable HAProxy server."""
    return {
        "ISODate": observed_at,
        "server": urlsplit(url).netloc or url,
        "url": url,
        "type": "SERVER",
        "status": "DOWN",
        "error": str(error),
    }


def print_summary(rows, server, observed_at):
    """Print a compact, aligned table of frontend and backend metrics."""
    headers = ["ISODate", "Server", "Proxy", "Type", "Status"] + [
        item[0] for item in SUMMARY_FIELDS
    ]
    table = [headers]
    iso_date = observed_at.isoformat(timespec="seconds").replace("+00:00", "Z")

    for row in rows:
        table.append(
            [
                iso_date,
                server,
                row.get("pxname", ""),
                row.get("svname", ""),
                row.get("status") or "unknown",
            ]
            + [format_value(row, field, total) for _, field, total in SUMMARY_FIELDS]
        )

    if not rows:
        print("No frontend or backend stats found.")
        return

    widths = [max(len(line[index]) for line in table) for index in range(len(headers))]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for values in table[1:]:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(values)))


def fetch_stats(url, authorization):
    """Fetch one HAProxy stats CSV document."""
    request = Request(url, headers={"Authorization": f"Basic {authorization}"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8")


def get_stats_urls():
    """Read STATS_URL as a JSON array of endpoint URLs."""
    raw_urls = STATS_URL

    try:
        urls = json.loads(raw_urls)
    except json.JSONDecodeError as error:
        raise ValueError("STATS_URL must be a JSON array of URL strings") from error

    if not isinstance(urls, list) or not urls or any(
        not isinstance(url, str) or not url.strip() for url in urls
    ):
        raise ValueError("STATS_URL must be a non-empty JSON array of URL strings")
    return [url.strip() for url in urls]


def connect_to_mongodb():
    """Connect to MongoDB, returning (client, collection) or (None, None)."""
    client = None
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        collection = client[MONGODB_DATABASE][MONGODB_COLLECTION]
        return client, collection
    except PyMongoError as error:
        if client is not None:
            client.close()
        print(
            f"Warning: MongoDB unavailable; continuing without persistence: {error}",
            file=sys.stderr,
            flush=True,
        )
        return None, None


def collect_once(stats_urls, collection, authorization):
    """Fetch, display, and save one collection cycle for all servers."""
    mongo_failed = False
    with ThreadPoolExecutor(max_workers=len(stats_urls)) as executor:
        futures = {
            executor.submit(fetch_stats, url, authorization): url for url in stats_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            print(f"\nStats URL: {url}", flush=True)
            try:
                csv_text = future.result()
            except (URLError, OSError) as error:
                print(
                    f"Warning: could not fetch HAProxy stats: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                if collection is not None:
                    try:
                        collection.insert_one(
                            build_failure_document(url, error, datetime.now(UTC))
                        )
                        print(
                            f"Saved server-down event to "
                            f"{MONGODB_DATABASE}.{MONGODB_COLLECTION}.",
                            flush=True,
                        )
                    except PyMongoError as save_error:
                        print(
                            f"Warning: could not save server-down event to MongoDB: "
                            f"{save_error}",
                            file=sys.stderr,
                            flush=True,
                        )
                        mongo_failed = True
                continue

            server = urlsplit(url).netloc or url
            rows = read_proxy_stats(csv_text)
            observed_at = datetime.now(UTC)
            print_summary(rows, server, observed_at)

            if rows and collection is not None:
                try:
                    result = collection.insert_many(
                        build_documents(rows, server, observed_at)
                    )
                    print(
                        f"Saved {len(result.inserted_ids)} records to "
                        f"{MONGODB_DATABASE}.{MONGODB_COLLECTION}.",
                        flush=True,
                    )
                except PyMongoError as error:
                    print(
                        f"Warning: could not save stats to MongoDB: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    mongo_failed = True

    return mongo_failed


def main():
    try:
        stats_urls = get_stats_urls()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    authorization = base64.b64encode(
        f"{USERNAME}:{PASSWORD}".encode()
    ).decode("ascii")
    stop_event = Event()

    def request_stop(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print(
        f"Starting HAProxy stats collector (every {POLL_INTERVAL_SECONDS}s).",
        flush=True,
    )

    mongo_client = None
    collection = None
    try:
        while not stop_event.is_set():
            cycle_started = time.monotonic()
            if mongo_client is None:
                mongo_client, collection = connect_to_mongodb()

            mongo_failed = collect_once(stats_urls, collection, authorization)
            if mongo_failed and mongo_client is not None:
                mongo_client.close()
                mongo_client = None
                collection = None

            remaining = max(
                0,
                POLL_INTERVAL_SECONDS - (time.monotonic() - cycle_started),
            )
            stop_event.wait(remaining)
    finally:
        if mongo_client is not None:
            mongo_client.close()

    print("HAProxy stats collector stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
