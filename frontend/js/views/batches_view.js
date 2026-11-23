export const batchesViewTemplate = `
<section class="panel">
  <h2>温控批次入库 · 窗闸门</h2>
  <p class="hint">正式建口撞排程窗即拒建零改；挤窗意图落影子不占配额，改线消冲突后整笔晋升。</p>
  <div class="form-row">
    <div><label>仓间</label>
      <select v-model.number="batchForm.chamber_id"><option v-for="c in chambers" :key="c.id" :value="c.id">{{ c.chamber_code }}</option></select>
    </div>
    <div><label>探头</label>
      <select v-model.number="batchForm.probe_id"><option v-for="p in batchProbes" :key="p.id" :value="p.id">{{ p.probe_code }}</option></select>
    </div>
    <div><label>货载编号</label><input v-model="batchForm.cargo_ref"/></div>
    <div><label>批次编码(可空)</label><input v-model="batchForm.batch_code"/></div>
    <div><label>承运线路档位</label>
      <select v-model.number="batchForm.route_id">
        <option :value="null">不挂线路</option>
        <option v-for="r in routes" :key="r.id" :value="r.id">{{ r.route_code }}（{{ r.window_start }}~{{ r.window_end }}）</option>
      </select>
    </div>
    <div><label>温控窗起</label><input v-model="batchForm.window_start" placeholder="2026-12-01T19:00:00"/></div>
    <div><label>温控窗止</label><input v-model="batchForm.window_end" placeholder="2026-12-01T21:00:00"/></div>
  </div>
  <label>温度点 JSON</label>
  <textarea v-model="batchForm.points_json" rows="4" placeholder="[{&quot;t&quot;:&quot;08:00&quot;,&quot;c&quot;:-19}]"></textarea>
  <div class="actions">
    <button @click="createBatch">正式建批（甲）</button>
    <button class="secondary" @click="createShadowIntent">挤窗影子（乙）</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <h3>正式批次（影子不混入）</h3>
  <table class="data-table">
    <thead><tr><th>批次</th><th>仓间</th><th>探头</th><th>状态</th><th>意图</th><th>温控窗</th><th>窗状态</th><th>代</th><th></th></tr></thead>
    <tbody>
      <tr v-for="b in batches" :key="b.id" @click="selectBatch(b)">
        <td>{{ b.batch_code }}</td>
        <td>{{ b.chamber_code }}</td>
        <td>{{ b.probe_code }}</td>
        <td><span class="badge" :class="statusClass(b.status)">{{ b.status }}</span></td>
        <td>{{ b.plan_intent }}</td>
        <td class="mono">{{ b.window_start || '—' }} ~ {{ b.window_end || '—' }}</td>
        <td><span v-if="b.route_id" class="badge" :class="b.window_conflict ? 'fail' : 'pass'">{{ b.window_conflict ? "撞车" : "无撞" }}</span><span v-else class="hint">未挂线</span></td>
        <td>{{ b.generation }}</td>
        <td><button class="mini-btn" @click.stop="loadBatchDetail(b.id)">详情</button></td>
      </tr>
    </tbody>
  </table>
  <h3>挤窗影子待晋</h3>
  <table class="data-table">
    <thead><tr><th>批次</th><th>仓间</th><th>状态</th><th>温控窗</th><th>窗状态</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="b in shadowBatches" :key="b.id">
        <td>{{ b.batch_code }}</td>
        <td>{{ b.chamber_code }}</td>
        <td><span class="badge frozen">{{ b.status }}</span></td>
        <td class="mono">{{ b.window_start }} ~ {{ b.window_end }}</td>
        <td><span class="badge" :class="b.window_conflict ? 'fail' : 'pass'">{{ b.window_conflict ? "撞车" : "已消" }}</span></td>
        <td>
          <button class="mini-btn" @click="promoteShadow(b.id)">整笔晋升</button>
          <button class="mini-btn" @click="voidShadow(b.id)">作废</button>
        </td>
      </tr>
      <tr v-if="!shadowBatches.length"><td colspan="6" class="hint">无待晋影子</td></tr>
    </tbody>
  </table>
  <h3>撞车窗纪行（当班封死，只读）</h3>
  <table class="data-table">
    <thead><tr><th>时刻</th><th>线路</th><th>批次</th><th>判定</th><th>附言</th></tr></thead>
    <tbody>
      <tr v-for="x in windowConflicts" :key="x.id">
        <td class="mono">{{ x.created_at }}</td>
        <td>{{ x.route_code }}</td>
        <td>{{ x.batch_code }}</td>
        <td><span class="badge" :class="x.decision === '晋升' ? 'pass' : 'fail'">{{ x.decision }}</span></td>
        <td>{{ x.detail }}</td>
      </tr>
    </tbody>
  </table>
  <div v-if="batchDetail" class="detail-box">
    <h3>{{ batchDetail.batch.batch_code }} · {{ batchDetail.batch.cargo_ref }}</h3>
    <p>点数 {{ batchDetail.batch.temp_points.length }} · 游标 {{ batchDetail.excursions.length }} 条
       · 窗状态 <span :class="batchDetail.batch.window_conflict ? 'bad' : 'ok'">{{ batchDetail.batch.window_conflict ? "撞车" : "无撞" }}</span></p>
  </div>
</section>
`;
