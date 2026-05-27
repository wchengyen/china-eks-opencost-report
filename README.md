# EKS 成本報告 - OpenCost 部署指南

## 目錄

- [系統概述](#系統概述)
- [前置條件](#前置條件)
- [部署步驟](#部署步驟)
- [配置說明](#配置說明)
- [貨幣設定 (CNY)](#貨幣設定-cny)
- [定價信息](#定價信息)
- [使用方法](#使用方法)
- [故障排查](#故障排查)

---

## 系統概述

OpenCost 是一個開源的 Kubernetes 成本監控工具，支持：

- 實時成本監控和分配
- 按 Namespace、Pod、Node 等維度分析成本
- CPU 和 RAM 成本分離
- Idle Cost（空閒資源成本）追蹤

### 當前環境

| 項目 | 值 |
|------|-----|
| 集群名稱 | chris-eks |
| 區域 | cn-northwest-1（中國寧夏） |
| 節點類型 | t3a.large（2 vCPU, 8GB） |
| 節點數量 | 3 |
| OpenCost 版本 | 1.118.0 |

---

## 前置條件

- Kubernetes 集群已部署 Prometheus
- kubectl 已配置並可訪問集群
- 節點已啟用 CloudWatch 監控

---

## 部署步驟

### 方法一：一鍵部署（推薦）

```bash
kubectl apply -f opencost-all-in-one.yaml
```

### 方法二：分步部署

```bash
kubectl apply -f opencost-namespace.yaml
kubectl apply -f opencost-sa.yaml
kubectl apply -f opencost-clusterrole.yaml
kubectl apply -f opencost-clusterrolebinding.yaml
kubectl apply -f opencost-service.yaml
kubectl apply -f opencost-cny-patch-configmap.yaml
kubectl apply -f opencost-deployment.yaml
```

### 驗證部署

```bash
kubectl get pods -n opencost
kubectl get svc -n opencost
```

---

## 配置說明

### 核心環境變量

| 變量名 | 說明 | 當前值 |
|--------|------|--------|
| PROMETHEUS_SERVER_ENDPOINT | Prometheus 服務地址 | http://prometheus.monitoring.svc:9090 |
| AWS_PRICING_URL | AWS 中國區定價 API | https://pricing.cn-northwest-1.amazonaws.com.cn/... |
| CLUSTER_ID | 集群標識 | cluster-one |
| IDLE_ENABLED | 啟用空閒成本分離 | true |

### Prometheus 配置要求

OpenCost 依賴 Prometheus 抓取以下指標才能正確計算成本：

| Job | Target | 必需指標 | 用途 |
|-----|--------|---------|------|
| opencost | opencost.opencost.svc:9003 | node_cpu_hourly_cost<br>node_ram_hourly_cost<br>node_total_hourly_cost | 節點定價數據源 |
| kubernetes-cadvisor | kubelet /metrics/cadvisor | container_cpu_usage_seconds_total<br>container_memory_working_set_bytes | 容器實際使用量 |

> **重要：** 若未配置上述 scrape job，OpenCost 將只能顯示 Disk 資產，Node 資產會缺失，且 allocation 成本會使用 fallback 默認價格（¥0.0316/vCPU）而非 AWS 實際價格（¥0.114/vCPU）。詳見 `prometheus-config.yaml`。

### 端口說明

| 端口 | 用途 |
|------|------|
| 9003 | OpenCost API |
| 9090 | OpenCost UI |
| 8081 | MCP 接口 |

---

## 貨幣設定 (CNY)

### 背景

OpenCost 1.118.0 的 UI 將貨幣 **USD** 硬編碼在 JavaScript 檔案中。API (`/allocation/compute`) 返回純數字，不含貨幣符號。貨幣顯示完全由 UI 層處理。

### 修改方案

由於容器使用 `readOnlyRootFilesystem: true`，無法直接修改 JS 檔案。採用以下方案：

1. **initContainer** (`patch-currency`): 以 root 運行，將 JS 中的 `"USD"` 替換為 `"CNY"`，輸出到 `emptyDir`
2. **主容器** (`opencost-ui`): 掛載 patch 後的 JS 檔案，並使用自定義啟動腳本執行 envsubst 生成 nginx 配置
3. **ConfigMap** (`cny-patch-root-v3`): 包含 patch 腳本

### 關鍵配置

```yaml
# Deployment 中的 initContainer
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

# opencost-ui 容器
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

### 注意事項

- **無需匯率轉換**：僅修改幣值符號，數值保持不變
- **API 不受影響**：`/allocation/compute` 仍返回純數字
- **UI 顯示變更**：Web 界面上的貨幣符號從 `$` 變為 `CNY`

### 驗證貨幣修改

```bash
# 檢查 UI JS 檔案中的貨幣
POD=$(kubectl get pod -n opencost -l app=opencost -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n opencost "$POD" -c opencost-ui -- grep -c '"CNY"' /var/www/opencost-ui.5f027c83.js
# 預期輸出: 2

kubectl exec -n opencost "$POD" -c opencost-ui -- grep -c '"USD"' /var/www/opencost-ui.5f027c83.js
# 預期輸出: 0 (或極少量，如註釋中)
```

---

## 定價信息

### t3a.large 節點定價（中國寧夏區）

| 資源 | 單價 | 每小時 | 每天 | 每月 |
|------|------|--------|------|------|
| CPU | ¥0.114/小時/vCPU | ¥0.228 (2vCPU) | ¥5.47 | ¥164.16 |
| RAM | ¥0.015/小時/GB | ¥0.120 (8GB) | ¥2.88 | ¥86.40 |
| **總計** | - | **¥0.345** | **¥8.28** | **¥248.40** |

**費用拆分比例：** CPU ~66% : RAM ~34%（基於 AWS 中國區 OnDemand 定價）

> **注意：** ¥0.345/小時 為 AWS 中國區 t3a.large OnDemand 實際價格。若 OpenCost 顯示 CPU 單價為 ¥0.0316，說明使用了 fallback 默認價格，請檢查 Prometheus 是否已配置抓取 OpenCost 指標。

---

## 使用方法

### 查詢節點成本

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/allocation/compute?window=1d&aggregate=node"
```

### 查詢含 Idle Cost 的成本

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/allocation/compute?window=1d&aggregate=node&idle=true"
```

### 查詢 Namespace 成本

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/allocation/compute?window=7d&aggregate=namespace"
```

### 查詢資產（含 Node / Disk）

```bash
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- "http://localhost:9003/assets?window=1d"
```

### 訪問 Web UI

#### 方式一：通過 NLB (推薦用於外部訪問)

已配置 NLB `opencost-ui-elb`，可直接通過以下地址訪問：

```
http://a86ede210ee1b45cc920f71a045d04e7-a93add39326ec13e.elb.cn-northwest-1.amazonaws.com.cn:9090
```

**注意：** `opencost-ui` 容器必須聲明 `containerPort: 9090`，否則 NLB 無法將流量路由到容器。

#### 方式二：通過端口轉發 (本地調試)

```bash
kubectl port-forward -n opencost svc/opencost 9090:9090
```

然後訪問：http://localhost:9090

---

## 報告生成

本專案包含一個基於 OpenCost API 的 HTML 成本報告生成腳本，針對 AWS 中國區（CNY）優化。

### 快速開始

```bash
# 1. 進入腳本目錄
cd eks-cost-report-cn-skill/scripts/

# 2. 啟動 OpenCost Port-forward（背景執行）
kubectl port-forward svc/opencost 9003:9003 -n opencost &

# 3. 生成昨日報告（AWS China 模式，直接輸出 CNY）
python3 generate_report.py --window 1d --offset 1d --cny-rate 1.0

# 4. 生成指定時間範圍報告
python3 generate_report.py \
  --window "2026-05-26T00:00:00Z,2026-05-27T00:00:00Z" \
  --cny-rate 1.0 \
  --output report-2026-05-26.html
```

### 參數說明

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--window` | `1d` | 時間窗口，支援相對時間 (`1d`, `7d`) 或絕對時間 (`2026-05-26T00:00:00Z,2026-05-27T00:00:00Z`) |
| `--offset` | `1d` | 相對於當前的偏移量（`1d` = 昨天） |
| `--cny-rate` | `7.25` | **AWS China 請設為 `1.0`**（OpenCost 直接輸出 CNY 數值） |
| `--output` | `opencost-report-{date}.html` | 輸出 HTML 檔案路徑 |

### 報告內容

- **總體成本**：實際發生的全部成本（allocated + idle）
- **使用成本**：僅計算被 Pod 實際 request 的部分
- **Idle 成本**：未被使用的閒置資源成本
- **Node / Namespace / Service / Pod** 多維度表格
- Chart.js 圖表（Namespace 分佈、成本組成）

### 已知問題

| 問題 | 說明 | 解決方案 |
|------|------|---------|
| AWS China 貨幣 Bug | OpenCost 讀取 CNY 定價但視為 USD 數值，導致成本膨脹 ~7.25x | 使用 `--cny-rate 1.0` 直接將輸出視為 CNY |
| 數據延遲（1d 窗口） | OpenCost 可能只累積數小時數據，非完整 24 小時 | 檢查 `minutes` 欄位，或使用較長窗口 |
| `<no-node>` 條目 | 未掛載 PVC 會產生極小額 pvCost | 腳本已自動排除 |

### 參考文件

- `references/aws-china-pricing-currency-bug.md` — AWS China 定價貨幣問題
- `references/opencost-data-latency.md` — 數據延遲與部分窗口問題
- `references/node-runtime-vs-window.md` — 節點運行時間 vs 查詢窗口

---

## 故障排查

### Pod 無法啟動

```bash
kubectl logs -n opencost deploy/opencost -c opencost
kubectl describe pod -n opencost -l app=opencost
```

### 無法連接 Prometheus

```bash
kubectl get svc -n monitoring prometheus
```

### 定價數據缺失

```bash
curl -I https://pricing.cn-northwest-1.amazonaws.com.cn/offers/v1.0/cn/AmazonEC2/current/index.json
```

### OpenCost 費用與 EC2 實際費用差異過大

如果 `/allocation` 返回的 CPU 單價為 ¥0.0316（而非 ¥0.114），說明 Prometheus 未抓取 OpenCost 指標：

```bash
# 檢查 Prometheus 是否抓取 opencost
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- 'http://prometheus.monitoring.svc:9090/api/v1/targets' | grep opencost

# 檢查 node_cpu_hourly_cost 指標是否存在
kubectl exec -n opencost deploy/opencost -c opencost -- \
  wget -qO- 'http://prometheus.monitoring.svc:9090/api/v1/query?query=node_cpu_hourly_cost'
```

若結果為 0 條記錄，請更新 Prometheus ConfigMap 並重啟：

```bash
kubectl apply -f prometheus-config.yaml -n monitoring
kubectl rollout restart deployment/prometheus -n monitoring
```

### 貨幣修改未生效

1. 確認 ConfigMap 已創建：
   ```bash
   kubectl get configmap cny-patch-root-v3 -n opencost
   ```

2. 確認 Pod 已使用新配置：
   ```bash
   kubectl get pod -n opencost -l app=opencost -o yaml | grep -A5 "cny-patch"
   ```

3. 手動重啟 Deployment：
   ```bash
   kubectl rollout restart deployment opencost -n opencost
   ```

---

## 文件清單

| 文件 | 說明 |
|------|------|
| `opencost-all-in-one.yaml` | 完整部署文件（推薦，含 CNY patch） |
| `opencost-namespace.yaml` | Namespace 定義 |
| `opencost-deployment.yaml` | Deployment 配置（含 CNY patch） |
| `opencost-cny-patch-configmap.yaml` | CNY Patch ConfigMap |
| `opencost-service.yaml` | Service 定義 |
| `opencost-sa.yaml` | ServiceAccount |
| `opencost-clusterrole.yaml` | ClusterRole 權限 |
| `opencost-clusterrolebinding.yaml` | ClusterRoleBinding |
| `prometheus-config.yaml` | **Prometheus Scrape 配置（含 opencost + cadvisor）** |
| `opencost-full.yaml` | 完整備份（含運行時狀態） |

---

## 更新歷史

| 日期 | 變更 |
|------|------|
| 2026-05-25 | **新增 CNY 貨幣 patch**（無需匯率轉換）<br>修正定價信息（¥0.097 → ¥0.345/小時）<br>新增 prometheus-config.yaml（含 opencost + cadvisor scrape）<br>移除不必要的 CLOUD_PROVIDER_API_KEY<br>更新故障排查：添加成本差異排查說明 |
| 2026-05-22 | 啟用 IDLE_ENABLED 環境變量，支持空閒成本分離 |
| 2025-05-13 | 初始部署 OpenCost 1.118.0 |

---

文檔生成時間: 2026-05-25 |
[OpenCost 官網](https://opencost.io)
