# Ossie WebUI

Chinese-language local data workspace with a screenshot-inspired enterprise layout.

## Development

```sh
npm ci
npm run dev
```

The Vite development server proxies `/api` requests to `http://127.0.0.1:8787`.
Start the project's Python API server before using the application.

```sh
npm run build
npm run format:check
```

Production files are written to `dist/` and served by the project's API server.
The lockfile is committed; generated output and dependencies are ignored.

## Behavior

- All charts and data grids use API responses. The initial chart runs the real
  local example-data query `月度销售额趋势`.
- Natural-language queries display their actual provider and data source.
- SQL, field dictionaries, quality checks, and the semantic model editor use the
  local API. YAML edits can be validated and exported; they are not silently saved.
- Polaris Catalog and identity pages read the live service. The API console
  discovers operations from the backend's official OpenAPI inventory and supports
  request parameters, JSON bodies, responses, and deletion confirmation.
- The semantic editor links to Polaris's native semantic-model API operations.
- Pages use hash URLs, for example `/#questions`, `/#sql`, and `/#explorer`.

The UI deliberately does not claim to have a Trino connection or an LLM when its
backend is using the local DuckDB example database and rule-based SQL provider.

Licensed under the Apache License, Version 2.0.
