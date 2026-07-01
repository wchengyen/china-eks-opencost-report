# EKS Cost Report - OpenCost Deployment Guide

## Table of Contents

- [System Overview](#system-overview)
- [Prerequisites](#prerequisites)
- [Deployment Steps](#deployment-steps)
- [Configuration](#configuration)
- [Currency Settings (CNY)](#currency-settings-cny)
- [Pricing Information](#pricing-information)
- [Usage](#usage)
- [Troubleshooting](#troubleshooting)

---

## System Overview

OpenCost is an open-source Kubernetes cost monitoring tool that supports:

- Real-time cost monitoring and allocation
- Cost analysis by Namespace, Pod, Node, and other dimensions
- Separate CPU and RAM cost tracking
- Idle cost tracking for unused resources

### Current Environment

| Item | Value |
|------|-------|
| Cluster Name | chris-eks |
| Region | cn-northwest-1 (China Ningxia) |
| Node Type | t3a.large (2 vCPU, 8GB) |
| Node Count | 3 |
| OpenCost Version | 1.118.0 |

---

## Prerequisites

- Kubernetes cluster with Prometheus deployed
- kubectl configured and able to access the cluster
- CloudWatch monitoring enabled on nodes

---

## Deployment Steps

### Method 1: One-click Deployment (Recommended)

```bash
kubectl apply -f opencost-all-in-one.yaml
```

### Method 2: Step-by-step Deployment

```bash
kubectl apply -f opencost-namespace.yaml
kubectl apply -f opencost-sa.yaml
kubectl apply -f opencost-clusterrole.yaml
kubectl apply -f opencost-clusterrolebinding.yaml
kubectl apply -f opencost-service.yaml
kubectl apply -f opencost-cny-patch-configmap.yaml
kubectl apply -f opencost-deployment.yaml
```

### Verify Deployment

```bash
kubectl get pods -n opencost
kubectl get svc -n opencost
```

---

## Configuration

### Core Environment Variables

| Variable | Description | Current Value |
|----------|-------------|---------------|
| PROMETHEUS_SERVER_ENDPOINT | Prometheus service endpoint | http://prometheus.monitoring.svc:9090 |
| AWS_PRICING_URL | AWS China pricing API | https://pricing.cn-northwest-1.amazonaws.com.cn/... |
| CLUSTER_ID | Cluster identifier | cluster-one |
| IDLE_ENABLED | Enable idle cost allocation | true |

### Prometheus Configuration Requirements

OpenCost relies on Prometheus scraping the following metrics to calculate costs correctly:

| Job | Target | Required Metrics | Purpose |
|-----|--------|------------------|---------|
| opencost | opencost.opencost.svc:9003 | node_cpu_hourly_cost<br>node_ram_hourly_cost<br>node_total_hourly_cost | Node pricing data source |
| kubernetes-cadvisor | kubelet /metrics/cadvisor | container_cpu_usage_seconds_total<br>container_memory_working_set_bytes | Actual container resource usage |

> **Important:** If the above scrape jobs are not configured, OpenCost will only show Disk assets and Node assets will be missing. Allocation costs will use fallback default prices (¥0.0316/vCPU) instead of the actual AWS price (¥0.114/vCPU). See `prometheus-config.yaml` for details.

### Port Description

| Port | Purpose |
|------|---------|
| 9003 | OpenCost API |
| 9090 | OpenCost UI |
| 8081 | MCP interface |

---

## Currency Settings (CNY)

### Background

OpenCost 1.118.0 hard-codes **USD** in the UI JavaScript files. The API (`/allocation/compute`) returns plain numbers without currency symbols. Currency display is handled entirely by the UI layer.

### Solution

Because the container uses `readOnlyRootFilesystem: true`, the JS files cannot be modified directly. The following approach is used:

1. **initContainer** (`patch-currency`): runs as root, replaces `"USD"` with `"CNY"` in the JS files, and writes them to an `emptyDir` volume
2. **Main container** (`opencost-ui`): mounts the patched JS files and uses a custom startup script to generate the nginx config with `envsubst`
3. **ConfigMap** (`cny-patch-root-v3`): contains the patch script

### Key Configuration

```yaml
# initContainer in the Deployment
initContainers:
- name: patch-currency
  image: public.ecr.aws/h4m7v9o4/ghcr.io/opencost/opencost-ui:1.118.0
  command: ["sh", "-c"]
  args:
  - |
    cp /opt/ui/dist/opencost-ui.5f027c83.js /tmp-patched/ && \
    sed -i 's/"USD"/"CNY"/g' /tmp-patched/opencost-ui.5f027c83.js && \
    cp /opt/ui/dist/opencost-ui.c57fa29e.js /tmp-patched/ && \
    sed -i 's/"USD"/"CNY"/g' /tmp-patched/opencost-ui.c57fa29e.js
  volumeMounts:
  - name: ui-files
    mountPath: /tmp-patched

# opencost-ui container
- name: opencost-ui
  command: ["/bin/sh"]
  args: ["/patch.sh"]
  securityContext:
    runAsUser: 0
  volumeMounts:
  - name: cny-patch
    mountPath: /patch.sh
    subPath: "patch.sh"
  - name: ui-files
    mountPath: /opt/ui/dist/opencost-ui.5f027c83.js
    subPath: opencost-ui.5f027c83.js
  - name: ui-files
    mountPath: /opt/ui/dist/opencost-ui.c57fa29e.js
    subPath: opencost-ui.c57fa29e.js

# Volumes
volumes:
- name: ui-files
  emptyDir: {}
- name: cny-patch
  configMap:
    name: cny-patch-root-v3
    defaultMode: 493
```

### Notes

- **No exchange rate conversion**: only the currency symbol is changed, the numeric value remains the same
- **API is not affected**: `/allocation/compute` still returns plain numbers
- **UI display changes**: the currency symbol on the web interface changes from `$` to `CNY`

### Verify Currency Patch

```bash
# Check the currency in the UI JS file
POD=$(kubectl get pod -n opencost -l app=opencost -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n opencost "$POD" -c opencost-ui -- grep -c '"CNY"' /var/www/opencost-ui.5f027c83.js
# Expected output: 2

kubectl exec -n opencost "$POD" -c opencost-ui -- grep -c '"USD"' /var/www/opencost-ui.5f027c83.js
# Expected output: 0 (or very few, e.g. in comments)
```

---

## Pricing Information

### t3a.large Node Pricing (China Ningxia Region)

| Resource | Unit Price | Hourly | Daily | Monthly |
|----------|------------|--------|-------|---------|
| CPU | ¥0.114/hour/vCPU | ¥0.228 (2 vCPU) | ¥5.47 | ¥164.16 |
| RAM | ¥0.015/hour/GB | ¥0.120 (8GB) | ¥2.88 | ¥86.40 |
| **Total** | - | **¥0.345** | **¥8.28** | **¥248.40** |

**Cost split ratio:** CPU ~66% : RAM ~34% (based on AWS China On-Demand pricing)

> **Note:** ¥0.345/hour is the actual AWS China t3a.large On-Demand price. If OpenCost shows a CPU unit price of ¥0.0316, it means the fallback default price is being used. Please check whether Prometheus is configured to scrape OpenCost metrics.

---

## Usage

### Query Node Cost

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/allocation/compute?window=1d&aggregate=node"
```

### Query Cost Including Idle Cost

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/allocation/compute?window=1d&aggregate=node&idle=true"
```

### Query Namespace Cost

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/allocation/compute?window=7d&aggregate=namespace"
```

### Query Assets (Including Node / Disk)

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/assets?window=1d"
```

### Access the Web UI

#### Port Forwarding (Local Debugging)

```bash
kubectl port-forward -n opencost svc/opencost 9090:9090
```

Then visit: http://localhost:9090

---

## Report Generation

This project includes an OpenCost API-based HTML cost report generator, optimized for AWS China (CNY). An English version of the script is also available.

### Quick Start

```bash
# 1. Enter the script directory
cd eks-cost-report-cn-skill/scripts/

# 2. Start OpenCost port-forward (run in background)
kubectl port-forward svc/opencost 9003:9003 -n opencost &

# 3. Generate yesterday's report (AWS China mode, outputs CNY directly)
python3 generate_report.py --window 1d --offset 1d

# 4. Generate a report for a specific time range
python3 generate_report.py \
  --window "2026-05-26T00:00:00Z,2026-05-27T00:00:00Z" \
  --output report-2026-05-26.html

# 5. Generate an English report
python3 generate_report_en.py --window 1d --offset 1d
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--window` | `1d` | Time window, supports relative time (`1d`, `7d`) or absolute time (`2026-05-26T00:00:00Z,2026-05-27T00:00:00Z`) |
| `--offset` | `1d` | Offset from now (`1d` = yesterday) |
| `--cny-rate` | `1.0` | OpenCost AWS China returns CNY values directly, so the default `1.0` requires no conversion. For non-China regions with USD output, set the appropriate exchange rate |
| `--output` | `opencost-report-{date}.html` | Output HTML file path |

### Report Content

- **Total Cost**: Total actual cost (allocated + idle)
- **Used Cost**: Only the portion actually requested by Pods
- **Idle Cost**: Unused resource cost
- **Node / Namespace / Service / Pod** multi-dimensional tables
- Chart.js charts (Namespace distribution, cost composition)

### Reference Documents

- `references/aws-china-pricing-currency-bug.md` — AWS China pricing currency issue
- `references/opencost-data-latency.md` — Data latency and partial window issue
- `references/node-runtime-vs-window.md` — Node runtime vs query window

---

## Troubleshooting

### Pod Fails to Start

```bash
kubectl logs -n opencost deploy/opencost -c opencost
kubectl describe pod -n opencost -l app=opencost
```

### Cannot Connect to Prometheus

```bash
kubectl get svc -n monitoring prometheus
```

### Pricing Data Missing

```bash
curl -I https://pricing.cn-northwest-1.amazonaws.com.cn/offers/v1.0/cn/AmazonEC2/current/index.json
```

### OpenCost Cost Differs Significantly from Actual EC2 Cost

If `/allocation` returns a CPU unit price of ¥0.0316 (instead of ¥0.114), Prometheus is not scraping OpenCost metrics:

```bash
# Check whether Prometheus is scraping opencost
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- 'http://prometheus.monitoring.svc:9090/api/v1/targets' | grep opencost

# Check whether node_cpu_hourly_cost metric exists
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- 'http://prometheus.monitoring.svc:9090/api/v1/query?query=node_cpu_hourly_cost'
```

If the result contains 0 records, update the Prometheus ConfigMap and restart:

```bash
kubectl apply -f prometheus-config.yaml -n monitoring
kubectl rollout restart deployment/prometheus -n monitoring
```

### Currency Patch Not Applied

1. Confirm the ConfigMap exists:
   ```bash
   kubectl get configmap cny-patch-root-v3 -n opencost
   ```

2. Confirm the Pod is using the new configuration:
   ```bash
   kubectl get pod -n opencost -l app=opencost -o yaml | grep -A5 "cny-patch"
   ```

3. Manually restart the Deployment:
   ```bash
   kubectl rollout restart deployment opencost -n opencost
   ```

---

## File List

| File | Description |
|------|-------------|
| `opencost-all-in-one.yaml` | Complete deployment file (recommended, includes CNY patch) |
| `opencost-namespace.yaml` | Namespace definition |
| `opencost-deployment.yaml` | Deployment configuration (includes CNY patch) |
| `opencost-cny-patch-configmap.yaml` | CNY patch ConfigMap |
| `opencost-service.yaml` | Service definition |
| `opencost-sa.yaml` | ServiceAccount |
| `opencost-clusterrole.yaml` | ClusterRole permissions |
| `opencost-clusterrolebinding.yaml` | ClusterRoleBinding |
| `prometheus-config.yaml` | **Prometheus scrape configuration (includes opencost + cadvisor)** |
| `opencost-full.yaml` | Full backup (includes runtime state) |

---

## Change History

| Date | Change |
|------|--------|
| 2026-05-25 | **Added CNY currency patch** (no exchange rate conversion required)<br>Corrected pricing information (¥0.097 → ¥0.345/hour)<br>Added prometheus-config.yaml (includes opencost + cadvisor scrape)<br>Removed unnecessary CLOUD_PROVIDER_API_KEY<br>Updated troubleshooting: added cost discrepancy guide |
| 2026-05-22 | Enabled IDLE_ENABLED environment variable to support idle cost allocation |
| 2025-05-13 | Initial OpenCost 1.118.0 deployment |

---

Document generated: 2026-05-25 |
[OpenCost Official Website](https://opencost.io)
