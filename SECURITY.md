# Security policy

## Supported version

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Use the repository's private **Security → Report a vulnerability** workflow on
GitHub. Do not disclose credentials, exploitable queries, or sensitive data in
a public issue.

Include:

- affected version or commit;
- reproduction steps;
- expected impact;
- suggested mitigation, if known.

## Deployment boundary

PRING-PACKAGE is research software. Keep source credentials in environment or
secret-management systems, validate remote endpoints, use TLS, and restrict
Neo4j access. Generated manifests and logs must not contain secrets.

