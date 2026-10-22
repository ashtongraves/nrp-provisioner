# nrp-ospool-ep (worker image)

The worker-node image the provisioner launches. Each pod runs the OSPool pilot with one static slot
sized to the job group it was created for. See the [main README](../README.md) for the provisioner,
configuration and deployment.

## Where it's built

**The image lives upstream:** `osg-htc/nrp-ospool-ep` in
[opensciencegrid/images](https://github.com/opensciencegrid/images). OSG's CI builds it and publishes
`hub.opensciencegrid.org/osg-htc/nrp-ospool-ep:25-release`, which every namespace's `k8s_image` uses.
The tag moves: OSG rebuilds it regularly, not only when this directory changes (it was rebuilt on
2026-10-22 with no change here), and new worker pods pull it (`k8s_image_pull_policy` defaults to
`Always`).

This directory is a copy of that source, kept identical, so the provisioner and its worker can be
read and tested together. **Make changes upstream first** (a PR to opensciencegrid/images), then copy
them here. Before changing a namespace to a new build, run the image test on it (see [Tests](#tests)).

Until 2026-10-22 the namespaces ran our own build, `hub.opensciencegrid.org/ashtongraves/nrp-provisioner-wn`.

## What's in it

Built on OSG's `ospool-ep` (the pilot). On top of it, the image adds OpenCL for NVIDIA, `kubectl`
(pinned by checksum), wrappers around `apptainer`/`singularity` that drop the `--pid`/`-p` options,
its own `entrypoint.sh` and the startup scripts below. It defaults `ACCEPT_JOBS_FOR_HOURS=24` and
`ACCEPT_IDLE_MINUTES=20`, since the pods run opportunistically and may be over-provisioned.

To build it locally (e.g. to test a change before the upstream PR):

```bash
docker build -t nrp-ospool-ep:local wn
docker build --build-arg BASE_YUM_REPO=release-20260726-0746 -t nrp-ospool-ep:local wn   # on a specific dated base
```

## Startup

1. **`entrypoint.sh`** sources `/etc/entrypoint/image-config.d/*.sh` as root, then starts the OSG
   entrypoint as user `osg`. It also starts **`check_master.sh 300`**, which kills the pilot if
   `condor_master` hasn't started within 5 minutes, or once it dies or restarts.
2. The OSG entrypoint (`supervisord_startup.sh`) **sources** `/etc/osg/image-init.d/*.sh`, which append
   to the pilot's config. Because they're sourced, an `exit` in one ends the whole startup: skip with
   `if`/`else`, never `exit 0`.

| Script | Stage | What it does |
|---|---|---|
| `01_token.sh` | image-config | Reads the pool token from `/etc/condor/tokens.d/prp-wn.token` into `TOKEN`; aborts if it's missing |
| `01_no_condor_host.sh` | image-config | Unsets `CONDOR_HOST`, which the pilot doesn't want |
| `11_set_OSGInstitutionID.sh` | image-config | Looks up the node's `nautilus.io/OSGInstitutionID` label (needs the `view-nodes` binding; see the main README) |
| `02_validate_apptainer.sh`, `02_validate_singularity.sh` | image-init | Run a test container; failure aborts the pilot. Skipped when `PROVISIONER_APPTAINER=no` |
| `19_set_resources.sh` | image-init | One static slot from `NUM_CPUS`, `MEMORY`, `DISK`, `NUM_GPUS` |
| `20_advertise_glidein.sh` | image-init | `IS_GLIDEIN = true` |
| `20_advertise_k8s_domain.sh`, `21_advertise_k8s_provisioner.sh` | image-init | `K8SPodName`, `K8SNamespace`, `K8SDomain`, `K8SPhysicalHostName`, `K8SProvisionerType`, `K8SProvisionerName`: how the provisioner finds its own slots |
| `22_set_requirements.sh` | image-init | Adds `PROVISIONING_START` (the slot only takes jobs of its own shape, with the provisioner's rounding) and `MATCHING_START` (namespace matching, `ADDITIONAL_REQUIREMENTS`) to `START` |

## Environment from the provisioner

Besides the OSPool pilot's own variables, the provisioner sets `NUM_CPUS`, `MEMORY`, `DISK`,
`NUM_GPUS`, `PHYSICAL_HOSTNAME`, `K8S_NAMESPACE`, `K8S_DOMAIN`, `K8S_PROVISIONER_NAME`,
`K8S_PROVISIONER_TYPE`, `FORCE_K8SNAMESPACE_MATCHING`, `ADDITIONAL_REQUIREMENTS` and
`PROVISIONER_APPTAINER`, plus `OSG_DEFAULT_CONTAINER_CVMFS=1` when `apptainer=no` and anything in the
namespace's `envs_dict`.

**Unprivileged user namespaces are disabled on the Nautilus nodes on purpose**, so apptainer can't run
and all namespaces use `apptainer=no`: validation is skipped, slots advertise `HAS_SINGULARITY=false`,
and the pilot uses the CVMFS copy of its default image instead of downloading a `.sif`.

## Troubleshooting

### Pods killed after exactly 5 minutes (exit 143)

Exit 143 is SIGTERM: preemption at the `opportunistic` priority class, or `check_master.sh`. A
watchdog kill always comes at about 5 minutes. Seen when the pilot's download of the default container
image (`htc__rocky__9.sif`, about 1.4 GB, via Pelican) stalled on some Missouri, Great Plains and UNL
nodes (most fetch it in 5–35 s), so `condor_master` never started in time. With `apptainer=no` nothing
is downloaded. With `apptainer=yes` the risk remains; raising the 300-second limit would let slow nodes
finish (not done yet).

### `maximum number of user namespaces exceeded`

Expected while user namespaces are disabled: the apptainer test fails with ENOSPC. Run with
`apptainer=no`.

### `KeyError: 'ca'` from `generate-hostcert`

Harmless. The pilot asks the OSG registry for a certificate to forward its logs to OSG's central
syslog, using the pool token, and gets `401`: that endpoint wants a registry-issued token. The script
then crashes with `KeyError` instead of exiting cleanly (an upstream bug in `ospool-ep`), and the pilot
skips remote syslog. Jobs are unaffected. LIGO never hits it, because its `POOL` setting turns remote
syslog off. Set `ENABLE_REMOTE_SYSLOG=false` in `envs_dict` to silence it.

## Tests

```bash
tests/image/wn.sh hub.opensciencegrid.org/osg-htc/nrp-ospool-ep:25-release   # from the repo root
```

It sources the validation scripts the way the pilot does, for `PROVISIONER_APPTAINER=no`, `yes` and
unset; checks the token script (a missing token aborts; a token without a trailing newline is read);
and checks the CA bundle has Let's Encrypt `YE1`. It needs no network. CI runs it on a build of this
directory on every PR.
