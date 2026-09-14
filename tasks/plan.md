# Lattice WebUI and Apache Polaris integration

The user authorized a screenshot-style Chinese data platform — Lattice — and integration of Apache Polaris. Preserve the existing CLI tools and run a real pinned upstream service. Lattice is the platform layer built in this repository; Apache Ossie is the specification and its official validators and converters, which keep their names.

## Architecture

- React/TypeScript UI follows the supplied navy-sidebar, tabbed workspace and question/chart layout.
- One loopback FastAPI origin serves the built UI, local sample DuckDB queries, Apache Ossie validation and the authenticated Lattice gateway to Polaris.
- Apache Polaris 1.7.0 (commit `4ac2f059d1cce149453d0a5f1ff1dff980ec97cc`) runs unchanged using its official binary, with a dedicated persistent PostgreSQL cluster.
- Resolve the upstream management and combined catalog source specs: 33 + 45 = 78 documented operations, including five native semantic model operations carrying Apache Ossie documents. Do not use the stale generated bundle or unsupported raw Iceberg endpoints.
- A general API workbench exposes every documented operation; common catalog/identity views simplify discovery.
- The five native semantic-model operations are stubs in the pinned release, so the Lattice gateway implements the same source-spec contract on top of real Polaris generic tables, keeping entity-version concurrency.
- Nine data source types (DuckDB, MySQL, PostgreSQL, ClickHouse, StarRocks, Doris, Hive, Iceberg, Paimon) each have a local demo instance seeded with the same six tables; StarRocks, Doris and Hive use pinned official container images because they have no native macOS build.
- The data-quality module follows the DataVines metric model (invalidate-items query, actual value, expected value, result formula, operator, threshold) and stores its rules, schedules, executions and results in the user's real PostgreSQL database under a dedicated lattice_quality schema, which it creates additively and never mixes with the existing application tables. Every runtime identifier the platform creates carries the lattice name: lattice_quality, lattice_demo, lattice_polaris, t_lattice_*, the Polaris catalog lattice in realm LATTICE, the lattice-warehouse bucket, the lattice.project Docker label, the lattice-engines network and the lattice-* containers and volumes. The start scripts recreate all of them, so they are renamed together and must stay spelled identically across the modules that share them.
- Sample SQL data and deterministic local question parsing are labeled explicitly. Optional external model/query/cloud services require their own configuration and are not reported as tested.
- Keep root credentials server-side, enforce loopback Host/origin/CSRF protections, route only known operations, and constrain sample queries to read-only local tables.

## Verification

1. Build and test backend boundaries, source-spec operation coverage and frontend behavior.
2. Run actual Polaris CRUD for catalogs, namespaces, identities, roles/grants, tables, policies, generic tables and Apache Ossie models.
3. Verify service restart preserves data and launch scripts do not kill unrelated listeners.
4. Open the UI and verify query/chart, SQL, model validation and Polaris operations visually.
5. Review code, resolve findings and document enabled features versus external configuration requirements.

## Work ownership

- Polaris agent: integrations/polaris and scripts/polaris-service.sh.
- Frontend agent: web directory.
- Primary agent: webapi, startup integration, tests and end-to-end verification.

User authorization covers the implementation and local startup; no deployment or external account changes are planned.
