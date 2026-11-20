#!/bin/bash
# After terraform apply, point kubectl at the cluster and run this.
# It does not run terraform apply or terraform destroy.
#
# A hello-world Job on each node, then the check the plan asks for before B2:
# deny-all, strict mode with no policy, and a positive control that is allowed.

set -euo pipefail

if ! kubectl version --client >/dev/null 2>&1; then
  echo "kubectl is not installed" >&2
  exit 1
fi
if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "kubectl has no cluster. Run: $(terraform -chdir="$(dirname "$0")" output -raw configure_kubectl)" >&2
  exit 1
fi

node_job() {
  local name="$1"
  local node="$2"
  local tolerate="$3"
  kubectl delete job "$name" --ignore-not-found >/dev/null
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${name}
  namespace: default
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        pr-verification/node: ${node}
${tolerate}
      automountServiceAccountToken: false
      enableServiceLinks: false
      containers:
        - name: hello
          image: public.ecr.aws/docker/library/python:3.12-slim
          command: ["python", "-c", "print('ok')"]
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
EOF
  kubectl wait --for=condition=complete "job/${name}" --timeout=180s
  kubectl logs "job/${name}" | grep -q '^ok$'
  echo "hello on ${node}: ok"
}

node_job hello-system system ""
node_job hello-sandbox sandbox "$(printf '      tolerations:\n        - key: sandbox\n          operator: Equal\n          value: "true"\n          effect: NoSchedule')"

probe() {
  local ns="$1"
  local expect="$2"
  local policy="$3"
  kubectl delete namespace "$ns" --wait=true --ignore-not-found >/dev/null
  kubectl create namespace "$ns" >/dev/null
  if [ -n "$policy" ]; then
    kubectl apply -n "$ns" -f - <<<"$policy" >/dev/null
  fi
  kubectl apply -n "$ns" -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: probe
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        pr-verification/node: system
      automountServiceAccountToken: false
      enableServiceLinks: false
      containers:
        - name: probe
          image: public.ecr.aws/docker/library/python:3.12-slim
          command: ["python", "-c"]
          args:
            - |
              import socket
              s = socket.socket()
              s.settimeout(5)
              try:
                  s.connect(("1.1.1.1", 443))
              except socket.timeout:
                  print("timeout")
              except ConnectionRefusedError:
                  print("refused")
              else:
                  print("connected")
EOF
  kubectl wait -n "$ns" --for=condition=complete job/probe --timeout=180s
  local got
  got="$(kubectl logs -n "$ns" job/probe | tail -n 1)"
  kubectl delete namespace "$ns" --wait=false >/dev/null
  if [ "$got" != "$expect" ]; then
    echo "probe ${ns}: got ${got}, expected ${expect}" >&2
    exit 1
  fi
  echo "probe ${ns}: ${got}"
}

deny_all='apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all
spec:
  podSelector: {}
  policyTypes: ["Ingress", "Egress"]'

allow_all='apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-all
spec:
  podSelector: {}
  policyTypes: ["Ingress", "Egress"]
  ingress:
    - {}
  egress:
    - {}'

probe probe-deny timeout "$deny_all"
probe probe-strict timeout ""
probe probe-open connected "$allow_all"
echo "smoke passed"
