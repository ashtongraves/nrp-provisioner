# How this system works, and what we learned the hard way

Background for anyone (human or AI) working on the provisioner. It covers the parts of HTCondor, the
OSPool, the OSPool pilot image and Kubernetes that the provisioner depends on, the facts we
established about this deployment, and the reasoning behind decisions. See
[troubleshooting.md](troubleshooting.md) for symptom-by-symptom diagnosis.

Facts here were verified against the live system in October 2026 unless marked as inference.

## The moving parts

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
  serves, and collector access alone is never enough to provision.
- **Worker pods join the OSPool** with the pool token. Any OSPool schedd can then claim them through
  normal matchmaking. Schedds don't need to trust the provisioner, and the provisioner doesn't need
  anything from a schedd to *run* jobs, only to *see* them. Jobs from schedds the provisioner can't
  read still land on its pods when the shapes match.
- **A namespace is not a pool.** Each namespace has its own provisioner, config, accounting and
  quotas. Three of the four join the OSPool; `osg-ligo` joins the IGWN pool via `POOL=condor.igwn.org`.
  A separate namespace is also needed because two provisioners with the same `app_name` in one
  namespace would count each other's pods.

## How the provisioner decides

- **Each loop (about 120 s):** query idle jobs, filtered by `additional_requirements` and the
  `apptainer` setting. Query the collector for the provisioner's own slots (label `k8s-app=<app_name>`,
  `K8SNamespace`, `K8SDomain`). List its pods. Group both by shape:
  `CPUs;Memory;Disk;DiskVolumes;GPUs;GPUTypes;Labels`.
- **Rounding:** job memory is rounded up to 1 GB and disk to 4 GB, so similar jobs share a group.
  Requests below 4096 MB or 8000000 KB are treated as unset. The worker's `PROVISIONING_START` accepts
  `Memory >= request` within 1.5 GB of headroom, and disk within 4.5 GB. The two are deliberately
  consistent.
- **Pod count:** per group it keeps `min(max_pods_per_cluster=20, 1 + idle/4)` unclaimed pods (slowed
  further above 20), capped by `max_submit_pods_per_cluster` in total.
- **Idle-outs:** worker pods exit after 20 minutes unclaimed (`STARTD_NOCLAIM_SHUTDOWN=1200`) and the
  provisioner replaces them. A group that can never match therefore burns up to 20 pods per 20 minutes.
- **What reaches the worker:** the provisioner passes `additional_requirements` (only when it's in the
  `[k8s]`/`[DEFAULT]` section) into the worker's `START`. It also passes `PROVISIONER_APPTAINER`, and
  `OSG_DEFAULT_CONTAINER_CVMFS=1` when `apptainer=no`.

### The back-off

The provisioner can't see *why* a group never matches (job `Requirements` typos, site pinning, annex
jobs, GPU models the node affinity doesn't include). So it judges by outcome. A pod that ends
`Succeeded` with a startd that was never claimed (`State` never `Claimed` and `JobStarts` never above
0) counts as an unclaimed exit. Two in a row, with no claim, pause the group for 1 hour, then 2, 4, up
to 8; any claim clears it.

We chose this over pre-matching jobs against a template slot because the template approach got GPU
groups wrong: a test slot's GPU isn't the GPU a real pod lands on. One GPU group the template
predicted "can't match" was in fact matched on real nodes. The back-off's blind spot is mixed groups:
a shape where *some* jobs match is never paused.

Trial pods soften the cost before the back-off kicks in: a group with no claimed pods right now gets
at most `trial_pods` (default 2) instead of up to 20. Once one is claimed, the group gets the normal
number. It's stateless (it looks only at the current pods), so it also limits a mixed group whose
matching jobs have run out, and a group that matches again recovers on the first claim. The cost is a
slower start for a large new group: it ramps up one provisioner loop after its first claim.

## Containers, apptainer and user namespaces

- **Unprivileged user namespaces are disabled on Nautilus nodes on purpose** (a CVE response). It's a
  standing decision, not a bug. Apptainer in the worker image has no setuid helper, so it can't run,
  and validation fails with `maximum number of user namespaces exceeded` (ENOSPC). Slots correctly
  advertise `HAS_SINGULARITY=false`.
- **Container jobs then can't run here.** The OSPool `START` clause
  (`isUndefined(TARGET.SingularityImage) || MY.SINGULARITY_START_CLAUSE`) rejects any job with a
  `SingularityImage`, and many jobs' own `Requirements` also ask for `HasSingularity`/`HasSIF`.
- **`apptainer=yes|no`** (in `[DEFAULT]`) makes the provisioner, the worker validation and the pilot's
  image download agree:

  | | `apptainer=yes` | `apptainer=no` |
  |---|---|---|
  | Jobs it provisions for | only jobs with a `SingularityImage` | only jobs without one |
  | Worker apptainer check | runs; failure is fatal | skipped |
  | Default `.sif` download | yes | no (`OSG_DEFAULT_CONTAINER_CVMFS=1`) |

  Before the option existed, the config hard-coded `!isUndefined(SingularityImage)` while apptainer
  was off. That provisioned only for jobs that could never run, and nearly every pod idled out.
- **How much work there is without containers depends on the community:**

  | Namespace | Container-free share of matching idle jobs |
  |---|---|
  | `osg-opportunistic` | a few thousand of ~145k (October 2026) |
  | `osg-icecube` | none (all of `ice3simusr`'s matching GPU jobs set `SingularityImage`) |
  | `osg-ligo` | about 1 in 1,100 |
  | `osg-nrao` | about half |

- **CVMFS is needed either way.** It carries job software (`oasis`, community repos), the pilot's
  config (`config-osg`), and the `HAS_CVMFS_*` attributes jobs match on. Only
  `singularity.opensciencegrid.org` is container-specific. The provisioner adds the `cvmfs` PVC to
  every worker pod from code.

## Authentication, and why queue reads fail

- **The pool token** (`prp-htcondor-wn-secret`) is an OSPool IDTOKEN: issuer
  `flock.opensciencegrid.org`, HS256, scopes `READ`, `ADVERTISE_MASTER` and `ADVERTISE_STARTD`, and
  no expiry. It's signed with the OSPool's **shared secret key**, so only daemons holding that key
  (the OSPool central managers) can verify it. An access point can't. The provisioner and the worker
  pods share it.
- **Reading a schedd's queue is therefore anonymous in practice.** The provisioner sets client
  authentication, integrity and encryption to `OPTIONAL`. Anonymous HTCondor access normally goes
  over **SSL without a client certificate**: the client still verifies the *server's* certificate. A
  schedd with `ALLOW_READ = *` lets that through. That means a stale CA bundle on our side looks
  exactly like "the site locked us out".
- **Getting another OSPool `READ` token doesn't help;** the AP still can't verify it. Real options,
  if an AP truly requires authentication:
  1. the AP re-allows anonymous reads;
  2. the AP creates its own **named** signing key (not `POOL`, which needs no pool password) and
     issues a token with `-authz READ` from it. We'd mount that next to the pool token; HTCondor
     picks the token whose issuer matches the server, which only works if the AP's `TRUST_DOMAIN`
     differs from the OSPool's;
  3. SSL with a client certificate the AP maps to a read-only identity.
- **The pilot's remote-syslog step** asks the OSG registry for a certificate using the pool token, and
  gets `401`: that endpoint wants a registry-issued token. The `KeyError: 'ca'` that follows is an
  upstream bug (no exit after the error message). It's harmless and only affects log forwarding.

## The OSPool pilot (worker image)

The worker image is built on OSG's `ospool-ep`. Behaviour that matters:

- **`/etc/osg/image-init.d/*.sh` scripts are *sourced*** by `supervisord_startup.sh`. An `exit` in one
  of them ends the whole startup, so a skip has to be an `if`/`else`, never `exit 0`.
- **`entrypoint.sh` runs `check_master.sh 300`**, which kills the pilot if `condor_master` hasn't
  started within 5 minutes. A slow default-image download (`htc__rocky__9.sif`, about 1.4 GB, via
  Pelican) tripped it on some Missouri, Great Plains and UNL nodes. Most nodes fetch it in 5–35 s.
  With `apptainer=yes` this risk still exists.
- **Slots are static, one per pod**, sized from `NUM_CPUS`/`MEMORY`/`DISK`/`NUM_GPUS`.
- **Pods ending with exit 143** are SIGTERM: preemption at the `opportunistic` priority class, or the
  watchdog above. A watchdog kill always comes at about 5 minutes.

## Kubernetes specifics

- **`kubernetes` client ≥ 37 validates request bodies client-side** (pydantic). Resource amounts must
  be strings, and unknown fields are rejected. The API server used to accept numbers and silently drop
  `defaultMode` inside a secret volume item. That's why the worker token file has the default mode
  (0644) and not the 0400 the old code intended. Test submission through the real client
  (`tests/test_k8s_submit.py`), never a fake API object.
- **The provisioner runs as the `provisioner` user via `su -l`**, a clean login shell. Kubernetes and
  HTCondor settings reach it only through `~/.bashrc`, which only `~/.bash_profile` sources. EL10's
  `useradd` under amd64 emulation (Docker on an arm64 Mac) created an empty home with no
  `.bash_profile`, so the Dockerfile copies `/etc/skel` itself.
- **Worker Jobs have `backoffLimit: 0`** and are deleted `k8s_job_ttl` after finishing. A Job whose
  pods were deleted while Pending can be stranded forever, with no active, succeeded or failed count.
- **All deployments pull with `imagePullPolicy: Always`**, so a pushed tag changes what runs at the
  next restart.
- **The ConfigMap is mounted with `subPath`**, so config changes need a provisioner restart.

## HTCondor Python bindings

- **HTCondor 25 ships only `htcondor2`/`classad2`.** HTCondor 24 ships both; 23 only the classic
  `htcondor`. The port meant: `Schedd.xquery()` → `Schedd.query()`, and `htcondor.AdTypes` →
  `htcondor2.AdType`.
- **`htcondor2.Schedd(ad)` reads `ad['CondorVersion']`.** Location ads need `MyAddress`, `AddressV1`,
  `CondorVersion`, `CondorPlatform`, `Name` and `Machine`. A narrower collector projection makes every
  schedd query raise `KeyError`.
- **`classad2` `eval()` stringifies the same as the classic module**, which the grouping code depends
  on. We verified this on 1,000 real job ads (identical output).
- **`condor_q -debug` is needed** for `_CONDOR_TOOL_DEBUG` output to show; the environment variable
  alone prints nothing.

## Namespaces

| Namespace | Deployment / label | Pool | Serves | State (Oct 2026) |
|---|---|---|---|---|
| `osg-opportunistic` | `direct-provisioner` / `direct-provisioner` | OSPool | general OSPool APs (`ap*.osg-htc.org`, UChicago, JLab, AMNH, `login*.osgconnect.net`) | main workload; hundreds of claimed slots |
| `osg-icecube` | `direct-provisioner-icecube` / `direct-provisioner` | OSPool (was IceCube's own `glidein-cm.icecube.wisc.edu` until at least late 2024; that host no longer answers) | `ice3simusr` 1-GPU `IceCube` jobs from `grid-submitter.icecube.wisc.edu` | idle while `apptainer=no` (all matching jobs use containers) |
| `osg-nrao` | `nrao-provisioner` / `nrao-provisioner` | OSPool | project `NRAO_2022_Bhatnagar` GPU jobs, mostly from PATh (`ap1.facility.path-cc.io`) | bursty; jobs have mostly run at PATh EPs, not here |
| `osg-ligo` | `direct-provisioner-ligo` / `direct-provisioner` | IGWN (`CONDOR_HOST=condor.igwn.org`, `POOL` set in `envs_dict`) | LIGO GPU jobs | 0 replicas: almost all LIGO work needs containers |

Per-namespace quirks:

- **`osg-opportunistic`** excludes annex jobs (`isUndefined(TargetAnnexName)`). AMNH's
  `huxley-osgsub-001` advertises a private address and always fails (it adds about 20 s per loop).
  Most claimed work comes from a handful of `ap40`/`ap41` users at a time.
- **`osg-icecube`** logs to a PVC named `nrp-provisioner-nrpnb-log-pvc`, a leftover name that's still
  in use. The whitelist changed from `sub-.\.icecube…` to `.*sub.*\.icecube…` in May 2026, when
  `grid-submitter` replaced the old submit hosts.
- **`osg-ligo`** mounts `wn-glidein-config` into the workers (`40_glidein.config`, setting
  `POOL_NAME = "IGWN"`). Its namespace also hosts an unrelated Flux-managed SciTokens issuer.
- **All namespaces** share the SDSC-PRP identity in the pool (`GLIDEIN_Site=SDSC-PRP`).

## Incident history

| When | Symptom | Root cause | Fix |
|---|---|---|---|
| 2025-09-16 → 2026-10-21 | `osg-opportunistic` couldn't read JLab's `scosg220x` queues (~116k idle jobs invisible) | HTCondor 23 client vs JLab's newer schedd: the old client concluded auth was *required* and gave up after SciTokens and FS. A 24/25 client reads anonymously with no handshake. (Exact server setting not confirmed.) | Provisioner on a newer HTCondor |
| 2026-06-11 09:32 → 2026-10-21 | `osg-icecube` failed every loop on `grid-submitter`; 0 GPU-hours | `grid-submitter`'s certificate moved to Let's Encrypt `YE1`; the OSG 23 image's CA bundle lacked it, so the SSL verification failed (`err 20`) | OSG 24+ `osg-ca-certs` (≥ 1.144) |
| until 2026-10-20 | ~464 worker pods an hour, almost none claimed | Config targeted only container jobs while apptainer was off | `apptainer=no` option |
| 2026-10-20 | Some pods killed at exactly 5 min | Default `.sif` download stalling on some nodes vs the 5-minute watchdog | `OSG_DEFAULT_CONTAINER_CVMFS=1` when `apptainer=no` |
| 2026-10-20 | Groups that can never match (typo, site-pinned, annex) cycling 20 pods each | No feedback from outcomes | Annex exclusion and the back-off |
| 2026-10-21 | EL10 test image exited at start | Empty home dir under emulated `useradd` | Copy `/etc/skel` |
| 2026-10-21 | EL10 test image: `Failed to submit N pods` | `kubernetes` 37 client-side validation | Quantity strings, drop the invalid field |

Dates of when each started come from binary-searching the provisioner's daily log files for the first
failure. The provisioner logs only failures, never successful queries.

## Measuring things (and traps)

- **NRP accounting counts reserved resources** (pod requests), not use. Idle pods show up as CPU- and
  GPU-hours, so a site running nothing can look busy. Use slot states in the collector
  (`Claimed`/`Busy`, `JobStarts`) and job history on the APs instead.
- **A collector snapshot only shows live slots.** "No slot has run a job" from a snapshot says nothing
  about yesterday. Check AP job history (`LastRemoteHost` matching `osg-direct`).
- **Hold reasons on an AP may predate any change you're testing.** Group them by hold date and owner,
  and compare with other sites before blaming this one.
- **Pods created in the first minutes after a restart are a burst** (each group refilled at once),
  not churn. Churn is pods ending unclaimed after about 20 minutes.
- **A test passing for the wrong reason:** an expected-looking error (`Service host/port is not set`)
  hid the `su -l` bug, and a fake Kubernetes API hid the client validation. Make smoke tests get past
  the expected failure point.
- **Local tooling on the maintainer's Mac:** no `timeout` command. Docker builds are amd64 under
  emulation (slow, and sometimes behaving differently from native). A file in a mounted script
  directory can shadow stdlib modules (a stray `ssl.py` broke `import urllib3`).
