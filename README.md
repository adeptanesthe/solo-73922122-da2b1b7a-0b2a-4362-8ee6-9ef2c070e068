# Panel Re-inspection Ranking API

玻璃板来不及全检时，用已检板拟合“缺陷分 → 缺陷率”的非降曲线，再按
`缺陷率 × 漏检损失` 从未检板中排出优先复检名单；并提供后验核查接口，
计算质检报告中若干档玻璃板的缺陷率上限**同时成立**的后验概率。

## 启动

```bash
docker compose up --build -d
# http://127.0.0.1:8000/docs
```

服务为单进程内存状态（最多 5 万块板），`docker-compose.yml` 因此固定
`--workers 1`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/panels` | 批量登记板：`panel_id`、`score`(0–255)、`loss`(正整数)、可选 `inspection_result`(0/1)。整批校验通过才生效。 |
| `PUT` | `/panels/{id}/inspection` | 补录或更正单板实检结果，立即重拟合。 |
| `GET` | `/queue?k=N` | 未检板中按预期损失取前 K（1–20），返回板号、分值、缺陷率、预期损失。 |
| `GET` | `/calibration` | 拟合出的各分值与缺陷率（含回退来源），供核对。 |
| `POST` | `/audit` | 后验核查：1–12 个**互异**缺陷分及各自整数百分比上限，返回所有上限同时成立的后验概率与校准版号。 |
| `GET` | `/panels/{id}` | 单板信息（含实检状态）。 |
| `GET` | `/health` | 计数与校准版本。 |

无任何实检数据时 `GET /queue` 返回 `409 calibration_missing`，
`POST /audit` 返回 `409 evidence_insufficient`（证据不足，无法形成后验）。

## 算法

* **缺陷率拟合**：把已检板按分值聚合为 `(缺陷数, 已检数)`，对每分值的平均
  缺陷率做**加权等渗回归**（PAVA，权重=已检数），使逐板平方误差之和最小；
  同分板天然共用一个缺陷率。全程 `Fraction` 精确有理数运算。
* **缺口回退**：某分值无实检 → 取左侧最近实检分值的缺陷率；左侧没有 →
  取最低实检分值的缺陷率。
* **排序**：`缺陷率 × 漏检损失` 降序；预期损失相同取较小板号。实检后已检板
  不再进入名单。

## “不做全量扫描”的实现方式

* 校准只依赖已检板的**每分值计数**（`score -> [defect, inspected]`），重拟合
  固定 O(256)，与待检板数量无关。
* 待检板按分值分桶，每桶维护两个索引堆：`(-loss, id)` 用于正缺陷率（预期
  损失随损失单调），`id` 用于零缺陷率（预期损失全为 0，只能按板号排序）。
  取名单是对至多 256 个桶顶做 K 路归并，复杂度 `O(256 + K log 256)`，不遍历
  待检板；归并弹出的元素在返回前还原，因此名单可重复读取。
* 补录/更正只改计数并重拟合，绝不触碰待检堆；变为已检的板通过“live 集合”
  惰性删除。`tests/test_service.py` 用计数器断言这两条性质。

## 请求示例（更正后名单换序）

```bash
# 1) 登记：两块待检板 + 两条实检（score 10 全好，score 90 全坏）
curl -sS -X POST localhost:8000/panels -H 'Content-Type: application/json' -d '{
  "panels": [
    {"panel_id": 1001, "score": 10, "loss": 1000000},
    {"panel_id": 1002, "score": 90, "loss": 9000},
    {"panel_id": 2001, "score": 10, "loss": 1, "inspection_result": 0},
    {"panel_id": 2002, "score": 90, "loss": 1, "inspection_result": 1}
  ]}'

# 2) 名单：1002 领先（缺陷率 1.0 × 9000）
curl -sS 'localhost:8000/queue?k=2'

# 3) 更正 score-10 的实检为缺陷
curl -sS -X PUT localhost:8000/panels/2001/inspection \
  -H 'Content-Type: application/json' -d '{"inspection_result": 1}'

# 4) 名单：1001 反超（缺陷率 1.0 × 1000000）
curl -sS 'localhost:8000/queue?k=2'

# 5) 无效更正（结果=7）→ 422，名单不变
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT localhost:8000/panels/1002/inspection \
  -H 'Content-Type: application/json' -d '{"inspection_result": 7}'
```

完整可执行版本见 `demo/recheck_reorder.sh`（需 `jq`）。

## 后验核查（`POST /audit`）

请求体：

```json
{
  "checks": [
    {"score": 10, "cap_percent": 50},
    {"score": 50, "cap_percent": 50},
    {"score": 90, "cap_percent": 100}
  ],
  "calibration_version": 3
}
```

* `checks` 为 1～12 项，`score` 必须互异（0–255），`cap_percent` 为
  0–100 的整数百分比；`calibration_version` 可选，传入报告所用版号。
* 响应给出 `probability`（所有上限**同时**成立的后验概率）、当前
  `calibration_version`、每个分值实际约束的组件与回退来源
  （`resolved_checks`，`fitted` / `left` / `lowest`）。
* 传入的版号与当前版不一致时 `recheck_required=true` 并在 `warnings`
  中提示重查；不传版号则永不提示，只返回当前后验。
* 概率始终基于**当前版**的实检计数计算；补录、更正与核查可并发进行，
  计数与版号在同一把锁内读取，不会读到半更新状态。

### 概率模型

* 只有**有实检**的分值是潜变量：按分值升序记为 `x_1 <= ... <= x_m`，
  每个 `x_j` 取 0%～100% 的整数百分比（共 101 档），同分板共用一项
  缺陷率。所有非降向量先验等权。
* 逐板独立的伯努利似然：分值 `s` 实检 `n_s` 块、缺陷 `d_s` 块，则组件
  取值 `p` 的权重为 `p^d_s · (1-p)^(n_s-d_s)`。
* **无实检的分值不是自由变量**，沿用现有回退规则（左侧最近实检分值，
  否则最低实检分值）；对该分值的上限即对其借用组件的上限。多个查询
  落到同一组件时取最紧（最小）上限。
* 全无实检时没有任何证据锚定后验，接口返回 `409 evidence_insufficient`。

### “不穷举向量”的实现方式

非降网格向量的后验归一化常数与带约束质量，都是单调格点上的求和，用
**前缀和动态规划**一次完成：

```
dp_j(x) = 权重_j(x) · Σ_{y ≤ x} dp_{j-1}(y)
```

每个组件只做一次 101 档线性扫描，整体复杂度 `O(m × 101)`、内存
`O(101)`，与板数（最多 5 万块）和 `101^m` 个向量都无关。所有求和在
对数空间用 `logaddexp` 进行，似然下溢为 0 的档位（被证据否定的比率，
如全缺陷数据下的 0% 档）按 `0^0 = 1` 约定自然剔除。测试用
`Fraction` 精确枚举全部向量的独立预言机做随机差分对照，并对千块级
重数据核对绝对误差小于 `10⁻⁸`。

### 核查示例

```bash
# 三个档位的上限同时核查（score 50 无实检，回退借用 score 10 的组件）
curl -sS -X POST localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "checks": [
    {"score": 10, "cap_percent": 50},
    {"score": 50, "cap_percent": 50},
    {"score": 90, "cap_percent": 100}
  ]}'

# 带版号核查；若期间实检被补录/更正，响应会提示 recheck_required
curl -sS -X POST localhost:8000/audit -H 'Content-Type: application/json' -d '{
  "checks": [{"score": 10, "cap_percent": 50}],
  "calibration_version": 3}'
```

完整可执行版本见 `demo/cap_audit.sh`（需 `jq`）。

## 测试

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
```

覆盖：PAVA 与独立 min-max 预言机的一致性、加权平方误差最优性、回退规则、
与全量扫描的随机差分对照（含每步变更后复核）、零率平局按板号、无效补改
不改名单、5 万上限、并发读写，以及“不扫描待检板”的计数断言。核查部分
另覆盖：前缀和 DP 与 `Fraction` 全枚举预言机的随机差分（小网格与生产
101 档网格）、多上限联合事件、回退映射与同组件取最紧上限、重查提示、
证据不足、请求校验、千块级数据 `10⁻⁸` 精度，以及 256 个实检分值/5 万
板规模下的单次核查耗时。

## 已知限制

* 状态在进程内存中，重启即清空；多副本需外置存储。
* 只提供登记与补录，不提供删除板或修改分值/损失。
* 缺陷率以浮点返回（内部为精确有理数）；仅用于展示与排序依据，不参与排序比较。
