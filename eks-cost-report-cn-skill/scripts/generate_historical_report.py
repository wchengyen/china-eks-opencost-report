#!/usr/bin/env python3
"""Generate OpenCost-style HTML report for a past date by recomputing from Prometheus.

Use when the target date is older than OpenCost's ~24-48h data retention.
The OpenCost API only retains recent data; for older dates we have to derive
the cost ourselves from Prometheus metrics that OpenCost itself emits
(node_cpu_hourly_cost / node_ram_hourly_cost) plus cAdvisor container usage
and kube-state-metrics request/limit.

Output: a single HTML file in the same format as generate_report.py, but
without the idle/PV/LB/Network columns (which require OpenCost allocation data).

Usage:
    python3 scripts/generate_historical_report.py YYYY-MM-DD [output.html]
    python3 scripts/generate_historical_report.py 2026-07-14

Prerequisites:
    - kubectl port-forward svc/opencost 9003:9003 -n opencost   (for pod labels)
    - kubectl port-forward svc/prometheus 9090:9090 -n monitoring  (for usage + prices)
    - or set OPENCOST_URL / PROMETHEUS_URL env vars
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

# Make generate_report importable
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

from generate_report import (
    DEFAULT_TAG_KEYS,
    aggregate,
    build_report,
    fetch_pod_resources,
)

PROM = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
OPENCOST = os.environ.get("OPENCOST_URL", "http://localhost:9003")
CNY_RATE = 1.0  # AWS China: OpenCost prices are already in CNY


def query_range(q, start, end, step):
    url = f"{PROM}/api/v1/query_range?query={urllib.parse.quote(q)}&start={start}&end={end}&step={step}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())["data"]["result"]


def query_instant(q, time_str):
    url = f"{PROM}/api/v1/query?query={q}&time={urllib.parse.quote(time_str)}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["data"]["result"]


def fetch_pod_info():
    """Use current OpenCost allocation to get pod->labels mapping.

    Labels don't usually change, so current mapping is good enough for a
    historical report. The trade-off is documented in the
    historical-reports-from-prometheus reference.
    """
    url = f"{OPENCOST}/allocation/compute?window=24h&offset=24h"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode())
    pod_info = {}
    for item in raw.get("data", []):
        if not isinstance(item, dict):
            continue
        for key, alloc in item.items():
            if key == "__idle__":
                continue
            props = alloc.get("properties", {})
            pod = props.get("pod", "")
            ns = props.get("namespace", "")
            pod_info[f"{ns}/{pod}"] = {
                "namespace": ns,
                "pod": pod,
                "node": props.get("node", ""),
                "labels": props.get("labels", {}),
                "services": props.get("services", []),
            }
    return pod_info


def compute_container_cpu_hours(raw):
    """For cumulative counter, total core-hours used in the time range.

    For each container, sum the counter deltas between consecutive samples
    divided by 3600. Handles counter resets (pod restart) by adding the
    post-reset absolute value rather than the negative delta.
    """
    result = defaultdict(float)
    for r in raw:
        m = r["metric"]
        key = f"{m.get('namespace','')}/{m.get('pod','')}/{m.get('container','')}"
        m["node"] = m.get("instance", "")
        values = sorted(r["values"], key=lambda x: x[0])
        total_delta = 0.0
        prev_val = None
        for _ts, v in values:
            v_float = float(v)
            if prev_val is not None:
                if v_float >= prev_val:
                    total_delta += v_float - prev_val
                else:
                    # Counter reset
                    total_delta += v_float
            prev_val = v_float
        result[key] = total_delta / 3600.0
    return result


def compute_container_mem_avg(raw):
    """Average bytes per container across the range."""
    sums = defaultdict(float)
    counts = defaultdict(int)
    for r in raw:
        m = r["metric"]
        key = f"{m.get('namespace','')}/{m.get('pod','')}/{m.get('container','')}"
        for _ts, v in r["values"]:
            sums[key] += float(v)
            counts[key] += 1
    return {k: sums[k] / counts[k] if counts[k] > 0 else 0 for k in sums}


def get_node_prices(target_date):
    """Read per-node CPU/RAM hourly cost from OpenCost's own Prom metrics.

    Price snapshots are taken at noon of the target date. Pricing rarely
    changes, so this is a reasonable approximation.
    """
    time_str = f"{target_date}T12:00:00Z"
    cpu_p, ram_p = {}, {}
    for q, target in [("node_cpu_hourly_cost", cpu_p), ("node_ram_hourly_cost", ram_p)]:
        for r in query_instant(q, time_str):
            target[r["metric"]["node"]] = float(r["value"][1])
    return cpu_p, ram_p


def get_resource_snapshot(target_date):
    """Request and limit per pod at the target date."""
    time_str = f"{target_date}T12:00:00Z"
    result = defaultdict(lambda: {
        "cpu_request": 0.0, "cpu_limit": 0.0,
        "mem_request": 0.0, "mem_limit": 0.0,
        "namespace": "", "node": "",
    })
    for r in query_instant("kube_pod_container_resource_requests", time_str):
        m = r["metric"]
        key = f"{m.get('namespace','')}/{m.get('pod','')}"
        result[key]["namespace"] = m.get("namespace", "")
        result[key]["node"] = m.get("node", "")
        if m.get("resource") == "cpu":
            result[key]["cpu_request"] += float(r["value"][1])
        elif m.get("resource") == "memory":
            result[key]["mem_request"] += float(r["value"][1])
    for r in query_instant("kube_pod_container_resource_limits", time_str):
        m = r["metric"]
        key = f"{m.get('namespace','')}/{m.get('pod','')}"
        if m.get("resource") == "cpu":
            result[key]["cpu_limit"] += float(r["value"][1])
        elif m.get("resource") == "memory":
            result[key]["mem_limit"] += float(r["value"][1])
    return dict(result)


def build_synthetic_allocations(target_date):
    """Build per-container synthetic allocation dicts matching OpenCost's schema.

    Returns (allocations, pod_info) where allocations is a list of dicts
    that generate_report.aggregate() can consume.
    """
    start = f"{target_date}T00:00:00Z"
    end = f"{target_date}T23:59:59Z"
    step = "5m"

    print(f"Building synthetic allocations for {target_date} from Prometheus")
    pod_info = fetch_pod_info()
    cpu_prices, ram_prices = get_node_prices(target_date)
    print(f"  Node CPU prices: {cpu_prices}")
    print(f"  Node RAM prices: {ram_prices}")

    cpu_usage_raw = query_range("container_cpu_usage_seconds_total", start, end, step)
    mem_usage_raw = query_range("container_memory_working_set_bytes", start, end, step)
    print(f"  CPU usage series: {len(cpu_usage_raw)}")
    print(f"  Mem usage series: {len(mem_usage_raw)}")

    cpu_hours = compute_container_cpu_hours(cpu_usage_raw)
    mem_avg = compute_container_mem_avg(mem_usage_raw)

    # Pre-index mem_usage_raw by container for fast node lookup
    container_node = {}
    for r in mem_usage_raw:
        m = r["metric"]
        key = f"{m.get('namespace','')}/{m.get('pod','')}/{m.get('container','')}"
        container_node[key] = m.get("instance", "")
    for r in cpu_usage_raw:
        m = r["metric"]
        key = f"{m.get('namespace','')}/{m.get('pod','')}/{m.get('container','')}"
        if key not in container_node:
            container_node[key] = m.get("instance", "")

    allocs = []
    for key, hours in cpu_hours.items():
        ns, pod, container = key.split("/")
        pod_key = f"{ns}/{pod}"
        info = pod_info.get(pod_key, {
            "namespace": ns, "pod": pod, "node": "",
            "labels": {}, "services": [],
        })
        node = container_node.get(key, info["node"])

        cpu_cost = hours * cpu_prices.get(node, 0) * CNY_RATE
        mem_bytes_avg = mem_avg.get(key, 0)
        # mem_bytes_avg is bytes; multiply by 24h to get byte-hours
        # then convert to GiB-hours for the price (price is per GiB-hr)
        mem_gib_hours = (mem_bytes_avg / (1024**3)) * 24.0
        ram_cost = mem_gib_hours * ram_prices.get(node, 0) * CNY_RATE

        allocs.append({
            "name": f"{ns}/{pod}/{container}",
            "cpuCost": cpu_cost,
            "ramCost": ram_cost,
            "pvCost": 0, "gpuCost": 0, "networkCost": 0,
            "loadBalancerCost": 0, "totalCost": cpu_cost + ram_cost,
            "cpuCoreHours": hours,
            "ramByteHours": mem_bytes_avg * 24.0,
            "cpuCoreRequestAverage": 0, "cpuCoreUsageAverage": 0,
            "ramByteRequestAverage": 0, "ramByteUsageAverage": 0,
            "cpuEfficiency": 0,
            "minutes": 1440,
            "properties": {
                "namespace": ns, "pod": pod, "node": node,
                "container": container,
                "labels": info["labels"],
                "services": info["services"],
            },
        })

    return allocs, pod_info


def main():
    if len(sys.argv) < 2:
        print("Usage: generate_historical_report.py YYYY-MM-DD [output.html]")
        sys.exit(1)
    target_date = sys.argv[1]
    output_path = (
        sys.argv[2] if len(sys.argv) > 2
        else f"/home/ubuntu/opencost-report-chris-eks-{target_date}.html"
    )

    allocs, _pod_info = build_synthetic_allocations(target_date)
    print(f"  Generated {len(allocs)} container allocations")

    # Build pod_resources mapping (Request/Limit) from current Prom state
    pod_resources = fetch_pod_resources(PROM)
    print(f"  Pod request/limit data: {len(pod_resources)} pods")

    # Re-feed resource info from the target date, overriding current snapshot
    target_res = get_resource_snapshot(target_date)
    for k, v in target_res.items():
        if k in pod_resources:
            pod_resources[k]["cpu_request"] = v["cpu_request"]
            pod_resources[k]["cpu_limit"] = v["cpu_limit"]
            pod_resources[k]["mem_request"] = v["mem_request"]
            pod_resources[k]["mem_limit"] = v["mem_limit"]
        else:
            pod_resources[k] = {
                "cpu_request": v["cpu_request"],
                "cpu_limit": v["cpu_limit"],
                "mem_request": v["mem_request"],
                "mem_limit": v["mem_limit"],
                "namespace": v["namespace"],
            }

    window_info = {
        "start": f"{target_date}T00:00:00Z",
        "end": f"{target_date}T23:59:59Z",
    }

    # No idle entry in historical mode
    idle_entry = {"cpuCost": 0, "ramCost": 0, "totalCost": 0}

    node_data, ns_data, svc_data, pod_data, tag_data = aggregate(
        allocs, pod_resources, tag_keys=DEFAULT_TAG_KEYS,
    )
    build_report(
        node_data, ns_data, svc_data, pod_data, tag_data,
        idle_entry, window_info, output_path,
    )
    print(f"\nReport written to: {output_path}")


if __name__ == "__main__":
    main()
