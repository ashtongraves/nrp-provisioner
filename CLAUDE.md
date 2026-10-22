# nrp-provisioner: notes for agents

**Read [README.md](README.md) before changing behaviour or diagnosing anything**, and
[wn/README.md](wn/README.md) for the worker image. They record how the system works and what was
learned in production, including dead ends, so you don't have to rediscover it. Keep them up to date
when you learn something new; there are no other docs.

## Working here

- **Cluster access may be read-only** (context `claude-readonly`). Give the maintainer exact
  `kubectl` commands to run, and verify their effect read-only afterwards. Never print secret or
  token contents; decode only token claims, never the signature.
- **Unprivileged user namespaces are disabled on the nodes on purpose.** Don't propose turning them
  back on.
- **Test the image, not just the code.** Run the unit tests, then `tests/image/provisioner.sh` or
  `tests/image/wn.sh` on every image you build, before inventing ad-hoc checks.
- **Push test builds to a tag nothing uses** (deployments pull with `Always`), and roll out one
  namespace at a time. See "Rolling out" in the README.
- **The maintainer reviews changes as PRs** on github.com/ashtongraves/nrp-provisioner. Open them
  against `main`, not stacked on another PR's branch: a stacked PR merged after its base once never
  reached `main`.
- **No sensitive data in the repo or docs:** no tokens, user names, internal IPs or real job ads.
- **Prefer evidence over inference.** Many wrong turns came from reading a symptom without checking.
  Look at an AP's job history, a slot's ad, `condor_q -debug` with `_CONDOR_TOOL_DEBUG=D_SECURITY:2`,
  or a server-side dry run, before concluding.
