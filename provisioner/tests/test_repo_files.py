#
# Consistency checks for the deployment files under k8s/: provisioner configs and manifests.
# Catches bad edits before they reach the cluster. Needs PyYAML and the kubernetes client
# (both installed in the provisioner image).
#
# Run from the provisioner directory:
#   python3 -m unittest discover -s tests
#

import configparser
import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

try:
   import yaml
except ImportError:
   yaml = None
try:
   from prp_provisioner import provisioner_k8s
except ImportError:
   provisioner_k8s = None

K8S_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'k8s')
NAMESPACES = sorted(os.path.basename(d) for d in glob.glob(os.path.join(K8S_DIR, 'osg-*')))
IMAGE_REPO = "hub.opensciencegrid.org/ashtongraves/"
WN_IMAGE_REPO = "hub.opensciencegrid.org/osg-htc/nrp-ospool-ep:"  # built by OSG from opensciencegrid/images

def load_conf(ns):
   f = configparser.ConfigParser()
   f.read(os.path.join(K8S_DIR, ns, 'osg_provisioner.conf'))
   return f

def load_manifests(ns):
   docs = []
   for p in sorted(glob.glob(os.path.join(K8S_DIR, ns, '*.yaml'))):
      with open(p) as fd:
         for d in yaml.safe_load_all(fd):
            if d:
               docs.append((os.path.basename(p), d))
   return docs

class TestProvisionerConfigs(unittest.TestCase):
   def test_namespaces_found(self):
      self.assertGreaterEqual(len(NAMESPACES), 1)

   def test_apptainer_setting(self):
      for ns in NAMESPACES:
         with self.subTest(namespace=ns):
            self.assertIn(load_conf(ns)['DEFAULT'].get('apptainer'), ('yes', 'no'))

   def test_required_settings(self):
      for ns in NAMESPACES:
         f = load_conf(ns)
         k = f['k8s'] if 'k8s' in f else f['DEFAULT']
         with self.subTest(namespace=ns):
            self.assertTrue(k.get('k8s_image', '').startswith(WN_IMAGE_REPO))
            self.assertEqual(k.get('service_account_name'), 'prp-htcondor-wn-ro')
            self.assertTrue(f['DEFAULT'].get('app_name'))

   def test_config_volume_sources_exist(self):
      for ns in NAMESPACES:
         f = load_conf(ns)
         k = f['k8s'] if 'k8s' in f else f['DEFAULT']
         vols = k.get('config_volumes_dict')
         if not vols:
            continue
         for el in vols.split(','):
            cm, path = el.split(':', 1)
            with self.subTest(namespace=ns, configmap=cm):
               self.assertTrue(os.path.exists(os.path.join(K8S_DIR, ns, os.path.basename(path))),
                               "no source file for ConfigMap %s in k8s/%s" % (cm, ns))

   @unittest.skipIf(provisioner_k8s is None, "kubernetes client not installed")
   def test_k8s_config_parses(self):
      for ns in NAMESPACES:
         f = load_conf(ns)
         with self.subTest(namespace=ns):
            k = provisioner_k8s.ProvisionerK8SConfig(ns)
            k.parse(f['k8s'] if 'k8s' in f else f['DEFAULT'])
            self.assertTrue(k.k8s_image.startswith(WN_IMAGE_REPO))
            self.assertIn(k.apptainer, ('yes', 'no'))

@unittest.skipIf(yaml is None, "PyYAML not installed")
class TestManifests(unittest.TestCase):
   def test_parse_and_namespace_matches_directory(self):
      for ns in NAMESPACES:
         for fname, d in load_manifests(ns):
            with self.subTest(file="%s/%s" % (ns, fname)):
               self.assertEqual(d['metadata'].get('namespace'), ns)

   def test_deployment_references_exist(self):
      for ns in NAMESPACES:
         docs = load_manifests(ns)
         pvcs = {d['metadata']['name'] for _, d in docs if d['kind'] == 'PersistentVolumeClaim'}
         for fname, d in docs:
            if d['kind'] != 'Deployment':
               continue
            spec = d['spec']['template']['spec']
            with self.subTest(file="%s/%s" % (ns, fname)):
               image = spec['containers'][0]['image']
               self.assertTrue(image.startswith(IMAGE_REPO + "nrp-provisioner:"), image)
               self.assertNotEqual(image.split(':')[-1], "", "image must have an explicit tag")
               self.assertEqual(spec.get('serviceAccountName'), 'prp-htcondor-provisioner')
               for v in spec.get('volumes', []):
                  if 'persistentVolumeClaim' in v:
                     self.assertIn(v['persistentVolumeClaim']['claimName'], pvcs)
                  if 'configMap' in v:
                     keys = [i['key'] for i in v['configMap'].get('items', [])]
                     self.assertIn('osg_provisioner.conf', keys)
               self.assertIn('cvmfs', pvcs, "worker pods mount the cvmfs PVC")
               env = {e['name']: e.get('value') for e in spec['containers'][0].get('env', [])}
               for var in ('CONDOR_HOST', 'K8S_NAMESPACE', 'CVMFS_MOUNTS'):
                  self.assertTrue(env.get(var), var)
               self.assertEqual(env['K8S_NAMESPACE'], ns)

if __name__ == '__main__':
   unittest.main()
