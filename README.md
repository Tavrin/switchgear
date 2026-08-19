# switchgear

Generic provider/worker substrate. First adapter: OpenCode.

This is **not** a manager, tracker, or scheduler. A project profile supplies
model allowlists, role maps, and command verbs. The adapter does not know
about any particular product, gate, or lock.

## Status

Remediation of Stage-0 after independent review of `47e21bdd`.
Python control plane + `bwrap` filesystem/process boundary.
**Not installed. Not a production security authorization.**
Write remains disabled on the example profile. Gate C is NO-GO.

See `docs/SECURITY-REMEDIATION.md`.

## Do not install

See `docs/INSTALL-MAP.md`. The live OpenCode rail stays untouched.

## CLI

```
switchgear --state DIR state provision DIR
switchgear [--profile P] [--state S] [--provider ABS] models
switchgear ... scout  <dir> "<prompt>"
switchgear ... review <dir> <role> "<prompt>"
switchgear ... write  <dir> <role> --envelope FILE
```

`--provider` must be an absolute path. No PATH lookup. Live OpenCode
requires `SWITCHGEAR_ALLOW_LIVE_PROVIDER=1` and the pinned version.

`write` is refused unless `SWITCHGEAR_WRITE=1` **and** the profile has
`write_enabled: true`. The example profile leaves write off.

## Tests

```bash
tests/run.sh
```

Hermetic. Uses `tests/helpers/mock-opencode`. Never calls a live model.
