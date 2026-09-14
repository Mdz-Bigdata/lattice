# Apache Polaris integration

Lattice runs the unmodified **Apache Polaris 1.7.0** JVM server and admin tool. The source is pinned to
[`4ac2f059d1cce149453d0a5f1ff1dff980ec97cc`](https://github.com/apache/polaris/tree/4ac2f059d1cce149453d0a5f1ff1dff980ec97cc).
The release URL and SHA-512 checksum are in [`upstream.json`](upstream.json); the startup helper verifies
the pinned checksum before extracting a downloaded distribution. Upstream code and copied specifications
retain their [Apache 2.0 license](LICENSE) and [NOTICE](NOTICE). No upstream source is patched.

```bash
./scripts/polaris-service.sh start
./scripts/polaris-service.sh status
./scripts/polaris-service.sh stop
./scripts/polaris-service.sh restart
./scripts/polaris-service.sh admin --help
```

Requires JDK 21+, Python 3.12+, curl, OpenSSL, and PostgreSQL 15+ binaries. The current macOS setup
automatically finds the installed PostgreSQL 16 binaries. Set `LATTICE_PG_BIN` if they are elsewhere.
An existing cluster keeps its original binary major version; changing that major requires a PostgreSQL
upgrade or restore rather than simply replacing the saved binary path.

The first start downloads the official 395 MB binary distribution; subsequent starts use the local cache.
The source checkout used during integration is `.runtime/polaris-source`. To reproduce it:

```bash
git clone --depth 1 --branch apache-polaris-1.7.0 https://github.com/apache/polaris.git .runtime/polaris-source
```

## Local service and data

| Resource | Location |
| --- | --- |
| Polaris REST APIs | `http://127.0.0.1:8181` |
| Health and metrics | `http://127.0.0.1:8182/q/health`, `/q/metrics` |
| PostgreSQL | dedicated instance at `127.0.0.1:55432`, database `lattice_polaris` |
| Local S3 object store | actual MinIO at `http://127.0.0.1:19000` |
| Local object data | `.runtime/polaris/object-store`, bucket `lattice-warehouse` |
| Persistent metadata | `.runtime/polaris/postgres` |
| Distribution | `.runtime/polaris/polaris-bin-1.7.0` |
| Credentials | `.runtime/polaris/credentials.json`, owner-only permissions |
| MinIO credentials | `.runtime/polaris/local-s3.json`, owner-only permissions |
| Server logs | `.runtime/polaris/server.log`, `.runtime/polaris/polaris.log` |
| PostgreSQL logs | `.runtime/polaris/postgres.log` |
| Bootstrap logs | `.runtime/polaris/bootstrap.log` |
| MinIO log | `.runtime/polaris/minio.log` |

The launcher initializes a separate PostgreSQL cluster and password. It does not connect to, change,
or stop an existing PostgreSQL server on port 5432. Stopping this integration preserves data.
Process identity includes the command and start time; a reused PID never authorizes stopping an unrelated process.
Root client credentials and token signing keys are generated once, kept on disk with owner-only permissions,
and reused after restart. Back up the whole private `.runtime/polaris` directory when services are stopped.
Never expose this directory through a static file server or include it in source control.

The local object store is the official, unmodified MinIO binary, checksum-pinned in
[`minio.json`](minio.json). MinIO is a separate **AGPL-3.0** program; its license is preserved in
[`MINIO-LICENSE`](MINIO-LICENSE), and its source is available at the repository/release named in the pin.
It runs as its own process and persists actual S3 objects; it is not an in-memory mock.
Supported binary targets are macOS/Linux on arm64/amd64. The MinIO browser console is disabled.

The initial catalog `lattice` and namespace `demo` use `s3://lattice-warehouse/lattice`. The local
`catalog_admin` role has `CATALOG_MANAGE_CONTENT`; credential vending through MinIO STS is enabled.
No insecure FILE storage or production-readiness bypass is enabled. Iceberg create/load/drop/purge
and temporary credential vending have been exercised against these actual services.

The backend reads only the root OAuth fields from `credentials.json`. Use
`POST /api/catalog/v1/oauth/tokens`, form-encoded `grant_type=client_credentials`, `client_id`,
`client_secret`, and `scope=PRINCIPAL_ROLE:ALL`; send `Polaris-Realm: LATTICE`. The WebUI keeps
these credentials on the server side. Direct REST clients use the same upstream APIs.

## Upstream surface

The full source specifications are kept in [`spec/`](spec/). Use `polaris-catalog-service.yaml`
with its referenced `polaris-catalog-apis/*` files: the release's generated catalog bundle omits
the new semantic-model operations. The management specification defines 33 operations and the combined
catalog specification defines 45 operations, including the five semantic-model operations.

| Capability | Integration |
| --- | --- |
| Catalogs, principals, principal roles, catalog roles, grants | Native management API |
| Iceberg namespaces, tables, views, commit transactions, metadata | Native catalog API |
| Generic tables | Enabled |
| Policy store and policy attachments | Enabled |
| Apache Ossie native semantic models | Implemented by the Lattice gateway; upstream 1.7.0 answers all five routes with HTTP 501 |
| Catalog federation | Enabled; connection configuration is required |
| OAuth, token exchange and credential vending | Native authentication and storage APIs |
| Admin bootstrap, purge, maintenance tooling | Official `admin` distribution through wrapper |
| Health, metrics, event listeners, external auth and authorization | Upstream configuration |

This integration preserves the upstream implementation rather than reimplementing its catalog behavior:
the 73 operations Polaris implements are forwarded to the running server unchanged.

The five semantic-model operations are the exception. The native feature flag is enabled, but the pinned
release's `SemanticModelCatalogAdapter` returns HTTP 501 for every one of them, and rebuilding the same
source cannot change that. Lattice therefore serves those five routes itself, in
[`webapi/semantic_models.py`](../../webapi/semantic_models.py), against the same source specification:

* request and response bodies, error types and status codes follow `spec/polaris-catalog-apis/semantic-models-api.yaml`;
* the target namespace must exist in the running Polaris (404 otherwise);
* documents are validated against this repository's Apache Ossie JSON Schema (400 otherwise);
* updates use the spec's opaque `entity-version` for optimistic concurrency (409 on a mismatch);
* each model is stored as a Generic Table record in the requested catalog and namespace, so it persists
  in Polaris's own PostgreSQL metastore and survives a restart.

External clients can call the same paths on the Lattice service
(`http://127.0.0.1:8787/polaris/v1/{prefix}/namespaces/{namespace}/semantic-models`) with their own
Polaris bearer token. Every upstream call on those paths is made **with the caller's token**, so Polaris
itself decides what that principal may do and the gateway never lends its root identity: a read-only
principal lists and loads models but receives 403 on create, update and drop, and a caller with no token
receives 401. `scripts/verify-webui.py` creates a temporary read-only principal on each run to check this
and deletes it afterwards. Upstream Polaris itself keeps returning 501
on port 8181, and the API console labels these five operations as gateway-implemented rather than claiming
upstream support. The separate Apache Ossie YAML version archive remains identified separately.
Features needing cloud storage, external Iceberg catalogs, Kafka, OIDC, OPA/Ranger, or other infrastructure
still require their corresponding endpoints and credentials. Polaris is a catalog service; it does not
execute SQL queries or provide an LLM. Those components belong to the Lattice application or a configured query engine.

## Configuration

Local settings are written to `.runtime/polaris/application.properties` on each start. Put upstream
runtime options in `.runtime/polaris/overrides.properties`; the launcher merges them on the next start.
Generated credentials should not be copied into committed configuration. The helper fixes its own HTTP
and PostgreSQL ports and loopback binding. Use a separate deployment configuration for remote access.

For explicit process-environment changes, create private `.runtime/polaris/server-env.json` and
restart. Values are strings; `null` removes a variable. The defaults select the private local MinIO
credentials. An external AWS deployment can remove `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`,
then set `AWS_PROFILE` or the SDK's other credential-discovery variables. This switches the server's
AWS service identity; the seeded MinIO catalog consequently requires its local identity again before
it can be used. External catalog/storage configuration, OIDC and other upstream options can be supplied
in `overrides.properties`. Production deployments should use their own secrets manager and identity.

Supervisors can use `start --receipt /absolute/path.json` followed by `rollback /absolute/path.json`
if a later component fails. The private receipt records only newly started process identities, so rollback
preserves dependencies that were already running and refuses to stop replacement processes. Ordinary
`stop` intentionally stops all services owned by this project. Startup failures clean up only components
created during that attempt. `status` emits JSON and verifies health, OAuth, management APIs, and MinIO.

The official binary includes PostgreSQL JDBC. H2 support is an upstream test dependency, so this local
installation uses durable PostgreSQL rather than attempting to switch the binary to an unavailable H2 driver.
Upstream default `in-memory` user-secret storage is retained; cloud static secrets or federation secrets
must be configured using the appropriate external secrets manager for durable production usage.

See the [official release](https://github.com/apache/polaris/releases/tag/apache-polaris-1.7.0),
[Polaris documentation](https://polaris.apache.org/), and the pinned source's
`site/content/in-dev/unreleased/configuration/` for the full set of configuration options.

Run the lifecycle safety checks with:

```bash
python3 -m unittest discover -s integrations/polaris -p 'test_*.py' -v
```
