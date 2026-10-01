# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

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
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 设施编号位号修订台账 | `facility_revision` | 移位/换型/修复连续修订 | 修订编号、桩号/位号变化、审批结论、责任组仲裁 |
| 工程材料计划 | `material_plan`（前端页，后端投影接口） | 修订结论生成的材料计划 | 计划编号、来源修订号、材料明细 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 设施编号位号修订台账

交安设施每次移位、换型、修复（含存量位号对齐）都在设施范围内生成连续修订
（`REV-设施编号-序号`），经审批后结论才生效。关键设计：

- **事务一致**：审批通过在 `store.transaction()` 工作单元内同时提交设施主表、
  空间索引（`facility_spatial_index`）、事件投影（`facility_event_log`）、
  历史位置快照、路段投影与工程材料计划；任一步失败整体回滚，不会只更新地图。
- **三处同读一修订**：设施台账、路段清单、工程材料计划均按同一 `revision_id`
  读取事件投影，不各自抄存结论。
- **责任组仲裁**：申报责任组与资产验收单冲突时以验收单为准，仲裁过程记入修订。
- **历史位置快照**：每次施工的前/后桩号与示意图坐标在审批时固化，之后不回写。
- **存量对齐**：`POST /api/facility_revision/legacy/align` 为缺失位号的设施批量
  开立“存量对齐”修订，审批后位号迁移为里程位号（如 `W-K5+640`）。
- **重复消息幂等**：提交与审批均支持 `message_id`；缺省时按业务内容 sha1 指纹
  去重，重放只返回首次结果。
- **地图轨迹**：`GET /api/facility_revision/facility/{id}/track` 返回历史位置、
  受影响路面病害（150m 缓冲）与养护工程。

回归验证：`cd backend && .venv/bin/python tests/test_facility_revision.py`
