# Troubleshooting

Symptoms seen in production, what caused them, how to confirm, and the fix. Commands assume
`kubectl` access to the namespace; `<ns>` and `<deploy>` are the namespace and provisioner
deployment (see [knowledge.md](knowledge.md#namespaces)).

Two ways to read the provisioner's log:

- `kubectl logs -f deploy/<deploy> -n <ns>`: recent lines (the log is mirrored to stdout).
- `/var/log/provisioner/logs/provisioner.log.YYYYMMDD` on the log PVC: the full history, one file per day.

The provisioner logs a loop every ~2 minutes: `Iteration started`, then one
`Cluster '<cpus>;<mem MB>;<disk KB>;<disk vols>;<gpus>;<gpu types>;<labels>' n_jobs_idle … n_pods_unclaimed …`
line per job group, and `Submitted N pods as job …` when it adds pods.

---

## `Failed to query HTCondor schedd '<name>'`

The provisioner reads each whitelisted schedd's queue **anonymously** (`HTCONDOR_QUERY_INSECURE=yes`
makes client authentication optional). It presents the OSPool pool token too, but an access point
can't verify that (it's signed with the OSPool's shared key), so in practice reads only work if the
schedd allows unauthenticated reads (`ALLOW_READ = *`) *and* the handshake that gets there succeeds.

If **every** whitelisted schedd fails, the whole loop fails (`Failed to query HTCondor`, then
`[ProvisionerEventLoop] Failed to query`) and nothing is provisioned.

**Confirm from the provisioner pod** (shows which auth methods were tried and why they failed):

```bash
kubectl exec -n <ns> deploy/<deploy> -- su -l provisioner -c '_CONDOR_TOOL_DEBUG="D_SECURITY:2" condor_q -debug -name <schedd> -pool cm-1.ospool.osg-htc.org -totals 2>&1 | grep -E "my_methods|server replied|AUTHENTICATE|certificate|issuer|err [0-9]+:|Total for"'
```

Causes seen so far:

| Cause | What `-debug` shows | Fix |
|---|---|---|
| **Outdated CA bundle in the provisioner image.** The client verifies the *server's* certificate even for anonymous reads. | `Error with certificate at depth: 0 … issuer = /C=US/O=Let's Encrypt/CN=YE1 … err 20: unable to get local issuer certificate` | Rebuild on a newer OSG series. `osg-ca-certs` ≥ 1.144 (OSG 24/25) has Let's Encrypt's "Y" chains (YE1–3, YR1–3, Root YE/YR); OSG 23's last bundle (1.137) doesn't. This was IceCube's `grid-submitter`, broken from 2026-06-11. |
| **Old HTCondor client vs newer schedd.** The 23.x client concluded authentication was required and gave up after SciTokens and FS. | `my_methods = 'SCITOKENS,FS'` … `required authentication … failed, so aborting command QUERY_JOB_ADS` | Newer client (HTCondor 24/25): it reads the queue without starting an authentication handshake. This was JLab's `scosg220x`, broken from about 2025-09-16. |
| **Schedd advertises a private address.** | connection timeouts, ~20s per attempt | Unreachable from NRP. Harmless apart from slowing each loop; remove it from the whitelist if it matters. (AMNH's `huxley-osgsub-001`.) |

Things that **don't** help:

- **Another OSPool `READ` token.** The pool token already has `condor:/READ`; the AP can't verify it.
- **Reading the OSPool collector instead.** The OSPool has no job queue: the collector only has a
  summary ad per schedd and a submitter ad per user (idle/running *counts*). The provisioner needs the
  job ads themselves (resource requests, owner, project, container image) to group jobs and size pods.

If an AP really does require authentication, the options are: the AP re-allows anonymous reads; or
the AP creates a **named** signing key of its own (not `POOL`) and issues a token with `-authz READ`
from it, mounted into the provisioner next to the pool token (HTCondor picks the token whose issuer
matches the server); or SSL with a client certificate the AP maps to a read-only identity.

**When did it start?** The provisioner doesn't log successful queries, only failures. Daily log files
sort by date, so binary-search them for the first failure (reads at most 20 MB per file, ~10 files):

```bash
cd /var/log/provisioner/logs; files=($(ls provisioner.log.* | sort)); s=<schedd>; has() { head -c 20M "$1" | LC_ALL=C grep -q -F "Failed to query HTCondor schedd '$s'"; }; lo=0; hi=$((${#files[@]}-1)); if ! has "${files[$hi]}"; then echo "not failing in the latest file"; else while [ $lo -lt $hi ]; do mid=$(((lo+hi)/2)); if has "${files[$mid]}"; then hi=$mid; else lo=$((mid+1)); fi; done; echo "first failing file: ${files[$lo]}"; fi
```

This assumes the failures are continuous once they start. Don't scan every file with a plain `grep`:
the logs go back years and live on network storage, so a full scan takes many minutes.

---

## `Failed to submit N pods`

The provisioner could query HTCondor and list pods, but creating the worker Job failed. The event loop
catches the exception without logging the reason, so reproduce it with a **server-side dry run**, which
validates fully (permissions, quotas, admission webhooks) without creating anything:

```bash
kubectl exec -i -n <ns> deploy/<deploy> -- su -l provisioner -c 'cd ~ && python3 -' <<'EOF'
import configparser, traceback, kubernetes
import prp_provisioner.provisioner_k8s as pk
f = configparser.ConfigParser(); f.read(("pod.conf", "osg_provisioner.conf"))
k = pk.ProvisionerK8SConfig(f["DEFAULT"]["namespace"])
k.additional_labels["osg-provisioner"] = "wn"; k.app_name = "osg-wn"
k.parse(f["k8s"] if "k8s" in f else f["DEFAULT"]); k.base_pvc_volumes["cvmfs"] = "#/cvmfs"
o = pk.ProvisionerK8S(k); o.authenticate()
orig = kubernetes.client.BatchV1Api.create_namespaced_job
def dry(self, *a, **kw):
    kw["dry_run"] = "All"; return orig(self, *a, **kw)
kubernetes.client.BatchV1Api.create_namespaced_job = dry
try:
    print("dry-run submit OK:", o.submit({"CPUs": "2", "GPUs": "1", "Memory": "32768", "Disk": "8000000"}, 1))
except kubernetes.client.exceptions.ApiException as e:
    print("SERVER REJECTED: HTTP", e.status, e.reason); print(e.body)
except Exception:
    print("CLIENT-SIDE ERROR:"); traceback.print_exc()
EOF
```

Seen so far: **`kubernetes` client ≥ 37 validates bodies client-side** (pydantic) and rejected numeric
`cpu`/`nvidia.com/gpu` amounts and a `defaultMode` inside a secret volume item. Quantities are now
formatted as strings, and the misplaced field is gone (the API server always dropped it).
`tests/test_k8s_submit.py` runs `submit()` through the real client for every namespace config, so a
client upgrade that breaks this fails the tests. Don't test submission against a fake API object: that
skips the client-side validation.

---

## Provisioner exits at start: `ConfigException: Service host/port is not set`

The provisioner runs as the `provisioner` user via `su -l`, which starts a clean login shell. The
setup scripts write `KUBERNETES_SERVICE_HOST/PORT`, `K8S_NAMESPACE` and the HTCondor insecure-query
settings into `~/.bashrc`, which only gets read through `~/.bash_profile`.

Seen when building the Rocky 10 image under amd64 emulation (Docker on an arm64 Mac): EL10's `useradd`
left the home directory empty. A native build copies `/etc/skel` normally. The Dockerfile now copies
`/etc/skel` explicitly. Check in an image:

```bash
docker run --rm --entrypoint bash <image> -c 'ls -A /home/provisioner'   # must include .bash_profile
```

To smoke-test startup outside the cluster, give the container a fake service account (a token file, a
CA file and a namespace file mounted at `/var/run/secrets/kubernetes.io/serviceaccount`, plus
`KUBERNETES_SERVICE_HOST/PORT`). It should then run the loop, with only the Kubernetes calls failing.
Without the fake service account it stops at exactly this error, which hides the bug.

---

## Pods start, idle about 20 minutes, exit unclaimed (churn)

The provisioner groups idle jobs by shape and keeps up to `min(20, 1 + idle/4)` waiting pods per group.
If nothing claims them, they exit after 20 minutes (`STARTD_NOCLAIM_SHUTDOWN=1200`) and get replaced.
Common reasons nothing claims them:

- **Container jobs while apptainer is unavailable.** The slots advertise `HAS_SINGULARITY=false`, and
  the OSPool `START` clause rejects jobs with a `SingularityImage`. Set `apptainer=no` (see the README).
- **Jobs aimed at an HTCondor annex** (`TargetAnnexName` set). These only match their owner's annex.
  Excluded in `osg-opportunistic` with `&&isUndefined(TargetAnnexName)` in `additional_requirements`.
- **Jobs whose own `Requirements` can't match here**: pinned to another site
  (`GLIDEIN_Site == "…"`), needing a GPU model or capability the node affinity doesn't include, or
  plain typos in the job's requirements.

The [back-off](../README.md#provisioning-back-off) pauses groups whose pods keep idling out. Look for
`suspending provisioning` / `back-off cleared` in the log. Its limit: groups are by shape only, so a
group where *some* jobs match is never paused.

To see *why* a particular slot and job don't match, evaluate the job's `Requirements` and the slot's
`Start` clause by clause, using a real slot ad from the collector (resize CPUs/memory/disk/GPUs to the
group's shape). That's how the annex and typo cases were found. Expect false positives on GPU-specific
clauses, since the test slot's GPU may not be the GPU a real pod lands on.

---

## Pods killed after exactly 5 minutes (exit 143)

`wn/scripts/entrypoint.sh` runs `check_master.sh 300`, which kills the pilot if `condor_master` hasn't
started within 5 minutes. On some nodes (seen on Missouri, Great Plains and UNL nodes) the pilot's
download of the default container image (`htc__rocky__9.sif`, about 1.4 GB, via Pelican) stalled, so
`condor_master` never started in time.

- With `apptainer=no` the provisioner sets `OSG_DEFAULT_CONTAINER_CVMFS=1`, so the pilot uses the
  CVMFS copy and downloads nothing.
- With `apptainer=yes` the download still happens. Raising the watchdog's 300-second limit would let
  slow nodes finish (not done yet).

---

## `KeyError: 'ca'` from `generate-hostcert` in worker pod logs

Harmless. The pilot tries to get a TLS certificate for forwarding its own logs to OSG's central syslog.
The OSG registry rejects the pool token for that (`401 Endpoint requires authorization`), and the
script then crashes with `KeyError` instead of exiting cleanly (an upstream bug in `ospool-ep`). The
pilot then skips remote syslog; jobs are unaffected. LIGO never hits it, because its `POOL` setting
turns remote syslog off. Set `ENABLE_REMOTE_SYSLOG=false` in `envs_dict` to silence it.

---

## `maximum number of user namespaces exceeded` in worker pod logs

Expected: unprivileged user namespaces are disabled on the nodes on purpose (security). Apptainer
can't run, and slots advertise `HAS_SINGULARITY=false`. Run with `apptainer=no`.

---

## Pods stuck `Pending`: `Insufficient smarter-devices/fuse` / node affinity

Usually large shapes (many CPUs or GPUs) that fit few nodes, combined with the FUSE device request and
the GPU node affinity. They wait until a node frees up.

## Jobs with no status that never go away

Worker Jobs are cleaned up `k8s_job_ttl` seconds after they *finish*. A Job whose pods were deleted
while Pending can be left with no active, succeeded or failed count, so it never "finishes". List and
delete those older than a day:

```bash
kubectl get jobs -n <ns> -o json | python3 -c 'import json,sys,datetime as d; n=d.datetime.now(d.timezone.utc); print("\n".join(j["metadata"]["name"] for j in json.load(sys.stdin)["items"] if not any(j["status"].get(k) for k in ("active","succeeded","failed")) and (n-d.datetime.fromisoformat(j["metadata"]["creationTimestamp"].replace("Z","+00:00"))).days>=1))' | xargs -r kubectl delete job -n <ns>
```
