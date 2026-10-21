# nrp-provisioner: notes for agents

Demand-driven HTCondor provisioner for OSG workloads on the NRP Nautilus Kubernetes cluster. One
provisioner pod per namespace reads idle jobs from whitelisted HTCondor schedds, groups them by
resource shape, and creates Kubernetes Jobs whose pods run the OSPool pilot (one static slot per pod).

**Read [docs/knowledge.md](docs/knowledge.md) before changing behaviour or diagnosing anything**, and
[docs/troubleshooting.md](docs/troubleshooting.md) for a specific symptom. They record what was
learned in production, including dead ends, so you don't have to rediscover it.

## Layout

- `provisioner/`: the provisioner image (`Dockerfile`, startup scripts, `provisioner_main.py`).
  - `prp_provisioner/`: the library. `provisioner_htcondor.py` is the only file that touches the
    HTCondor bindings, and `provisioner_k8s.py` the only one that builds Job bodies.
  - `tests/`: `python3 -m unittest discover -s tests`, run from `provisioner/`.
- `wn/`: the worker image. Keep it identical to `osg-htc/nrp-ospool-ep` in
  github.com/opensciencegrid/images.
- `k8s/<namespace>/`: deployment, `osg_provisioner.conf` (its live ConfigMap) and PVCs per namespace.
  `k8s/rbac/` has the service accounts.

## Facts that aren't obvious from the code

- **The OSPool has no central job queue.** Job ads live only on each access point's schedd; the
  collector has only per-user counts. The provisioner must be able to read each schedd it serves.
- **Schedd reads are anonymous in practice.** The pool token is an OSPool HS256 IDTOKEN that access
  points can't verify. Anonymous reads go over SSL, so the image's **CA bundle** decides whether they
  work. "Site locked us out" has twice been our own image (stale CA certs, an old HTCondor client).
- **Unprivileged user namespaces are disabled on the nodes on purpose** (a security decision; don't
  propose turning them back on). Apptainer can't run, so container jobs can't run. All namespaces use
  `apptainer=no`, which makes the provisioner, the worker validation and the pilot's image download
  consistent.
- **A namespace isn't a pool.** `osg-ligo` joins the IGWN pool; the other three join the OSPool.
  `osg-icecube` used to be IceCube's own pool but isn't any more.
- **Worker pods that nothing claims exit after 20 minutes and get replaced.** The back-off pauses job
  groups that keep doing that. Its blind spot is groups where only some of the jobs can match.
- **Worker-image startup scripts in `/etc/osg/image-init.d/` are sourced**, so never `exit 0` to skip.
- **The `cvmfs` PVC is added to worker pods in code**, not in the deployment, so it looks unused.
  Don't delete it.

## Working here

- **Cluster access may be read-only** (context `claude-readonly`). Give the maintainer exact
  `kubectl` commands to run, and verify their effect read-only afterwards. Never print secret or
  token contents; decode only token claims, never the signature.
- **Images build on the maintainer's arm64 Mac under amd64 emulation.** It's slow, and EL10 behaves
  differently from a native build (e.g. `useradd` skipping `/etc/skel`). `--pull` forces a full
  rebuild, so skip it for quick test builds. There's no `timeout` command on that Mac.
- **Test the image, not just the code.** Run the unit tests inside the built image. Smoke-test
  `provisioner_startup.sh` with a *fake service account* mounted, so the run gets past the Kubernetes
  login. Test Job submission through the real `kubernetes` client (`tests/test_k8s_submit.py`), since
  a fake API object skips its validation.
- **Tags:** deployments pull with `Always`. Push test builds to a tag nothing uses, and check users
  first:
  `kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.template.spec.containers[*].image}{"\n"}{end}' | grep nrp-provisioner:`.
  `nrp-provisioner:X` and `nrp-provisioner-wn:X` are independent images.
- **Roll out one namespace at a time:** `osg-icecube`/`osg-nrao` first (they launch little), then
  `osg-opportunistic`. The ConfigMap is mounted with `subPath`, so config changes need a restart.
- **The maintainer reviews changes as PRs** on github.com/ashtongraves/nrp-provisioner.
- **Prefer evidence over inference.** Many wrong turns came from reading a symptom without checking.
  Look at an AP's job history, a slot's ad, `condor_q -debug` with `_CONDOR_TOOL_DEBUG=D_SECURITY:2`,
  or a server-side dry run, before concluding.
