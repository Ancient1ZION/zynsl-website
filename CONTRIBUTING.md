# Contributing to ZYN Empire

Thanks for considering a contribution. ZYN Empire is MIT-licensed and welcomes PRs from the community.

## Development Loop

1. Fork the repo and clone your fork.
2. Create a feature branch: `git checkout -b feat/my-improvement`.
3. Install Python deps: `pip install -r zyn-empire-agents/requirements.txt`.
4. Make your changes. Run the local pre-flight: `python3 zyn-empire-agents/test_connection.py`.
5. Run linters: `ruff check . && python -m py_compile $(find . -name "*.py")`.
6. Commit using conventional-commit style: `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`.
7. Push and open a PR against `main`. CI will run automatically.

## Adding a New Agent

Agents are defined declaratively in `zyn-empire-agents/agents_config.json`. To add one:

1. Append a new entry with `id`, `role`, `goal`, `tools`, `persona`, `memory_key`, `discord_channel`.
2. Make sure the `tools` you reference exist in `tools.py` (or add new tool functions there).
3. The orchestrator picks up new agents on the next `pm2 reload`.

## Modifying Agent Behavior

If an existing agent is misbehaving, **do not patch the code path** — refine the `persona` or `goal` field in `agents_config.json` instead. The drift-detector will validate your change against recent log signals.

## Mission-Control Daemons

Changes to anything in `zyn-ops/` should preserve the three core invariants:

- The STOP cell (`CONTROL!A1`) must always halt activity within 60 seconds.
- Every action must be logged to `logs/zyn_empire.log` with agent name + timestamp.
- Discord notifications must route through the GAS proxy, never raw webhooks.

## Code Style

- Python: PEP 8, type hints required on all public functions, `ruff` for linting.
- Line length: 100 characters.
- Logging: `loguru` only; no `print()` in production code paths.
- Imports: stdlib first, third-party second, local last, alphabetized within each block.

## Testing

For now the project relies on the live `test_connection.py` pre-flight as smoke testing. A formal pytest suite is on the roadmap — early contributions welcome.

## Issue Triage

Use the issue templates: bug report, feature request, or drift report. Critical security issues should follow [`SECURITY.md`](./SECURITY.md) instead of the public tracker.
