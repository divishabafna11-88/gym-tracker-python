param (
    [string]$DockerUser = $env:DOCKERHUB_USER,
    [string]$DockerToken = $env:DOCKERHUB_TOKEN
)

if (-not $DockerUser -or -not $DockerToken) {
    Write-Host "Usage: .\deploy.ps1 -DockerUser <user> -DockerToken <token>" -ForegroundColor Red
    Write-Host "Or set env vars DOCKERHUB_USER and DOCKERHUB_TOKEN."
    exit 1
}

Write-Host "==> Creating namespaces (qa, stage, prod)..." -ForegroundColor Cyan
kubectl apply -f 01-namespaces.yaml

foreach ($ns in @("qa", "stage", "prod")) {
    Write-Host "==> Creating Docker Hub pull secret in namespace: $ns..." -ForegroundColor Cyan
    kubectl create secret docker-registry dockerhub `
        --docker-username="$DockerUser" `
        --docker-password="$DockerToken" `
        -n "$ns" --dry-run=client -o yaml | kubectl apply -f -
}

Write-Host "==> Deploying Postgres and Redis services..." -ForegroundColor Cyan
kubectl apply -f 02-postgres.yaml
kubectl apply -f 03-redis.yaml

Write-Host "==> Updating image references for user: $DockerUser..." -ForegroundColor Cyan
(Get-Content 04-app.yaml) -replace 'image: .*/gym-tracker-python:', "image: ${DockerUser}/gym-tracker-python:" | Set-Content 04-app.yaml

Write-Host "==> Deploying Gym Tracker applications..." -ForegroundColor Cyan
kubectl apply -f 04-app.yaml

Write-Host "==> Checking QA rollout status..." -ForegroundColor Cyan
kubectl rollout status deployment/gym-tracker -n qa --timeout=120s

Write-Host "==> Complete! You can verify with:" -ForegroundColor Green
Write-Host "kubectl get pods -n qa -o jsonpath='{range .items[*]}{.spec.containers[*].image}{\`"\\n\`"}{end}'"
