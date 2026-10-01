<template>
  <section class="page" data-module="material_plan">
    <header class="page-head">
      <div>
        <h2>工程材料计划（修订投影）</h2>
        <p class="page-desc">每条计划都由设施编号位号修订的审批结论生成，携带同一修订编号；设施台账、路段清单与本页读取同一修订，审批未通过不会产生计划。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新计划</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>路段编号</span>
        <input v-model="road" placeholder="如 ROAD-G104-CS01" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="road = ''; reload()">全部路段</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>计划编号</th>
          <th>来源修订（同一修订三处同读）</th>
          <th>设施编号</th>
          <th>所属路段</th>
          <th>修订类型</th>
          <th>关联工程</th>
          <th>责任组</th>
          <th>材料明细</th>
          <th>施工日期</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="plan in items" :key="String(plan.id)">
          <td>{{ plan['计划编号'] }}</td>
          <td><span class="rev-chip">{{ plan.revision_no }}</span></td>
          <td>{{ plan.facility_code }}</td>
          <td>{{ plan.road_code }}</td>
          <td>{{ plan.revision_type }}</td>
          <td>{{ plan.project_code || '—' }}</td>
          <td>{{ plan.responsible_group }}</td>
          <td>
            <span v-for="(it, idx) in plan.items" :key="idx" class="mat-chip">
              {{ it['材料名称'] }} ×{{ it['数量'] }}{{ it['单位'] }}
            </span>
          </td>
          <td>{{ plan.construction_date }}</td>
          <td><span class="status-tag st-ok">{{ plan.status }}</span></td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="10" class="empty-state">暂无材料计划，请先在“设施修订台账”中提交并审批通过一条修订</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ items.length }} 条计划，全部可回溯至修订台账</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const items = ref<Row[]>([])
const road = ref('')
const errorMessage = ref('')

async function reload() {
  errorMessage.value = ''
  try {
    const query = new URLSearchParams()
    if (road.value) query.set('road', road.value)
    const response = await request(`/api/facility_revision/material/plans?${query.toString()}`)
    const payload = await response.json()
    if (!response.ok) throw new Error('材料计划读取失败')
    items.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '材料计划读取失败'
  }
}

onMounted(reload)
</script>
