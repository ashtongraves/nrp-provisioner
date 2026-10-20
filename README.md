# nrp-provisioner

HTCondor provisioner for OSG workloads on the NRP Nautilus cluster. The provisioner
watches HTCondor schedds for idle jobs and submits Kubernetes Jobs whose pods run
OSPool worker nodes (glideins) to serve them.

## Layout

```
provisioner/          Provisioner image -> hub.opensciencegrid.org/ashtongraves/nrp-provisioner
  prp_provisioner/    Python library (schedd queries, job clustering, k8s Job submission)
wn/                   Worker-node image -> hub.opensciencegrid.org/ashtongraves/nrp-provisioner-wn
                      (built on hub.opensciencegrid.org/osg-htc/ospool-ep)
k8s/
  rbac/               Service accounts and roles for the provisioner and the worker nodes
  osg-opportunistic/  Per-namespace deployment, provisioner config and PVCs
  osg-icecube/
  osg-ligo/
  osg-nrao/
docs/                 Provisioner design notes
```

The manifests and configs under `k8s/` match what was deployed on the cluster on 2026-10-20.

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
