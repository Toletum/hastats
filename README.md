# HAProxy Stats Summary

Fetch and display HAProxy frontend/backend metrics from the CSV stats endpoint at
`http://localhost:8080/stats;csv`. The script uses Basic Auth credentials
`admin:admin`, matching the provided HAProxy configuration. It uses Python's
standard library for HAProxy requests and PyMongo to store results in MongoDB.

## Setup and run

From this directory, run:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python haproxy_stats.py
```

The collector runs continuously, polling every 10 seconds. Stop it with `Ctrl+C`
when running in the foreground. To run it in the background:

```sh
nohup .venv/bin/python -u haproxy_stats.py >> haproxy_stats.log 2>&1 &
```

Each summary row includes an `ISODate` UTC timestamp and a `Server` column
(host and port parsed from its stats URL), plus status, current/total sessions,
bytes in/out, and request, connection, and response errors. Make sure HAProxy is
running and exposing port 8080 before running the script.

`STATS_URL` is an optional environment variable containing a JSON array of stats
URLs. If unset, the script uses `http://localhost:8080/stats;csv`:

```sh
export STATS_URL='["http://localhost:8080/stats;csv", "http://haproxy2:8080/stats;csv"]'
python haproxy_stats.py
```

Each polling cycle fetches URLs concurrently using one thread per server, prints
separate summaries as requests complete, and continues if an endpoint cannot be
fetched. A server-down event is saved for each failed poll. The daemon handles
`SIGINT` and `SIGTERM` for graceful shutdown.

Each frontend/backend summary row is saved as a document in the `hastats.ldap_lb`
MongoDB collection. If a server cannot be reached, a `SERVER`/`DOWN` event is
saved there with the URL and error message. `ISODate` is stored as a BSON date;
normal stats documents also include server, proxy, status, sessions, traffic, and
errors. If MongoDB is unavailable, the collector still fetches and displays
HAProxy stats, skips database writes for that cycle, and retries the connection on
the next cycle. The default MongoDB URI is
`mongodb://admin:admin@localhost:27017/?authSource=admin`; set `MONGODB_URI` if
your MongoDB user authenticates against a different auth source.
