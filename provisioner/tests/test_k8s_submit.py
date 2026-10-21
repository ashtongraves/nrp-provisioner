#
# Unit tests for the k8s Job submission
#
# The kubernetes client (>= 34) validates request bodies before sending them,
# so run submit() through the real client and only intercept the network send.
#
# Run from the provisioner directory:
#   python3 -m unittest discover -s tests
#

import configparser
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

try:
   import kubernetes
   from prp_provisioner import provisioner_k8s
except ImportError:
   kubernetes = None

K8S_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'k8s')

class Sent(Exception):
   pass

@unittest.skipIf(kubernetes is None, "kubernetes client not installed")
class TestSubmit(unittest.TestCase):
   def setUp(self):
      self.sent = []
      self.orig_call_api = kubernetes.client.ApiClient.call_api
      def fake_call_api(api_client, method, url, header_params=None, body=None, *args, **kwargs):
         self.sent.append(body)
         raise Sent()
      kubernetes.client.ApiClient.call_api = fake_call_api

   def tearDown(self):
      kubernetes.client.ApiClient.call_api = self.orig_call_api

   def make_k8s(self, namespace, use_large_limit):
      f = configparser.ConfigParser()
      f.read(os.path.join(K8S_DIR, namespace, 'osg_provisioner.conf'))
      k = provisioner_k8s.ProvisionerK8SConfig(namespace)
      k.parse(f['k8s'] if 'k8s' in f else f['DEFAULT'])
      k.base_pvc_volumes['cvmfs'] = "#/cvmfs"
      k.use_large_limit = use_large_limit
      return provisioner_k8s.ProvisionerK8S(k)

   def submit(self, k8s, cpus, gpus):
      with self.assertRaises(Sent):  # Sent means the body passed the client validation
         k8s.submit({'CPUs': cpus, 'GPUs': gpus, 'Memory': "8192", 'Disk': "8000000"}, 2)
      return self.sent[-1]

   def test_all_configs_pass_client_validation(self):
      for namespace in ('osg-opportunistic', 'osg-icecube', 'osg-nrao', 'osg-ligo'):
         for large in ('yes', 'no'):
            for gpus in ('0', '1'):
               with self.subTest(namespace=namespace, use_large_limit=large, gpus=gpus):
                  self.submit(self.make_k8s(namespace, large), "3", gpus)

   def test_quantities_are_strings(self):
      body = self.submit(self.make_k8s('osg-opportunistic', 'yes'), "3", "1")
      res = body['spec']['template']['spec']['containers'][0]['resources']
      self.assertEqual(res['requests']['cpu'], "2.25")
      self.assertEqual(res['limits']['cpu'], "3.25")
      self.assertEqual(res['requests']['nvidia.com/gpu'], "1")
      body = self.submit(self.make_k8s('osg-opportunistic', 'no'), "3", "0")
      res = body['spec']['template']['spec']['containers'][0]['resources']
      self.assertEqual(res['requests']['cpu'], "3")
      self.assertNotIn('nvidia.com/gpu', res['requests'])

if __name__ == '__main__':
   unittest.main()
