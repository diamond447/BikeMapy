# Contributing to BikeMapy

BikeMapy uses small, issue-scoped pull requests. Create a branch from `main`
using `<type>/<issue-number>-<slug>`, keep code and documentation in English,
and include tests with behavior changes. Only the repository owner merges pull
requests.

Before opening a pull request, run `make check`. The CI workflow is
authoritative and runs backend Ruff, mypy, and pytest checks alongside frontend
ESLint, Prettier, TypeScript, Vitest, and build checks. Do not commit `.env`
files, credentials, tokens, generated local volumes, or production settings.

Use Conventional Commits for commits and pull-request titles, for example
`feat: add route catalogue endpoint` or `test: cover source validation`.
