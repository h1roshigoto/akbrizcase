# AGENTS.md — akbrizcase

## Purpose

akbrizcase is a B2B catalog and request-management application.

Primary stack: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy v2, Alembic, PostgreSQL 16, React, TypeScript, Vite, SQLAdmin, Docker Compose, Nginx, GitHub Actions.

Prioritize correctness, security, maintainability, and reproducible verification over speed or token savings.

## Source of truth

- `ARCHITECTURE.md` defines approved architecture, security boundaries, and business contracts.
- `IMPLEMENTATION_PLAN.md` defines MVP scope, dependencies, acceptance criteria, and outstanding decisions.
- Existing schemas, migrations, tests, and working code define the currently implemented behavior.
- The latest `main` branch is the integration baseline.

Read the sections relevant to the task. Follow references and inspect additional files whenever necessary for correctness. Do not skip required context to save tokens, but do not read unrelated documents or the entire repository by default.

If documentation and implementation conflict, identify the discrepancy rather than silently inventing a resolution.

## Engineering principles

- Follow existing project conventions and reuse established services, DTOs, dependencies, and Unit of Work patterns.
- Keep changes focused on the requested MVP. Avoid unrelated refactoring and speculative functionality.
- Preserve public API contracts, existing data, schema constraints, and security behavior.
- Treat both products and services correctly; never invent prices, images, equipment compatibility, or customer data.
- Handle failures explicitly. Do not replace real backend failures with successful-looking fixtures.
- Use PostgreSQL for database integration acceptance; mocks alone are insufficient.
- Add meaningful positive and negative tests for changed behavior.
- Fix root causes rather than weakening tests or bypassing safeguards.

## Security and data

- Use only synthetic data for development, demos, and automated tests.
- Never commit credentials, private keys, runtime secrets, raw session tokens, or real personal data.
- Do not expose sensitive values in responses, logs, reports, screenshots, or test artifacts.
- Preserve database-backed staff authorization, CSRF/Origin protection, session revocation, and audit guarantees.
- Respect publication visibility, consent, data minimization, and personal-data erasure rules.
- Do not use unsafe shortcuts or silently weaken authentication, authorization, validation, or transaction guarantees.
- Never seed an unmarked or production database.
- The owner approved and merged [AQ-01](docs/decisions/AQ-01.md) in PR #21. Its contract is normative for MVP-010; do not alter that decision or retain token-verification material beyond PII erasure.

## Database and migrations

- Keep the Alembic revision graph linear with a single head.
- Inspect the existing migration chain before adding a migration.
- Preserve compatibility and existing records through upgrades.
- Never use destructive reset, downgrade, stamp, or volume deletion to hide a migration problem.
- Coordinate schema changes with the current `main` branch and other active PRs.

## Verification

Use the repository's existing commands and workflows. Select checks based on the files and behavior changed, and complete all MVP-specific acceptance criteria.

Relevant commands include:

- `make backend-test backend-lint backend-types`
- `make integration`
- `npm --prefix frontend test`
- `make frontend-typecheck frontend-build`
- `make config build smoke`
- HTTPS browser acceptance where authentication, cookies, CSRF, or proxy behavior is affected.

Preserve all required GitHub Actions jobs: backend, frontend, PostgreSQL integration, and Compose smoke.

Use targeted test runs during development when helpful, but do not claim completion without the required regression and integration checks. Report unavailable or failed tests honestly.

Keep terminal output and final reports concise; include detailed logs when failures require diagnosis.

## Git and delivery

- Start from the latest `origin/main`, unless explicitly instructed otherwise.
- Use a dedicated branch and PR for each MVP.
- Update an existing task branch/PR when fixing that task; do not create duplicate PRs without a reason.
- Check compatibility with intervening changes in `main`.
- Do not merge PRs, deploy, alter approved architecture, or begin the next MVP without explicit authorization.
- Do not force-push, rewrite shared history, or delete persistent data without explicit approval.

At completion, report the PR URL, head SHA, changed files, actual tests and CI results, remaining limitations, and any decisions requiring human approval.

## Task-specific instructions

The current task prompt defines the immediate scope. These repository instructions are persistent guardrails, not a replacement for the MVP specification.

For cross-cutting or security-sensitive changes, investigate as broadly as correctness requires. Never trade correctness for reduced context usage.
