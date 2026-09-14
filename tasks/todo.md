# Lattice WebUI integration work

- [x] Pin and run actual Polaris with persistent isolated storage; verify health and restart.
- [x] Build screenshot-style UI with functional query, SQL, metadata and validation pages.
- [x] Expose all 78 supported public Polaris operations from resolved source specifications.
- [x] Connect catalog, identity, governance and native Apache Ossie model operations to live Polaris.
- [x] Implement and test backend host/origin/query/proxy protections.
- [x] Add repeatable start/stop/status commands and preserve existing tool setup.
- [x] Run live API lifecycle tests, frontend build and browser checks.
- [x] Implement the five native semantic-model operations the upstream release leaves as HTTP 501 stubs.
- [x] Execute those REST routes as the calling Polaris principal, and verify a read-only principal is refused.
- [x] Support nine data source types and run a local demo instance of each with identical sample data.
- [x] Build the data-quality module on the real PostgreSQL business database, following the DataVines metric model.
- [x] Deliver the five quality screens: rules, schedules, report, statistics and execution log.
- [x] Review the quality module adversarially and fix the confirmed findings.
- [x] Fix the Iceberg table-detail crash and make built-in questions answer on every engine.
- [x] Define the 53 CSS classes the components used but the stylesheet never declared.
- [x] Configure a real model provider from dropdowns, and repair model SQL the target engine rejects.
- [x] Load a provider's real model list into the dropdown, honouring the environment proxy.
- [x] Rename the platform layer to Lattice, including every runtime identifier the start scripts
      recreate: lattice_demo, t_lattice_*, lattice_quality, lattice_polaris, the Polaris catalog
      lattice in realm LATTICE, lattice-warehouse, the lattice.project label, the lattice-engines
      network and the lattice-* containers and volumes. Apache Ossie keeps its name everywhere it
      means the specification, its schema and its converters.
