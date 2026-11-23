# Panel Re-inspection Ranking API

玻璃板来不及全检时，用已检板拟合“缺陷分 → 缺陷率”的非降曲线，再按
`缺陷率 × 漏检损失` 从未检板中排出优先复检名单。

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
| `GET` | `/panels/{id}` | 单板信息（含实检状态）。 |
| `GET` | `/health` | 计数与校准版本。 |

无任何实检数据时 `GET /queue` 返回 `409 calibration_missing`。

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

## 测试

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
```

覆盖：PAVA 与独立 min-max 预言机的一致性、加权平方误差最优性、回退规则、
与全量扫描的随机差分对照（含每步变更后复核）、零率平局按板号、无效补改
不改名单、5 万上限、并发读写，以及“不扫描待检板”的计数断言。

## 已知限制

* 状态在进程内存中，重启即清空；多副本需外置存储。
* 只提供登记与补录，不提供删除板或修改分值/损失。
* 缺陷率以浮点返回（内部为精确有理数）；仅用于展示与排序依据，不参与排序比较。
