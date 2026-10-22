#!/usr/bin/env python3
#
# Live, read-only check: can this provisioner image read the job queue of every schedd its
# config whitelists? Reports OK / FAILED per schedd, with the authentication or certificate
# error behind each failure, and how many idle jobs the config would target.
#
# Run it inside the provisioner image (it needs the htcondor2 bindings and the image's CA
# bundle, which is often what matters), with the provisioner's client settings:
#
#   docker run --rm -v $PWD:/repo:ro \
#     -e _CONDOR_SEC_CLIENT_AUTHENTICATION=OPTIONAL -e _CONDOR_SEC_CLIENT_INTEGRITY=OPTIONAL \
#     -e _CONDOR_SEC_CLIENT_ENCRYPTION=OPTIONAL --entrypoint python3 <provisioner image> \
#     /repo/tests/live/check_schedds.py /repo/k8s/osg-icecube/osg_provisioner.conf cm-1.ospool.osg-htc.org
#
# or in a running provisioner pod:
#   kubectl exec -i -n <ns> deploy/<deploy> -- su -l provisioner -c \
#     'python3 - osg_provisioner.conf cm-1.ospool.osg-htc.org' < tests/live/check_schedds.py
#
# The collector is cm-1.ospool.osg-htc.org for the OSPool namespaces, condor.igwn.org for osg-ligo.
#
import configparser
import re
import sys
import time

try:
   import htcondor2 as htcondor
   AdType = htcondor.AdType
except ImportError:  # older images (HTCondor 23) only have the classic bindings
   import htcondor
   AdType = htcondor.AdTypes
   htcondor.Schedd.query = htcondor.Schedd.xquery

conf_file = sys.argv[1] if len(sys.argv) > 1 else "osg_provisioner.conf"
pool = sys.argv[2] if len(sys.argv) > 2 else "cm-1.ospool.osg-htc.org"

f = configparser.ConfigParser(); f.read(conf_file)
h = f['htcondor'] if 'htcondor' in f else f['DEFAULT']
whitelist = h.get('schedd_whitelist_regexp', '.*')
req = h.get('additional_requirements', '')
apptainer = f['DEFAULT'].get('apptainer', 'yes')
constraint = "(JobStatus=?=1)" + (" && (%s)" % req if req else "") + \
             (" && isUndefined(SingularityImage)" if apptainer == "no" else " && (!isUndefined(SingularityImage))")

print("config: %s   pool: %s   apptainer=%s" % (conf_file, pool, apptainer))
print("whitelist: %s" % whitelist)
coll = htcondor.Collector(pool)
ads = coll.query(AdType.Schedd,
                 projection=['Name', 'AuthenticatedIdentity', 'MyAddress', 'AddressV1', 'CondorVersion', 'CondorPlatform', 'Machine', 'TotalIdleJobs'])
ads = sorted((a for a in ads if re.fullmatch(whitelist, a.get('Name', '')) and 'AuthenticatedIdentity' in a), key=lambda a: a['Name'])
print("whitelisted schedds in the collector: %d\n" % len(ads))

failed = 0; targeted = 0
for a in ads:
   t = time.time()
   try:
      n = sum(1 for _ in htcondor.Schedd(a).query(constraint, ['ClusterId']))
      targeted += n
      print("  OK      %-40s idle on schedd=%-7s matching this config=%-6d (%.0fs)" % (a['Name'], a.get('TotalIdleJobs'), n, time.time() - t))
   except Exception as e:
      failed += 1
      reasons = re.findall(r"AUTHENTICATE:\d+:[^|]*", str(e)) or [str(e)[:120]]
      print("  FAILED  %-40s (%.0fs) %s" % (a['Name'], time.time() - t, " | ".join(reasons)))
      ver = re.search(r"\$CondorVersion: (\S+)", a.get('CondorVersion', ''))
      print("          schedd HTCondor %s, address %s" % (ver.group(1) if ver else "?", a.get('MyAddress', '?')[:60]))

print("\n%d of %d schedds readable; %d idle jobs match this config" % (len(ads) - failed, len(ads), targeted))
if failed:
   print("For details on a failure, see docs/troubleshooting.md ('Failed to query HTCondor schedd'):\n"
         "a private address times out; 'certificate verify failed' means the image's CA bundle;\n"
         "'required authentication ... failed' after SciTokens/FS points at the client version or the schedd's policy.")
sys.exit(1 if failed else 0)
