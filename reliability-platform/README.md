# Cloud-native reliability platform

A hands-on reliability lab: three FastAPI roles, a Kubernetes/GitOps deployment, and a repeatable slow-service experiment. This is a **production-style learning project**, not a production-ready installation. No AWS resources are created by this repository until you apply Terraform.

```mermaid
flowchart LR
  U[Client / k6] --> I[Ingress] --> G[Gateway pods]
  G --> O[Orders pods]
  O --> V[Inventory pods]
  O --> P[(PostgreSQL: orders + outbox)]
  O --> R[(Redis Stream)]
  R --> W[Order workers in orders pods]
  W --> P
  G & O & V --> M[Prometheus / Grafana]
  G & O & V --> J[OTLP / Jaeger]
  A[Argo CD] --> K[Helm / Kubernetes]
  K --> C[Argo Rollouts canary]
```

`POST /orders` reaches gateway → orders → inventory. Orders writes the order and outbox record in one database transaction, returns 202, and publishes the event to Redis Streams asynchronously. A consumer group marks it processed. The outbox may publish twice if a publisher crashes between XADD and commit; processing uses an idempotent status update, acknowledges only after the database write, and reclaims abandoned messages. `GET /orders/{id}` shows the state. W3C trace context crosses HTTP and the event payload; search Jaeger for `gateway` and look for the `process order` span. Inventory is a simulated read, not stock reservation.

## Run locally

Requirements: Docker with Compose and enough disk/RAM for the images; k6 is optional for load tests. Start from the repository root:

```sh
docker compose up --build -d
docker compose ps
curl -sS -X POST http://localhost:8000/orders -H 'Content-Type: application/json' -d '{"sku":"demo","quantity":1}'
# Use the returned ID:
curl -sS http://localhost:8000/orders/REPLACE_WITH_ID
```

Grafana: http://localhost:3000 (anonymous viewer; admin/admin for edits). Dashboard: **Reliability platform**. Prometheus: http://localhost:9090. Jaeger: http://localhost:16686. Metrics use route templates, not order IDs, to avoid high cardinality. Shutdown: `docker compose down`; adding `-v` also deletes the demo data.

For Python unit checks: `python -m pip install -r requirements.txt && make test`. Container builds need access to PyPI; corporate proxies may require their CA in the build environment.

## Reliability experiment

Use a dedicated test cluster/machine. Keep the same k6 rate, duration, resource limits, and warm-up across runs. Record p95, error rate, achieved request rate, dropped iterations, replica count, and time window. k6 exits nonzero when thresholds fail; that is expected during degradation. Save actual outputs, not estimates:

```sh
mkdir -p results
RATE=20 DURATION=2m k6 run --summary-export results/baseline.json load/orders.js
# Introduce a bounded, per-pod inventory bottleneck and recreate inventory.
FAULT_DELAY_MS=300 INVENTORY_CONCURRENCY=4 docker compose up -d --no-deps --force-recreate inventory
RATE=20 DURATION=2m k6 run --summary-export results/degraded.json load/orders.js
# Improve capacity per pod; this retains the injected delay for a fair comparison.
FAULT_DELAY_MS=300 INVENTORY_CONCURRENCY=16 docker compose up -d --no-deps --force-recreate inventory
RATE=20 DURATION=2m k6 run --summary-export results/recovered.json load/orders.js
# Restore normal behavior.
FAULT_DELAY_MS=0 INVENTORY_CONCURRENCY=4 docker compose up -d --no-deps --force-recreate inventory
```

The delayed service can complete roughly `concurrency / delay_seconds` requests per second before queueing, ignoring overhead; 4 / 0.3 ≈ 13 requests/s. At 20 requests/s, latency should grow and the orders client's 3-second timeout can cause 503s. Capacity and local machine limits vary; adjust the rate if degradation is not visible. In Grafana compare gateway and inventory p95 and error ratio; in Jaeger inspect `inventory capacity wait` to locate both waiting and simulated work. Check k6 `dropped_iterations` before attributing results to the server. The script's 500 ms p95 threshold is a teaching target, not an established SLO. To demonstrate unavailability, stop inventory (`docker compose stop inventory`), observe 503s, then `docker compose start inventory` and repeat. An example short local run and its limitations are recorded in `evidence/`; run the experiment on your own system and keep the exported results.

On Kubernetes, change `inventory.faultDelayMs` and `inventory.concurrency` in the committed Helm values and allow Argo CD to sync. To demonstrate replica scaling, set `inventory.hpa.min` and `inventory.replicas` to a higher value (and max at least that high) in Git; keep load running during canary analysis. The CPU HPA will **not** reliably scale this artificial sleep/queue bottleneck: it is intentionally CPU-light. A real implementation would use a relevant custom metric such as queueing or request concurrency, and a suitable autoscaler. Scaling can still be shown by changing the minimum replica count. Persistent HTTP connections can distribute traffic unevenly.

## AWS / Kubernetes deployment

This path creates billable EKS, EC2, NAT, EBS and load-balancer resources. Prerequisites: AWS CLI with suitable IAM permissions, Docker, Terraform >=1.6, kubectl, Helm 3, an AWS account, a Git repository accessible to Argo CD, and a domain you control. Verify Kubernetes version and provider/module compatibility for your region. The EKS API is publicly reachable only from the supplied CIDRs; nodes are private. The VPC deliberately uses one NAT gateway to reduce demo cost, creating an availability bottleneck.

1. Create infrastructure and configure kubectl. Store Terraform state in a secured remote backend with locking for team use; the default here is local state.

```sh
cd terraform
terraform init
terraform plan -var='admin_cidrs=["YOUR_PUBLIC_IP/32"]' -out=tfplan
terraform apply tfplan
aws eks update-kubeconfig --region "$(terraform output -raw region)" --name "$(terraform output -raw cluster_name)"
export ECR_REPO="$(terraform output -raw ecr_repository_url)"
export AWS_REGION="$(terraform output -raw region)"
cd ..
```

2. Build and push one immutable image tag, using a real commit SHA (the ECR repository rejects overwrites). Build for your node architecture.

```sh
export IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${ECR_REPO%%/*}"
docker build -t "$ECR_REPO:$IMAGE_TAG" .
docker push "$ECR_REPO:$IMAGE_TAG"
```

3. Install cluster prerequisites and observability. Chart versions below are examples; review and pin versions in a real deployment. The EBS CSI addon is provisioned by Terraform. If an existing default StorageClass exists, remove its default annotation before applying `gp3`.

```sh
kubectl apply -f gitops/storage-class.yaml
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
helm repo add argo https://argoproj.github.io/argo-helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system
helm upgrade --install argo-cd argo/argo-cd -n argocd --create-namespace
helm upgrade --install argo-rollouts argo/argo-rollouts -n argo-rollouts --create-namespace
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack -n observability --create-namespace -f gitops/monitoring-values.yaml
kubectl apply -f gitops/jaeger.yaml
```

4. Set up credentials and GitOps. Use a random URL-safe demo password (letters/numbers) and never commit it. Kubernetes Secrets are not a full secret-management solution. Replace with External Secrets / a secret manager in real use.

```sh
kubectl create namespace reliability
read -rs -p 'Demo database password (letters/numbers): ' DB_PASS; echo
kubectl -n reliability create secret generic platform-db --from-literal="password=$DB_PASS" --from-literal="database-url=postgresql://platform:$DB_PASS@postgres:5432/platform"
unset DB_PASS
cp helm/reliability/values-aws.example.yaml helm/reliability/values-aws.yaml
# Edit values-aws.yaml: set ECR repository, image tag, and your ingress host.
# Edit gitops/application.yaml: set repoURL and targetRevision.
# Commit and push these files to the repository Argo CD can access, then:
kubectl apply -f gitops/application.yaml
kubectl -n reliability get deployments,rollouts,pods,hpa,ingress
```

Point your DNS host to the ingress controller's external address (`kubectl -n ingress-nginx get svc`). For quick validation without public DNS, port-forward `kubectl -n reliability port-forward svc/gateway 8000:8000`. Port-forward Grafana `kubectl -n observability port-forward svc/monitoring-grafana 3000:80` and Jaeger `kubectl -n observability port-forward svc/jaeger-collector 16686:16686`. Grafana's generated admin password is in the `monitoring-grafana` Secret. Send test traffic while observing rollout progress with `kubectl -n reliability get rollout inventory -w`.

Argo Rollouts uses replica-based canary weights (approximate, especially with few pods and persistent connections), waits, then gates on the **aggregate inventory** 5xx success ratio over three samples. It is not a canary-specific comparison and fails without meaningful traffic; use a canary-specific metric and traffic router for a real release. The HPA targets the Rollout scale subresource. To recover a bad release, revert the Git commit and let Argo CD reconcile; inspect `kubectl -n reliability describe rollout inventory` for analysis failures. Alert rules exist for gateway p95 and errors; Alertmanager routing is not configured.

5. Cleanup: remove the Argo Application and uninstall ingress before `terraform destroy` to allow AWS load balancers to be deleted. Deleting PVCs destroys demo data. Verify AWS resources and costs afterward; orphaned resources can still incur charges.

## Boundaries and hardening

- Postgres and Redis are single replicas with persistent volumes, static inventory is not reserved, Redis has no authentication, and Jaeger is in-memory. They are for demonstrations, not highly available or durable production data services. The streams and outbox are not pruned; add retention/archival and operational monitoring.
- No authentication, TLS ingress, network policies, backups, disaster recovery, secret rotation, centralized logs, or CI image scanning/signing are configured. Add these and controlled access before exposing a real service. Grafana/Jaeger/Prometheus local ports are for a trusted machine.
- API retries lack an idempotency key, so a timed-out client retry can create a second order. Outbox publication is at-least-once, not exactly-once. The simple worker's update is replay-safe but this alone does not make future side effects safe.
- Database tables are created on startup as a convenience, not via migrations. Startup depends on Postgres and Redis; readiness checks them, liveness does not. Introduce migrations, dependency resilience, connection-pool sizing, alert routing, trace storage and SLOs for a real deployment.
- Terraform, Kubernetes deployment, and benchmark results require an AWS environment and must be validated there. Review node sizing and subnet/region quotas before applying.
