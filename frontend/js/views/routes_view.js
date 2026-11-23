export const routesViewTemplate = `
<section class="panel">
  <h2>冷链线路 · 承运排程窗</h2>
  <p class="hint">排程窗与温控批次窗撞车时建批被闸；窗状态与排程板/批次/封存/互证同源。</p>
  <div class="form-row">
    <div><label>线路编码</label><input v-model="routeForm.route_code"/></div>
    <div><label>起点区域</label><input v-model="routeForm.origin_region"/></div>
    <div><label>终点区域</label><input v-model="routeForm.dest_region"/></div>
    <div><label>冷链时长(h)</label><input type="number" v-model.number="routeForm.cold_hours"/></div>
    <div><label>车型</label><input v-model="routeForm.vehicle_type"/></div>
    <div><label>排程窗起</label><input v-model="routeForm.window_start" placeholder="2026-12-01T08:00:00"/></div>
    <div><label>排程窗止</label><input v-model="routeForm.window_end" placeholder="2026-12-01T20:00:00"/></div>
  </div>
  <div class="actions"><button @click="createRoute">新建线路</button></div>
  <table class="data-table">
    <thead><tr><th>编码</th><th>起</th><th>讫</th><th>时长</th><th>排程窗</th><th>正式占用</th><th>影子待晋</th><th>窗状态</th><th>改线消冲突</th></tr></thead>
    <tbody>
      <tr v-for="r in routes" :key="r.id">
        <td>{{ r.route_code }}</td>
        <td>{{ r.origin_region }}</td>
        <td>{{ r.dest_region }}</td>
        <td>{{ r.cold_hours }}</td>
        <td class="mono">{{ r.window_start || '—' }} ~ {{ r.window_end || '—' }}</td>
        <td>{{ r.formal_occupancy }}</td>
        <td>{{ r.shadow_pending }}</td>
        <td><span class="badge" :class="r.window_conflict ? 'fail' : 'pass'">{{ r.window_conflict ? "撞车" : "无撞" }}</span></td>
        <td>
          <input v-model="routeAdjust[r.id].start" placeholder="新窗起" style="width:13em"/>
          <input v-model="routeAdjust[r.id].end" placeholder="新窗止" style="width:13em"/>
          <button class="mini-btn" @click="adjustRoute(r.id)">改线</button>
        </td>
      </tr>
    </tbody>
  </table>
  <h3>排程板（同源窗证据）</h3>
  <table class="data-table">
    <thead><tr><th>线路</th><th>批次</th><th>意图</th><th>状态</th><th>温控窗</th><th>撞车</th></tr></thead>
    <tbody>
      <template v-for="r in scheduleBoard" :key="'b'+r.id">
        <tr v-for="b in r.batches" :key="r.id+'-'+b.batch_code">
          <td>{{ r.route_code }}</td>
          <td>{{ b.batch_code }}</td>
          <td>{{ b.plan_intent }}</td>
          <td>{{ b.status }}</td>
          <td class="mono">{{ b.window_start || '—' }} ~ {{ b.window_end || '—' }}</td>
          <td><span class="badge" :class="b.window_conflict ? 'fail' : 'pass'">{{ b.window_conflict ? "撞" : "清" }}</span></td>
        </tr>
        <tr v-if="!r.batches.length" :key="'e'+r.id"><td>{{ r.route_code }}</td><td colspan="5" class="hint">暂无在档批次</td></tr>
      </template>
    </tbody>
  </table>
</section>
`;
