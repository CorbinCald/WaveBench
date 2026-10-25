# Security

Please report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/CorbinCald/WaveBench/security/advisories/new).
Include the affected commit, reproduction steps, and expected versus observed behavior.
Keep API keys, personal prompts, and private logs out of public issues.

Security fixes target the current `main` branch. Older snapshots are not maintained separately.

Harness requires the documented Linux sandbox and fails if it cannot start safely.
Generated code runs without host credentials or network access, but web previews
execute JavaScript in your browser, outside that process sandbox. Avoid entering
credentials into generated apps. Prompts and tool results are sent to OpenRouter
and the providers handling the run; local conversations and logs may contain
private data. See [costs and data](README.md#costs-and-data).

Symphony is optional trusted local automation: its hooks and worker processes have
the permissions of the user running the daemon. Review the workflow before starting it.
