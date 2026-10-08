# Security Policy

## Supported versions

This project is pre-1.0 and only the latest `main` / release is supported.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately using GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
open the repository's **Security** tab → **Report a vulnerability**.

Please include:

- what the issue is and its potential impact,
- steps to reproduce (redact any credentials or personal data),
- the affected version or commit.

You can expect an initial response within a few days. Thank you for reporting
responsibly.

## Handling of credentials

This tool authenticates to Huckleberry with your account credentials and writes
to a Sprout Track instance with an API key. Both are supplied via environment
variables (see `.env.example`):

- Never commit a real `.env` or paste live credentials into issues/PRs.
- Raw personal data read from Huckleberry (or written to Sprout Track) is
  inherently sensitive — treat logs and captured payloads accordingly.
