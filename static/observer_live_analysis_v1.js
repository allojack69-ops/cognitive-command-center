(() => {
  if (window.location.pathname !== '/observer/control') return;

  const STORAGE_KEY = 'universe_observer_live_analysis_v1';
  const MAX_POINTS = 120;
  const POLL_MS = 3000;

  const $ = (id) => document.getElementById(id);
  const n = (v) => {
    const x = Number(v);
    return Number.isFinite(x) ? x : null;
  };
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const fmt = (v, d=2) => {
    const x = n(v);
    return x === null ? '—' : x.toLocaleString(undefined, {maximumFractionDigits:d});
  };
  const pct = (v, d=0) => {
    const x = n(v);
    return x === null ? '—' : `${fmt(x * 100, d)}%`;
  };
  const esc = (x) => String(x ?? '').replace(/[&<>"']/g, (m) => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[m]);

  function deepExactNumber(obj, names, depth=0) {
    if (!obj || typeof obj !== 'object' || depth > 8) return null;
    const wanted = new Set(names.map(x => x.toLowerCase()));
    for (const [k,v] of Object.entries(obj)) {
      if (wanted.has(String(k).toLowerCase())) {
        const x = n(v);
        if (x !== null) return {value:x, key:k};
      }
    }
    for (const v of Object.values(obj)) {
      if (v && typeof v === 'object') {
        const hit = deepExactNumber(v, names, depth + 1);
        if (hit) return hit;
      }
    }
    return null;
  }

  function loadHistory() {
    try {
      const rows = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      return Array.isArray(rows) ? rows.slice(-MAX_POINTS) : [];
    } catch (_) {
      return [];
    }
  }

  function saveHistory(rows) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(rows.slice(-MAX_POINTS)));
    } catch (_) {}
  }

  function style() {
    if ($('ola-style')) return;
    const el = document.createElement('style');
    el.id = 'ola-style';
    el.textContent = `
      .ola-shell{margin:18px 0;display:grid;grid-template-columns:minmax(0,1.34fr) minmax(330px,.86fr);gap:14px}
      .ola-card{border:1px solid rgba(105,190,230,.24);border-radius:20px;background:rgba(4,17,30,.54);padding:15px;min-width:0}
      .ola-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}
      .ola-kicker{font-size:.68rem;letter-spacing:.10em;color:#7dd3fc;text-transform:uppercase;font-weight:850}
      .ola-title{font-size:1.06rem;font-weight:900;margin-top:4px}
      .ola-sub{font-size:.74rem;color:#879caf;line-height:1.4;margin-top:3px}
      .ola-legend{display:flex;gap:7px;flex-wrap:wrap;margin:9px 0 11px}
      .ola-chip{border:1px solid rgba(130,180,215,.22);border-radius:999px;padding:6px 9px;font-size:.66rem;color:#a5b8c9}
      .ola-chip.good{color:#7ef0bd;border-color:rgba(80,210,150,.35)}
      .ola-chip.warn{color:#ffd27b;border-color:rgba(255,190,90,.35)}
      .ola-chip.bad{color:#ff94a0;border-color:rgba(255,110,130,.34)}
      .ola-chart-wrap{border:1px solid rgba(130,180,215,.14);border-radius:16px;padding:9px;background:rgba(2,10,18,.48);overflow:hidden}
      .ola-svg{width:100%;height:auto;display:block;touch-action:manipulation}
      .ola-gridline{stroke:rgba(135,170,200,.12);stroke-width:1}
      .ola-price{stroke:#70dcff;stroke-width:3;fill:none}
      .ola-core{stroke:#f4d35e;stroke-width:2.2;fill:none}
      .ola-attractor{stroke:#c98bff;stroke-width:2.2;fill:none}
      .ola-geometry{stroke:#7ef0bd;stroke-width:2.2;fill:none}
      .ola-erl{stroke:#ffcd75;stroke-width:2.2;fill:none}
      .ola-eh{stroke:#9ab7ff;stroke-width:2.2;fill:none}
      .ola-gsr{stroke:#65d4bd;stroke-width:2;fill:none;stroke-dasharray:6 5}
      .ola-threshold{stroke:rgba(255,255,255,.16);stroke-width:1;stroke-dasharray:4 5}
      .ola-axis{font-size:10px;fill:#7f93a7}
      .ola-marker-buy{fill:#7ef0bd;stroke:#dfffee;stroke-width:1}
      .ola-marker-sell{fill:#ff8f9d;stroke:#ffe4e8;stroke-width:1}
      .ola-marker-exec{stroke:#fff;stroke-width:2}
      .ola-inspector{margin-top:8px;min-height:36px;color:#91a5ba;font-size:.72rem;line-height:1.4}
      .ola-no-core{font-size:.70rem;color:#7f93a7;margin-top:8px}
      .ola-narrator{display:grid;gap:10px}
      .ola-narrative-block{border:1px solid rgba(130,180,215,.15);border-radius:14px;padding:11px 12px;background:rgba(4,14,24,.42)}
      .ola-narrative-block h4{margin:0 0 5px;font-size:.66rem;letter-spacing:.09em;color:#7dd3fc;text-transform:uppercase}
      .ola-narrative-block p{margin:0;color:#c3d1de;font-size:.78rem;line-height:1.5}
      .ola-trace{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
      .ola-trace span{border:1px solid rgba(130,180,215,.22);border-radius:999px;padding:6px 8px;font-size:.64rem}
      .ola-trace .pass{color:#7ef0bd;border-color:rgba(80,210,150,.35)}
      .ola-trace .wait{color:#ffd27b;border-color:rgba(255,190,90,.35)}
      .ola-trace .block{color:#ff94a0;border-color:rgba(255,110,130,.34)}
      .ola-state-pills{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
      .ola-state-pills span{font-size:.64rem;border-radius:999px;padding:6px 8px;border:1px solid rgba(130,180,215,.18)}
      .ola-state-pills .contradict{color:#ff94a0;border-color:rgba(255,110,130,.34)}
      .ola-state-pills .insufficient{color:#ffd27b;border-color:rgba(255,190,90,.35)}
      .ola-state-pills .forming{color:#70dcff;border-color:rgba(90,200,255,.35)}
      @media(max-width:900px){.ola-shell{grid-template-columns:1fr}.ola-card{min-width:0}}
    `;
    document.head.appendChild(el);
  }

  function mount() {
    if ($('observer-live-analysis')) return;
    const readiness = $('edge-readiness');
    if (!readiness) return;
    style();

    const shell = document.createElement('section');
    shell.id = 'observer-live-analysis';
    shell.className = 'ola-shell';
    shell.innerHTML = `
      <div class="ola-card">
        <div class="ola-head">
          <div>
            <div class="ola-kicker">LIVE STATE FIELD</div>
            <div class="ola-title">Що робить система у часі?</div>
            <div class="ola-sub">Реальні Observer/MOR стани. Ціна окремо від нормалізованих readiness-метрик.</div>
          </div>
          <span class="ola-chip" id="ola-count">0 states</span>
        </div>
        <div class="ola-legend">
          <span class="ola-chip">PRICE</span>
          <span class="ola-chip" id="ola-core-chip">CORE · waiting</span>
          <span class="ola-chip" id="ola-attractor-chip">ATTRACTOR · waiting</span>
          <span class="ola-chip good">GEOMETRY</span>
          <span class="ola-chip warn">ERL1</span>
          <span class="ola-chip">EH1</span>
          <span class="ola-chip">GSR1</span>
        </div>
        <div class="ola-chart-wrap">
          <svg class="ola-svg" id="ola-svg" viewBox="0 0 900 430" role="img" aria-label="Observer live state field">
            <g id="ola-grid"></g>
            <g id="ola-price-layer"></g>
            <g id="ola-core-layer"></g>
            <g id="ola-readiness-layer"></g>
            <g id="ola-marker-layer"></g>
            <g id="ola-axis-layer"></g>
          </svg>
        </div>
        <div class="ola-no-core" id="ola-core-note">CORE / ATTRACTOR будуть накладені на price-площину лише якщо ці поля реально опубліковані в payload у цінових одиницях.</div>
        <div class="ola-inspector" id="ola-inspector">Торкнись BUY / SELL marker, щоб побачити стан у цій точці.</div>
      </div>

      <div class="ola-card">
        <div class="ola-head">
          <div>
            <div class="ola-kicker">OBSERVER NARRATOR</div>
            <div class="ola-title">Чому він робить або не робить угоду?</div>
            <div class="ola-sub">Причинний переклад поточного стану без зміни execution logic.</div>
          </div>
          <span class="ola-chip" id="ola-state-id">—</span>
        </div>
        <div class="ola-state-pills" id="ola-state-pills"></div>
        <div class="ola-narrator">
          <div class="ola-narrative-block"><h4>ЩО ВІДБУВАЄТЬСЯ ЗАРАЗ</h4><p id="ola-now">Чекаю live state.</p></div>
          <div class="ola-narrative-block"><h4>ЩО БАЧИТЬ МОДЕЛЬ</h4><p id="ola-sees">—</p></div>
          <div class="ola-narrative-block"><h4>ЩО БЛОКУЄ ВХІД</h4><p id="ola-blocks">—</p></div>
          <div class="ola-narrative-block"><h4>ЩО ЩЕ НЕ НАСТАЛО</h4><p id="ola-notyet">—</p></div>
          <div class="ola-narrative-block"><h4>ЩО ЗМІНЮЄТЬСЯ</h4><p id="ola-changing">—</p></div>
          <div class="ola-narrative-block"><h4>ЩО МАЄ СТАТИСЯ ДАЛІ</h4><p id="ola-next">—</p></div>
        </div>
        <div class="ola-trace" id="ola-trace"></div>
      </div>
    `;
    readiness.parentNode.insertBefore(shell, readiness);
  }

  function currentPoint(j) {
    const s = j.status || {};
    const snap = (s.runtime_status && typeof s.runtime_status === 'object') ? s.runtime_status : {};
    const raw = (snap.current_state && typeof snap.current_state === 'object') ? snap.current_state : {};
    const er = (raw.execution_readiness && typeof raw.execution_readiness === 'object') ? raw.execution_readiness : {};
    const eh = (raw.execution_horizon_arbitration && typeof raw.execution_horizon_arbitration === 'object') ? raw.execution_horizon_arbitration : {};
    const gsr = (raw.geometric_stability_reversal && typeof raw.geometric_stability_reversal === 'object') ? raw.geometric_stability_reversal : {};
    const pfl = (raw.phase_front_lag && typeof raw.phase_front_lag === 'object') ? raw.phase_front_lag : {};

    const core = deepExactNumber(raw, ['core','core_price','core_level']);
    const attractor = deepExactNumber(raw, ['attractor','attractor_price','attractor_level']);

    return {
      id: String(raw.state_id || s.state_id || ''),
      time: raw.market_time || raw.time || new Date().toISOString(),
      price: n(raw.price ?? (s.account || {}).price ?? s.price),
      action: String(er.action || raw.action || s.action || 'HOLD').toUpperCase(),
      strategy: String(raw.chosen_strategy || s.strategy || ''),
      regime: String(raw.regime || ''),
      geometry: n(er.geometry_alignment),
      erl1: n(er.score),
      eh1: n(er.eh1_score ?? eh.execution_score ?? eh.selected_score),
      ehStatus: String(er.eh1_status || eh.execution_status || eh.status || ''),
      gsrCont: n(er.gsr1_continuation ?? gsr.continuation_index),
      gsrRev: n(er.gsr1_reversal ?? gsr.reversal_index),
      phase: String(((er.pfl1 || {}).front_direction) || pfl.front_direction || ''),
      propagation: String(((er.pfl1 || {}).propagation_direction) || pfl.propagation_direction || ''),
      blockers: Array.isArray(er.blockers) ? er.blockers.map(String) : [],
      strictReady: er.strict_ready === true,
      testnetReady: er.testnet_ready === true,
      tradeability: !!((er.candidate_tradeability_gate || raw.tradeability_gate || {}).allowed),
      evidence: Array.isArray(er.validated_flags) ? er.validated_flags.length :
        (raw.edge_gate && raw.edge_gate.allowed ? 1 : 0),
      preflight: er.preflight_ok === true,
      core: core ? core.value : null,
      attractor: attractor ? attractor.value : null,
      coreKey: core ? core.key : null,
      attractorKey: attractor ? attractor.key : null
    };
  }

  function seedRecent(j, history) {
    const s = j.status || {};
    const snap = (s.runtime_status && typeof s.runtime_status === 'object') ? s.runtime_status : {};
    const rows = (((snap.recent || {}).states) || []);
    if (!Array.isArray(rows) || !rows.length) return history;
    const have = new Set(history.map(x => x.id));
    const seeded = [];
    for (const r of rows) {
      const id = String(r.state_id || '');
      if (!id || have.has(id)) continue;
      seeded.push({
        id,
        time: r.market_time || r.time || '',
        price: n(r.price),
        action: String(r.action || 'HOLD').toUpperCase(),
        strategy: '',
        regime: '',
        geometry: null, erl1: null, eh1: null, ehStatus: '',
        gsrCont: null, gsrRev: null, phase: '', propagation: '',
        blockers: [], strictReady:false, testnetReady:false,
        tradeability:false, evidence:0, preflight:false,
        core:null, attractor:null, seeded:true
      });
    }
    return [...seeded, ...history].slice(-MAX_POINTS);
  }

  function addPoint(history, p) {
    if (!p.id) return history;
    const idx = history.findIndex(x => x.id === p.id);
    if (idx >= 0) history[idx] = {...history[idx], ...p};
    else history.push(p);
    history = history.slice(-MAX_POINTS);
    saveHistory(history);
    return history;
  }

  function pathFrom(points, xfn, yfn) {
    let d = '';
    let open = false;
    points.forEach((p,i) => {
      const y = yfn(p);
      if (y === null || !Number.isFinite(y)) { open = false; return; }
      const x = xfn(i);
      d += `${open ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)} `;
      open = true;
    });
    return d.trim();
  }

  function renderChart(history, j) {
    const svg = $('ola-svg');
    if (!svg) return;
    const rows = history.slice(-80);
    $('ola-count').textContent = `${rows.length} states`;
    if (!rows.length) return;

    const W=900, left=58, right=22;
    const topY0=28, topY1=218;
    const readyY0=258, readyY1=397;
    const innerW=W-left-right;
    const xfn = (i) => left + (rows.length <= 1 ? innerW/2 : i * innerW/(rows.length-1));

    const prices = rows.map(r=>n(r.price)).filter(v=>v!==null);
    let pmin = prices.length ? Math.min(...prices) : 0;
    let pmax = prices.length ? Math.max(...prices) : 1;
    if (pmin === pmax) { pmin -= 1; pmax += 1; }
    const pad=(pmax-pmin)*.08;
    pmin-=pad; pmax+=pad;
    const py=(v)=>{
      v=n(v); if(v===null)return null;
      return topY1-(v-pmin)/(pmax-pmin)*(topY1-topY0);
    };
    const ry=(v)=>{
      v=n(v); if(v===null)return null;
      return readyY1-clamp(v,0,1)*(readyY1-readyY0);
    };

    let grid='';
    for(let i=0;i<=4;i++){
      const y=topY0+i*(topY1-topY0)/4;
      grid+=`<line class="ola-gridline" x1="${left}" y1="${y}" x2="${W-right}" y2="${y}"/>`;
    }
    for(let i=0;i<=4;i++){
      const y=readyY0+i*(readyY1-readyY0)/4;
      grid+=`<line class="ola-gridline" x1="${left}" y1="${y}" x2="${W-right}" y2="${y}"/>`;
    }
    $('ola-grid').innerHTML=grid;

    const pricePath = pathFrom(rows,xfn,p=>py(p.price));
    $('ola-price-layer').innerHTML = `<path class="ola-price" d="${pricePath}"/>`;

    const latest=rows[rows.length-1];
    const corePriceLike = latest.core !== null && latest.price !== null &&
      Math.abs(latest.core-latest.price) <= Math.max(1000,Math.abs(latest.price)*.20);
    const attrPriceLike = latest.attractor !== null && latest.price !== null &&
      Math.abs(latest.attractor-latest.price) <= Math.max(1000,Math.abs(latest.price)*.20);

    let coreHtml='';
    if(corePriceLike){
      coreHtml += `<path class="ola-core" d="${pathFrom(rows,xfn,p=>py(p.core))}"/>`;
      $('ola-core-chip').textContent=`CORE · ${fmt(latest.core,2)}`;
      $('ola-core-chip').className='ola-chip warn';
    }else{
      $('ola-core-chip').textContent=latest.core===null?'CORE · not published':`CORE · ${fmt(latest.core,4)} (own scale)`;
      $('ola-core-chip').className='ola-chip';
    }
    if(attrPriceLike){
      coreHtml += `<path class="ola-attractor" d="${pathFrom(rows,xfn,p=>py(p.attractor))}"/>`;
      $('ola-attractor-chip').textContent=`ATTRACTOR · ${fmt(latest.attractor,2)}`;
      $('ola-attractor-chip').className='ola-chip';
    }else{
      $('ola-attractor-chip').textContent=latest.attractor===null?'ATTRACTOR · not published':`ATTRACTOR · ${fmt(latest.attractor,4)} (own scale)`;
      $('ola-attractor-chip').className='ola-chip';
    }
    $('ola-core-layer').innerHTML=coreHtml;
    $('ola-core-note').textContent=(corePriceLike||attrPriceLike)
      ? 'CORE / ATTRACTOR показані лише там, де payload дає реальні числові поля у ціновій площині.'
      : 'У поточному live payload немає надійної price-scale пари CORE / ATTRACTOR. Я її не вигадую: графік показує лише реально доступні поля.';

    const thresholdHtml = [
      [0.50,'GEO 50%'],[0.72,'ERL1 72%'],[0.65,'EH1 65%'],[0.42,'GSR1 42%']
    ].map(([v,label])=>{
      const y=ry(v);
      return `<line class="ola-threshold" x1="${left}" y1="${y}" x2="${W-right}" y2="${y}"/><text class="ola-axis" x="${W-right-3}" y="${y-3}" text-anchor="end">${label}</text>`;
    }).join('');

    $('ola-readiness-layer').innerHTML = thresholdHtml
      + `<path class="ola-geometry" d="${pathFrom(rows,xfn,p=>ry(p.geometry))}"/>`
      + `<path class="ola-erl" d="${pathFrom(rows,xfn,p=>ry(p.erl1))}"/>`
      + `<path class="ola-eh" d="${pathFrom(rows,xfn,p=>ry(p.eh1))}"/>`
      + `<path class="ola-gsr" d="${pathFrom(rows,xfn,p=>ry(p.gsrCont))}"/>`;

    const trades = (((j.status||{}).session_trades)||[]);
    const tradeStateIds = new Map();
    if(Array.isArray(trades)){
      for(const t of trades){
        const sid=String(t.state_id||'');
        if(sid)tradeStateIds.set(sid,String(t.action||t.side||'').toUpperCase());
      }
    }

    let markers='';
    rows.forEach((p,i)=>{
      if(p.action!=='BUY' && p.action!=='SELL')return;
      const x=xfn(i), y=py(p.price);
      if(y===null)return;
      const cls=p.action==='BUY'?'ola-marker-buy':'ola-marker-sell';
      const executed=tradeStateIds.has(p.id);
      markers+=`<circle class="${cls}${executed?' ola-marker-exec':''}" data-ola-index="${i}" cx="${x}" cy="${y}" r="${executed?7:5}"><title>${esc(p.id)} · ${esc(p.action)}${executed?' EXECUTED':''} · ${esc(fmt(p.price,2))}</title></circle>`;
    });
    $('ola-marker-layer').innerHTML=markers;
    $('ola-marker-layer').querySelectorAll('[data-ola-index]').forEach(el=>{
      el.style.cursor='pointer';
      el.addEventListener('click',()=>{
        const p=rows[Number(el.dataset.olaIndex)];
        $('ola-inspector').textContent =
          `${p.id} · ${p.action} · price ${fmt(p.price,2)} · geometry ${pct(p.geometry)} · ERL1 ${pct(p.erl1)} · EH1 ${pct(p.eh1)} · blockers ${(p.blockers||[]).join(', ')||'none published'}`;
      });
    });

    $('ola-axis-layer').innerHTML =
      `<text class="ola-axis" x="8" y="${topY0+10}">PRICE</text>`
      + `<text class="ola-axis" x="8" y="${readyY0+10}">READINESS</text>`
      + `<text class="ola-axis" x="${left}" y="420">${esc(rows[0].id||'')}</text>`
      + `<text class="ola-axis" x="${W-right}" y="420" text-anchor="end">${esc(latest.id||'')}</text>`;
  }

  function deltaText(name, now, prev, unit='', inverse=false) {
    now=n(now); prev=n(prev);
    if(now===null || prev===null) return null;
    const d=now-prev;
    if(Math.abs(d)<1e-9)return `${name} без помітної зміни`;
    const arrow=d>0?'↑':'↓';
    const val=unit==='pp'?`${fmt(d*100,1)} pp`:fmt(d,3)+unit;
    const sense=inverse ? (d<0?'краще':'гірше') : (d>0?'зростає':'знижується');
    return `${name} ${arrow} ${val} (${sense})`;
  }

  function blockerHuman(code, action) {
    const side=action==='BUY'?'BUY':action==='SELL'?'SELL':'входу';
    const map={
      NO_DIRECTIONAL_ACTION:`Напрямок ще не сформувався: модель тримає HOLD.`,
      GEOMETRY_NOT_ALIGNED:`Геометрія ще не вирівнялась під ${side}. Потрібна форма ще не настала.`,
      NO_VALIDATED_ACTIVE_FLAG:`Структурне підтвердження ще не накопичене: немає валідованого активного патерну.`,
      GSR1_CONTINUATION_LOW:`Продовження руху ще недостатньо стійке для ${side}.`,
      GSR1_REVERSAL_RISK:`Ризик розвороту зараз занадто великий.`,
      GSR1_WARMUP:`GSR1 ще накопичує історію. Це «ще не сформувалось», а не заборона.`,
      EH1_NOT_READY:`Часовий горизонт ще не дозрів. EH1 поки не READY.`,
      PHASE_FRONT_OPPOSES_ACTION:`Фазовий фронт зараз реально штовхає проти ${side}.`,
      EXCHANGE_PREFLIGHT:`Канал виконання ще не пройшов технічний preflight.`,
      ERL1_SCORE_LOW:`Сумарної якості ще недостатньо: ERL1 нижче порога 0.72.`,
      GEOMETRY_ACTION_TESTNET_ONLY:`Геометричний напрям поки research/testnet-only і не може замінити стратегічний сигнал.`
    };
    return map[code] || code.replaceAll('_',' ').toLowerCase();
  }

  function renderNarrator(history, p, j) {
    $('ola-state-id').textContent=p.id||'—';
    const action=p.action||'HOLD';
    const side=action==='BUY'?'купівлю':action==='SELL'?'продаж':'вхід';

    $('ola-now').textContent =
      `Стан ${p.id||'—'}: режим ${p.regime||'—'}, стратегія ${p.strategy||'—'}, дія ${action}. `
      +(action==='HOLD'
        ? 'Система спостерігає і не має достатньо сформованого напрямку.'
        : `Модель розглядає ${side}, але execution залежить від усіх воріт.`);

    const sees=[];
    if(p.price!==null)sees.push(`ціна ${fmt(p.price,2)} USDT`);
    if(p.core!==null)sees.push(`core ${fmt(p.core,4)}`);
    if(p.attractor!==null)sees.push(`attractor ${fmt(p.attractor,4)}`);
    if(p.geometry!==null)sees.push(`geometry ${pct(p.geometry)} із потрібних 50%`);
    if(p.phase)sees.push(`phase front ${p.phase}${p.propagation?` / ${p.propagation}`:''}`);
    if(p.eh1!==null)sees.push(`EH1 ${pct(p.eh1)} (${p.ehStatus||'—'})`);
    $('ola-sees').textContent=sees.length?`${sees.join('; ')}.`:'Поточні структурні поля ще не опубліковані в status payload.';

    const blockers=(p.blockers||[]);
    $('ola-blocks').textContent=blockers.length
      ? blockers.slice(0,4).map(b=>blockerHuman(b,action)).join(' ')
      : (p.testnetReady||p.strictReady
          ? 'Активних ERL1 blocker-ів немає.'
          : 'Явний список blocker-ів не опублікований для цього стану.');

    const categories={contradict:[],insufficient:[],forming:[]};
    if(action==='HOLD')categories.forming.push('direction');
    if(blockers.includes('PHASE_FRONT_OPPOSES_ACTION'))categories.contradict.push('phase');
    if(blockers.includes('GSR1_REVERSAL_RISK'))categories.contradict.push('reversal');
    if(p.geometry!==null && p.geometry<0.50)categories.insufficient.push('geometry');
    if(p.erl1!==null && p.erl1<0.72)categories.insufficient.push('ERL1');
    if(p.gsrCont!==null && p.gsrCont<0.42)categories.insufficient.push('GSR1');
    if(String(p.ehStatus).toUpperCase()!=='READY')categories.forming.push('EH1');
    if(blockers.includes('NO_VALIDATED_ACTIVE_FLAG'))categories.forming.push('evidence');
    if(blockers.includes('GSR1_WARMUP'))categories.forming.push('GSR1 history');

    const notyet=[];
    if(categories.contradict.length)notyet.push(`Суперечить входу зараз: ${categories.contradict.join(', ')}.`);
    if(categories.insufficient.length)notyet.push(`Є напрям, але сили ще недостатньо: ${categories.insufficient.join(', ')}.`);
    if(categories.forming.length)notyet.push(`Ще не сформувалось: ${categories.forming.join(', ')}.`);
    $('ola-notyet').textContent=notyet.length?notyet.join(' '):'Ключові умови поточного readiness не показують явної незрілості.';

    const pills=$('ola-state-pills');
    pills.innerHTML=[
      categories.contradict.length?`<span class="contradict">СУПЕРЕЧИТЬ · ${esc(categories.contradict.join(', '))}</span>`:'',
      categories.insufficient.length?`<span class="insufficient">НЕДОСТАТНЬО · ${esc(categories.insufficient.join(', '))}</span>`:'',
      categories.forming.length?`<span class="forming">ЩЕ ФОРМУЄТЬСЯ · ${esc(categories.forming.join(', '))}</span>`:''
    ].join('');

    const readyRows=history.filter(x=>x.erl1!==null||x.geometry!==null||x.eh1!==null);
    const prev=readyRows.length>6?readyRows[readyRows.length-7]:readyRows.length>1?readyRows[0]:null;
    const changes=[];
    if(prev){
      for(const txt of [
        deltaText('Geometry',p.geometry,prev.geometry,'pp'),
        deltaText('ERL1',p.erl1,prev.erl1,''),
        deltaText('EH1',p.eh1,prev.eh1,''),
        deltaText('GSR1 continuation',p.gsrCont,prev.gsrCont,'')
      ]) if(txt) changes.push(txt);
      if(p.price!==null&&prev.price!==null){
        const dp=(p.price/prev.price-1)*100;
        changes.push(`Ціна ${dp>=0?'↑':'↓'} ${fmt(dp,3)}% за ${Math.max(1,readyRows.length>6?6:readyRows.length-1)} збережених readiness-станів`);
      }
    }
    $('ola-changing').textContent=changes.length?changes.slice(0,4).join('. ')+'.':'Потрібно щонайменше два live readiness-стани, щоб порахувати Δ і напрям зміни.';

    const next=[];
    if(action==='HOLD')next.push('спочатку потрібен чіткий BUY або SELL сигнал');
    if(p.geometry!==null&&p.geometry<0.50)next.push(`geometry має зрости ще на ${fmt((0.50-p.geometry)*100,0)} pp до 50%`);
    if(p.gsrCont!==null&&p.gsrCont<0.42)next.push(`GSR1 continuation має дійти до 0.42`);
    if(String(p.ehStatus).toUpperCase()!=='READY'||(p.eh1!==null&&p.eh1<0.65))next.push('EH1 має стати READY і пройти 0.65');
    if(p.erl1!==null&&p.erl1<0.72)next.push(`ERL1 має набрати ще ${fmt(0.72-p.erl1,3)} до 0.72`);
    if(!p.tradeability)next.push('tradeability має підтвердити достатній motion budget');
    if(p.evidence<1)next.push('потрібен хоча б один валідований структурний evidence flag');
    if(!p.preflight)next.push('exchange preflight має бути OK');
    $('ola-next').textContent=next.length?`${next.join(' → ')}.`:'Усі опубліковані умови виглядають готовими; далі рішення належить існуючому execution layer.';

    const pass=(ok)=>ok?'pass':'wait';
    const executed=Array.isArray((j.status||{}).session_trades) &&
      (j.status.session_trades||[]).some(t=>String(t.state_id||'')===p.id);
    const trace=[
      ['DIRECTION',action==='BUY'||action==='SELL'],
      ['GEOMETRY',p.geometry!==null&&p.geometry>=0.50&&!blockers.includes('GEOMETRY_NOT_ALIGNED')],
      ['TRADEABILITY',p.tradeability],
      ['EVIDENCE',p.evidence>0&&!blockers.includes('NO_VALIDATED_ACTIVE_FLAG')],
      ['EH1',String(p.ehStatus).toUpperCase()==='READY'&&(p.eh1===null||p.eh1>=0.65)],
      ['ERL1',p.erl1!==null&&p.erl1>=0.72&&!blockers.includes('ERL1_SCORE_LOW')]
    ];
    $('ola-trace').innerHTML=trace.map(([name,ok])=>`<span class="${pass(ok)}">${ok?'✓':'…'} ${name}</span>`).join('')
      +`<span class="${executed?'pass':(p.testnetReady?'wait':'block')}">${executed?'✓ EXECUTED':(p.testnetReady?'READY':'BLOCKED')}</span>`;
  }

  async function refresh() {
    mount();
    if (!$('observer-live-analysis')) return;
    try {
      const r=await fetch('/observer/edge/status',{cache:'no-store',credentials:'same-origin'});
      if(r.status===401||r.status===403)return;
      const j=await r.json();
      if(!r.ok||!j.ok)throw new Error(j.error||`HTTP ${r.status}`);
      let history=loadHistory();
      history=seedRecent(j,history);
      const p=currentPoint(j);
      history=addPoint(history,p);
      renderChart(history,j);
      renderNarrator(history,p,j);
    } catch (e) {
      if($('ola-now'))$('ola-now').textContent=`Live analysis error: ${e.message}`;
    }
  }

  const start=()=>{
    mount();
    refresh();
    setInterval(refresh,POLL_MS);
  };

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);
  else start();
})();

