from __future__ import annotations

PINNED_OPENCODE = "1.18.18"
PINNED_BINARY = "/home/user/.opencode/bin/opencode"

# NOTE: the version check deliberately does not live here as a host-side
# subprocess. Executing an untrusted provider on the host to ask its version
# hands it controller-side code execution before any boundary exists.
# provider.assert_pinned_version validates output captured from inside bwrap.
