# nrp-provisioner

Demand-driven HTCondor provisioner for OSG workloads on the NRP Nautilus Kubernetes cluster. One
provisioner pod per namespace reads idle jobs from whitelisted HTCondor schedds, groups them by
resource shape, and creates Kubernetes Jobs whose pods run the OSPool pilot (one static slot per pod).

This README covers the provisioner, its deployment and everything learned operating it. The worker
image has its own: [wn/README.md](wn/README.md). Facts were checked against the live system in
October 2026 unless marked as inference.

- [Layout](#layout)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Namespaces](#namespaces)
- [Deploying](#deploying)
- [Building, testing and rolling out](#building-testing-and-rolling-out)
- [Authentication, and why queue reads fail](#authentication-and-why-queue-reads-fail)
- [Troubleshooting](#troubleshooting)
- [Background and pitfalls](#background-and-pitfalls)
- [Origin](#origin)

## Layout

```
provisioner/          Provisioner image -> hub.opensciencegrid.org/ashtongraves/nrp-provisioner
  prp_provisioner/    Python library. provisioner_htcondor.py is the only file that touches the
                      HTCondor bindings, provisioner_k8s.py the only one that builds Job bodies.
  tests/              Unit tests
wn/                   Worker image -> hub.opensciencegrid.org/ashtongraves/nrp-provisioner-wn
k8s/
  rbac/               Service accounts and roles for the provisioner and the worker pods
  osg-*/              Per namespace: deployment, osg_provisioner.conf (its live ConfigMap), PVCs
tests/
  image/              Checks run against a built image
  live/               Read-only checks against the real pool and cluster
CLAUDE.md             Short brief for AI agents
```

The manifests and configs under `k8s/` match what was deployed on 2026-10-21.

## How it works

```
 OSPool access points (schedds)            OSPool central managers (cm-1/cm-2.ospool.osg-htc.org)
 ap40, ap41, ap23, JLab, IceCube, PATh…    collector  = directory of ads (schedds, submitters, slots)
   hold the actual job queues              negotiator = matches jobs to slots each cycle
          │  ▲                                       ▲
 provisioner reads idle jobs                         │ slots advertise themselves
 (QUERY_JOB_ADS, anonymous)                          │
          ▼                                          │
 nrp-provisioner (one pod per namespace) ──creates──▶ k8s Jobs ──▶ worker pods (OSPool pilot)
 groups idle jobs by shape, sizes pods                            one static slot per pod
```

- **There is no central OSPool job queue.** Jobs exist only in each schedd's queue. The collector
  holds a summary ad per schedd and a **submitter ad per user per schedd** with idle and running
  *counts*, but never the job ads. The negotiator pulls resource requests from schedds during each
  cycle and keeps nothing you can query. So the provisioner must be able to read each schedd it
  serves; collector access alone is never enough.
- **Worker pods join the pool** with the pool token. Any schedd in the pool can then claim them through
  normal matchmaking. Schedds don't need to trust the provisioner, and the provisioner doesn't need
  anything from a schedd to *run* jobs, only to *see* them. Jobs from schedds it can't read still land
  on its pods when the shapes match.

### Each loop (about every 120 s)

1. Query idle jobs from the whitelisted schedds, filtered by `additional_requirements` and the
   `apptainer` setting.
2. Query the collector for its own slots (`K8SProvisionerName`, `K8SNamespace`, `K8SDomain`), and
   list its pods.
3. Group jobs and pods by shape: `CPUs;Memory;Disk;DiskVolumes;GPUs;GPUTypes;Labels`. Job memory is
   rounded up to 1 GB and disk to 4 GB, so similar jobs share a group. Requests below 4096 MB or
   8000000 KB count as unset. The worker's `START` accepts memory within 1.5 GB and disk within 4.5 GB
   of the request, deliberately consistent with the rounding.
4. Per group, keep `1 + idle/4` unclaimed pods (slowed further above 20), at most
   `max_pods_per_cluster`. Stop adding when the group has `max_submit_pods_per_cluster` pods in total.
5. Submit a k8s Job for the missing pods.

Worker pods exit after 20 minutes unclaimed (`STARTD_NOCLAIM_SHUTDOWN=1200`) and the provisioner
replaces them. A group that can never match would burn up to 20 pods every 20 minutes, which is what
the next two mechanisms limit.

### Trial pods

A group with **no claimed pods right now** gets at most `trial_pods` (default 2) instead of up to 20.
Once one is claimed, the group gets the usual number on the next loop. It's stateless (it looks only
at the current pods), so it also limits a mixed group whose matching jobs have run out, and a group
that matches again recovers on its first claim. The cost: a large new group ramps up one loop after
its first claim.

### The back-off

The provisioner can't see *why* a group never matches (job `Requirements` typos, site pinning, annex
jobs, GPU models the node affinity doesn't include), so it judges by outcome. A pod that ends
`Succeeded` with a startd that was never claimed (`State` never `Claimed`, `JobStarts` never above 0)
counts as an unclaimed exit. Two in a row with no claim pause the group for 1 hour, then 2, 4, up to
8. Any claim clears it. Failed or deleted pods (preemption, node problems) don't count. The state is
in memory and starts fresh when the provisioner restarts.

We chose this over pre-matching jobs against a template slot because the template approach got GPU
groups wrong: a test slot's GPU isn't the GPU a real pod lands on, and one GPU group it predicted
"can't match" was in fact matched on real nodes. The back-off's blind spot is mixed groups: a shape
where *some* jobs match is never paused. Trial pods cover part of that.

## Configuration

Each namespace's `osg_provisioner.conf` is mounted from a ConfigMap **with `subPath`**, so changes need
a provisioner restart. Sections: `[DEFAULT]`, `[k8s]` (falls back to `[DEFAULT]` if missing) and
`[htcondor]`.

| Setting | Section | Default | Meaning |
|---|---|---|---|
| `apptainer` | `[DEFAULT]` | `yes` | Whether worker pods can run containers; see below |
| `schedd_whitelist_regexp` | `[htcondor]` | `.*` | Schedds to read |
| `additional_requirements` | `[htcondor]` / `[k8s]` | | Extra job constraint for the schedd query. In `[k8s]`/`[DEFAULT]` it's also added to the worker's `START` |
| `force_k8s_namespace_matching` | any | `yes` | Only jobs whose `RequestK8SNamespace` matches the namespace |
| `app_name`, `k8s_domain` | `[DEFAULT]` | | Identify this provisioner's pods and slots. Two provisioners with the same `app_name` in one namespace count each other's pods |
| `k8s_image` | `[k8s]` | | Worker image |
| `max_pods_per_cluster` | `[k8s]` | `20` | Most unclaimed pods per group |
| `max_submit_pods_per_cluster` | `[k8s]` | `1000` | No new pods once a group has this many in total |
| `trial_pods` | `[k8s]` | `2` | A group with no claimed pods gets at most this many (`0` disables) |
| `backoff_unclaimed_exits` | `[k8s]` | `2` | Pause a group after this many unclaimed exits in a row (`0` disables) |
| `backoff_time` | `[k8s]` | `3600` | First pause, in seconds; doubled each time |
| `backoff_max_time` | `[k8s]` | `28800` | Longest pause, in seconds |
| `k8s_job_ttl` | `[k8s]` | `86400` | Delete worker Jobs this long after they finish |
| `sleep_time` | `[DEFAULT]` | `120` | Seconds between loops |
| `envs_dict`, `labels_dict`, `annotations_dict`, `tolerations_list`, `node_affinity_dict`, `config_volumes_dict`, `pvc_volumes_dict`, `priority_class`, `service_account_name`, `use_large_limit` | `[k8s]` | | Passed into the worker pod spec |

### Apptainer, containers and user namespaces

**Unprivileged user namespaces are disabled on the Nautilus nodes on purpose** (a CVE response). It's a
standing decision, not a bug. Apptainer in the worker image has no setuid helper, so it can't run,
and container jobs can't run here: the OSPool `START` clause
(`isUndefined(TARGET.SingularityImage) || MY.SINGULARITY_START_CLAUSE`) rejects any job with a
`SingularityImage`, and many jobs' own `Requirements` ask for `HasSingularity`/`HasSIF` too.

`apptainer=yes|no` makes the provisioner, the worker validation and the pilot's image download agree.
The provisioner passes it to the workers as `PROVISIONER_APPTAINER`. All namespaces use `no`.

| | `apptainer=yes` (default) | `apptainer=no` |
|---|---|---|
| Jobs provisioned for | only jobs with a `SingularityImage` | only jobs without one |
| Worker apptainer check | runs; failure aborts the pilot | skipped |
| Default container image | downloaded as a `.sif` | not downloaded (`OSG_DEFAULT_CONTAINER_CVMFS=1`) |

Before the option existed, the config hard-coded `!isUndefined(SingularityImage)` while apptainer was
off, so it provisioned only for jobs that could never run, and nearly every pod idled out.

How much work there is without containers depends on the community:

| Namespace | Container-free share of matching idle jobs |
|---|---|
| `osg-opportunistic` | a few thousand of ~145k (October 2026) |
| `osg-icecube` | none (all of `ice3simusr`'s matching GPU jobs set `SingularityImage`) |
| `osg-ligo` | about 1 in 1,100 |
| `osg-nrao` | about half |

**CVMFS is needed either way.** It carries job software (`oasis`, community repos), the pilot's config
(`config-osg`) and the `HAS_CVMFS_*` attributes jobs match on. Only `singularity.opensciencegrid.org`
is container-specific. The provisioner adds the `cvmfs` PVC to every worker pod in code, not in the
deployment, so it looks unused. Don't delete it.

## Namespaces

A namespace is not a pool. Each has its own provisioner, config, accounting and quotas.

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
provisioner and the worker pods (see [Authentication](#authentication-and-why-queue-reads-fail)). Get
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

## Building, testing and rolling out

```bash
docker build -t hub.opensciencegrid.org/ashtongraves/nrp-provisioner:<tag> provisioner
docker build -t hub.opensciencegrid.org/ashtongraves/nrp-provisioner-wn:<tag> wn
```

The provisioner image is Rocky 10, OSG 25, HTCondor 25 with the `htcondor2` bindings, Python 3.12 and
a pinned `kubernetes` client. `nrp-provisioner:X` and `nrp-provisioner-wn:X` are independent images.

### Tests

CI (`.github/workflows/test.yml`) runs the unit and image tests on every PR.

**Unit tests** (`provisioner/tests/`): `cd provisioner && python3 -m unittest discover -s tests`.
Without the HTCondor bindings (e.g. on a laptop) the `htcondor2` tests are skipped; inside the image
nothing is.

| File | What it covers |
|---|---|
| `test_backoff.py` | the back-off state machine, trial pods, and the event loop skipping paused groups |
| `test_htcondor_ads.py` | job/startd ad conversion (rounding, "too small" means unset, expressions, stringified values) and job grouping |
| `test_schedd_query.py` | the schedd list (whitelist, identity filter, the location attributes `htcondor2.Schedd` needs), the query constraint (apptainer, namespace matching), failure handling, every repo config |
| `test_k8s_submit.py` | Job submission through the **real** `kubernetes` client (only the network send is intercepted), for every namespace config |
| `test_logging.py` | log file and stdout mirroring |
| `test_repo_files.py` | `k8s/`: configs parse and have the required settings, manifests parse, deployments reference volumes that exist |

**Image tests** (`tests/image/`), on every image before pushing a tag:

- `tests/image/provisioner.sh <image>` runs the unit tests in the image, checks the `provisioner`
  user's home has `.bash_profile` and that the setup scripts' environment reaches `su -l provisioner`,
  then starts the provisioner with a **fake service account** and requires it to reach the loop with
  no traceback and no deprecation warnings. This makes real read-only queries to the OSPool.
- `tests/image/wn.sh <image>`: see [wn/README.md](wn/README.md#tests).

**Live checks** (`tests/live/`, read-only, run by hand; usage at the top of each):

- `check_schedds.py <osg_provisioner.conf> [collector]`: can this image read each whitelisted schedd?
  Prints the authentication or certificate error for each failure, and how many idle jobs match. Run
  it in the image you're about to deploy. Works with old and new bindings, so you can compare.
- `dryrun_submit.py`: run in a provisioner pod. Submits one worker Job with `dry_run=All`, so the API
  server validates everything and creates nothing.

Writing tests: don't put real job ads or logs in fixtures (they carry user and project names); write
synthetic ads with the same attribute names and expression forms. Test the client path you actually
use. Make smoke tests get past the expected failure point: an "expected" error once hid a real bug.

### Rolling out

- Deployments pull with `imagePullPolicy: Always`, so a pushed tag changes what runs at the next
  restart. Push test builds to a tag nothing uses, and check first:
  ```bash
  kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.template.spec.containers[*].image}{"\n"}{end}' | grep nrp-provisioner
  ```
- Roll out one namespace at a time: `osg-icecube`/`osg-nrao` first (they launch little), then
  `osg-opportunistic`.
- Pods created in the first minutes after a restart are a burst (each group refilled at once), not
  churn. Churn is pods ending unclaimed after about 20 minutes.

## Authentication, and why queue reads fail

- **The pool token** is an OSPool IDTOKEN: issuer `flock.opensciencegrid.org`, HS256, scopes `READ`,
  `ADVERTISE_MASTER` and `ADVERTISE_STARTD`, no expiry. It's signed with the OSPool's **shared secret
  key**, so only daemons holding that key (the OSPool central managers) can verify it. An access point
  can't.
- **Reading a schedd's queue is therefore anonymous in practice.** The provisioner sets client
  authentication, integrity and encryption to `OPTIONAL` (`HTCONDOR_QUERY_INSECURE=yes`). Anonymous
  access normally goes over **SSL without a client certificate**: the client still verifies the
  *server's* certificate. A schedd with `ALLOW_READ = *` lets that through. So a stale CA bundle on
  our side looks exactly like "the site locked us out", and that has twice been the real cause.
- **Another OSPool `READ` token doesn't help;** the AP still can't verify it. If an AP truly requires
  authentication, the options are:
  1. the AP re-allows anonymous reads;
  2. the AP creates its own **named** signing key (not `POOL`) and issues a token with `-authz READ`
     from it, mounted next to the pool token. HTCondor picks the token whose issuer matches the server,
     which only works if the AP's `TRUST_DOMAIN` differs from the OSPool's;
  3. SSL with a client certificate the AP maps to a read-only identity.

## Troubleshooting

Read the provisioner log with `kubectl logs -f deploy/<deploy> -n <ns>` (recent lines; the log is
mirrored to stdout), or `/var/log/provisioner/logs/provisioner.log.YYYYMMDD` on the log PVC (full
history, one file per day). Each loop logs `Iteration started`, then one line per job group:
`Cluster '<shape>' n_jobs_idle … n_pods_unclaimed … min_pods …`, and `Submitted N pods as job …` when
it adds pods. The provisioner logs failures, never successful queries.

### `Failed to query HTCondor schedd '<name>'`

If **every** whitelisted schedd fails, the loop fails (`Failed to query HTCondor`) and nothing is
provisioned. Confirm from the provisioner pod; it shows which auth methods were tried and why they
failed:

```bash
kubectl exec -n <ns> deploy/<deploy> -- su -l provisioner -c '_CONDOR_TOOL_DEBUG="D_SECURITY:2" condor_q -debug -name <schedd> -pool cm-1.ospool.osg-htc.org -totals 2>&1 | grep -E "my_methods|server replied|AUTHENTICATE|certificate|issuer|err [0-9]+:|Total for"'
```

`-debug` is required; `_CONDOR_TOOL_DEBUG` alone prints nothing. `tests/live/check_schedds.py` does
this for every whitelisted schedd.

| Cause | What `-debug` shows | Fix |
|---|---|---|
| **Outdated CA bundle in the provisioner image** | `Error with certificate at depth: 0 … issuer = /C=US/O=Let's Encrypt/CN=YE1 … err 20: unable to get local issuer certificate` | Build on a newer OSG series. `osg-ca-certs` ≥ 1.144 (OSG 24/25) has Let's Encrypt's "Y" chains; OSG 23's last bundle (1.137) doesn't |
| **Old HTCondor client vs newer schedd** | `my_methods = 'SCITOKENS,FS'` … `required authentication … failed, so aborting command QUERY_JOB_ADS` | HTCondor 24/25 client: it reads the queue without starting an authentication handshake |
| **Schedd advertises a private address** | connection timeouts, ~20 s each | Unreachable from NRP. Harmless apart from slowing each loop |

**When did it start?** Daily log files sort by date, so binary-search them for the first failure.
Don't `grep` every file: the logs go back years on network storage, and a full scan takes many
minutes. This assumes the failures are continuous once they start:

```bash
cd /var/log/provisioner/logs; files=($(ls provisioner.log.* | sort)); s=<schedd>; has() { head -c 20M "$1" | LC_ALL=C grep -q -F "Failed to query HTCondor schedd '$s'"; }; lo=0; hi=$((${#files[@]}-1)); if ! has "${files[$hi]}"; then echo "not failing in the latest file"; else while [ $lo -lt $hi ]; do mid=$(((lo+hi)/2)); if has "${files[$mid]}"; then hi=$mid; else lo=$((mid+1)); fi; done; echo "first failing file: ${files[$lo]}"; fi
```

### `Failed to submit N pods`

Creating the worker Job failed, and the event loop doesn't log why. Reproduce it with a server-side
dry run (`tests/live/dryrun_submit.py` in a provisioner pod), which validates permissions, quotas and
admission webhooks without creating anything. Seen so far: the `kubernetes` client ≥ 37 validates
bodies client-side and rejected numeric quantities and a misplaced field (see
[Background](#background-and-pitfalls)).

### Provisioner exits at start: `ConfigException: Service host/port is not set`

The provisioner runs as the `provisioner` user via `su -l`, a clean login shell. The setup scripts
write the Kubernetes and HTCondor settings into `~/.bashrc`, which only `~/.bash_profile` sources.
EL10's `useradd` under amd64 emulation (Docker on an arm64 Mac) left the home empty, so the Dockerfile
copies `/etc/skel` itself. Check: `docker run --rm --entrypoint bash <image> -c 'ls -A /home/provisioner'`
must include `.bash_profile`.

### Pods start, idle about 20 minutes, exit unclaimed (churn)

Common reasons nothing claims them:

- **Container jobs while apptainer is unavailable.** Set `apptainer=no`.
- **Jobs aimed at an HTCondor annex** (`TargetAnnexName` set). These only match their owner's annex.
  Excluded in `osg-opportunistic` by `additional_requirements`.
- **Jobs whose own `Requirements` can't match here:** pinned to another site (`GLIDEIN_Site == "…"`),
  needing a GPU model the node affinity doesn't include, or typos.

Trial pods and the back-off limit the cost; look for `suspending provisioning` / `back-off cleared`
in the log. To see *why* a slot and a job don't match, evaluate the job's `Requirements` and the
slot's `Start` clause by clause against a real slot ad from the collector, resized to the group's
shape. Expect false positives on GPU-specific clauses.

### Pods stuck `Pending`: `Insufficient smarter-devices/fuse` / node affinity

Usually large shapes (many CPUs or GPUs) that fit few nodes, combined with the FUSE device request and
the GPU node affinity. They wait until a node frees up.

### Jobs with no status that never go away

Worker Jobs have `backoffLimit: 0` and are deleted `k8s_job_ttl` after they *finish*. A Job whose pods
were deleted while Pending can be left with no active, succeeded or failed count, so it never
finishes. List and delete those older than a day:

```bash
kubectl get jobs -n <ns> -o json | python3 -c 'import json,sys,datetime as d; n=d.datetime.now(d.timezone.utc); print("\n".join(j["metadata"]["name"] for j in json.load(sys.stdin)["items"] if not any(j["status"].get(k) for k in ("active","succeeded","failed")) and (n-d.datetime.fromisoformat(j["metadata"]["creationTimestamp"].replace("Z","+00:00"))).days>=1))' | xargs -r kubectl delete job -n <ns>
```

Worker pod problems (exit 143 after 5 minutes, `KeyError: 'ca'`, user namespace errors) are in
[wn/README.md](wn/README.md#troubleshooting).

## Background and pitfalls

### Kubernetes

- **The `kubernetes` client ≥ 37 validates request bodies client-side** (pydantic). Resource amounts
  must be strings, and unknown fields are rejected. The API server used to accept numbers and silently
  drop a `defaultMode` inside a secret volume item, which is why the worker token file has the default
  mode (0644). Test submission through the real client, never a fake API object.
- **The ConfigMap is mounted with `subPath`**, so config changes need a restart.

### HTCondor Python bindings

- **HTCondor 25 ships only `htcondor2`/`classad2`.** 24 ships both; 23 only the classic `htcondor`.
  The port meant `Schedd.xquery()` → `Schedd.query()` and `htcondor.AdTypes` → `htcondor2.AdType`.
- **`htcondor2.Schedd(ad)` reads `ad['CondorVersion']`.** Location ads need `MyAddress`, `AddressV1`,
  `CondorVersion`, `CondorPlatform`, `Name` and `Machine`. A narrower collector projection makes every
  schedd query raise `KeyError`.
- **`classad2` `eval()` stringifies the same as the classic module**, which the grouping depends on
  (verified on 1,000 real job ads).

### Measuring

- **NRP accounting counts reserved resources** (pod requests), not use. Idle pods show up as CPU- and
  GPU-hours. Use slot states in the collector (`Claimed`/`Busy`, `JobStarts`) and AP job history.
- **A collector snapshot only shows live slots.** "No slot has run a job" from a snapshot says nothing
  about yesterday. Check AP job history (`LastRemoteHost` matching `osg-direct`).
- **Hold reasons on an AP may predate the change you're testing.** Group them by hold date and owner,
  and compare with other sites first.

### Building on an arm64 Mac

Images build as amd64 under emulation: slow, and sometimes different from native (the `useradd`
case). `--pull` forces a full rebuild. macOS has no `timeout` command. A file in a mounted script
directory can shadow stdlib modules (a stray `ssl.py` broke `import urllib3`).

### Incident history

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
