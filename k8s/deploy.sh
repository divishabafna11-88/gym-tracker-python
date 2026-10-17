#!/bin/bash
set -e

DOCKER_USER="${1:-$DOCKERHUB_USER}"
DOCKER_TOKEN="${2:-$DOCKERHUB_TOKEN}"

if [ -z "$DOCKER_USER" ] || [ -z "$DOCKER_TOKEN" ]; then
  echo "Usage: ./deploy.sh <dockerhub_username> <dockerhub_token>"
  echo "Or export DOCKERHUB_USER and DOCKERHUB_TOKEN environment variables."
  exit 1
fi

echo "==> Creating namespaces (qa, stage, prod)..."
kubectl apply -f 01-namespaces.yaml

for ns in qa stage prod; do
  echo "==> Creating Docker Hub pull secret in namespace: $ns..."
  kubectl create secret docker-registry dockerhub \
    --docker-username="$DOCKER_USER" \
    --docker-password="$DOCKER_TOKEN" \
    -n "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

echo "==> Deploying Postgres and Redis services..."
kubectl apply -f 02-postgres.yaml
kubectl apply -f 03-redis.yaml

echo "==> Updating image reference for user: $DOCKER_USER..."
if [[ "$OSTYPE" == "darwin"* ]]; then
  sed -i '' "s|image: .*/gym-tracker-python:|image: ${DOCKER_USER}/gym-tracker-python:|g" 04-app.yaml
else
  sed -i "s|image: .*/gym-tracker-python:|image: ${DOCKER_USER}/gym-tracker-python:|g" 04-app.yaml
fi

echo "==> Deploying Gym Tracker applications..."
kubectl apply -f 04-app.yaml

echo "==> Checking QA rollout status..."
kubectl rollout status deployment/gym-tracker -n qa --timeout=120s

echo "==> Verification Commands:"
echo "kubectl get pods -n qa -o jsonpath='{range .items[*]}{.spec.containers[*].image}{\"\\n\"}{end}'"
echo "kubectl describe pod \$(kubectl get pods -n qa -l app=gym-tracker -o jsonpath='{.items[0].metadata.name}') -n qa | grep -i \"image id\""
