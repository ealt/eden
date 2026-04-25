Findings:

- [AGENTS.md](/Users/ericalt/Documents/eden/AGENTS.md:82): the rule is right, but the pinned failure text is too specific. In this repo shape, a second `tests/__init__.py` fails as `ImportPathMismatchError` under default `prepend`; the `Plugin already registered under a different name` error shows up under `--import-mode=importlib`. I’d reword this to “collides on `tests.conftest` import/registration” instead of naming one exact error.
- [AGENTS.md](/Users/ericalt/Documents/eden/AGENTS.md:85): point 6 is realistic, but incomplete. `unique-package-name + importlib` also requires updating bare service-test imports like `from conftest` to package-relative imports, or those tests will stop importing.

Otherwise the substance is accurate. The repo state matches the note: `pyproject.toml` has default pytest import mode, only `reference/packages/eden-contracts/tests` has `__init__.py`, and duplicate bare `test_app.py` names do collide under `prepend`.

`consider_namespace_packages` is probably not worth mentioning here. It helps with namespace-package resolution, but your problem is multiple dirs literally named `tests`, and paths like `eden-contracts` / `web-ui` are not clean package names anyway.

Scope is fine in `AGENTS.md` under contribution conventions: this is a repo-wide contributor trap, not just a local helper detail. I would consider adding an automated basename-uniqueness check later, since this is mechanical and AGENTS-only guidance is weaker than a guardrail.

Sources: pytest docs on import modes and good practices: https://docs.pytest.org/en/latest/explanation/pythonpath.html , https://docs.pytest.org/en/latest/explanation/goodpractices.html