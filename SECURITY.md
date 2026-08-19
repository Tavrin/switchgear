# Security policy

Switchgear's product is a boundary and a record. A defect that lets a worker
escape the boundary, reach a credential, or alter its own evidence is the most
serious kind of bug this project can have.

## Reporting a vulnerability

Please report privately, not as a public issue: open a
[GitHub security advisory](https://github.com/etiennedoux/switchgear/security/advisories/new).

Useful to include: what you did, what happened, and — if you have one — a
reproduction against a disposable worktree. A `switchgear doctor --json` output
and the relevant `evidence/events.jsonl` help, but **check them for credentials
before sending**: a retained sandbox home can contain a live access token for a
fallback-tier provider.

There is no bounty and no SLA. This is a small project; you will get an honest
answer rather than a fast one.

## Scope

In scope, and treated as serious:

- Escaping the sandbox: writing outside the worktree, reading the host home,
  reaching the network when a broker is in use.
- Reaching a credential that should not be in the sandbox, or extracting one
  from the broker.
- Altering, truncating or rewriting evidence for a job that already ran.
- Promoting a change that the gate should have refused — a forged review, a
  freeze that does not match the tree, a bypassed independence rule.
- Any command that reports success for work that did not happen, or failure for
  work that did. A tool whose claims are wrong is worse than one that refuses.

## Known limits — not vulnerabilities

These are documented in [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) and are
current design decisions rather than oversights:

- **Bounded-write jobs run as the invoking user.** Isolation is mounts and
  namespaces; there is no uid boundary on that lane. Read-only jobs do get one.
- **Prompt injection is not solved.** The promotion path fails closed when a
  reviewed diff is addressed at the reviewer, which bounds the reach into the
  one gate that depends on a model's judgement. It does not detect injection.
- **Exfiltration through the legitimate model channel is unbounded.** The broker
  restricts which host a job may reach, not what it sends there.
- **A hostile provider binary is out of scope.** Switchgear pins and verifies the
  builds it runs, but a compromised agent CLI is a compromised agent CLI.
- **Not multi-tenant.** One machine, one user, a state root with no access
  control of its own.

If you think one of these is drawn in the wrong place, that is a legitimate
issue and an interesting one — please open it publicly.

## Supported versions

Pre-1.0: only the current `main` is supported.
