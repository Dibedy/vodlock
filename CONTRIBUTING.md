# Contributing to SPOILLESS

Contributions are welcome when they preserve the project's central promise: a viewer should not learn a result, series length, tournament advancement, or future matchup before choosing to reveal it.

## Before changing the interface

- Do not expose scores, winners, map or round totals, durations, progress, bracket position, or future opponents.
- Treat tournament stage names and qualification paths as possible spoilers.
- Keep controls usable without making their presence or absence reveal whether more rounds, maps, or matches exist.
- Preserve keyboard access, focus states, reduced-motion behavior, and the existing responsive layout.
- Reuse the current visual system instead of introducing an unrelated component style.

## Development checks

Run both suites from the repository root:

```powershell
node --test tests/*.test.cjs
indexer/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
```

Changes to indexing should also be checked against the stored detector fixtures. Never weaken publication validation merely to make a held VOD pass.

## Pull requests

Keep changes focused and explain any spoiler-safety implications. Do not commit generated virtual environments, local VODs, diagnostics, credentials, provider cookies, or downloaded analysis files.

When reporting a match-specific problem, prefer the public provider and VOD identifier. Avoid putting a winner, score, map count, or later tournament matchup in the issue title.
