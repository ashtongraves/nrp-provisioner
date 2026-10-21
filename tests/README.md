# Tests

Three tiers, from fast and offline to live and read-only. CI (`.github/workflows/test.yml`) runs
the first two on every PR.

## 1. Unit tests: `provisioner/tests/`

```bash
cd provisioner && python3 -m unittest discover -s tests
```

| File | What it covers |
|---|---|
| `test_backoff.py` | the provisioning back-off state machine, and the event loop skipping suspended groups |
| `test_htcondor_ads.py` | job/startd ad conversion (rounding, "too small" means unset, expressions, stringified values) and job clustering. Synthetic ads in the shape real schedds return |
| `test_schedd_query.py` | the schedd list (whitelist, identity filter, the location attributes `htcondor2.Schedd` needs), the query constraint (apptainer toggle, namespace matching), failure handling, every repo config |
| `test_k8s_submit.py` | Job submission through the **real** `kubernetes` client (only the network send is intercepted), for every namespace config |
| `test_logging.py` | log file and stdout mirroring |
| `test_repo_files.py` | `k8s/`: configs parse and have the required settings, manifests parse, namespaces match directories, deployments reference volumes that exist |

Without the HTCondor bindings (e.g. on a laptop), the `htcondor2`-dependent tests are skipped.
Inside the provisioner image nothing is skipped; `tests/image/provisioner.sh` fails if anything is.

## 2. Image tests: `tests/image/`

```bash
tests/image/provisioner.sh <provisioner image>
tests/image/wn.sh <worker image>
```

- **`provisioner.sh`:**
  - runs the unit tests inside the image;
  - checks the `provisioner` user's home has `.bash_profile`, and that the setup scripts' environment reaches `su -l provisioner`;
  - starts `provisioner_startup.sh` with a **fake service account**, and requires it to reach the provisioning loop with no traceback and no deprecation warnings. This makes real read-only queries to the OSPool.
- **`wn.sh`:**
  - sources the validation scripts the way the pilot does, for `PROVISIONER_APPTAINER=no`, `yes` and unset;
  - checks the token script (a missing token aborts; a token without a trailing newline is read);
  - checks the CA bundle has Let's Encrypt `YE1`.

Run them on every image before pushing a tag. They're the checks that would have caught the
`useradd`/`su -l` and `kubernetes` client problems before deployment.

## 3. Live checks: `tests/live/` (read-only, run by hand)

- `check_schedds.py <osg_provisioner.conf> [collector]`: for every schedd a config whitelists, can this
  image read its queue? Prints the authentication or certificate error for each failure, and how many
  idle jobs match the config. Run it in the image you're about to deploy. It works with old
  (classic bindings) and new (`htcondor2`) images, so you can compare them.
- `dryrun_submit.py`: run it in a provisioner pod. It submits one worker Job with `dry_run=All`, so the
  API server validates everything and creates nothing. Use it when the log says `Failed to submit N pods`.

Both scripts have usage examples at the top. See `docs/troubleshooting.md` for interpreting results.

## Writing new tests

- Don't put real job ads or logs in fixtures: they carry user names, project names and paths. Write
  synthetic ads with the same attribute names and expression forms (schedds return attribute names in
  the casing of the query's projection, e.g. `RequestCPUs`).
- Test the client path you actually use. A fake Kubernetes API object skips the client-side validation
  that broke submission once.
- Smoke tests must get past the expected failure point. An "expected" error once hid a real bug.
