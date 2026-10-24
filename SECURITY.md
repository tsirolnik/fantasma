# Security policy

Fantasma is experimental software for authorized security research. This policy
covers vulnerabilities in Fantasma itself, not findings generated against other
systems. Report those to the relevant system owner through their disclosure process.

## Report privately

Use GitHub's **Report a vulnerability** form:

https://github.com/tsirolnik/fantasma/security/advisories/new

Include the affected version or commit, impact, minimal reproduction steps,
expected versus observed behavior, and relevant environment details. Use a local
or synthetic target where possible. Redact credentials and personal information.
Avoid public issues or pull requests containing exploit details or sensitive logs
before the report has been reviewed.

This is a community-maintained project with no guaranteed response or remediation
SLA. The latest release and main branch receive fixes; older releases do not have
separate maintenance branches. Update before reporting an already-fixed issue.

## Runtime boundaries

- Model-generated findings and reports require human review.
- Scope checks are not a complete sandbox for arbitrary code or external tools.
- MCP servers and Python profile extensions execute trusted code and may access
  files, credentials, networks, or external state with the permissions you give them.
- The dashboard binds to localhost by default and uses token authentication. A
  public hosted service needs separate deployment and isolation work.
- Logs, model traces, reports, and databases can contain sensitive target data.
  Review them before sharing, even when they are ignored by Git.

See the [MCP reference](docs/mcp.md) and
[dashboard reference](docs/reference/dashboard.md) for implementation boundaries.
