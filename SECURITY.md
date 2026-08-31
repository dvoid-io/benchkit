# Security

Report vulnerabilities through GitHub private vulnerability reporting (the repository's
**Security** tab → *Report a vulnerability*). Please do not open a public issue for a
suspected vulnerability.

benchkit never stores secrets: Langfuse and gateway credentials are read from the environment
only, never from files or argv, and are never printed (`doctor` reports presence only). Use
whatever secret manager you like to put them in the environment. Dependencies are kept current
by Dependabot/Renovate; releases are built by CI from `v*` tags.
