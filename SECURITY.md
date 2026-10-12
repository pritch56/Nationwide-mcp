# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** using GitHub's private vulnerability reporting for this
repository ("Security" tab, "Report a vulnerability"). Do not open a public issue for a security
problem, and never include real tokens, client secrets, API keys, private keys, account numbers or
statement files in a report: describe the problem with made-up data.

Include what you did, what happened, what you expected, and the version (`nationwide-mcp --version`).
This is a volunteer project with no guaranteed response time.

## Scope

In scope:

- Leaks of credentials or tokens (logs, error messages, files written with wrong permissions).
- Requests sent to hosts other than the configured provider, or path / parameter injection through
  tool arguments.
- Bypasses of the HTTP transport's bearer-token check or of its refusal to bind publicly without one.
- The link helper accepting an authorisation response that does not belong to the current session.
- The statement importer reading files outside its directory, or inputs that make it hang.
- Anything that lets this read-only server change something at the bank or the aggregator.

Out of scope (documented behaviour, see the README "Security" section):

- Token files are plaintext, protected only by file permissions.
- `NATIONWIDE_TOKEN_STORE=package.module:ClassName` runs code named in the environment.
- The HTTP transport is plain HTTP with one static bearer token; TLS is your responsibility.
- Transaction descriptions, merchant names, notes and account names are untrusted third-party text
  and may contain prompt-injection or spreadsheet-formula content. The server passes them through.
- Vulnerabilities in the aggregators or the bank themselves: report those to them.

## Supported versions

Only the latest release (or the main branch before the first release) receives fixes.
