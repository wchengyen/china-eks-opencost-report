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

### 3. Fetch Allocation Data

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
   - Service (top 15)
   - Pod (top 20)
6. **Insights**: optimization recommendations

### 5. Currency Conversion

Default: 1 USD = 7.25 CNY. Display both USD and CNY.

**⚠️ AWS China Currency Bug**: OpenCost reads AWS China pricing files where prices are in CNY (e.g., `{"CNY": "0.345"}`), but it treats the numeric value as USD. This inflates all China-region costs by ~7.25×. The script works around this by treating OpenCost output as CNY directly (`CNY_RATE = 1.0`) instead of converting from USD. See `references/aws-china-pricing-currency-bug.md` for details.

### 6. Cleanup

Kill the port-forward process when done.

## Reference Files

- `scripts/generate_report.py` — Main report generation script
- `references/opencost-idle-api-behavior.md` — Detailed notes on how OpenCost returns idle costs (via `__idle__` entry, not per-allocation fields)
- `references/aws-china-pricing-currency-bug.md` — **Critical**: OpenCost interprets AWS China CNY prices as USD, inflating costs by ~7.25×. Workarounds included.
- `references/node-runtime-vs-window.md` — Why OpenCost node runtime (`minutes`) can be less than the query window, and how this affects cost calculations.
- `references/opencost-data-latency.md` — Data latency issue: OpenCost may return partial windows even when nodes are running continuously. Diagnostic steps and workarounds.

Key data structures passed to HTML generation:
- `nodes[]`: name, cpuCost, ramCost, pvCost, lbCost, totalCost, efficiency, **cpuIdleCost, ramIdleCost, idleTotal**
- `namespaces[]`: name, cpuCost, ramCost, pvCost, lbCost, totalCost, pct, efficiency
- `services[]`: name, cpuCost, ramCost, lbCost, totalCost, pct
- `pods[]`: name, cpuCost, ramCost, lbCost, totalCost

## Parameters

| Param | Default | Description |
|-------|---------|-------------|
| window | `1d` | Time window for allocation |
| offset | `1d` | Offset from now (1d = yesterday) |
| cny_rate | `1.0` | **AWS China workaround**: OpenCost returns CNY values directly due to currency bug. Set to `1.0` for AWS China, `7.25` for standard USD→CNY conversion. |
| output | `/home/ubuntu/opencost-report-{date}.html` | Output file path |

## Example Usage

```bash
# Generate yesterday's report (AWS China - CNY values direct from OpenCost)
python3 scripts/generate_report.py --window 1d --offset 1d --cny-rate 1.0

# Generate last 7 days report
python3 scripts/generate_report.py --window 7d --offset 0d --cny-rate 1.0
```

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

- OpenCost `/allocation/query` was removed in v1.108+. Always use `/allocation/compute`.
- **Idle cost is NOT in `cpuCostIdle`/`ramCostIdle` fields** — those are always 0. Use `includeIdle=true` and extract the `__idle__` entry instead.
- **AWS China pricing bug**: OpenCost reads AWS China pricing files (CNY) but treats the numeric values as USD. This inflates all China-region costs by the USD→CNY exchange rate (~7.25×). The script default `CNY_RATE = 1.0` treats OpenCost output as CNY directly. See `references/aws-china-pricing-currency-bug.md` for workarounds.
- **Node runtime may be < 24 hours**: OpenCost measures actual node uptime from Prometheus metrics. If a node was restarted or metrics collection was interrupted, `minutes` will be less than 1440. Do not assume 24-hour windows mean 24 hours of cost. See `references/node-runtime-vs-window.md` and `references/opencost-data-latency.md`.
- The API returns a list of dicts, each containing allocation key-value pairs. Flatten carefully.
- Some allocations have `node: null` (unmounted PVs). Filter or label as `<no-node>`.
- LB cost may be attributed to a single pod even if the ELB serves multiple. Check `lbAllocations` for details.
- CPU efficiency can exceed 100% if usage > request. Cap display at 100% or flag as anomaly.
- **Idle cost fields are zero by default** — OpenCost returns idle as a separate `__idle__` allocation entry when using `includeIdle=true`. Do not rely on `cpuCostIdle`/`ramCostIdle`.
- **Data latency**: Even when nodes are running continuously, OpenCost may return partial windows (short `minutes`). Check `references/opencost-data-latency.md` for diagnostics.
