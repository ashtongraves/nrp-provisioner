#!/usr/bin/env python3
#
# Live check: can the provisioner create its worker Jobs? Builds the same Kubernetes setup
# provisioner_main.py does (from the pod's own config and service account) and submits one
# Job with dry_run=All. The API server validates it fully (permissions, quotas, admission
# webhooks) but creates nothing. Prints the real error if submission fails.
#
# Run it in a running provisioner pod:
#   kubectl exec -i -n <ns> deploy/<deploy> -- su -l provisioner -c 'cd ~ && python3 -' < tests/live/dryrun_submit.py
#
# Optional argument: the shape to test, as CPUs,GPUs,MemoryMB,DiskKB (default 2,1,32768,8000000).
#
import configparser
import sys
import traceback

import kubernetes
try:
   import src.provisioner_k8s as pk
except ImportError:  # images built before prp_provisioner was renamed to src
   import prp_provisioner.provisioner_k8s as pk

cpus, gpus, mem, disk = (sys.argv[1] if len(sys.argv) > 1 else "2,1,32768,8000000").split(",")
print("kubernetes client", kubernetes.__version__)

f = configparser.ConfigParser(); f.read(("pod.conf", "osg_provisioner.conf"))
k = pk.ProvisionerK8SConfig(f["DEFAULT"]["namespace"])
k.additional_labels["osg-provisioner"] = "wn"; k.app_name = "osg-wn"
k.parse(f["k8s"] if "k8s" in f else f["DEFAULT"])
k.base_pvc_volumes["cvmfs"] = "#/cvmfs"
o = pk.ProvisionerK8S(k); o.authenticate()

orig = kubernetes.client.BatchV1Api.create_namespaced_job
def dry(self, *a, **kw):
   kw["dry_run"] = "All"
   return orig(self, *a, **kw)
kubernetes.client.BatchV1Api.create_namespaced_job = dry

try:
   name = o.submit({"CPUs": cpus, "GPUs": gpus, "Memory": mem, "Disk": disk}, 1)
   print("OK: dry-run submit accepted (nothing was created), job name would be", name)
except kubernetes.client.exceptions.ApiException as e:
   print("SERVER REJECTED: HTTP", e.status, e.reason); print(e.body); sys.exit(1)
except Exception:
   print("CLIENT-SIDE ERROR (the request was never sent):"); traceback.print_exc(); sys.exit(1)
