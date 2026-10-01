<template>
  <section class="page" data-module="traffic_facility">
    <header class="page-head">
      <div>
        <h2>交安设施管理</h2>
        <p class="page-desc">维护交安设施，围绕设施编号、设施类型、所属路段、桩号位置做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记交安设施</button>
        <button class="btn" type="button" @click="exportRows">导出交安设施清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="showTrajectory(Number(row.id))">轨迹</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无交安设施数据，可先登记交安设施</td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>设施编号位号修订台账</h3>
          <p class="page-desc">
            移位、换型、修复都会生成连续修订；审批结论一处写入，设施台账、路段清单、工程材料计划三处读取同一修订。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="openRevisionForm">发起修订</button>
          <button class="btn" type="button" @click="migrateLegacy">迁移缺失位号</button>
          <button class="btn ghost" type="button" @click="exportRevisions">导出修订台账</button>
        </div>
      </header>

      <form v-if="revisionForm.visible" class="revision-form" @submit.prevent="submitRevision">
        <label class="filter-item">
          <span>设施</span>
          <select v-model="revisionForm.设施id">
            <option v-for="row in rows" :key="String(row.id)" :value="String(row.id)">
              {{ row.设施编号 }} · {{ row.设施类型 }}
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>修订类型</span>
          <select v-model="revisionForm.修订类型">
            <option v-for="kind in revisionTypes" :key="kind" :value="kind">{{ kind }}</option>
          </select>
        </label>
        <template v-if="revisionForm.修订类型 === '移位'">
          <label class="filter-item">
            <span>新桩号位置</span>
            <input v-model="revisionForm.新桩号位置" placeholder="如 K3+000" />
          </label>
          <label class="filter-item">
            <span>新位号（留空按里程生成）</span>
            <input v-model="revisionForm.新位号" placeholder="如 W003000" />
          </label>
          <label class="filter-item">
            <span>新经度</span>
            <input v-model="revisionForm.新经度" placeholder="可留空" />
          </label>
          <label class="filter-item">
            <span>新纬度</span>
            <input v-model="revisionForm.新纬度" placeholder="可留空" />
          </label>
        </template>
        <label v-if="revisionForm.修订类型 === '换型'" class="filter-item">
          <span>新设施类型</span>
          <input v-model="revisionForm.新设施类型" placeholder="如 限速标志牌" />
        </label>
        <label v-if="revisionForm.修订类型 === '修复'" class="filter-item">
          <span>修复说明</span>
          <input v-model="revisionForm.修复说明" placeholder="如 更换反光膜" />
        </label>
        <label class="filter-item">
          <span>责任组（与验收单冲突时以验收单为准）</span>
          <input v-model="revisionForm.责任组" placeholder="可留空" />
        </label>
        <label class="filter-item">
          <span>施工时间</span>
          <input v-model="revisionForm.施工时间" type="date" />
        </label>
        <label class="filter-item">
          <span>申请人</span>
          <input v-model="revisionForm.申请人" placeholder="姓名" />
        </label>
        <span class="message-id">幂等键：{{ revisionForm.message_id }}</span>
        <button class="btn primary" type="submit">提交修订</button>
        <button class="btn ghost" type="button" @click="revisionForm.visible = false">取消</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>修订编号</th>
            <th>设施编号</th>
            <th>修订序号</th>
            <th>修订类型</th>
            <th>责任组</th>
            <th>状态</th>
            <th>施工时间</th>
            <th>审批结论</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="rev in revisions" :key="String(rev.id)">
            <td>{{ rev.修订编号 }}</td>
            <td>{{ rev.设施编号 }}</td>
            <td>第 {{ rev.修订序号 }} 次</td>
            <td>{{ rev.修订类型 }}</td>
            <td>
              {{ rev.责任组 }}
              <span class="muted">（{{ rev.责任组来源 }}）</span>
            </td>
            <td>
              <span class="tag" :class="statusTag(String(rev.状态))">{{ rev.状态 }}</span>
            </td>
            <td>{{ rev.施工时间 }}</td>
            <td>{{ rev.审批结论 || '—' }}</td>
            <td class="row-actions">
              <template v-if="rev.状态 === '待审批'">
                <button class="link" type="button" @click="approveRevision(rev)">批准</button>
                <button class="link" type="button" @click="rejectRevision(rev)">驳回</button>
              </template>
              <button class="link" type="button" @click="showTrajectory(Number(rev.设施id))">轨迹</button>
              <button class="link" type="button" @click="showProjection(rev)">三处投影</button>
            </td>
          </tr>
          <tr v-if="!revisions.length">
            <td colspan="9" class="empty-state">暂无修订记录，可点击「发起修订」登记第一条</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="trajectory" class="panel">
      <header class="panel-head">
        <div>
          <h3>地图轨迹 · {{ trajectory.设施.设施编号 }}</h3>
          <p class="page-desc">历史位置按施工时快照保留，灰色为历史、蓝色为当前；轨迹旁列出受影响的路面病害与养护工程。</p>
        </div>
        <button class="btn ghost" type="button" @click="trajectory = null">收起</button>
      </header>
      <div class="trajectory-wrap">
        <svg viewBox="0 0 640 240" class="trajectory-map" role="img">
          <polyline
            v-if="trajectoryPoints"
            :points="trajectoryPoints"
            fill="none"
            stroke="#1f6feb"
            stroke-width="2"
            stroke-dasharray="4 3"
          />
          <g v-for="point in trajectory.轨迹" :key="String(point.id)">
            <circle
              :cx="pointX(point)"
              :cy="pointY(point)"
              r="6"
              :fill="point.状态 === '当前' ? '#1f6feb' : '#94a3b8'"
            />
            <text :x="pointX(point) + 10" :y="pointY(point) - 8" class="map-label">
              {{ point.位号 || '缺位号' }} · {{ point.快照时间 }}
            </text>
          </g>
        </svg>
        <aside class="trajectory-side">
          <h4>受影响的路面病害</h4>
          <ul v-if="trajectory.影响病害.length">
            <li v-for="disease in trajectory.影响病害" :key="String(disease.病害编号)">
              {{ disease.病害编号 }} · {{ disease.病害类型 }} · {{ disease.起止桩号 }}（{{ disease.病害状态 }}）
            </li>
          </ul>
          <p v-else class="page-desc">轨迹附近没有受影响病害</p>
          <h4>受影响的养护工程</h4>
          <ul v-if="trajectory.影响工程.length">
            <li v-for="project in trajectory.影响工程" :key="String(project.工程编号)">
              {{ project.工程编号 }} · {{ project.工程名称 }}（{{ project.工程状态 }}）
            </li>
          </ul>
          <p v-else class="page-desc">轨迹附近没有受影响工程</p>
        </aside>
      </div>
    </section>

    <section v-if="projection" class="panel">
      <header class="panel-head">
        <div>
          <h3>同一修订 · 三处读取（{{ projection.修订.修订编号 }}）</h3>
          <p class="page-desc">审批结论：{{ projection.修订.审批结论 || '待审批' }}；三处读取同一条修订，不各存一份。</p>
        </div>
        <button class="btn ghost" type="button" @click="projection = null">收起</button>
      </header>
      <div class="projection-grid">
        <article class="stat-card">
          <h4>设施台账</h4>
          <template v-if="projection.设施台账">
            <p v-for="(value, key) in projection.设施台账" :key="key" class="kv">{{ key }}：{{ value ?? '—' }}</p>
          </template>
          <p v-else class="page-desc">设施已归档</p>
        </article>
        <article class="stat-card">
          <h4>路段清单</h4>
          <template v-if="projection.路段清单">
            <p v-for="(value, key) in projection.路段清单" :key="key" class="kv">{{ key }}：{{ value ?? '—' }}</p>
          </template>
          <p v-else class="page-desc">所属路段不在清单中</p>
        </article>
        <article class="stat-card">
          <h4>工程材料计划</h4>
          <ul v-if="projection.工程材料计划.length" class="plan-list">
            <li v-for="plan in projection.工程材料计划" :key="String(plan.id)">
              {{ plan.计划编号 }} · {{ plan.材料名称 }} × {{ plan.数量 }}{{ plan.单位 }}（{{ plan.状态 }}）
            </li>
          </ul>
          <p v-else class="page-desc">该修订未产生材料计划</p>
        </article>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条交安设施记录 · {{ revisionTotal }} 条修订记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="noticeMessage" class="muted">{{ noticeMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/traffic_facility'
const REVISION_ENDPOINT = '/api/traffic_facility_revision'
const columns = ["设施编号", "设施类型", "所属路段", "桩号位置", "位号", "责任组", "设置日期", "反光等级", "完好程度", "设施状态"]
const actions = ["登记污损", "登记缺失", "更换设施"]
const revisionTypes = ["移位", "换型", "修复"]
const stats = [{"label": "完好设施", "value": 0}, {"label": "污损设施", "value": 0}, {"label": "缺失设施", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const revisions = ref<Row[]>([])
const revisionTotal = ref(0)
const trajectory = ref<Row | null>(null)
const projection = ref<Row | null>(null)

const emptyRevisionForm = () => ({
  visible: false,
  设施id: '',
  修订类型: '移位',
  新桩号位置: '',
  新位号: '',
  新经度: '',
  新纬度: '',
  新设施类型: '',
  修复说明: '',
  责任组: '',
  施工时间: '',
  申请人: '',
  message_id: '',
})
const revisionForm = ref(emptyRevisionForm())

function statusTag(status: string) {
  return { 待审批: 'pending', 已批准: 'approved', 已驳回: 'rejected' }[status] ?? 'pending'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function exportRevisions() {
  window.open(`${REVISION_ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '交安设施登记入口尚未接入审批流'
}

function openRevisionForm() {
  const form = emptyRevisionForm()
  form.visible = true
  form.设施id = rows.value.length ? String(rows.value[0].id) : ''
  // 每次打开生成新的幂等键：同一次表单重复提交/重试只会命中同一条修订
  form.message_id = `WEB-${crypto.randomUUID()}`
  revisionForm.value = form
}

async function submitRevision() {
  errorMessage.value = ''
  noticeMessage.value = ''
  const form = revisionForm.value
  try {
    const response = await request(REVISION_ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          设施id: Number(form.设施id),
          修订类型: form.修订类型,
          新桩号位置: form.新桩号位置,
          新位号: form.新位号,
          新经度: form.新经度,
          新纬度: form.新纬度,
          新设施类型: form.新设施类型,
          修复说明: form.修复说明,
          责任组: form.责任组,
          施工时间: form.施工时间,
          申请人: form.申请人,
          message_id: form.message_id,
        },
      }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '修订登记失败')
    }
    noticeMessage.value = payload.message
    revisionForm.value.visible = false
    await Promise.all([reload(), reloadRevisions()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修订登记失败'
  }
}

async function approveRevision(rev: Row) {
  const conclusion = window.prompt(`批准修订 ${rev.修订编号}，请填写审批结论`, '同意本次修订')
  if (conclusion === null) return
  await settleRevision(rev, 'approve', conclusion)
}

async function rejectRevision(rev: Row) {
  const conclusion = window.prompt(`驳回修订 ${rev.修订编号}，请填写审批结论`, '资料不全，退回补正')
  if (conclusion === null) return
  await settleRevision(rev, 'reject', conclusion)
}

async function settleRevision(rev: Row, action: 'approve' | 'reject', conclusion: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${REVISION_ENDPOINT}/${rev.id}/${action}`, {
      method: 'POST',
      body: JSON.stringify({ values: { 审批结论: conclusion, 审批人: '值班员' } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '审批未生效')
    }
    noticeMessage.value = payload.message
    await Promise.all([reload(), reloadRevisions()])
    if (projection.value && projection.value.修订.id === rev.id) {
      await showProjection(rev)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审批操作失败'
  }
}

async function migrateLegacy() {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${REVISION_ENDPOINT}/migrate`, { method: 'POST', body: '{}' })
    const payload = await response.json()
    noticeMessage.value = payload.message
    await Promise.all([reload(), reloadRevisions()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移失败'
  }
}

async function showTrajectory(facilityId: number) {
  errorMessage.value = ''
  try {
    const response = await request(`${REVISION_ENDPOINT}/trajectory/${facilityId}`)
    if (!response.ok) {
      throw new Error('轨迹读取失败')
    }
    trajectory.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '轨迹读取失败'
  }
}

async function showProjection(rev: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${REVISION_ENDPOINT}/${rev.id}/projection`)
    if (!response.ok) {
      throw new Error('投影读取失败')
    }
    projection.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '投影读取失败'
  }
}

const trajectoryRange = computed(() => {
  const points = trajectory.value?.轨迹 ?? []
  const lngs = points.map((point: Row) => Number(point.经度))
  const lats = points.map((point: Row) => Number(point.纬度))
  return {
    lngMin: Math.min(...lngs, 0),
    lngMax: Math.max(...lngs, 1),
    latMin: Math.min(...lats, 0),
    latMax: Math.max(...lats, 1),
  }
})

function pointX(point: Row) {
  const { lngMin, lngMax } = trajectoryRange.value
  const span = lngMax - lngMin || 1
  return 40 + ((Number(point.经度) - lngMin) / span) * 560
}

function pointY(point: Row) {
  const { latMin, latMax } = trajectoryRange.value
  const span = latMax - latMin || 1
  return 200 - ((Number(point.纬度) - latMin) / span) * 160
}

const trajectoryPoints = computed(() => {
  const points = trajectory.value?.轨迹 ?? []
  return points.map((point: Row) => `${pointX(point)},${pointY(point)}`).join(' ')
})

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '交安设施动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交安设施操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('交安设施列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交安设施列表读取失败'
  }
}

async function reloadRevisions() {
  try {
    const response = await request(`${REVISION_ENDPOINT}?size=100`)
    if (!response.ok) {
      throw new Error('修订台账读取失败')
    }
    const payload = await response.json()
    revisions.value = payload.items ?? []
    revisionTotal.value = payload.total ?? revisions.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修订台账读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadRevisions()
})
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 16px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
.panel-head h3 { margin: 0 0 4px; font-size: 15px; }
.revision-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; border: 1px dashed var(--border); border-radius: 8px; padding: 10px; margin-bottom: 12px; }
.revision-form input, .revision-form select { padding: 4px 6px; border: 1px solid var(--border); border-radius: 4px; }
.message-id { font-size: 12px; color: var(--muted); align-self: center; }
.muted { color: var(--muted); font-size: 12px; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 4px; font-size: 12px; }
.tag.pending { background: #fef3c7; color: #92400e; }
.tag.approved { background: #dcfce7; color: #166534; }
.tag.rejected { background: #fee2e2; color: #991b1b; }
.trajectory-wrap { display: flex; gap: 16px; }
.trajectory-map { flex: 1; min-width: 0; background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; }
.trajectory-side { width: 280px; font-size: 13px; }
.trajectory-side h4 { margin: 8px 0 4px; font-size: 13px; }
.trajectory-side ul { margin: 0; padding-left: 18px; }
.map-label { font-size: 11px; fill: #334155; }
.projection-grid { display: flex; gap: 12px; }
.projection-grid .stat-card { flex: 1; }
.projection-grid h4 { margin: 0 0 6px; font-size: 13px; }
.kv { margin: 2px 0; font-size: 12px; color: #334155; }
.plan-list { margin: 0; padding-left: 18px; font-size: 12px; }
</style>
