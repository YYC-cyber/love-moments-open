# Security Policy

## Private data boundary

This repository must never contain production databases, media, backups,
environment files, API credentials, private keys, real account credentials,
private planning documents, or production service addresses. Runtime data
belongs in external persistent storage and configuration belongs in local
environment variables or a secret manager.

Before publishing changes, run:

```bash
python3 scripts/check_repo_safety.py
```

## Reporting a vulnerability

Use GitHub Security Advisories to report vulnerabilities privately. Do not
include real user data or credentials in a public issue.
