## Summary

Describe the focused change and why it belongs in SPOILLESS.

## Spoiler safety

Explain whether the change can affect scores, durations, round or map totals, tournament progression, future opponents, or the visibility of navigation controls.

## Verification

- [ ] `node --test tests/*.test.cjs`
- [ ] `indexer/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"` when indexer code is affected
- [ ] Relevant responsive and keyboard behavior checked when the interface is affected
- [ ] No credentials, cookies, private VODs, generated environments, or local diagnostics included
