# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 | 预警编号、关联指标、比较运算符 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 预测交接 | `fchandoff` | 交接信封 | 信封编号、预测目标、模型版本 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 预测到分析的版本链（fchandoff）

预测口径进入分析输出的版本边界如下：

- **部署固化**：预测方案「部署模型」时把预测窗口、特征字段、模型版本、置信水平固化成
  交接信封（`fc_envelope`），口径不全拒绝建版；同一预测目标+窗口的当前部署唯一，
  重复部署同一版本只生效一次，部署新版本时旧版本转 `superseded` 并保留。
- **分析消费**：分析方案只能引用 `deployed` 状态的信封，引用未部署模型返回
  `UNDEPLOYED_MODEL_31`（徽标 `NOT_ALLOWED_31`）；「启用方案」与生成输出都幂等，
  重复启用只生效一次。输出（`fc_output`）记录预测结果、置信水平、对比基线，
  可按 `envelope_id` 回溯模型版本、窗口与样本范围。
- **重训换版**：重训成功生成新版本信封；训练失败或窗口不完整时不建新版，
  只把当前版的输出标记为受影响，不覆盖上一版数值。
- **审计**：`deploy`、`supersede`、`reject` 三类事件全部落 `fc_audit`。
- **文件流转**：`GET /api/fchandoff/export` 打包信封、输出与审计下载；
  `POST /api/fchandoff/reload` 重新载入时模型版本与样本范围原样保留，已存在记录跳过。

示例数据：`FORE-GMV_DAILY`（14 天窗口、7 天训练周期、置信水平 0.95）部署为
`model-v8`（信封 `analysis_handoff_v8`），`ANAL-GMV_TREND` 消费该模型，
2026-10-15 回测输出预测中值 1284000、对比基线 1198000、徽标 `DEPLOYED_V8`。

验收测试：`cd backend && .venv/bin/python -m unittest tests.test_fchandoff -v`

领域：数据工程
子方向：数据分析与数据应用
