# Agent Instructions for Django

## Repository Shape

- Core framework code lives in `django/`.
- Django's own test suite lives in `tests/`; tests are organized by subsystem
  rather than by package path.
- Documentation lives in `docs/` and is written in reStructuredText.
- Project metadata and style configuration are in `pyproject.toml`, `tox.ini`,
  `.flake8`, `.editorconfig`, and `.pre-commit-config.yaml`.

## Working Principles

- Prefer small, subsystem-local changes that match existing Django patterns.
- Read the surrounding implementation and tests before editing; many APIs have
  backend-specific contracts or long-standing compatibility behavior.
- Do not perform broad refactors, formatting churn, or import reshuffling unless
  required for the task.
- Preserve public API compatibility unless the task explicitly asks for a
  documented breaking change.
- For ORM, SQL compiler, database backend, migrations, async, cache, template,
  form, and settings changes, look for cross-file contracts before patching.
- Keep backend-specific behavior in `django/db/backends/<backend>/` when
  possible. Shared behavior belongs in base classes only when all supported
  backends can honor the same contract.
- Avoid process-global mutable state for request/query/connection-sensitive
  behavior. Be careful with class-level caches, mutable defaults, and returning
  mutable internal objects by reference.

## Style

- Django targets Python 3.12+ in this checkout.
- Format Python with Black's 88-character line length.
- Documentation, comments, and docstrings should generally wrap at 79
  characters.
- Follow the local import style: standard library, third-party, Django, then
  local imports. Use `isort` rules from `pyproject.toml`.
- Use surrounding string-formatting style. Avoid f-strings for translatable
  strings and logging/error messages that may require translation.
- In comments, avoid "we"; write direct descriptions such as "Return ..." or
  "Handle ...".
- Use succinct comments only where they explain non-obvious behavior.

## Tests

- Run focused tests with Django's test runner from the repository root:

  ```console
  python tests/runtests.py <test_label> --settings=test_sqlite --parallel=1
  ```

- If the checkout is not installed, set `PYTHONPATH` to the repository root.
- SQLite is the default lightweight backend. PostgreSQL, MySQL, and Oracle
  tests require separate settings and services.
- Use backend feature decorators such as `@skipUnlessDBFeature()` and
  `@skipIfDBFeature()` for database-dependent behavior.
- Prefer precise test labels, for example `queries.test_explain`, before
  running large suites.
- For expected exceptions and warnings, prefer `assertRaisesMessage()` and
  `assertWarnsMessage()`.
- For booleans, prefer `assertIs(value, True)` or `assertIs(value, False)` when
  the exact boolean value matters.

## Documentation

- Update docs for public APIs, behavior changes, settings, management commands,
  and backwards-incompatible changes.
- Put API reference updates in the matching file under `docs/ref/`.
- Put conceptual or workflow explanations under `docs/topics/` or `docs/howto/`
  as appropriate.
- Keep examples runnable and consistent with nearby documentation style.
- Avoid documenting backend behavior more broadly than the code and feature
  flags actually support.

## Database and ORM Changes

- Route SQL generation through `Query`, `SQLCompiler`, and backend operations
  instead of duplicating SQL assembly in `QuerySet`.
- Clone `Query` objects when following an existing API pattern that preserves
  queryset laziness or avoids mutating reusable query state.
- Validate option names, supported backend features, and public return formats
  consistently with nearby APIs.
- Consider `EmptyResultSet`, `EmptyQuerySet`, combined queries, subqueries,
  annotations, aliases, transactions, async wrappers, and multi-database use.
- Keep feature flags in `DatabaseFeatures` meaningful and actually consumed by
  the implementation or tests that rely on them.

## Validation Commands

- Focused unit tests:

  ```console
  python tests/runtests.py queries.test_explain --settings=test_sqlite --parallel=1
  ```

- Formatting and linting:

  ```console
  tox -e black
  tox -e isort
  tox -e flake8
  ```

- Documentation checks:

  ```console
  tox -e lint-docs
  tox -e docs
  ```

Run the smallest relevant checks first, then broaden when the change touches
shared infrastructure or public behavior.
