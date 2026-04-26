# Security Policy

## Reporting a Vulnerability

If you discover a security issue in ZYN Empire, **do not open a public GitHub issue**.

Instead, email the maintainer directly with:

- A description of the vulnerability
- Steps to reproduce
- Affected components (dashboard, agent stack, mission control)
- Your suggested severity (low / medium / high / critical)

Security reports are acknowledged within 48 hours and patched within a target of 7 days for critical issues.

## Scope

In-scope:

- The dashboard (`dashboard.html`) — XSS, CSRF, broken access control
- The agent stack (`zyn-empire-agents/`) — code injection via agent prompts, credential leakage, sandbox escape
- The mission-control daemons (`zyn-ops/`) — privilege escalation, command injection
- The GAS proxy — webhook URL leakage, unauthorized invocation

Out of scope:

- Issues requiring physical access to the GCP VM
- Social engineering of the operator
- DoS attacks against the free-tier LLM providers (report those upstream)

## Supported Versions

Only the `main` branch is supported. Tagged releases are best-effort.

## Disclosure

Once patched, the vulnerability is disclosed in a CHANGELOG entry and credited to the reporter unless they request anonymity.
