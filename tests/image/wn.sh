#!/bin/bash
#
# Checks a built worker-node image. Usage (from the repo root):
#
#   tests/image/wn.sh hub.opensciencegrid.org/osg-htc/nrp-ospool-ep:25-release
#
# Needs docker; no network. The apptainer test itself fails here (no CVMFS, no user
# namespaces), which is what the "yes" case expects.
#
set -u
IMG=${1:?usage: $0 <image>}
PLATFORM=${PLATFORM:-linux/amd64}
fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails+1)); }
run() { docker run --rm --platform "$PLATFORM" "$@"; }

echo "== $IMG"
run --entrypoint bash "$IMG" -c 'condor_version | head -1; rpm -q osg-ca-certs; kubectl version --client 2>/dev/null | head -1' | sed 's/^/      /'

# the image-init.d scripts are *sourced* by the pilot startup, so test them that way
validate() {
  run --entrypoint bash "$IMG" -c "$1"'
    sleep() { :; }
    for x in /etc/osg/image-init.d/02_validate_*.sh; do source "$x"; done
    echo ">>> startup continued"' 2>&1
}
out=$(validate 'export PROVISIONER_APPTAINER=no;')
[ "$(echo "$out" | grep -c "skipping")" -eq 2 ] && echo "$out" | grep -q ">>> startup continued" \
  && pass "PROVISIONER_APPTAINER=no skips both checks and startup continues" || { fail "apptainer=no"; echo "$out" | tail -5; }
for v in yes unset; do
  out=$(validate "$( [ $v = yes ] && echo 'export PROVISIONER_APPTAINER=yes;' )")
  echo "$out" | grep -q "test execution failed" && ! echo "$out" | grep -q ">>> startup continued" \
    && pass "PROVISIONER_APPTAINER=$v runs the check and aborts startup when it fails" || { fail "apptainer=$v"; echo "$out" | tail -5; }
done

# 01_token.sh: fails clearly without a token, and reads one without a trailing newline
out=$(run --entrypoint bash "$IMG" -c 'source /etc/entrypoint/image-config.d/01_token.sh; echo ">>> continued"' 2>&1)
echo "$out" | grep -q "missing or unreadable" && ! echo "$out" | grep -q ">>> continued" \
  && pass "missing token aborts startup with a clear error" || { fail "missing token"; echo "$out" | tail -3; }
out=$(run --entrypoint bash "$IMG" -c 'mkdir -p /etc/condor/tokens.d; printf "aaa.bbb.ccc" > /etc/condor/tokens.d/prp-wn.token; source /etc/entrypoint/image-config.d/01_token.sh; echo "[$TOKEN]"' 2>&1)
echo "$out" | grep -q "^\[aaa.bbb.ccc\]$" && pass "token file without a trailing newline is read" || { fail "token read"; echo "$out" | tail -3; }

# Let's Encrypt's current chains must be trusted (an old CA bundle broke schedd/SSL access before)
run --entrypoint bash "$IMG" -c 'ls /etc/grid-security/certificates/lets-encrypt-ye1.pem' >/dev/null 2>&1 \
  && pass "CA bundle has Let's Encrypt YE1" || fail "CA bundle lacks Let's Encrypt YE1"

echo; [ $fails -eq 0 ] && echo "ALL PASSED" || echo "$fails FAILED"
exit $fails
