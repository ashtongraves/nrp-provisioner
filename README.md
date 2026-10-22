# nrp-provisioner

HTCondor provisioner for OSG workloads on the NRP Nautilus cluster. The provisioner
watches HTCondor schedds for idle jobs and submits Kubernetes Jobs whose pods run
OSPool worker nodes (glideins) to serve them.

## Layout

```
provisioner/          Provisioner image -> hub.opensciencegrid.org/ashtongraves/nrp-provisioner
  prp_provisioner/    Python library (schedd queries, job clustering, k8s Job submission)
wn/                   Worker-node image -> hub.opensciencegrid.org/ashtongraves/nrp-provisioner-wn
                      (built on hub.osg-htc.org/osg-htc/ospool-ep; kept identical to
                      osg-htc/nrp-ospool-ep in github.com/opensciencegrid/images)
k8s/
  rbac/               Service accounts and roles for the provisioner and the worker nodes
  osg-opportunistic/  Per-namespace deployment, provisioner config and PVCs
  osg-icecube/
  osg-ligo/
  osg-nrao/
docs/
  knowledge.md        How the system works and what was learned operating it (start here)
  troubleshooting.md  Symptoms seen in production, their causes and fixes
  logic.txt, resource_states.txt  Original design notes
CLAUDE.md             Short brief for AI agents working on this repo
```

The manifests and configs under `k8s/` match what was deployed on the cluster on 2026-10-21.

The worker image's base is `ospool-ep:${BASE_OSG_SERIES}-${BASE_YUM_REPO}` (default `25-release`).
To build on a specific dated base instead, pass it in the repo argument, e.g.
`docker build --build-arg BASE_YUM_REPO=release-20260726-0746 wn`.

## Apptainer toggle

Apptainer needs unprivileged user namespaces on the nodes. Set `apptainer` in the
`[DEFAULT]` section of `osg_provisioner.conf` to match the cluster:

| | `apptainer=yes` (default) | `apptainer=no` |
|---|---|---|
| Jobs provisioned for | jobs with a `SingularityImage` | jobs without a `SingularityImage` |
| WN apptainer/singularity validation | runs; the pilot aborts if it fails | skipped |
| Default container image | downloaded as a `.sif` | not downloaded (`OSG_DEFAULT_CONTAINER_CVMFS=1`) |

The provisioner passes the value to the worker pods as `PROVISIONER_APPTAINER`. Restart the
provisioner after changing it, since the config file is mounted with `subPath`.

## Provisioning back-off

The provisioner groups idle jobs by resource shape and keeps pods waiting for each group. Some
jobs can never match these pods (for example jobs pinned to another site, or aimed at an HTCondor
annex), so their pods idle out and get replaced. To limit that, a group whose pods keep exiting
without ever being claimed is suspended for a while. Before that, a group only gets a couple of
trial pods until one of them is claimed; then it gets the usual number.

| Setting (`[k8s]` section) | Default | Meaning |
|---|---|---|
| `trial_pods` | `2` | A group with no claimed pods gets at most this many pods (`0` disables) |
| `backoff_unclaimed_exits` | `2` | Suspend a group after this many pods exit unclaimed with no claim in between (`0` disables) |
| `backoff_time` | `3600` | First suspension, in seconds; doubled each time it happens again |
| `backoff_max_time` | `28800` | Longest suspension, in seconds |

Only pods that end `Succeeded` with a startd that was never claimed count. Failed or deleted pods
(preemption, node problems) don't. Any claim in a group clears its back-off. Groups are by shape
only, so a group mixing unmatchable jobs with jobs that do match is never suspended. The state
lives in memory and starts fresh when the provisioner restarts.

Tests: unit tests in `provisioner/tests/`, image and live checks in `tests/`; see [tests/README.md](tests/README.md). CI runs them on every PR.

## Origin

Consolidated from the parts of two repositories that are actually deployed, with git
history preserved:

| Path | Source |
|---|---|
| `provisioner/` | [sfiligoi/prp-osg-pool](https://github.com/sfiligoi/prp-osg-pool) `provisioner_direct_2202`: `provisioner/image/` |
| `k8s/osg-*/` | [sfiligoi/prp-osg-pool](https://github.com/sfiligoi/prp-osg-pool) `provisioner_direct_2202`: `provisioner/k8s_*/` |
| `provisioner/prp_provisioner/` | [ashtongraves/prp-htcondor-portal](https://github.com/ashtongraves/prp-htcondor-portal) `osg_pool_base`: `provisioner/python/prp_provisioner/` |
| `k8s/rbac/`, `docs/` | [ashtongraves/prp-htcondor-portal](https://github.com/ashtongraves/prp-htcondor-portal) `osg_pool_base`: `provisioner/k8s/`, `provisioner/docs/` |
| `wn/` | [ashtongraves/prp-htcondor-portal](https://github.com/ashtongraves/prp-htcondor-portal) `ospool_pool_base_24`: `wn-osgvo/image/` |

The two source repositories carry different licenses; both are in `LICENSES/`.
