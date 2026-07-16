#!/usr/bin/env python3
"""EKS OpenCost HTML Report Generator

Usage:
    python3 generate_report.py --window 1d --offset 1d --output report.html
"""

import argparse
import json
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
import os
import sys

CNY_RATE = 1.0  # OpenCost AWS China returns CNY values directly (treats CNY price as numeric value)

# Tags/labels used for grouping pod-level costs. Priority order: first match wins.
DEFAULT_TAG_KEYS = ["app", "app.kubernetes.io/name", "app_kubernetes_io_name", "env", "app.kubernetes.io/part-of"]

# Prometheus endpoint for fetching pod request/limit metrics
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")


def get_val(d, key, default=0):
    v = d.get(key, default)
    return v if v is not None else default


def fetch_allocations(window="1d", offset="1d"):
    url = f"http://localhost:9003/allocation/compute?window={window}&offset={offset}&includeIdle=true"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode())

    data = raw.get("data", [])
    allocations = []
    idle_entry = None
    for item in data:
        if isinstance(item, dict):
            for key, alloc in item.items():
                if key == "__idle__" or alloc.get("name") == "__idle__":
                    idle_entry = alloc
                else:
                    allocations.append(alloc)
    return allocations, idle_entry


def fetch_pod_resources(prometheus_url=PROMETHEUS_URL):
    """Fetch current pod-level CPU/memory requests and limits from Prometheus.

    Returns dict: pod_name -> {cpu_request, cpu_limit, mem_request, mem_limit, namespace}
    """
    pod_resources = defaultdict(lambda: {
        "cpu_request": 0.0, "cpu_limit": 0.0,
        "mem_request": 0.0, "mem_limit": 0.0,
        "namespace": ""
    })

    def query(q):
        url = f"{prometheus_url}/api/v1/query?query={urllib.parse.quote(q)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            return data.get("data", {}).get("result", [])
        except Exception as e:
            print(f"Warning: Prometheus query failed: {e}", file=sys.stderr)
            return []

    for r in query("kube_pod_container_resource_requests"):
        m = r.get("metric", {})
        pod = m.get("pod", "")
        if not pod:
            continue
        ns = m.get("namespace", "")
        resource = m.get("resource", "")
        val = float(r.get("value", [0, "0"])[1])
        pod_resources[pod]["namespace"] = ns
        if resource == "cpu":
            pod_resources[pod]["cpu_request"] += val
        elif resource == "memory":
            pod_resources[pod]["mem_request"] += val

    for l in query("kube_pod_container_resource_limits"):
        m = l.get("metric", {})
        pod = m.get("pod", "")
        if not pod:
            continue
        resource = m.get("resource", "")
        val = float(l.get("value", [0, "0"])[1])
        if resource == "cpu":
            pod_resources[pod]["cpu_limit"] += val
        elif resource == "memory":
            pod_resources[pod]["mem_limit"] += val

    return pod_resources


def compute_node_prices(allocations, cny_rate=CNY_RATE):
    """Compute per-node CPU/RAM unit prices from actual allocations.

    Returns dict: node -> {'cpu': price per core-hour, 'ram': price per byte-hour}
    """
    from collections import defaultdict
    cpu_totals = defaultdict(lambda: {"cost": 0.0, "hours": 0.0})
    ram_totals = defaultdict(lambda: {"cost": 0.0, "hours": 0.0})
    for alloc in allocations:
        props = alloc.get("properties", {})
        node = props.get("node", "<no-node>")
        cpu_hours = get_val(alloc, "cpuCoreHours")
        ram_hours = get_val(alloc, "ramByteHours")
        cpu_cost = get_val(alloc, "cpuCost")
        ram_cost = get_val(alloc, "ramCost")
        if cpu_hours > 0:
            cpu_totals[node]["cost"] += cpu_cost
            cpu_totals[node]["hours"] += cpu_hours
        if ram_hours > 0:
            ram_totals[node]["cost"] += ram_cost
            ram_totals[node]["hours"] += ram_hours

    prices = {}
    for node in set(cpu_totals.keys()) | set(ram_totals.keys()):
        cpu_price = cpu_totals[node]["cost"] / cpu_totals[node]["hours"] if cpu_totals[node]["hours"] > 0 else 0
        ram_price = ram_totals[node]["cost"] / ram_totals[node]["hours"] if ram_totals[node]["hours"] > 0 else 0
        prices[node] = {"cpu": cpu_price * cny_rate, "ram": ram_price * cny_rate}
    return prices


def aggregate(allocations, pod_resources, tag_keys=None):
    tag_keys = tag_keys or DEFAULT_TAG_KEYS
    node_prices = compute_node_prices(allocations, cny_rate=CNY_RATE)

    def tag_of(alloc):
        labels = alloc.get("properties", {}).get("labels", {})
        for k in tag_keys:
            # OpenCost may flatten dots to underscores in some configs
            for actual in (k, k.replace(".", "_")):
                if actual in labels and labels[actual]:
                    return labels[actual]
        return "<untagged>"

    node_data = defaultdict(lambda: {"cpuCost": 0, "ramCost": 0, "pvCost": 0, "gpuCost": 0,
                                      "networkCost": 0, "loadBalancerCost": 0, "totalCost": 0,
                                      "cpuCoreHours": 0, "ramByteHours": 0, "cpuEfficiency": 0, "effCount": 0,
                                      "cpuCoreRequestAverage": 0, "cpuCoreUsageAverage": 0,
                                      "ramByteRequestAverage": 0, "ramByteUsageAverage": 0})
    ns_data = defaultdict(lambda: {"cpuCost": 0, "ramCost": 0, "pvCost": 0, "gpuCost": 0,
                                    "networkCost": 0, "loadBalancerCost": 0, "totalCost": 0,
                                    "cpuCoreHours": 0, "ramByteHours": 0, "cpuEfficiency": 0, "effCount": 0})
    svc_data = defaultdict(lambda: {"cpuCost": 0, "ramCost": 0, "pvCost": 0, "gpuCost": 0,
                                     "networkCost": 0, "loadBalancerCost": 0, "totalCost": 0,
                                     "cpuCoreHours": 0, "ramByteHours": 0})
    pod_data = defaultdict(lambda: {"cpuCost": 0, "ramCost": 0, "pvCost": 0, "gpuCost": 0,
                                     "networkCost": 0, "loadBalancerCost": 0, "totalCost": 0,
                                     "cpuCoreHours": 0, "ramByteHours": 0, "cpuEfficiency": 0,
                                     "effCount": 0, "namespace": "", "node": ""})
    tag_data = defaultdict(lambda: {"cpuCost": 0, "ramCost": 0, "pvCost": 0, "gpuCost": 0,
                                     "networkCost": 0, "loadBalancerCost": 0, "totalCost": 0,
                                     "cpuCoreHours": 0, "ramByteHours": 0, "podCount": 0,
                                     "pods": set(), "cpuEfficiency": 0, "effCount": 0,
                                     "cpuRequest": 0.0, "cpuLimit": 0.0,
                                     "memRequest": 0.0, "memLimit": 0.0,
                                     "cpuReqCost": 0.0, "cpuLimCost": 0.0,
                                     "memReqCost": 0.0, "memLimCost": 0.0})

    for alloc in allocations:
        props = alloc.get("properties", {})
        node = props.get("node", "<no-node>")
        namespace = props.get("namespace", "unknown")
        pod = props.get("pod", "unknown")
        services = props.get("services", [])
        minutes = get_val(alloc, "minutes", 1440)
        hours = minutes / 60.0

        vals = {k: get_val(alloc, k) for k in
                ["cpuCost", "ramCost", "pvCost", "gpuCost", "networkCost",
                 "loadBalancerCost", "totalCost", "cpuCoreHours", "ramByteHours"]}
        cpu_eff = get_val(alloc, "cpuEfficiency")
        cpu_req = get_val(alloc, "cpuCoreRequestAverage")
        cpu_use = get_val(alloc, "cpuCoreUsageAverage")
        ram_req = get_val(alloc, "ramByteRequestAverage")
        ram_use = get_val(alloc, "ramByteUsageAverage")

        pod_res = pod_resources.get(pod, {})
        pod_cpu_req = pod_res.get("cpu_request", 0.0)
        pod_cpu_lim = pod_res.get("cpu_limit", 0.0)
        pod_mem_req = pod_res.get("mem_request", 0.0)
        pod_mem_lim = pod_res.get("mem_limit", 0.0)

        # Estimated cost if pod ran at full request or limit for the window duration
        price = node_prices.get(node, {"cpu": 0, "ram": 0})
        cpu_req_cost = pod_cpu_req * hours * price["cpu"]
        cpu_lim_cost = pod_cpu_lim * hours * price["cpu"]
        mem_req_cost = pod_mem_req * hours * price["ram"]
        mem_lim_cost = pod_mem_lim * hours * price["ram"]

        tag = tag_of(alloc)
        td = tag_data[tag]
        for k, v in vals.items():
            td[k] += v
        td["podCount"] += 1
        td["pods"].add(f"{namespace}/{pod}")
        td["cpuEfficiency"] += cpu_eff
        td["effCount"] += 1
        td["cpuRequest"] += pod_cpu_req
        td["cpuLimit"] += pod_cpu_lim
        td["memRequest"] += pod_mem_req
        td["memLimit"] += pod_mem_lim
        td["cpuReqCost"] += cpu_req_cost
        td["cpuLimCost"] += cpu_lim_cost
        td["memReqCost"] += mem_req_cost
        td["memLimCost"] += mem_lim_cost

        # Node
        nd = node_data[node]
        for k, v in vals.items():
            nd[k] += v
        nd["cpuEfficiency"] += cpu_eff
        nd["effCount"] += 1
        nd["cpuCoreRequestAverage"] += cpu_req
        nd["cpuCoreUsageAverage"] += cpu_use
        nd["ramByteRequestAverage"] += ram_req
        nd["ramByteUsageAverage"] += ram_use

        # Namespace
        nsd = ns_data[namespace]
        for k, v in vals.items():
            nsd[k] += v
        nsd["cpuEfficiency"] += cpu_eff
        nsd["effCount"] += 1

        # Service
        svc_names = services if services else ["<none>"]
        for svc in svc_names:
            sd = svc_data[svc]
            for k, v in vals.items():
                sd[k] += v

        # Pod
        pod_key = f"{namespace}/{pod}"
        pd = pod_data[pod_key]
        for k, v in vals.items():
            pd[k] += v
        pd["cpuEfficiency"] += cpu_eff
        pd["effCount"] += 1
        pd["namespace"] = namespace
        pd["node"] = node

    return node_data, ns_data, svc_data, pod_data, tag_data


def build_report(node_data, ns_data, svc_data, pod_data, tag_data, idle_entry, window_info, output_path):
    start = window_info.get("start", "N/A")
    end = window_info.get("end", "N/A")
    # Format: 2026-05-26T00:00:00Z -> 2026-05-26 00:00 UTC
    start_fmt = start.replace("T", " ").replace("Z", " UTC") if "T" in start else start
    end_fmt = end.replace("T", " ").replace("Z", " UTC") if "T" in end else end
    total = sum(d["totalCost"] for d in node_data.values() if d.get("_is_real_node", True))

    # Native idle cost from OpenCost __idle__ entry
    idle_cpu = get_val(idle_entry, "cpuCost", 0)
    idle_ram = get_val(idle_entry, "ramCost", 0)
    idle_total_native = get_val(idle_entry, "totalCost", 0)

    # Sort and prepare rows
    nodes = []
    for n, d in sorted(node_data.items(), key=lambda x: x[1]["totalCost"], reverse=True):
        if n == "<no-node>":
            continue
        eff = d["cpuEfficiency"] / d["effCount"] * 100 if d["effCount"] > 0 else 0
        cpu_req = d["cpuCoreRequestAverage"]
        cpu_use = d["cpuCoreUsageAverage"]
        ram_req = d["ramByteRequestAverage"]
        ram_use = d["ramByteUsageAverage"]
        # Calculate idle cost: proportion of wasted request capacity
        cpu_idle_ratio = max(0, (cpu_req - cpu_use) / cpu_req) if cpu_req > 0 else 0
        ram_idle_ratio = max(0, (ram_req - ram_use) / ram_req) if ram_req > 0 else 0
        cpu_idle_cost = d["cpuCost"] * CNY_RATE * cpu_idle_ratio
        ram_idle_cost = d["ramCost"] * CNY_RATE * ram_idle_ratio
        idle_total = cpu_idle_cost + ram_idle_cost
        nodes.append({
            "name": n, "cpuCost": d["cpuCost"]*CNY_RATE, "ramCost": d["ramCost"]*CNY_RATE,
            "pvCost": d["pvCost"]*CNY_RATE, "lbCost": d["loadBalancerCost"]*CNY_RATE,
            "totalCost": d["totalCost"]*CNY_RATE, "efficiency": eff,
            "cpuIdleCost": cpu_idle_cost, "ramIdleCost": ram_idle_cost, "idleTotal": idle_total,
            "cpuReq": cpu_req, "cpuUse": cpu_use, "ramReqGB": ram_req/(1024**3), "ramUseGB": ram_use/(1024**3)
        })

    namespaces = []
    total_ns = sum(d["totalCost"] for d in ns_data.values())
    for ns, d in sorted(ns_data.items(), key=lambda x: x[1]["totalCost"], reverse=True):
        pct = d["totalCost"] / total_ns * 100 if total_ns > 0 else 0
        eff = d["cpuEfficiency"] / d["effCount"] * 100 if d["effCount"] > 0 else 0
        namespaces.append({
            "name": ns, "cpuCost": d["cpuCost"]*CNY_RATE, "ramCost": d["ramCost"]*CNY_RATE,
            "pvCost": d["pvCost"]*CNY_RATE, "lbCost": d["loadBalancerCost"]*CNY_RATE,
            "totalCost": d["totalCost"]*CNY_RATE, "pct": pct, "efficiency": eff
        })

    services = []
    total_svc = sum(d["totalCost"] for d in svc_data.values())
    for svc, d in sorted(svc_data.items(), key=lambda x: x[1]["totalCost"], reverse=True)[:15]:
        pct = d["totalCost"] / total_svc * 100 if total_svc > 0 else 0
        services.append({
            "name": svc, "cpuCost": d["cpuCost"]*CNY_RATE, "ramCost": d["ramCost"]*CNY_RATE,
            "lbCost": d["loadBalancerCost"]*CNY_RATE, "totalCost": d["totalCost"]*CNY_RATE, "pct": pct
        })

    pods = []
    for pod, d in sorted(pod_data.items(), key=lambda x: x[1]["totalCost"], reverse=True)[:20]:
        pods.append({
            "name": pod, "cpuCost": d["cpuCost"]*CNY_RATE, "ramCost": d["ramCost"]*CNY_RATE,
            "lbCost": d["loadBalancerCost"]*CNY_RATE, "totalCost": d["totalCost"]*CNY_RATE
        })

    tags = []
    total_tag = sum(d["totalCost"] for d in tag_data.values())
    for tag, d in sorted(tag_data.items(), key=lambda x: x[1]["totalCost"], reverse=True):
        pct = d["totalCost"] / total_tag * 100 if total_tag > 0 else 0
        eff = d["cpuEfficiency"] / d["effCount"] * 100 if d["effCount"] > 0 else 0
        tags.append({
            "name": tag, "cpuCost": d["cpuCost"]*CNY_RATE, "ramCost": d["ramCost"]*CNY_RATE,
            "pvCost": d["pvCost"]*CNY_RATE, "lbCost": d["loadBalancerCost"]*CNY_RATE,
            "totalCost": d["totalCost"]*CNY_RATE, "pct": pct, "efficiency": eff,
            "podCount": len(d["pods"]), "samplePods": sorted(d["pods"])[:3],
            "cpuRequest": d["cpuRequest"], "cpuLimit": d["cpuLimit"],
            "memRequest": d["memRequest"] / (1024**3), "memLimit": d["memLimit"] / (1024**3),
            "cpuReqCost": d["cpuReqCost"], "cpuLimCost": d["cpuLimCost"],
            "memReqCost": d["memReqCost"], "memLimCost": d["memLimCost"]
        })

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EKS OpenCost 成本分析報告</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #0f172a; color: #e2e8f0; line-height: 1.6;
  }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
  header {{
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #334155; border-radius: 16px;
    padding: 32px; margin-bottom: 24px; text-align: center;
  }}
  header h1 {{ font-size: 28px; color: #f8fafc; margin-bottom: 8px; }}
  header .subtitle {{ color: #94a3b8; font-size: 14px; }}
  header .date {{ color: #60a5fa; font-size: 16px; margin-top: 12px; font-weight: 600; }}
  .summary-cards {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px;
    margin-bottom: 24px;
  }}
  .card {{
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 20px; text-align: center; transition: transform 0.2s;
  }}
  .card:hover {{ transform: translateY(-2px); border-color: #60a5fa; }}
  .card .label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .value {{ font-size: 28px; font-weight: 700; color: #f8fafc; margin: 8px 0; }}
  .card .sub {{ font-size: 13px; color: #64748b; }}
  .card.total .value {{ color: #34d399; }}
  .card.cpu .value {{ color: #fbbf24; }}
  .card.ram .value {{ color: #a78bfa; }}
  .card.lb .value {{ color: #f472b6; }}
  .card.idle .value {{ color: #fb923c; }}
  .section {{
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 24px; margin-bottom: 24px;
  }}
  .section h2 {{
    font-size: 18px; color: #f8fafc; margin-bottom: 16px; padding-bottom: 12px;
    border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 8px;
  }}
  .section h2::before {{
    content: ''; display: inline-block; width: 4px; height: 20px;
    background: #60a5fa; border-radius: 2px;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{
    text-align: left; padding: 12px 10px; color: #94a3b8; font-weight: 600;
    text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px;
    border-bottom: 1px solid #334155;
  }}
  td {{ padding: 10px; border-bottom: 1px solid #1e293b; color: #cbd5e1; }}
  tr:hover td {{ background: #252f47; }}
  tr:last-child td {{ border-bottom: none; }}
  .text-right {{ text-align: right; }}
  .text-center {{ text-align: center; }}
  .bar-bg {{ background: #334155; border-radius: 4px; height: 8px; width: 100%; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.5s ease; }}
  .bar-green {{ background: #34d399; }}
  .bar-yellow {{ background: #fbbf24; }}
  .bar-purple {{ background: #a78bfa; }}
  .bar-pink {{ background: #f472b6; }}
  .bar-blue {{ background: #60a5fa; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600;
  }}
  .badge-low {{ background: #dc2626; color: #fff; }}
  .badge-mid {{ background: #f59e0b; color: #000; }}
  .badge-high {{ background: #22c55e; color: #fff; }}
  .badge-info {{ background: #3b82f6; color: #fff; }}
  .chart-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 24px;
    margin-bottom: 24px;
  }}
  .chart-container {{
    background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    padding: 20px; position: relative; height: 320px;
  }}
  .chart-container h3 {{ font-size: 14px; color: #94a3b8; margin-bottom: 12px; }}
  .alert {{
    background: rgba(220, 38, 38, 0.1); border: 1px solid #dc2626;
    border-radius: 8px; padding: 16px; margin-bottom: 24px;
  }}
  .alert-title {{ color: #fca5a5; font-weight: 600; margin-bottom: 4px; }}
  .alert-text {{ color: #f87171; font-size: 13px; }}
  .insights {{
    background: rgba(96, 165, 250, 0.05); border: 1px solid #3b82f6;
    border-radius: 8px; padding: 16px;
  }}
  .insights-title {{ color: #93c5fd; font-weight: 600; margin-bottom: 8px; }}
  .insights ul {{ list-style: none; }}
  .insights li {{
    color: #bfdbfe; font-size: 13px; padding: 4px 0;
    padding-left: 16px; position: relative;
  }}
  .insights li::before {{
    content: '▸'; position: absolute; left: 0; color: #60a5fa;
  }}
  @media (max-width: 768px) {{
    .chart-grid {{ grid-template-columns: 1fr; }}
    .summary-cards {{ grid-template-columns: repeat(2, 1fr); }}
    table {{ font-size: 12px; }}
    td, th {{ padding: 8px 6px; }}
  }}
</style>
</head>
<body>
<div class="container">
<header>
  <h1>☁️ EKS OpenCost 成本分析報告</h1>
  <div class="subtitle">OpenCost v1.118.0 · AWS China (CNY)</div>
  <div class="date">📅 {start_fmt} ~ {end_fmt}</div>
</header>

<div class="summary-cards">
  <div class="card total">
    <div class="label">日總成本</div>
    <div class="value">¥{total*CNY_RATE:.2f}</div>
    <div class="sub">CNY</div>
  </div>
  <div class="card cpu">
    <div class="label">CPU 成本</div>
    <div class="value">¥{sum(d['cpuCost'] for d in node_data.values())*CNY_RATE:.2f}</div>
    <div class="sub">{(sum(d['cpuCost'] for d in node_data.values())/total*100) if total>0 else 0:.1f}%</div>
  </div>
  <div class="card ram">
    <div class="label">RAM 成本</div>
    <div class="value">¥{sum(d['ramCost'] for d in node_data.values())*CNY_RATE:.2f}</div>
    <div class="sub">{(sum(d['ramCost'] for d in node_data.values())/total*100) if total>0 else 0:.1f}%</div>
  </div>
  <div class="card lb">
    <div class="label">LB 成本</div>
    <div class="value">¥{sum(d['loadBalancerCost'] for d in node_data.values())*CNY_RATE:.2f}</div>
    <div class="sub">{(sum(d['loadBalancerCost'] for d in node_data.values())/total*100) if total>0 else 0:.1f}%</div>
  </div>
  <div class="card idle">
    <div class="label">Idle 成本 (OpenCost)</div>
    <div class="value">¥{idle_total_native*CNY_RATE:.2f}</div>
    <div class="sub">CPU ¥{idle_cpu*CNY_RATE:.2f} + RAM ¥{idle_ram*CNY_RATE:.2f}</div>
  </div>
  <div class="card">
    <div class="label">PV 成本</div>
    <div class="value">¥{sum(d['pvCost'] for d in node_data.values())*CNY_RATE:.2f}</div>
    <div class="sub">{(sum(d['pvCost'] for d in node_data.values())/total*100) if total>0 else 0:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">Pod Tags</div>
    <div class="value">{len(tags)}</div>
    <div class="sub">groups</div>
  </div>
  </div>

<div class="alert">
  <div class="alert-title">⚠️ 低效率警告</div>
  <div class="alert-text">整體 CPU 效率極低，大部分資源 request 遠高於實際使用。請檢查各 namespace 的 resource request 設定。</div>
</div>

<div class="chart-grid">
  <div class="chart-container">
    <h3>Namespace 成本分佈</h3>
    <canvas id="nsChart"></canvas>
  </div>
  <div class="chart-container">
    <h3>Tag 成本分佈</h3>
    <canvas id="tagChart"></canvas>
  </div>
</div>

<div class="section">
<h2>1. Node 維度成本</h2>
<table>
  <thead><tr><th>Node</th><th class="text-right">CPU</th><th class="text-right">RAM</th><th class="text-right">PV</th><th class="text-right">LB</th><th class="text-right">總計</th><th class="text-right">CPU Idle</th><th class="text-right">RAM Idle</th><th class="text-right">Idle 合計</th><th class="text-center">CPU效率</th></tr></thead>
  <tbody>
"""

    for n in nodes:
        badge = "badge-low" if n["efficiency"] < 5 else "badge-mid" if n["efficiency"] < 50 else "badge-high"
        html += f"""      <tr><td>{n['name']}</td><td class="text-right">¥{n['cpuCost']:.2f}</td><td class="text-right">¥{n['ramCost']:.2f}</td><td class="text-right">¥{n['pvCost']:.2f}</td><td class="text-right">¥{n['lbCost']:.2f}</td><td class="text-right"><strong>¥{n['totalCost']:.2f}</strong></td><td class="text-right">¥{n['cpuIdleCost']:.2f}</td><td class="text-right">¥{n['ramIdleCost']:.2f}</td><td class="text-right"><strong>¥{n['idleTotal']:.2f}</strong></td><td class="text-center"><span class="badge {badge}">{n['efficiency']:.1f}%</span></td></tr>\n"""

    html += """    </tbody>
  </table>
</div>

<div class="section">
  <h2>2. Namespace 維度成本</h2>
  <table>
    <thead><tr><th>Namespace</th><th class="text-right">CPU</th><th class="text-right">RAM</th><th class="text-right">PV</th><th class="text-right">LB</th><th class="text-right">總計</th><th class="text-center">佔比</th><th class="text-center">CPU效率</th></tr></thead>
    <tbody>
"""

    for ns in namespaces:
        badge = "badge-low" if ns["efficiency"] < 5 else "badge-mid" if ns["efficiency"] < 50 else "badge-high"
        html += f"""      <tr><td>{ns['name']}</td><td class="text-right">¥{ns['cpuCost']:.2f}</td><td class="text-right">¥{ns['ramCost']:.2f}</td><td class="text-right">¥{ns['pvCost']:.2f}</td><td class="text-right">¥{ns['lbCost']:.2f}</td><td class="text-right"><strong>¥{ns['totalCost']:.2f}</strong></td><td class="text-center"><div class="bar-bg"><div class="bar-fill bar-green" style="width:{ns['pct']:.1f}%"></div></div><span style="font-size:11px">{ns['pct']:.1f}%</span></td><td class="text-center"><span class="badge {badge}">{ns['efficiency']:.1f}%</span></td></tr>\n"""

    html += """    </tbody>
  </table>
</div>

<div class="section">
  <h2>3. Pod Tag 維度成本</h2>
  <p style="color:#94a3b8; font-size:12px; margin-bottom:12px;">
    CPU / Memory Request & Limit 來自 Prometheus kube-state-metrics 即時查詢；若無法連線則顯示 0。<br>
    CPU Req Cost / Lim Cost 與 Mem Req Cost / Lim Cost 是根據該 Pod 所在 Node 的實際單價（由 OpenCost 實際成本反推）乘以 Request/Limit 量與時間窗口估算的理論成本。
  </p>
  <table>
    <thead><tr><th>Tag</th><th class="text-center">Pods</th><th class="text-right">CPU Req</th><th class="text-right">CPU Lim</th><th class="text-right">Mem Req</th><th class="text-right">Mem Lim</th><th class="text-right">CPU Req Cost</th><th class="text-right">CPU Lim Cost</th><th class="text-right">Mem Req Cost</th><th class="text-right">Mem Lim Cost</th><th class="text-right">實際 CPU 成本</th><th class="text-right">實際 RAM 成本</th><th class="text-right">總計</th><th class="text-center">佔比</th><th class="text-center">CPU效率</th><th>Sample Pods</th></tr></thead>
    <tbody>
"""

    for t in tags:
        badge = "badge-low" if t["efficiency"] < 5 else "badge-mid" if t["efficiency"] < 50 else "badge-high"
        samples = ", ".join(t["samplePods"]) if t["samplePods"] else "-"
        tag_name = t['name'] or "<untagged>"
        html += f"""      <tr><td>{tag_name}</td><td class="text-center">{t['podCount']}</td><td class="text-right">{t['cpuRequest']:.3f}</td><td class="text-right">{t['cpuLimit']:.3f}</td><td class="text-right">{t['memRequest']:.2f}Gi</td><td class="text-right">{t['memLimit']:.2f}Gi</td><td class="text-right">¥{t['cpuReqCost']:.2f}</td><td class="text-right">¥{t['cpuLimCost']:.2f}</td><td class="text-right">¥{t['memReqCost']:.2f}</td><td class="text-right">¥{t['memLimCost']:.2f}</td><td class="text-right">¥{t['cpuCost']:.2f}</td><td class="text-right">¥{t['ramCost']:.2f}</td><td class="text-right"><strong>¥{t['totalCost']:.2f}</strong></td><td class="text-center"><div class="bar-bg"><div class="bar-fill bar-blue" style="width:{t['pct']:.1f}%"></div></div><span style="font-size:11px">{t['pct']:.1f}%</span></td><td class="text-center"><span class="badge {badge}">{t['efficiency']:.1f}%</span></td><td style="font-size:11px;color:#94a3b8">{samples}</td></tr>\n"""

    html += """    </tbody>
  </table>
</div>

<div class="section">
  <h2>4. Service 維度成本 (Top 15)</h2>
  <table>
    <thead><tr><th>Service</th><th class="text-right">CPU</th><th class="text-right">RAM</th><th class="text-right">LB</th><th class="text-right">總計</th><th class="text-center">佔比</th></tr></thead>
    <tbody>
"""

    for svc in services:
        html += f"""      <tr><td>{svc['name']}</td><td class="text-right">¥{svc['cpuCost']:.2f}</td><td class="text-right">¥{svc['ramCost']:.2f}</td><td class="text-right">¥{svc['lbCost']:.2f}</td><td class="text-right"><strong>¥{svc['totalCost']:.2f}</strong></td><td class="text-center"><span class="badge badge-info">{svc['pct']:.1f}%</span></td></tr>\n"""

    html += """    </tbody>
  </table>
</div>

<div class="section">
  <h2>5. Pod 維度成本 (Top 20)</h2>
  <table>
    <thead><tr><th>Pod</th><th class="text-right">CPU</th><th class="text-right">RAM</th><th class="text-right">LB</th><th class="text-right">總計</th></tr></thead>
    <tbody>
"""

    for pod in pods:
        html += f"""      <tr><td>{pod['name']}</td><td class="text-right">¥{pod['cpuCost']:.2f}</td><td class="text-right">¥{pod['ramCost']:.2f}</td><td class="text-right">¥{pod['lbCost']:.2f}</td><td class="text-right"><strong>¥{pod['totalCost']:.2f}</strong></td></tr>\n"""

    ns_labels = [n['name'] for n in namespaces[:6]]
    ns_data_vals = [round(n['totalCost'], 2) for n in namespaces[:6]]
    tag_labels = [t['name'] for t in tags[:6]]
    tag_data_vals = [round(t['totalCost'], 2) for t in tags[:6]]

    html += f"""    </tbody>
  </table>
</div>

<div class="section">
  <h2>6. 洞察與建議</h2>
  <div class="insights">
    <div class="insights-title">💡 優化建議</div>
    <ul>
      <li><strong>CPU 效率極低：</strong>多數 namespace CPU 效率 &lt;5%，建議調整 resource request</li>
      <li><strong>LB 成本檢查：</strong>確認 ELB 是否必要，或改用 ClusterIP + Ingress 降低成本</li>
      <li><strong>DaemonSet 成本：</strong>cloudwatch-agent、fluent-bit 等 DaemonSet 佔比高，確認配置必要性</li>
      <li><strong>Prometheus 副本：</strong>檢查是否需要多副本 HA 配置</li>
      <li><strong>節點規模：</strong>整體 CPU 效率低，考慮縮減節點數或改用更小實例</li>
    </ul>
  </div>
</div>

</div>

<script>
const nsCtx = document.getElementById('nsChart').getContext('2d');
new Chart(nsCtx, {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(ns_labels)},
    datasets: [{{
      data: {json.dumps(ns_data_vals)},
      backgroundColor: ['#34d399', '#60a5fa', '#a78bfa', '#f472b6', '#fbbf24', '#3b82f6'],
      borderWidth: 0
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'right', labels: {{ color: '#94a3b8', font: {{ size: 11 }} }} }} }}
  }}
}});

const tagCtx = document.getElementById('tagChart').getContext('2d');
new Chart(tagCtx, {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(tag_labels)},
    datasets: [{{
      data: {json.dumps(tag_data_vals)},
      backgroundColor: ['#f472b6', '#a78bfa', '#fbbf24', '#34d399', '#60a5fa', '#3b82f6'],
      borderWidth: 0
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'right', labels: {{ color: '#94a3b8', font: {{ size: 11 }} }} }} }}
  }}
}});
</script>
</body>
</html>
"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Report written to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description='EKS OpenCost HTML Report Generator')
    parser.add_argument('--window', default='1d', help='Time window (e.g. 1d, 7d)')
    parser.add_argument('--offset', default='1d', help='Offset from now (e.g. 1d = yesterday)')
    parser.add_argument('--output', default=None, help='Output HTML file path')
    parser.add_argument('--cny-rate', type=float, default=7.25, help='USD to CNY rate')
    parser.add_argument('--tags', default=None, help='Comma-separated pod label keys used to group costs (default: app,app.kubernetes.io/name,app_kubernetes_io_name,env,app.kubernetes.io/part-of)')
    parser.add_argument('--prometheus-url', default=os.environ.get('PROMETHEUS_URL', 'http://localhost:9090'), help='Prometheus URL for fetching pod request/limit metrics')
    args = parser.parse_args()

    global CNY_RATE
    CNY_RATE = args.cny_rate

    global PROMETHEUS_URL
    PROMETHEUS_URL = args.prometheus_url

    tag_keys = [k.strip() for k in args.tags.split(",") if k.strip()] if args.tags else DEFAULT_TAG_KEYS

    if args.output is None:
        date_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        args.output = f"/home/ubuntu/opencost-report-{date_str}.html"

    print(f"Fetching allocations: window={args.window}, offset={args.offset}")
    allocations, idle_entry = fetch_allocations(args.window, args.offset)
    print(f"Got {len(allocations)} allocation entries")
    if idle_entry:
        print(f"Idle entry: cpuCost={idle_entry.get('cpuCost', 0):.4f}, ramCost={idle_entry.get('ramCost', 0):.4f}, totalCost={idle_entry.get('totalCost', 0):.4f}")

    print(f"Fetching pod resource requests/limits from Prometheus: {PROMETHEUS_URL}")
    pod_resources = fetch_pod_resources(PROMETHEUS_URL)
    print(f"Got resource data for {len(pod_resources)} pods")

    window_info = allocations[0].get("window", {}) if allocations else {}

    node_data, ns_data, svc_data, pod_data, tag_data = aggregate(allocations, pod_resources, tag_keys=tag_keys)
    build_report(node_data, ns_data, svc_data, pod_data, tag_data, idle_entry, window_info, args.output)


if __name__ == '__main__':
    main()
