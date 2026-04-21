# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities through [GitHub's private vulnerability reporting](https://github.com/phaelon74/C3-mose-agent/security/advisories/new).

**Do not** open a public issue for security vulnerabilities.

## Scope

Security issues we care about:

- **Sandbox escapes** — bypassing bash command blocklist or workspace write restrictions
- **Prompt injection** — manipulating the agent through tool output or memory content
- **Memory poisoning** — corrupting the memory database through crafted inputs
- **MCP server exploitation** — using MCP tool calls to access unintended resources

## Out of Scope

- Denial of service against the local LLM server
- Issues requiring physical access to the host machine
- Social engineering attacks against Discord users

## Response

We aim to acknowledge reports within 48 hours and provide a fix or mitigation within 7 days for confirmed vulnerabilities.
