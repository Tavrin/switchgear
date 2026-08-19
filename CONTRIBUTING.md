# Contributing

Thanks for looking. This project has an unusual centre of gravity, and knowing it
up front will save you time.

## The one rule that explains the rest

**A claim this tool makes must be true.** Switchgear's product is not features —
it is a boundary and a record that someone can rely on afterwards. So a comment
describing a check that does not exist, a doc stating a limit that was lifted, a
refusal naming a command that does not run, or a status that says `ok` for work
that did not happen are all treated as **serious defects**, on a par with a
crash. Several have been found and fixed exactly that way.

The corollary: if you are not sure a claim is true, measure it and say what you
measured. "I ran it and this happened" beats "this should work".

## Getting set up

```console
$ git clone https://github.com/Tavrin/switchgear && cd switchgear
$ sudo apt install bubblewrap        # or your distro's equivalent
$ pip install -e .
$ bash tests/run.sh                  # ~2 minutes, no network, no spend
$ switchgear doctor
```

The suite is hermetic: it runs against a committed mock provider, never a real
one, so it costs nothing and needs no credentials. It must stay that way.

`tests/soak.sh` is opt-in and takes minutes. Run it if you touch concurrency,
leases, the state root, or anything that only fails under load — it has already
found two races that nothing else did.

## What good change looks like here

- **Evidence over assertion.** If you fix a bug, show how you reproduced it. If
  you add a guard, show it failing without the guard. A test that would pass
  against the broken code is worse than no test.
- **Explain the *why* where it will be read.** Comments here carry the reason a
  thing is the way it is — usually the failure that caused it. That is
  deliberate; a future reader (human or agent) needs the reason more than the
  restatement.
- **Refusals name a remedy.** Every `Refuse` should tell the caller what to do
  next. A refusal without one is a dead end, especially for an agent.
- **Absence is not evidence.** A missing record never means "fine". This has been
  learned five separate times; look at `jobstate.py` before adding a code path
  that infers a state from something not being there.
- **Prefer refusing to guessing.** When the tool cannot establish a fact, it
  should say so and stop, not pick the likely answer.

## What will be turned down

- **Anything that makes this an orchestrator.** No queue, no board, no
  scheduling, no fan-out, no retry policy. One invocation is one agent. That line
  is the reason the tool is small enough to trust.
- **A new runtime dependency**, unless it is load-bearing and argued for. There
  is currently exactly one (`jsonschema`), because every durable record is
  validated before it is trusted.
- **Silent fallbacks.** No "if the sandbox is unavailable, run anyway".
- **Provider behaviour written from documentation.** Adapters are written from a
  captured real stream. See `docs/ADDING-A-PROVIDER.md`; the fixture is the
  authority and the docs are the suspect.

## Adding a provider

Seven methods, roughly 25 lines, plus a captured fixture. `docs/ADDING-A-PROVIDER.md`
is the procedure, and `tests/test_adapters.py::AddingAProvider` is a working
example that exists to keep that claim honest.

## Tests

New behaviour needs a test. Match the existing style: a docstring saying what
would break and, where it applies, what it cost when it did break. Structural
tests (grep the package for a pattern that must not return) are welcome and used
in several places.

Please run the full suite before opening a PR. If it is flaky, that is a bug —
say so rather than re-running until green.

## Commits and PRs

Commit messages here are long, and that is on purpose: they record what was
measured and why a decision went the way it did. You do not have to match the
length, but do say *why*, not only *what*.

## Licence

By contributing you agree your contribution is licensed under
[Apache-2.0](LICENSE), per section 5 of that licence. There is no separate CLA.

## If you are an AI agent

Read [AGENTS.md](AGENTS.md) — it has the specifics you will need and the
failure modes previous agents hit in this codebase.
