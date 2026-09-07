# Security

Please report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/CorbinCald/WaveBench/security/advisories/new).
Include the affected commit, reproduction steps, and expected versus observed behavior.
Keep API keys, personal prompts, and private logs out of public issues.

Security fixes target the current `main` branch. Older snapshots are not maintained separately.

Harness requires the documented Linux sandbox and fails if it cannot start safely.
Symphony is optional trusted local automation: its hooks and worker processes have
the permissions of the user running the daemon. Review the workflow before starting it.
