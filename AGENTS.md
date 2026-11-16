# HAProxy Stats Collector

## Requirements

- Fetch HAProxy statistics from the built-in CSV endpoint (`/stats;csv`) using
  Basic Auth. Default credentials are `admin:admin`, configurable with the
  `USERNAME` and `PASSWORD` environment variables.
- Read `STATS_URL` as a JSON array of stats endpoint URLs. Default to
  `["http://localhost:8080/stats;csv"]` when unset.
- Fetch all configured endpoints concurrently, using a worker thread per server,
  and print a clean summary with an ISO 8601 UTC timestamp, server address,
  frontend/backend status, current/total sessions, bytes in/out, and errors.
- Run continuously, polling every 10 seconds. Handle `SIGINT` and `SIGTERM` for
  graceful shutdown.
- Continue polling other servers when an endpoint is unreachable. Record a
  `SERVER`/`DOWN` event, including its URL and error, in MongoDB when available.
- Save each frontend/backend stats row to MongoDB database `hastats`, collection
  `ldap_lb`. Store `ISODate` as a BSON date. Default MongoDB connection is
  `mongodb://admin:admin@localhost:27017/?authSource=admin`; allow overriding the
  connection URI, database, and collection with `MONGODB_URI`,
  `MONGODB_DATABASE`, and `MONGODB_COLLECTION`.
- If MongoDB is unavailable, log a warning, continue polling and printing HAProxy
  stats, skip persistence for that cycle, and retry the connection on the next
  cycle.
- Use Python's standard library for HTTP and CSV handling, and PyMongo for
  MongoDB access. Keep the implementation simple, clean, and well-commented.

## Setup and run

From the project directory, create and activate a virtual environment and install
requirements:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -u haproxy_stats.py
```

To run it in the background and write logs to a file:

```sh
nohup .venv/bin/python -u haproxy_stats.py >> haproxy_stats.log 2>&1 &
```

Example for polling multiple HAProxy instances:

```sh
export STATS_URL='["http://localhost:8080/stats;csv", "http://haproxy2:8080/stats;csv"]'
```
