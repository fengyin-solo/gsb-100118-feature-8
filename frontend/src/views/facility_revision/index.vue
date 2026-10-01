<template>
  <section class="page" data-module="facility_revision">
    <header class="page-head">
      <div>
        <h2>设施编号位号修订台账</h2>
        <p class="page-desc">交安设施每次移位、换型、修复都生成连续修订；审批结论在同一事务内进入设施台账、路段清单与工程材料计划，地图轨迹旁标注受影响路面病害和养护工程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openSubmit">提交修订</button>
        <button class="btn" type="button" @click="alignLegacy">存量缺失位号对齐里程</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>修订类型</span>
        <select v-model="filters.revision_type">
          <option value="">全部</option>
          <option v-for="t in revisionTypes" :key="t" :value="t">{{ t }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>审批状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option value="待审批">待审批</option>
          <option value="审批通过">审批通过</option>
          <option value="已驳回">已驳回</option>
        </select>
      </label>
      <label class="filter-item">
        <span>设施ID</span>
        <input v-model="filters.facility_id" placeholder="如 1" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>修订编号</th>
          <th>设施编号</th>
          <th>类型</th>
          <th>桩号变化</th>
          <th>位号变化</th>
          <th>责任组(申报→裁定)</th>
          <th>施工日期</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row.revision_no }}</td>
          <td>{{ row.facility_code }}</td>
          <td>{{ row.revision_type }}</td>
          <td>{{ row.before_station || '—' }} → <strong>{{ row.new_station || '—' }}</strong></td>
          <td>{{ row.before_tag || '—' }} → <strong>{{ row.new_tag || '—' }}</strong></td>
          <td>
            {{ row.resolved_group || row.declared_group || '—' }}
            <span v-if="row.group_conflict" class="warn-badge" :title="row.conflict_note">冲突已按验收单裁定</span>
          </td>
          <td>{{ row.construction_date }}</td>
          <td>
            <span :class="['status-tag', statusClass(row.status)]">{{ row.status }}</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row.id)">明细/轨迹</button>
            <button v-if="row.status === '待审批'" class="link" type="button" @click="quickApprove(row.id, true)">通过</button>
            <button v-if="row.status === '待审批'" class="link danger" type="button" @click="quickApprove(row.id, false)">驳回</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="9" class="empty-state">暂无修订记录，可对交安设施提交第一次移位/换型/修复修订</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条修订</span>
      <span v-if="message" class="ok-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 提交修订弹层 -->
    <div v-if="showSubmit" class="modal-mask" @click.self="showSubmit = false">
      <div class="modal">
        <h3>提交设施修订</h3>
        <p class="page-desc">修订编号在设施范围内连续生成；相同消息编号或相同内容重复提交将幂等返回首次结果。</p>
        <label class="form-line"><span>交安设施 *</span>
          <select v-model="form.facility_id">
            <option v-for="f in facilities" :key="f.id" :value="f.id">
              {{ f['设施编号'] }}｜{{ f['设施类型'] }}｜{{ f['桩号位置'] }}
            </option>
          </select>
        </label>
        <label class="form-line"><span>修订类型 *</span>
          <select v-model="form.revision_type">
            <option v-for="t in revisionTypes" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>
        <label class="form-line"><span>新桩号（移位/对齐必填）</span>
          <input v-model="form.new_station" placeholder="如 K13+520" />
        </label>
        <label class="form-line"><span>新位号（留空按里程对齐）</span>
          <input v-model="form.new_tag" placeholder="如 W-K13+520" />
        </label>
        <label v-if="form.revision_type === '换型'" class="form-line"><span>换型后设施类型 *</span>
          <input v-model="form.new_facility_type" placeholder="如 标志牌（限速80）" />
        </label>
        <label class="form-line"><span>申报责任组</span>
          <input v-model="form.responsible_group" placeholder="与资产验收单冲突时以验收单为准" />
        </label>
        <label class="form-line"><span>关联工程ID（可空）</span>
          <input v-model="form.project_id" placeholder="如 2" />
        </label>
        <label class="form-line"><span>修订事由</span>
          <input v-model="form.reason" />
        </label>
        <label class="form-line"><span>消息编号（幂等键，可空）</span>
          <input v-model="form.message_id" placeholder="上游消息ID，重放不产生重复修订" />
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="showSubmit = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitRevision">
            {{ submitting ? '提交中…' : '提交待审批' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 明细 + 地图轨迹抽屉 -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal modal-wide">
        <h3>{{ detail.revision_no }} ｜ {{ detail.facility_code }} ｜ {{ detail.revision_type }}</h3>
        <div class="detail-grid">
          <div><span>状态</span><strong>{{ detail.status }}</strong></div>
          <div><span>桩号</span><strong>{{ detail.before_station || '—' }} → {{ detail.new_station }}</strong></div>
          <div><span>位号</span><strong>{{ detail.before_tag || '—' }} → {{ detail.new_tag }}</strong></div>
          <div><span>责任组</span><strong>{{ detail.resolved_group || detail.declared_group }}</strong></div>
          <div><span>施工日期</span><strong>{{ detail.construction_date }}</strong></div>
          <div><span>审批人</span><strong>{{ detail.approver || '—' }}</strong></div>
        </div>
        <p v-if="detail.conflict_note" class="warn-box">⚠ {{ detail.conflict_note }}</p>
        <p v-else class="muted-box">责任组与资产验收单一致（或无验收单回退申报组）。</p>

        <h4>地图轨迹（历史位置按施工时快照保留）</h4>
        <svg class="track-svg" :viewBox="`0 0 560 ${trackHeight}`" role="img" aria-label="设施移位轨迹示意图">
          <line x1="40" :y1="trackY" x2="520" :y2="trackY" class="road-line" />
          <g v-for="(p, i) in trackPoints" :key="i">
            <line :x1="p.x" :y1="trackY" :x2="p.x" :y2="trackY + 34" stroke="#c3ccd9" stroke-dasharray="3 3" />
            <circle :cx="p.x" :cy="trackY" r="6" :class="i === trackPoints.length - 1 ? 'dot-now' : 'dot-old'" />
            <text :x="p.x" :y="trackY - 12" text-anchor="middle" class="svg-label">{{ p.label }}</text>
            <text :x="p.x" :y="trackY + 48" text-anchor="middle" class="svg-sub">{{ p.revision_no }}</text>
          </g>
          <text v-if="!trackPoints.length" x="280" :y="trackY" text-anchor="middle" class="svg-sub">暂无历史位置</text>
        </svg>

        <div class="impact-cols">
          <div>
            <h4>轨迹旁受影响路面病害</h4>
            <ul class="impact-list">
              <li v-for="p in uniquePavements" :key="String(p.ref_id)">
                <strong>{{ p.ref_code }}</strong>（{{ p['病害类型'] }}）<br />
                <span class="muted">{{ p['起止桩号'] }} · {{ p.status }}</span>
              </li>
              <li v-if="!uniquePavements.length" class="muted">150m 缓冲范围内无路面病害</li>
            </ul>
          </div>
          <div>
            <h4>轨迹旁受影响养护工程</h4>
            <ul class="impact-listing">
              <li v-for="p in uniqueProjects" :key="String(p.ref_id)">
                <strong>{{ p.ref_code }}</strong>（{{ p['工程名称'] }}）<br />
                <span class="muted">{{ p['施工起桩号'] }}~{{ p['施工止桩号'] }} · {{ p.status }}</span>
              </li>
              <li v-if="!uniqueProjects.length" class="muted">缓冲范围内无养护工程</li>
            </ul>
          </div>
        </div>

        <div v-if="detail.material_plan" class="plan-box">
          <h4>工程材料计划（与台账、路段清单读取同一修订 {{ detail.material_plan.revision_no }}）</h4>
          <table class="data-table mini">
            <thead><tr><th>材料编号</th><th>材料名称</th><th>规格型号</th><th>数量</th><th>单位</th></tr></thead>
            <tbody>
              <tr v-for="(it, idx) in detail.material_plan.items" :key="idx">
                <td>{{ it['材料编号'] }}</td><td>{{ it['材料名称'] }}</td>
                <td>{{ it['规格型号'] }}</td><td>{{ it['数量'] }}</td><td>{{ it['单位'] }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
          <button v-if="detail.status === '待审批'" class="btn" type="button" @click="quickApprove(detail.id, false); detail = null">驳回</button>
          <button v-if="detail.status === '待审批'" class="btn primary" type="button" @click="quickApprove(detail.id, true); detail = null">审批通过</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'

const route = useRoute()

const ENDPOINT = '/api/facility_revision'
const revisionTypes = ['移位', '换型', '修复', '存量对齐']

type Row = Record<string, any>

const rows = ref<Row[]>([])
const total = ref(0)
const facilities = ref<Row[]>([])
const errorMessage = ref('')
const message = ref('')
const filters = reactive<Record<string, string>>({ revision_type: '', status: '', facility_id: '' })

const showSubmit = ref(false)
const submitting = ref(false)
const detail = ref<Row | null>(null)

const emptyForm = () => ({
  facility_id: 1,
  revision_type: '移位',
  new_station: '',
  new_tag: '',
  new_facility_type: '',
  responsible_group: '',
  project_id: '',
  reason: '',
  message_id: '',
})
const form = reactive(emptyForm())

const stats = computed(() => [
  { label: '修订总数', value: rows.value.length },
  { label: '待审批', value: rows.value.filter((r) => r.status === '待审批').length },
  { label: '审批通过', value: rows.value.filter((r) => r.status === '审批通过').length },
  { label: '责任组冲突仲裁', value: rows.value.filter((r) => r.group_conflict).length },
])

const trackPoints = computed(() => {
  if (!detail.value) return []
  const snaps: Row[] = detail.value.position_snapshots || []
  const valid = snaps.filter((s) => s.after && s.after.里程米 != null)
  if (!valid.length) return []
  const miles = valid.map((s) => Number(s.after.里程米))
  const min = Math.min(...miles)
  const max = Math.max(...miles)
  const span = Math.max(max - min, 1)
  return valid.map((s) => ({
    x: 60 + ((Number(s.after.里程米) - min) / span) * 440,
    label: s.after.桩号位置,
    revision_no: s.revision_no,
  }))
})

const trackY = 70
const trackHeight = computed(() => 150)

const uniqueBy = (list: Row[] | undefined, key: string) => {
  const seen = new Set<string>()
  return (list || []).filter((item) => {
    const k = String(item[key])
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
}
const uniquePavements = computed(() => uniqueBy(detail.value?.affected_pavements, 'ref_id'))
const uniqueProjects = computed(() => uniqueBy(detail.value?.affected_projects, 'ref_id'))

function statusClass(status: string) {
  if (status === '审批通过') return 'st-ok'
  if (status === '已驳回') return 'st-reject'
  return 'st-pending'
}

function resetFilters() {
  filters.revision_type = ''
  filters.status = ''
  filters.facility_id = ''
  void reload()
}

function openSubmit() {
  Object.assign(form, emptyForm(), { facility_id: facilities.value[0]?.id ?? 1 })
  errorMessage.value = ''
  showSubmit.value = true
}

async function loadFacilities() {
  const response = await request('/api/traffic_facility?size=200')
  if (response.ok) {
    const payload = await response.json()
    facilities.value = payload.items ?? []
  }
}

async function reload() {
  errorMessage.value = ''
  message.value = ''
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([k, v]) => v && params.set(k, v))
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload?.detail || '修订台账读取失败')
    rows.value = payload.items ?? []
    total.value = payload.total ?? 0
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修订台账读取失败'
  }
}

async function submitRevision() {
  errorMessage.value = ''
  submitting.value = true
  try {
    const body: Record<string, unknown> = {
      facility_id: Number(form.facility_id),
      revision_type: form.revision_type,
      new_station: form.new_station || null,
      new_tag: form.new_tag || null,
      responsible_group: form.responsible_group || null,
      reason: form.reason || null,
      message_id: form.message_id || null,
    }
    if (form.revision_type === '换型') body.new_facility_type = form.new_facility_type || null
    if (form.project_id) body.project_id = Number(form.project_id)
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify(body) })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '修订提交失败')
    message.value = payload.message
    showSubmit.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修订提交失败'
  } finally {
    submitting.value = false
  }
}

async function openDetail(id: number) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${id}`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload?.detail || '修订明细读取失败')
    detail.value = payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修订明细读取失败'
  }
}

async function quickApprove(id: number, approved: boolean) {
  errorMessage.value = ''
  const opinion = approved ? '同意，结论同步设施台账、路段清单与材料计划' : '资料不全，驳回修订'
  try {
    const response = await request(`${ENDPOINT}/${id}/decision`, {
      method: 'POST',
      body: JSON.stringify({ approved, approver: '值班审批人', opinion }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '审批失败')
    message.value = payload.message
    if (detail.value && detail.value.id === id) detail.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审批失败'
  }
}

async function alignLegacy() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/legacy/align`, { method: 'POST', body: '{}' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '存量对齐失败')
    message.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存量对齐失败'
  }
}

onMounted(async () => {
  const qid = route.query.facility_id
  if (qid) filters.facility_id = String(qid)
  await loadFacilities()
  await reload()
})
</script>
