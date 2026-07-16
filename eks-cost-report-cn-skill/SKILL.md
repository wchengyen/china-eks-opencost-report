---
name: eks-cost-report
title: EKS OpenCost HTML Report Generator
description: Generate EKS cost analysis reports from OpenCost API, outputting HTML with charts.
trigger: When the user asks for EKS cost reports, OpenCost reports, or Kubernetes cost analysis.
---

# EKS OpenCost HTML Report Generator

Generate a dark-themed HTML cost analysis report from OpenCost `/allocation/compute` API.

## Prerequisites

- `kubectl` configured for the target EKS cluster
- OpenCost running in the cluster (default namespace: `opencost`)
- Port 9003 available locally for port-forwarding

## Workflow

### 1. Port-forward OpenCost

```bash
kubectl port-forward svc/opencost 9003:9003 -n opencost
```

Run in background. Verify with:
```bash
curl -s "http://localhost:9003/allocation/compute?window=1d&offset=1d" | head -c 200
```

### 2. Fetch Allocation Data

Query OpenCost API. The response structure is:
```json
{"code": 200, "data": [{"allocation-key": {...allocation-object...}}]}
```

Each allocation object contains:
- `properties`: node, namespace, pod, container, services, labels
- `cpuCost`, `ramCost`, `pvCost`, `gpuCost`, `networkCost`, `loadBalancerCost`, `totalCost`
- `cpuCoreHours`, `ramByteHours`, `cpuEfficiency`
- `cpuCoreRequestAverage`, `cpuCoreUsageAverage`, `ramByteRequestAverage`, `ramByteUsageAverage`
- `window`: start/end timestamps (e.g. `2026-05-26T00:00:00Z`)

**Always use `includeIdle=true`** to get the native idle cost entry.

### 3. Aggregate Data

Flatten the nested response and aggregate by:

| Dimension | Key | Fields Summed |
|-----------|-----|---------------|
| Node | `properties.node` | all cost fields, cpuCoreHours, ramByteHours, request/usage averages |
| Namespace | `properties.namespace` | all cost fields, cpuCoreHours, ramByteHours |
| Service | `properties.services[]` | all cost fields (split across services) |
| Pod | `properties.namespace + "/" + properties.pod` | all cost fields |
| **Pod Tag** | `properties.labels` (default: `app`, `app.kubernetes.io/name`, `app_kubernetes_io_name`, `env`, `app.kubernetes.io/part-of`) | all cost fields, pod count, sample pods |

Pod tag grouping uses the **first non-empty matching label** in priority order. Use `--tags key1,key2` to override the default tag keys. Untagged pods are grouped under `<untagged>`.

Calculate average CPU efficiency per dimension: `sum(cpuEfficiency) / count`

**Idle cost — two sources**:

1. **Native OpenCost idle**: Use `includeIdle=true` parameter. OpenCost returns idle as a separate `__idle__` allocation entry (NOT per-allocation `cpuCostIdle`/`ramCostIdle` fields, which are always 0):
```python
url = "http://localhost:9003/allocation/compute?window=1d&offset=1d&includeIdle=true"
# Response contains a key "__idle__" with cpuCost, ramCost, totalCost
idle_cpu = idle_entry.get("cpuCost", 0)   # native idle CPU cost
idle_ram = idle_entry.get("ramCost", 0)   # native idle RAM cost
```

2. **Request-usage gap estimate** (fallback / per-node detail):
```python
cpu_idle_ratio = max(0, (cpu_req - cpu_use) / cpu_req) if cpu_req > 0 else 0
ram_idle_ratio = max(0, (ram_req - ram_use) / ram_req) if ram_req > 0 else 0
cpu_idle_cost = cpuCost * cpu_idle_ratio
ram_idle_cost = ramCost * ram_idle_ratio
```

### 4. Build HTML Report

Use the script at `scripts/generate_report.py`. Key sections:

1. **Header**: cluster name, date range with **hour precision** (e.g. `2026-05-26 00:00:00 UTC ~ 2026-05-27 00:00:00 UTC`), OpenCost version
2. **Summary Cards**: total cost, CPU/RAM/LB/PV breakdown
3. **Alert Box**: flag low CPU efficiency (<5%) or anomalous LB costs
4. **Charts**: Chart.js doughnut (namespace split) + bar (cost composition)
5. **Tables**: 
   - **Node** (with **CPU Idle / RAM Idle / Idle Total** columns)
   - Namespace
   - **Pod Tag** (cost by pod labels, with pod count and sample pods)
   - Service (top 15)
   - Pod (top 20)
6. **Insights**: optimization recommendations

### 5. Currency Conversion

**⚠️ AWS China Currency Bug**: OpenCost reads AWS China pricing files where prices are in CNY (e.g., `{"CNY": "0.345"}`), but it treats the numeric value as USD. This inflates all China-region costs by ~7.25×. The script works around this by treating OpenCost output as CNY directly (`CNY_RATE = 1.0`) instead of converting from USD. See `references/aws-china-pricing-currency-bug.md` for details.

**Report currency display**: When `--cny-rate=1.0` (AWS China default), the report shows **CNY only** — no USD subtext. When `--cny-rate=7.25` (standard USD→CNY conversion), both USD and CNY may be shown. The summary card subtext adapts to the rate setting.

### 6. Cleanup

Kill the port-forward process when done.

## Reference Files

- `scripts/generate_report.py` — Main report generation script (uses OpenCost API for the requested window)
- `scripts/generate_historical_report.py` — Generate a report for a date older than OpenCost's ~24-48h retention by recomputing from Prometheus. See `references/historical-reports-from-prometheus.md`.
- `scripts/port-forward-both.sh` — Convenience script: port-forward OpenCost (9003) + Prometheus (9090) in one command. Use `./port-forward-both.sh start|stop|check`.
- `references/opencost-idle-api-behavior.md` — Detailed notes on how OpenCost returns idle costs (via `__idle__` entry, not per-allocation fields)
- `references/aws-china-pricing-currency-bug.md` — **Critical**: OpenCost interprets AWS China CNY prices as USD, inflating costs by ~7.25×. Workarounds included.
- `references/node-runtime-vs-window.md` — Why OpenCost node runtime (`minutes`) can be less than the query window, and how this affects cost calculations.
- `references/opencost-data-latency.md` — Data latency issue: OpenCost may return partial windows even when nodes are running continuously. Diagnostic steps and workarounds.
- `references/opencost-1.118-window-offset-quirk.md` — On OpenCost 1.118, `--window 1d --offset Nd` silently returns TODAY regardless of offset. Use `--window 24h --offset 24h` to actually get yesterday.
- `references/pod-request-limit-prometheus.md` — How to fetch pod CPU/memory request and limit from kube-state-metrics via Prometheus and roll them up by Pod Tag. Also covers request/limit cost estimation.
- `references/historical-reports-from-prometheus.md` — When the target date is older than OpenCost's ~24-48h retention, recompute cost from Prometheus (cAdvisor usage + OpenCost's own `node_*_hourly_cost` metrics). Documents the cost formula, what's missing vs. the normal report, and why pod labels are read from the current OpenCost state.

Key data structures passed to HTML generation:
- `nodes[]`: name, cpuCost, ramCost, pvCost, lbCost, totalCost, efficiency, **cpuIdleCost, ramIdleCost, idleTotal**
- `namespaces[]`: name, cpuCost, ramCost, pvCost, lbCost, totalCost, pct, efficiency
- `tags[]`: name, cpuCost, ramCost, pvCost, lbCost, totalCost, pct, efficiency, podCount, samplePods, cpuRequest, cpuLimit, memRequest, memLimit, **cpuReqCost, cpuLimCost, memReqCost, memLimCost**
- `services[]`: name, cpuCost, ramCost, lbCost, totalCost, pct
- `pods[]`: name, cpuCost, ramCost, lbCost, totalCost

## Request / Limit Cost Estimation

OpenCost returns **actual measured costs** in `cpuCost` and `ramCost`. The skill can also estimate what the cost would be if every pod ran at full CPU/memory request or limit for the entire window.

How it works:
1. Derive a per-node unit price from the actual allocations: `cpuPrice = Σ(cpuCost) / Σ(cpuCoreHours)` and `ramPrice = Σ(ramCost) / Σ(ramByteHours)`.
2. Fetch each pod's request and limit from Prometheus (`kube_pod_container_resource_requests` / `kube_pod_container_resource_limits`).
3. Multiply request/limit by the window duration (OpenCost's `minutes` field, defaulting to 1440 minutes = 24h) and the node unit price.
4. Roll up by pod tag.

Columns added in the "Pod Tag 維度成本" table: `CPU Req Cost`, `CPU Lim Cost`, `Mem Req Cost`, `Mem Lim Cost`. These are estimates; actual costs (`實際 CPU 成本`, `實際 RAM 成本`) remain the OpenCost ground truth.

If Prometheus is unreachable, the request/limit columns and estimated costs show `0` and the script emits a warning. Cost columns still work because they come from OpenCost.

## Pod Request / Limit Metrics

Pod-level CPU / memory **request** and **limit** are fetched from Prometheus using `kube_pod_container_resource_requests` and `kube_pod_container_resource_limits` metrics (usually exposed by kube-state-metrics). They are aggregated by pod name and then rolled up into each Pod Tag group.

Prerequisites:
- Prometheus accessible at `http://localhost:9090` (override with `--prometheus-url` or `PROMETHEUS_URL` env var)
- kube-state-metrics scraping `kube_pod_container_resource_requests` and `kube_pod_container_resource_limits`

If Prometheus is unreachable, the request/limit columns show `0` and the script emits a warning. Cost columns still work because they come from OpenCost.

## Parameters

| Param | Default | Description |
|-------|---------|-------------|
| window | `1d` | Time window for allocation |
| offset | `1d` | Offset from now (1d = yesterday) |
| cny_rate | `1.0` | **AWS China workaround**: OpenCost returns CNY values directly due to currency bug. Set to `1.0` for AWS China, `7.25` for standard USD→CNY conversion. |
| tags | `app,app.kubernetes.io/name,app_kubernetes_io_name,env,app.kubernetes.io/part-of` | Comma-separated pod label keys used to group pod tag costs. |
| prometheus_url | `http://localhost:9090` | Prometheus URL for pod request/limit queries. |
| output | `/home/ubuntu/opencost-report-{date}.html` | Output file path |

## Tag-Based Cost Breakdown

OpenCost exposes pod labels under `properties.labels`. The report groups pods by the **first matching label** in the configured priority list. This lets teams/owners/apps see their cost share even when workloads are spread across namespaces.

Example: a deployment with `app=frontend` in namespace `web` and `app=frontend` in namespace `api` will both roll up under tag `frontend`.

Default tag keys are tried in order:
1. `app` / `app` (with underscores)
2. `app.kubernetes.io/name` / `app_kubernetes_io_name`
3. `env`
4. `app.kubernetes.io/part-of` / `app_kubernetes_io_part_of`

Untagged pods are shown as `<untagged>`. Override with `--tags`.

## Example Usage

```bash
# Generate yesterday's report (AWS China - CNY values direct from OpenCost)
python3 scripts/generate_report.py --window 1d --offset 1d --cny-rate 1.0

# Generate last 7 days report, grouping by custom tags
python3 scripts/generate_report.py --window 7d --offset 0d --cny-rate 1.0 --tags app,env,team

# Use a different Prometheus endpoint for request/limit metrics
python3 scripts/generate_report.py --window 1d --offset 1d --cny-rate 1.0 --prometheus-url http://localhost:9090

# Generate a historical report for a date older than OpenCost's retention
python3 scripts/generate_historical_report.py 2026-07-14
```

## Idle Cost Calculation

OpenCost 1.118 only retains ~24-48h of allocation data. For a report older than yesterday (e.g. "give me 2026-07-14 when today is 2026-07-16"), you can't query the API. Instead, recompute the cost from Prometheus.

```bash
# 1. Port-forward Prometheus (and OpenCost for pod labels)
./scripts/port-forward-both.sh start

# 2. Generate the historical report
python3 scripts/generate_historical_report.py 2026-07-14
# -> /home/ubuntu/opencost-report-chris-eks-2026-07-14.html
```

The historical report has the **same HTML format** as `generate_report.py` and **includes** Pod Tag costs + Request/Limit + estimated costs. It is **missing** the idle cost row and the PV/LB/Network columns, because those require OpenCost's allocation data. Total cost in a historical report is therefore lower than the OpenCost version of the same day.

See `references/historical-reports-from-prometheus.md` for the cost formula, what the per-container `container_cpu_usage_seconds_total` counter means, and why pod labels are read from the current OpenCost state.

## Idle Cost Calculation

**Important**: OpenCost handles idle cost in a non-obvious way. The per-allocation fields `cpuCostIdle`/`ramCostIdle` always return 0. Instead, idle cost is returned as a **separate `__idle__` allocation entry** when you pass `includeIdle=true`.

### Native OpenCost Idle (recommended)

```python
url = "http://localhost:9003/allocation/compute?window=1d&offset=1d&includeIdle=true"
# The response data list will contain an entry with key "__idle__"
idle_cpu = idle_entry.get("cpuCost", 0)
idle_ram = idle_entry.get("ramCost", 0)
idle_total = idle_entry.get("totalCost", 0)
```

### Request-Usage Gap Estimate (per-node detail)

For per-node idle breakdown when native idle is insufficient:

```python
cpu_idle_ratio = max(0, (cpuCoreRequestAverage - cpuCoreUsageAverage) / cpuCoreRequestAverage) if cpuCoreRequestAverage > 0 else 0
ram_idle_ratio = max(0, (ramByteRequestAverage - ramByteUsageAverage) / ramByteRequestAverage) if ramByteRequestAverage > 0 else 0
cpu_idle_cost = cpuCost * cpu_idle_ratio
ram_idle_cost = ramCost * ram_idle_ratio
```

## Date Format

Display window start/end with **hour precision**: `2026-05-26 00:00:00 UTC ~ 2026-05-27 00:00:00 UTC`. Extract from `allocation.window.start` and `allocation.window.end`.

## Total Cost Consistency

Exclude `<no-node>` entries (unmounted PVs) from both total cost calculation and display. Otherwise total shown in summary cards won't match sum of displayed nodes.

## Pitfalls

- **OpenCost 1.118 `window=1d` ignores `offset`**: `--window 1d --offset Nd` returns TODAY regardless of offset value. Use `--window 24h --offset 24h` to actually get yesterday's data. See `references/opencost-1.118-window-offset-quirk.md`.
- **Always verify the `window` field in the API response**: it tells you exactly what time range OpenCost returned. If it doesn't match expectations, switch from `1d` to `24h` form before debugging anything else.
- **Two port-forwards needed when Request/Limit columns are wanted**: OpenCost (9003) + Prometheus (9090). Both die when the kubectl context loses its connection. Use `./scripts/port-forward-both.sh start` to bring both up in one command, and `check` to verify.
- OpenCost `/allocation/query` was removed in v1.108+. Always use `/allocation/compute`.
- **Idle cost is NOT in `cpuCostIdle`/`ramCostIdle` fields** — those are always 0. Use `includeIdle=true` and extract the `__idle__` entry instead.
- **AWS China pricing bug**: OpenCost reads AWS China pricing files (CNY) but treats the numeric values as USD. This inflates all China-region costs by the USD→CNY exchange rate (~7.25×). The script default `CNY_RATE = 1.0` treats OpenCost output as CNY directly. See `references/aws-china-pricing-currency-bug.md` for workarounds.
- **Node runtime may be < 24 hours**: OpenCost measures actual node uptime from Prometheus metrics. If a node was restarted or metrics collection was interrupted, `minutes` will be less than 1440. Do not assume 24-hour windows mean 24 hours of cost. See `references/node-runtime-vs-window.md` and `references/opencost-data-latency.md`.
- **Pod Request/Limit requires Prometheus**: `kube_pod_container_resource_requests` and `kube_pod_container_resource_limits` come from kube-state-metrics, not OpenCost. If you don't need request/limit columns, you can run without Prometheus access. See `references/pod-request-limit-prometheus.md`.
- **Per-Node pricing for Request/Limit cost estimation**: `cpuPrice = Σ(cpuCost) / Σ(cpuCoreHours)` and `ramPrice = Σ(ramCost) / Σ(ramByteHours)` are derived per Node from real allocations, then multiplied by each pod's Request/Limit and window duration. If a Node has zero allocations the price falls back to 0 for pods on that node.
- The API returns a list of dicts, each containing allocation key-value pairs. Flatten carefully.
- **Pod labels may be flattened**: OpenCost sometimes stores `app.kubernetes.io/name` as `app_kubernetes_io_name`. The script tries both forms automatically.
- Some allocations have `node: null` (unmounted PVs). Filter or label as `<no-node>`.
- LB cost may be attributed to a single pod even if the ELB serves multiple. Check `lbAllocations` for details.
- CPU efficiency can exceed 100% if usage > request. Cap display at 100% or flag as anomaly.
- **Idle cost fields are zero by default** — OpenCost returns idle as a separate `__idle__` allocation entry when using `includeIdle=true`. Do not rely on `cpuCostIdle`/`ramCostIdle`.
- **Data latency**: Even when nodes are running continuously, OpenCost may return partial windows (short `minutes`). Check `references/opencost-data-latency.md` for diagnostics.
