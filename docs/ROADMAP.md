# What is unfinished

Ordered by how much it would change what Switchgear can honestly claim. This
list is deliberately not a wishlist — everything here is a known gap in
something the tool already does.

## 1. Prompt injection is unmitigated

The threat model names it: containment limits what a steered worker can *do*,
not what it can *say*. In a read-only job the blast radius is a wrong answer; in
a bounded-write job the worker writes into the worktree and the only check is a
review performed by another model, which is equally steerable.

What exists today is a bound on its *reach*, not a defence: promotion fails
closed when the reviewed diff contains content addressed at a reviewing agent,
or forging Switchgear's own handoff/review blocks. A careful attacker is not
caught by it.

**This is design work, not a fix.** It needs a decision about how much of the
promote gate may depend on a model's judgement at all.

## 2. Bounded-write jobs run as the invoking user

Read-only jobs get a real uid boundary: the payload runs as a subuid, and a
write to a file owned by the invoking user is refused by the kernel. Bounded-write
does not, because a worker must produce files the controller then reads and
commits, and files written by a subuid are owned by it with no way to hand them
back without privileges Switchgear does not have.

Closing it needs idmapped mounts, ACLs, or a controller-side ownership fix-up at
collection — an ownership model, not a flag.

## 3. Exfiltration through the model channel is unbounded

The broker restricts which *host* a job may reach, not *what* it sends there. A
secret the worker reads can leave inside an otherwise legitimate model call.
There is no proposal for this yet that does not amount to reading the traffic.

## 4. Spend is bounded before a job, not within one, and not across a fan-out

`daily_usd` refuses to start a job once the day's measured spend reaches it, and
the broker enforces a per-job call ceiling counting attempts. Neither bounds a
single runaway job's own cost, which is only known once its stream reports it.

Worse and more concrete: the daily check reads the ledger at job start and the
cost is appended at job end, with no lock. Several jobs launched together can
each pass the same pre-spend check. Overshoot scales with fan-out width.

## 5. No live channel into a running job

Messages are launch-time inputs plus cold resumes. `resume` continues the same
provider session, but there is no way to add information to a job that is already
running. Another project measured five of seven cancel-and-relaunch cycles as
pure waste for exactly this reason.

Whether the channel belongs here or in the orchestrator above is genuinely
unresolved: the *channel* looks like substrate, the *policy about when to send*
looks like orchestration.

## 6. A read-only job's answer is only available truncated or raw

The result record carries the worker's answer as `exitSummary`, clipped at 400
characters. The only unclipped path is `logs --format full`, which is the raw
provider stream and is explicitly not for an agent's context. Fan out ten scouts
and you get ten truncated paragraphs.

The answer *is* the product of a read-only job, so this is the gap most likely to
turn a job that already succeeded into wasted spend.

## 7. Nothing correlates a job with the caller's own identifiers

The task envelope is closed to extra keys and nothing caller-supplied is
persisted, so a supervisor driving a dozen jobs holds the id-to-intent map only
in its own context. Lose that context and the state root is a dozen anonymous
uuids. Several other gaps — deduplication, recovery after a supervisor dies,
cancelling a whole wave — are downstream of this one.

## 8. A crashed job cannot be attributed

Jobs that died without writing a result report `null` for mode, role, model,
provider and directory, because the listing reads attribution only from
`result.json`. The runner record already holds the provider. The `--worktree`
filter also silently drops these jobs, which is exactly the question a recovering
caller asks: *what was running here when I died?*

## 9. Fixture freshness is manual

`providers verify` checks that a new provider build still offers the CLI surface
the adapter needs, and explicitly does not check the event vocabulary — that
needs a re-captured stream. `doctor` reports when an installed version has moved
past the version its fixture came from, but re-capturing is a human step.

## 10. It has not run under sustained real-world load

The soak test passes at 60 jobs against a concurrency cap of 3, with the cap
holding exactly. That is a synthetic load on one machine for a few minutes. It is
not evidence about a week of real use.

## 11. Unsettled: where evidence becomes authority

Not a defect — an architectural question that has to be answered before
Switchgear sits underneath another system that also has gates.

Switchgear's `promote` binds a change to reviewer-attested evidence: the tree
still matches what was reviewed, the reviewer named the files it covered, the
independence rules held. An orchestrator above it typically has gates of its own:
project verification, review at an exact head, merge eligibility, the human merge
decision.

Stacked, those are either defence in depth or two partially overlapping sources
of truth, and which one you get is not automatic. The distinction that needs
drawing is between:

- **worker-execution trust** — did this agent do what the record says, inside the
  boundary? Switchgear owns this.
- **project-verification trust** — do the project's own gates pass on the result?
  The caller owns this, and notably needs protection from the *verification
  commands themselves*, not only from the coding agent. Switchgear's sandbox does
  not cover that; it is not in the loop.
- **workflow and merge authority** — should this land? Never Switchgear's.

The temptation is to conclude "Switchgear owns all isolation, the orchestrator
owns all workflow". That is too simple: verification runs outside Switchgear and
needs its own containment.

**Partly settled.** Two things have changed since this was written.

The vocabulary now distinguishes them. Switchgear's gate is an **interlock
review**: it runs on an uncommitted worker delta, before any project
verification. A caller's is a **project review**: it runs on the exact head that
passed the project's own tests. They are not redundant, because they happen at
different times against different material — but calling both "the review" made
them look redundant, and that is what invited the question of which to drop.

And an operator can now say which layer holds semantic acceptance, in the
operator-owned budget file:

```json
{"acceptance": "interlock"}   // default: switchgear's gate decides
{"acceptance": "external"}    // the caller has assumed that responsibility
```

Under `external`, a bounded write still freezes and still produces identical
evidence — what Switchgear attests, that the worker did what the record says
inside the boundary, does not depend on who accepts the result. The job reports
`awaiting_external_review` rather than `awaiting_review`, deliberately: a caller
polling for the latter and promoting would be acting on a decision nothing made.
`promote` refuses, naming who owns the call.

It is operator-owned rather than a profile field or a flag for the same reason
`daily_usd` is: a project that can vote itself out of review does not have
review, and a worker's own output can reach a caller's argv.

**Still open.** Whether the two reviews *should* both run under a caller that has
its own gate is a judgement about that caller's risk, not something this tool can
decide — so it is configurable and defaults to the stricter option. What has not
changed: `promote` is evidence that the worker did what is claimed, never
permission to merge.
