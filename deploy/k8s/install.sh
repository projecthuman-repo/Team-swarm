#!/usr/bin/env bash
# Tier B bring-up (runbook §14): k3s + Istio ambient mesh.
# Pin compatible k3s/Istio versions — some k3s versions (>1.31.6, esp.
# with k3d) broke Istio-CNI default paths; the overrides below handle it.
set -euo pipefail

# k3s without Traefik (Istio fronts ingress)
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable=traefik" sh -

helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update

# Istio ambient — note the k3s CNI path overrides
helm install istio-base istio/base -n istio-system --create-namespace --wait
helm install istio-cni istio/cni -n istio-system --set profile=ambient \
  --set global.platform=k3s \
  --set cniConfDir=/var/lib/rancher/k3s/agent/etc/cni/net.d \
  --wait
helm install istiod istio/istiod -n istio-system --set profile=ambient \
  --set global.platform=k3s --wait
helm install ztunnel istio/ztunnel -n istio-system --wait

kubectl apply -f namespace.yaml
kubectl label namespace swarm istio.io/dataplane-mode=ambient --overwrite

# Platform services (JetStream HA: Replicas=3)
helm repo add nats https://nats-io.github.io/k8s/helm/charts/
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add openbao https://openbao.github.io/openbao-helm
helm repo add opensearch https://opensearch-project.github.io/helm-charts

helm install nats nats/nats -n swarm \
  --set config.jetstream.enabled=true \
  --set config.cluster.enabled=true --set config.cluster.replicas=3
helm install postgres bitnami/postgresql -n swarm \
  --set image.repository=pgvector/pgvector --set image.tag=pg16 \
  --set auth.database=swarm
helm install valkey bitnami/valkey -n swarm
helm install openbao openbao/openbao -n swarm --set injector.enabled=true
helm install opensearch opensearch/opensearch -n swarm --set singleNode=true

kubectl apply -f runtimeclass-gvisor.yaml
kubectl apply -f networkpolicy-default-deny.yaml
kubectl apply -f relay.yaml
kubectl apply -f vllm.yaml
kubectl apply -f agent-backend.yaml
kubectl apply -f agent-roles.yaml # frontend / review / triage
kubectl create configmap litellm-config -n swarm \
  --from-file=config.yaml=../litellm/config.yaml \
  --from-file=entrypoint.sh=../litellm/entrypoint.sh \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f litellm.yaml  # v4.1 T4.3 (local-primary gateway)
kubectl apply -f headroom.yaml # v4.1 T1.2 (seed the assets PVC first)

echo "Verify:  istioctl ztunnel-config workloads   # agents in ambient, mTLS"
echo "Verify:  kubectl exec deploy/agent-backend -n swarm -- curl -m5 https://example.com  # must time out"
