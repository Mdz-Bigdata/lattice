<!--
  Licensed to the Apache Software Foundation (ASF) under one
  or more contributor license agreements.  See the NOTICE file
  distributed with this work for additional information
  regarding copyright ownership.  The ASF licenses this file
  to you under the Apache License, Version 2.0 (the
  "License"); you may not use this file except in compliance
  with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing,
  software distributed under the License is distributed on an
  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
  KIND, either express or implied.  See the License for the
  specific language governing permissions and limitations
  under the License.
-->

# Local demo engines

The Lattice WebUI data-source page supports nine engine types. This directory pins the third-party
runtimes that the project starts locally so that every type can be connected, browsed and
queried against a real server rather than a mock.

| Type | Driver used by the backend | Local demo instance |
| --- | --- | --- |
| DuckDB | `duckdb` | `.runtime/webui/sample.duckdb`, generated at startup |
| MySQL | PyMySQL | local `mysqld` (Homebrew), `127.0.0.1:33306` |
| PostgreSQL | psycopg 3 | `lattice_demo` database in the project's PostgreSQL 16 cluster, `127.0.0.1:55432` |
| ClickHouse | clickhouse-connect (HTTP) | official binary pinned in [`clickhouse.json`](clickhouse.json), `127.0.0.1:18123` |
| StarRocks | PyMySQL (MySQL protocol) | official container, FE `127.0.0.1:19030` |
| Apache Doris | PyMySQL (MySQL protocol) | official FE + BE containers, FE `127.0.0.1:29030` |
| Apache Hive | PyHive (HiveServer2 Thrift) | official container, `127.0.0.1:20000` |
| Apache Iceberg | pyiceberg + DuckDB | the project's Apache Polaris REST catalog, `127.0.0.1:8181` |
| Apache Paimon | pypaimon + DuckDB | filesystem warehouse under `.runtime/engines/paimon/warehouse` |

All nine hold the same six sample tables (3390 rows) in `lattice_demo`, so one query returns the
same result on every source. Manage them with:

```bash
./start-web.sh engines
.runtime/envs/core/bin/python scripts/local-engines.py status
.runtime/envs/core/bin/python scripts/local-engines.py stop
```

A regular `./start-web.sh start` reuses whatever is already installed and never downloads
multi-gigabyte images implicitly; engines whose runtime is missing are reported as not ready
and the rest of the WebUI is unaffected.

## Container-backed engines

StarRocks, Doris and Hive have no usable native macOS build, so they run as the official
container images pinned in [`docker.json`](docker.json) (about 13 GB in total) and need a
running Docker daemon. Images are pulled by digest when one is pinned, and a digest mismatch
refuses to start the engine.

Every container, volume and the private `lattice-engines` network carries the label
`lattice.project=<repository path>`, and each container also records a fingerprint of the image
and run options it was created with, so a changed pin rebuilds the container instead of silently
restarting the old one. Engine data lives in the named volumes, so a rebuild keeps the sample tables.

`scripts/lattice_docker_engines.py` only ever inspects, starts or stops objects carrying that label.
A foreign container occupying one of the names is an error rather than something to reuse, and a
volume that is neither labelled nor mounted by one of this project's own containers is refused
rather than written to, so nothing belonging to anything else on the machine is touched.

Published ports bind `127.0.0.1` only. Stopping keeps the containers and named volumes, so the
seeded demo data survives a restart and a cold start brings every engine back with its tables.

Each engine is created with its own demo account and a generated password stored with
owner-only permissions in `.runtime/engines/<engine>.json`; the backend reads those files to
register the builtin data sources and never returns the password to the browser.

Upstream projects keep their own licenses: StarRocks, Apache Doris, Apache Hive and ClickHouse
are Apache-2.0. The images are used unmodified; only configuration and sample data are supplied
by this project.

## Without Docker

The StarRocks, Doris and Hive connectors are part of the backend and do not depend on the local
containers. On a machine without Docker, add a data source of that type in the WebUI, point it
at an existing server, and test, browse and query it there.
