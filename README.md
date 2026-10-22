# nrp-provisioner

Deployment of the HTCondor provisioner for OSG workloads on the NRP Nautilus Kubernetes cluster: one
provisioner per namespace, launching worker pods that join the OSPool (or IGWN) to run its idle jobs.

- **How the provisioner works, its configuration, troubleshooting and tests:**
  [provisioner/README.md](provisioner/README.md). Start there.
- **The worker image:** [wn/README.md](wn/README.md).

Both images are built by OSG from [opensciencegrid/images](https://github.com/opensciencegrid/images)
(`osg-htc/nrp-provisioner` and `osg-htc/nrp-ospool-ep`). `provisioner/` and `wn/` here are kept
identical to those directories; **make changes upstream first**, then copy them here. This README
covers the deployment: namespaces, installing, rolling out, and what happened in production.

- [Layout](#layout)
- [Namespaces](#namespaces)
- [Deploying](#deploying)
- [Rolling out](#rolling-out)
- [Checks](#checks)
- [Measuring](#measuring)
- [Incident history](#incident-history)
- [Origin](#origin)

## Layout

```
provisioner/          Source of hub.opensciencegrid.org/osg-htc/nrp-provisioner (= osg-htc/nrp-provisioner
                      in opensciencegrid/images), with its README and tests
wn/                   Source of hub.opensciencegrid.org/osg-htc/nrp-ospool-ep (= osg-htc/nrp-ospool-ep)
k8s/
  rbac/               Service accounts and roles for the provisioner and the worker pods
  osg-*/              Per namespace: deployment, osg_provisioner.conf (its live ConfigMap), PVCs
tests/
  repo/               Consistency checks for k8s/
  image/wn.sh         Worker image test
  live/               Read-only checks against the real pool and cluster
CLAUDE.md             Short brief for AI agents
```

The manifests and configs under `k8s/` match what was deployed on 2026-10-21.

## Namespaces

A namespace is not a pool. All of them run with `apptainer=no` (see the
[provisioner README](provisioner/README.md#apptainer-containers-and-user-namespaces)). Each has its own provisioner, config, accounting and quotas.

| Namespace | Deployment / label | Pool | Serves | State (Oct 2026) |
|---|---|---|---|---|
| `osg-opportunistic` | `direct-provisioner` / `direct-provisioner` | OSPool | general OSPool APs (`ap*.osg-htc.org`, UChicago, JLab, AMNH, `login*.osgconnect.net`) | main workload; hundreds of claimed slots |
| `osg-icecube` | `direct-provisioner-icecube` / `direct-provisioner` | OSPool (was IceCube's own `glidein-cm.icecube.wisc.edu` until at least late 2024; that host no longer answers) | `ice3simusr` 1-GPU `IceCube` jobs from `grid-submitter.icecube.wisc.edu` | idle while `apptainer=no` (all matching jobs use containers) |
| `osg-nrao` | `nrao-provisioner` / `nrao-provisioner` | OSPool | project `NRAO_2022_Bhatnagar` GPU jobs, mostly from PATh (`ap1.facility.path-cc.io`) | bursty; jobs have mostly run at PATh EPs, not here |
| `osg-ligo` | `direct-provisioner-ligo` / `direct-provisioner` | IGWN (`CONDOR_HOST=condor.igwn.org`, `POOL` set in `envs_dict`) | LIGO GPU jobs | 0 replicas: almost all LIGO work needs containers |

- **`osg-opportunistic`** excludes annex jobs (`isUndefined(TargetAnnexName)`). AMNH's
  `huxley-osgsub-001` advertises a private address and always fails (about 20 s per loop). Most claimed
  work comes from a handful of `ap40`/`ap41` users at a time.
- **`osg-icecube`** logs to a PVC named `nrp-provisioner-nrpnb-log-pvc`, a leftover name that's still in
  use. The whitelist changed from `sub-.\.icecube…` to `.*sub.*\.icecube…` in May 2026, when
  `grid-submitter` replaced the old submit hosts.
- **`osg-ligo`** mounts `wn-glidein-config` into the workers (`40_glidein.config`, setting
  `POOL_NAME = "IGWN"`). Its namespace also hosts an unrelated Flux-managed SciTokens issuer.
- **All namespaces** share the SDSC-PRP identity in the pool (`GLIDEIN_Site=SDSC-PRP`).

## Deploying

### Once per namespace: accounts, node access and the token

```bash
kubectl apply -n <ns> -f k8s/rbac/prp-htcondor-provisioner-account.yaml   # provisioner: pods, jobs, deployments, secrets, configmaps
kubectl apply -n <ns> -f k8s/rbac/prp-htcondor-wn-account.yaml            # worker pods (prp-htcondor-wn-ro): read pods
```

**Node access.** The worker image looks up its node's `nautilus.io/OSGInstitutionID` label. Nodes are
cluster-scoped, so a namespaced Role can't grant this. It comes from the ClusterRole and
ClusterRoleBinding `view-nodes`, which NRP cluster admins manage. `k8s/rbac/view-nodes-clusterrole.yaml`
is a copy of the ClusterRole for reference. The binding is shared with other teams' service accounts,
so it's **not** in this repo: applying a copy would overwrite the live one. For a new namespace, ask
the cluster admins to add the subject `ServiceAccount prp-htcondor-wn-ro` in `<ns>`. Without it,
worker pods still run; they just don't advertise `OSG_INSTITUTION_ID`.

**The token.** `prp-htcondor-wn-secret` (key `prp-wn.token`) holds the pool IDTOKEN, shared by the
provisioner and the worker pods (see [Authentication](provisioner/README.md#authentication-and-why-queue-reads-fail)). Get
new ones from the OSPool. Tokens are never stored in this repo.

```bash
kubectl create secret generic prp-htcondor-wn-secret -n <ns> --from-file=prp-wn.token=<token file>
```

To check a token without exposing it, decode only its claims (the middle, base64url part); never
print or paste the signature.

### The provisioner

From `k8s/<ns>/`:

```bash
kubectl create configmap <configmap> -n <ns> --from-file=osg_provisioner.conf=osg_provisioner.conf
kubectl create -f <cvmfs file>
kubectl create -f pvc-provisioner-logs.yaml
kubectl create -f <deployment file>
```

| Namespace | ConfigMap | CVMFS file | Deployment file |
|---|---|---|---|
| `osg-opportunistic` | `direct-provisioner-config` | `cvmfs.yaml` | `provisioner-pod.yaml` |
| `osg-icecube` | `direct-provisioner-config` | `cvmfs.yaml` | `provisioner-icecube.yaml` |
| `osg-nrao` | `nrao-provisioner-config` | `cvmfs-nrao.yaml` | `provisioner-pod.yaml` |
| `osg-ligo` | `direct-provisioner-config` | `cvmfs.yaml` | `provisioner-ligo.yaml` |

`osg-ligo` also needs the worker config it mounts via `config_volumes_dict`:

```bash
kubectl create configmap wn-glidein-config -n osg-ligo --from-file=40_glidein.config=40_glidein.config
```

## Rolling out

- The ConfigMap is mounted with `subPath`, so config changes need a provisioner restart.
- Deployments pull with `imagePullPolicy: Always`, so a pushed tag changes what runs at the next
  restart. Push test builds to a tag nothing uses, and check first:
  ```bash
  kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.template.spec.containers[*].image}{"\n"}{end}' | grep nrp-provisioner
  ```
- Roll out one namespace at a time: `osg-icecube`/`osg-nrao` first (they launch little), then
  `osg-opportunistic`.
- Pods created in the first minutes after a restart are a burst (each group refilled at once), not
  churn. Churn is pods ending unclaimed after about 20 minutes.

## Checks

CI (`.github/workflows/test.yml`) runs on every PR: the provisioner's unit tests, `tests/repo/` (configs
parse and have the required settings, manifests parse, deployments reference volumes that exist), and
the image tests for both images built from this repo.

**Live checks** (`tests/live/`, read-only, run by hand; usage at the top of each):

- `check_schedds.py <osg_provisioner.conf> [collector]`: can this image read each whitelisted schedd?
  Prints the authentication or certificate error for each failure, and how many idle jobs match. Run
  it in the image you're about to deploy. Works with old and new bindings, so you can compare.
- `dryrun_submit.py`: run in a provisioner pod. Submits one worker Job with `dry_run=All`, so the API
  server validates everything and creates nothing.

## Measuring

- **NRP accounting counts reserved resources** (pod requests), not use. Idle pods show up as CPU- and
  GPU-hours. Use slot states in the collector (`Claimed`/`Busy`, `JobStarts`) and AP job history.
- **A collector snapshot only shows live slots.** "No slot has run a job" from a snapshot says nothing
  about yesterday. Check AP job history (`LastRemoteHost` matching `osg-direct`).
- **Hold reasons on an AP may predate the change you're testing.** Group them by hold date and owner,
  and compare with other sites first.

## Incident history

| When | Symptom | Root cause | Fix |
|---|---|---|---|
| 2025-09-16 → 2026-10-21 | `osg-opportunistic` couldn't read JLab's `scosg220x` queues (~116k idle jobs invisible) | HTCondor 23 client vs JLab's newer schedd: the old client concluded auth was *required*. (Exact server setting not confirmed.) | Newer HTCondor |
| 2026-06-11 → 2026-10-21 | `osg-icecube` failed every loop on `grid-submitter`; 0 GPU-hours | Its certificate moved to Let's Encrypt `YE1`, missing from the OSG 23 CA bundle | OSG 24+ `osg-ca-certs` |
| until 2026-10-20 | ~464 worker pods an hour, almost none claimed | Config targeted only container jobs while apptainer was off | `apptainer=no` |
| 2026-10-20 | Some pods killed at exactly 5 min | Default `.sif` download stalling vs the 5-minute watchdog | `OSG_DEFAULT_CONTAINER_CVMFS=1` |
| 2026-10-20 | Groups that can never match cycling 20 pods each | No feedback from outcomes | Annex exclusion, the back-off |
| 2026-10-21 | EL10 test image exited at start | Empty home dir under emulated `useradd` | Copy `/etc/skel` |
| 2026-10-21 | EL10 test image: `Failed to submit N pods` | `kubernetes` 37 client-side validation | Quantity strings, drop the invalid field |
| 2026-10-22 | ~20% of pods went to groups that never got a job | Up to 20 pods per group before the back-off | Trial pods |

## Origin

Consolidated from the parts of two repositories that are actually deployed, with git history
preserved:

| Path | Source |
|---|---|
| `provisioner/` | [sfiligoi/prp-osg-pool](https://github.com/sfiligoi/prp-osg-pool) `provisioner_direct_2202`: `provisioner/image/` |
| `k8s/osg-*/` | [sfiligoi/prp-osg-pool](https://github.com/sfiligoi/prp-osg-pool) `provisioner_direct_2202`: `provisioner/k8s_*/` |
| `provisioner/prp_provisioner/` | [ashtongraves/prp-htcondor-portal](https://github.com/ashtongraves/prp-htcondor-portal) `osg_pool_base`: `provisioner/python/prp_provisioner/` |
| `k8s/rbac/` | [ashtongraves/prp-htcondor-portal](https://github.com/ashtongraves/prp-htcondor-portal) `osg_pool_base`: `provisioner/k8s/` |
| `wn/` | [ashtongraves/prp-htcondor-portal](https://github.com/ashtongraves/prp-htcondor-portal) `ospool_pool_base_24`: `wn-osgvo/image/` |

The original design notes (`logic.txt`, `resource_states.txt`) are in the git history. The two source
repositories carry different licenses; both are in `LICENSES/`.
