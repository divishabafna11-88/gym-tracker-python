# Gym Progress Tracker — Complete DevOps Pipeline

**Language:** Python (FastAPI) &nbsp;|&nbsp; **Architecture:** Postgres + Redis + Docker + CircleCI + Kubernetes  
**Repository:** [https://github.com/divishabafna11-88/gym-tracker-python](https://github.com/divishabafna11-88/gym-tracker-python)  
**Author:** Divisha Bafna (3rd Year ISE — Capstone Project)

---

## The Hard Part: Is 100kg for 5 reps better than 110kg for 3?

### 1. Mathematical Breakdown

To determine whether $100\text{kg} \times 5\text{ reps}$ is better than $110\text{kg} \times 3\text{ reps}$, we evaluate their Estimated One-Rep Max ($e1RM$) under both formulas:

1. **Epley Formula**:
   $$\text{e1RM} = w \times \left(1 + \frac{r}{30}\right)$$
   - For **100kg x 5**: $\text{e1RM} = 100 \times \left(1 + \frac{5}{30}\right) = 100 \times 1.1667 = \mathbf{116.67\text{ kg}}$
   - For **110kg x 3**: $\text{e1RM} = 110 \times \left(1 + \frac{3}{30}\right) = 110 \times 1.1000 = \mathbf{121.00\text{ kg}}$
   - **Verdict under Epley**: **110kg x 3 wins by +4.33 kg**.

2. **Brzycki Formula**:
   $$\text{e1RM} = \frac{w \times 36}{37 - r}$$
   - For **100kg x 5**: $\text{e1RM} = \frac{100 \times 36}{37 - 5} = \frac{3600}{32} = \mathbf{112.50\text{ kg}}$
   - For **110kg x 3**: $\text{e1RM} = \frac{110 \times 36}{37 - 3} = \frac{3960}{34} = \mathbf{116.47\text{ kg}}$
   - **Verdict under Brzycki**: **110kg x 3 wins by +3.97 kg**.

**Conclusion:** Both formulas agree without ambiguity: **110kg for 3 reps is the stronger set**.

---

### 2. The 10-Rep Crossover and Why Formulas Disagree

While both formulas agree on $100\text{kg} \times 5$ vs $110\text{kg} \times 3$, they cross at exactly **10 reps**:
$$\text{At } r = 10: \quad \text{Epley} = w \times \left(1 + \frac{10}{30}\right) = \frac{4}{3}w, \quad \text{Brzycki} = \frac{w \times 36}{37 - 10} = \frac{36}{27}w = \frac{4}{3}w$$

- **Below 10 reps:** Brzycki is more conservative than Epley (penalizes fatigue).
- **Above 10 reps:** Brzycki diverges rapidly and rewards reps exponentially, eventually hitting a division-by-zero singularity at 37 reps.
- **Example of Direct Disagreement:**
  - $100\text{kg} \times 12\text{ reps} \implies \text{Epley: } 140.0\text{kg}, \quad \text{Brzycki: } 144.0\text{kg}$
  - $135\text{kg} \times 2\text{ reps} \implies \text{Epley: } 144.0\text{kg}, \quad \text{Brzycki: } 138.9\text{kg}$
  - Epley says $135\times 2$ is superior; Brzycki says $100\times 12$ is superior.

### 3. Which formula the app commits to, and why

The application commits to **Epley** as the authoritative default formula (`DEFAULT_FORMULA = "epley"`):
1. **Mathematical Stability:** Epley is linear ($\Delta \text{e1RM} / \Delta r = w/30$). It has no mathematical singularities, whereas Brzycki has a division-by-zero asymptote at 37 reps.
2. **Empirical Accuracy for Strength Training:** In the working range of 1–12 reps (`MAX_REPS = 12`), Epley reflects actual physiological 1RM testing across barbell movements with lower variance.
3. **Consistency Principle:** A personal best cannot oscillate depending on the algorithm evaluated on any given day. By committing to Epley and separating Redis cache keys by formula (`pb:{user}:{ex_id}:{formula}`), historical consistency is strictly guaranteed.

---

## Part 1 — Git & Branch Promotion

### Branching Model

| Branch | Role | Deployment Target | Notes |
|---|---|---|---|
| `main` | QA | QA Namespace (`:qa`) | The default branch. Receives work via PR. Runs full CI test suite. |
| `staging` | Pre-production | Stage Namespace (`:stage`) | Created from `main`. Build-only pipeline. |
| `prod` | Production | Prod Namespace (`:prod`) | Created from `staging`. Build-only pipeline. |

### Promotion Flow
```
feature/crud-api-and-dashboard ──(PR)──> main (QA) ──(Merge Commit PR)──> staging ──(Merge Commit PR)──> prod
```
- Direct pushes to `main`, `staging`, and `prod` are blocked by GitHub Ruleset `protected-branches`.
- Promotion uses **Create a merge commit** (never squash or rebase), ensuring identical commit histories across environments:
  ```bash
  git diff main staging  # returns empty after promotion
  git diff staging prod  # returns empty after promotion
  ```

### Branch Ruleset Verification
Ruleset name: `protected-branches`
- **Enforcement:** Active
- **Bypass List:** Empty (no exceptions, even for repo owner)
- **Target Branches:** `main`, `staging`, `prod`
- **Rules Enforced:**
  - Restrict deletions
  - Block force pushes
  - Require a pull request before merging (0 required approvals)

**Proof of Rejection:**
```bash
git checkout main
git commit --allow-empty -m "testing the ruleset"
git push origin main
# REJECTED by GitHub: GH013: Changes must be made through a pull request.
git reset --hard origin/main
```

---

## Part 2 — Dockerfile & Docker Compose

### Dockerfile Design
- **Base image pinned:** `python:3.12-slim`
- **Layer caching:** `requirements.txt` installed before copying source code.
- **Security:** Runs as a non-root user (`appuser:appgroup`) with UID/GID isolation.
- **Port:** `EXPOSE 8080`
- **Clean builds:** Comprehensive `.dockerignore` prevents caches, git metadata, and env files from entering the image.

### Docker Compose
Run with one single command:
```bash
docker compose up --build
```
Features:
- `db`: PostgreSQL 16 Alpine with `pg_isready` healthcheck.
- `cache`: Redis 7.2 Alpine with `redis-cli ping` healthcheck.
- `migration`: Automatically applies all `migrations/*.sql` in order using an alpine psql runner before the app starts.
- `app`: Starts only after `db` and `cache` report `service_healthy` and `migration` reports `service_completed_successfully`.
- Port mapping: `8080:8080` (outside:inside).

---

## Part 3 — CircleCI Pipeline

### Pipeline Architecture (`.circleci/config.yml`)

1. **On `main` (QA) — Full Pipeline:**
   - **Secret Scanning:** `gitleaks detect` prevents hardcoded tokens or secrets from ever being committed.
   - **Linting:** `ruff check .` validates PEP 8, unused imports, and code hygiene.
   - **Unit Tests:** `pytest tests/test_strength.py --cov=app.strength --cov-fail-under=70` (achieves **100% coverage**).
   - **Integration Tests:** Runs against real PostgreSQL (`cimg/postgres:16.2`) and Redis (`cimg/redis:7.2`) service containers on `localhost:5432` and `localhost:6379`.
   - **Image Build & Dual Tagging:** Builds image using `setup_remote_docker` and pushes two tags to Docker Hub:
     - Moving Tag: `<user>/gym-tracker-python:qa`
     - Immutable Tag: `<user>/gym-tracker-python:main-<short-sha>`

2. **On `staging` and `prod` — Build-Only:**
   - Skips test stages as required.
   - Pushes moving tag (`:stage` / `:prod`) and immutable tag (`:<branch>-<short-sha>`).

---

## Part 4 — Kubernetes

### Manifests (`k8s/`)
- `01-namespaces.yaml`: Namespaces `qa`, `stage`, `prod`.
- `02-postgres.yaml`: ConfigMap containing schema and seed data, Deployment, and Service.
- `03-redis.yaml`: Redis Deployment and Service.
- `04-app.yaml`: Deployment and Service configured with:
  - `imagePullPolicy: Always` (ensures moving tags pull latest image)
  - `imagePullSecrets: [{ name: dockerhub }]`
  - Matching label selectors `app: gym-tracker`

### Deployment & Verification
To deploy all environments:
```bash
# Windows
.\k8s\deploy.ps1 -DockerUser <your-user> -DockerToken <token>

# Linux / macOS
./k8s/deploy.sh <your-user> <token>
```

**Demonstration Loop:**
```bash
# 1. Trigger rollout restart in QA
kubectl rollout restart deployment/gym-tracker -n qa
kubectl rollout status deployment/gym-tracker -n qa

# 2. Verify running image and digest
kubectl get pods -n qa -o jsonpath='{range .items[*]}{.spec.containers[*].image}{"\n"}{end}'
kubectl describe pod <pod-name> -n qa | grep -i "image id"
```

---

## Demo Questions & Technical Answers (10 Marks)

1. **Why is this a Deployment and not a Pod?**
   A bare Pod is ephemeral; if the node fails or the container crashes, the Pod is gone forever. A Deployment manages a ReplicaSet that continuously ensures the desired state (self-healing, automatic restarts), supports declarative zero-downtime rolling updates, and enables rollback to previous revisions.

2. **Your integration test talks to localhost on CircleCI but to db in Compose. Why are those different?**
   In Docker Compose, each container runs on an isolated bridge network with its own IP and DNS name matching the service name (`db`, `cache`). In CircleCI, secondary service containers share the network namespace with the primary job container and bind directly to the loopback interface (`localhost`).

3. **What happens if I delete one of your Pods right now?**
   The Deployment's controller notices the current replica count (0) does not match the desired count (1) and immediately schedules and spawns a new replacement Pod.

4. **Why does your Dockerfile create a user instead of running as root?**
   Principle of Least Privilege: If a security vulnerability occurs within the web application, running as a non-root user prevents container breakout, prevents unauthorized file modification, and blocks malicious processes from acquiring root privileges on the underlying host kernel.

5. **Your Service says Running but the page does not load. Where do you look first?**
   First run `kubectl get endpoints <service-name> -n <namespace>`. If the `ENDPOINTS` column is `<none>`, the Service's `selector` labels do not match the Pod's `template.metadata.labels`. Second, check `targetPort` alignment between the Service and container `EXPOSE 8080`.

6. **Why :qa and :stage rather than one :latest tag for everything?**
   Environment isolation: Using specific moving tags (`:qa`, `:stage`, `:prod`) ensures each Kubernetes cluster environment runs strictly the version intended for its lifecycle stage. Additionally, `:latest` is ambiguous, prevents rollbacks, and creates caching race conditions.

7. **What exactly does rollout restart do — does it rebuild anything?**
   `kubectl rollout restart` does **not** rebuild any Docker images. It updates the Deployment's internal pod template metadata (injecting a timestamp annotation `kubectl.kubernetes.io/restartedAt`), which triggers a standard rolling recreation of the pods. Combined with `imagePullPolicy: Always`, each new pod pulls the newly pushed image digest from Docker Hub.
