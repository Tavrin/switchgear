# Switchgear

**Run one coding-agent job inside an OS sandbox and get a record of what happened.**

Switchgear is a command-line tool. You point it at a git worktree, tell it which
role to run, and give it a task. It resolves which model that role uses from a
registry it controls, compiles a policy, starts a credential broker, builds a
`bwrap` sandbox, runs the agent's own CLI inside it, streams the output to disk
as it arrives, translates that stream into a single normalised vocabulary, and
writes a structured result record.

```console
$ switchgear --state ~/.local/state/switchgear scout . "Where is auth checked?"
$ switchgear --state ~/.local/state/switchgear jobs
1f6d5bdf  ok  scout  claude/claude-haiku-4-5  4s  $0.0486  my-repo
```

## One interface, four agents

It drives **Claude Code, Codex, Grok and OpenCode**. The point is not that it can
launch them — anything can launch them — but that once it does, they behave
identically from outside: the same event vocabulary, the same job states, the
same exit codes, the same cost figure, the same `resume` path, the same polling
and blocking commands.

That translation layer is the largest single piece of the codebase. Each adapter
is written from a captured real stream from that provider, never from its
documentation. The practical consequence: choosing *which* agent runs a job
becomes a line in a config file rather than an integration.

## Knowing when a job is done

```console
$ switchgear scout . "..."                      # blocks, prints the record, exits 0/1/2/124
$ job=$(switchgear --json scout . "..." --background | jq -r .job_id)
$ switchgear wait "$job"                        # same record, same exit code, one call
$ switchgear status "$job"                      # ~30-token poll, valid while it runs
$ switchgear logs "$job"                        # byte-capped structured digest
```

No polling loop, no guessed interval. `wait` is bounded and distinguishes *the
job timed out* (124) from *I stopped waiting* (1, `waited_out`, job untouched).

## The write lane

Read-only jobs just run. A bounded-write job needs a lease on the worktree and an
explicit opt-in, and when it finishes it parks at `awaiting_review` — not `ok`. A
separate review job, on a different model and vendor, has to approve it, and
`promote` only flips it to `ok` if the tree still matches what was reviewed, the
reviewer named the files it covered, the independence rules held, and no
disqualifying finding was reported.

Almost all of that gate is digests and identity rather than a model's opinion.

## Install

Linux only. Requires Python 3.11+, `bubblewrap`, and `git`.

```console
$ sudo apt install bubblewrap          # or your distro's equivalent
$ pip install switchgear
$ switchgear doctor                    # tells you what is missing and how to fix it
```

`doctor` is the intended starting point. Every check that reports a problem also
says what to do about it.

You also need at least one agent CLI installed — Claude Code, Codex, Grok or
OpenCode. Switchgear discovers them; `switchgear providers` shows what it found.

## What it deliberately does not do

It does not schedule, queue, prioritise or fan out. **One invocation is one
agent, one process, one sandbox.** Running ten agents means calling it ten times.
Deciding *which* ten is somebody else's job.

It also does not write code, review code, or decide anything about your project.
It runs someone else's agent and records what happened.

## Honest limits

- **Linux only.** Containment is `bwrap`. No macOS or Windows path, and no
  unsandboxed fallback — it refuses to run rather than degrade. See
  [docs/PORTABILITY.md](docs/PORTABILITY.md); a Linux VM works unchanged.
- **Read-only jobs run as a separate uid; bounded-write jobs run as you.** For
  write jobs the isolation is mounts and namespaces only.
- **Prompt injection is not solved.** What is bounded is its reach: a change
  whose diff is addressed at the reviewer cannot auto-promote. A careful attacker
  is not caught by this.
- **Exfiltration through the model channel is unbounded.** The broker restricts
  which host a job may reach, not what it sends there.
- **Spend is bounded before a job starts, not within one.**
- **Single machine, single user.** The state root is a directory with no access
  control of its own.
- **Provider CLIs update often.** Their flags are checked automatically; their
  event vocabulary is only checked against a captured fixture, so `doctor` tells
  you when a fixture has aged out.
- **It has not run under sustained real-world load.** A synthetic soak at 40
  concurrent jobs passes; that is not the same thing.

## Documentation

| | |
|---|---|
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | the caller contract — read this first |
| [docs/ADDING-A-PROVIDER.md](docs/ADDING-A-PROVIDER.md) | adding an agent CLI (7 methods, ~25 lines) |
| [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) | what is defended, and what is not |
| [docs/CONTAINMENT.md](docs/CONTAINMENT.md) | how the sandbox is built |
| [docs/PORTABILITY.md](docs/PORTABILITY.md) | what is Linux-bound and what to do elsewhere |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [AGENTS.md](AGENTS.md) | for humans · for AI agents |

## Status

Alpha. Around 11,000 lines, 20 commands, ~390 tests plus an opt-in soak. All four
providers are proven live on both lanes — every claim that a lane works means a
real job completed, not that the code looks right.

Most of what is in it exists because running it surfaced something that reading
it did not.

## Licence

[Apache-2.0](LICENSE).
