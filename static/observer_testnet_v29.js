
(() => {
  if (window.__observerTestnetV29Loaded) return;
  window.__observerTestnetV29Loaded = true;

  const path = window.location.pathname.replace(/\/+$/, "");
  if (path !== "/observer/control") return;

  function installStyle() {
    if (document.getElementById("observer-testnet-v29-style")) return;
    const style = document.createElement("style");
    style.id = "observer-testnet-v29-style";
    style.textContent = `
      .tn29-card{margin:14px 0 18px;padding:0;overflow:hidden;border:1px solid rgba(74,190,230,.34);border-radius:18px;background:linear-gradient(145deg,rgba(4,19,29,.90),rgba(5,13,22,.82))}
      .tn29-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:17px 18px;border-bottom:1px solid rgba(78,143,180,.18)}
      .tn29-kicker{color:#73ddff;font-size:9px;font-weight:900;letter-spacing:.13em}.tn29-head h2{margin:5px 0 4px;font-size:25px}.tn29-head p{margin:0;color:#91a7b8;font-size:11px;line-height:1.45}
      .tn29-badge{padding:7px 10px;border-radius:999px;border:1px solid rgba(123,157,180,.3);color:#93a9ba;font-size:9px;font-weight:900;letter-spacing:.08em;white-space:nowrap}
      .tn29-badge.ready,.tn29-badge.running{color:#7df0b8;border-color:rgba(73,220,158,.5);background:rgba(17,76,55,.28)}
      .tn29-badge.error{color:#ffd07c;border-color:rgba(236,177,73,.42);background:rgba(77,54,17,.2)}
      .tn29-body{padding:16px 18px 18px}.tn29-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
      .tn29-kpi{padding:11px 12px;border:1px solid rgba(80,138,176,.2);border-radius:12px;background:rgba(2,10,17,.48)}
      .tn29-kpi span{display:block;color:#6f899e;font-size:7px;font-weight:900;letter-spacing:.09em}.tn29-kpi b{display:block;margin-top:6px;font-size:12px;overflow-wrap:anywhere}
      .tn29-kpi b.pos{color:#7dedb7}.tn29-kpi b.neg{color:#ff9ba3}
      .tn29-form{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:14px}.tn29-form label{color:#7891a4;font-size:8px;font-weight:850}
      .tn29-form input{width:100%;box-sizing:border-box;margin-top:6px;padding:11px 10px;border:1px solid rgba(92,147,184,.28);border-radius:10px;background:rgba(2,9,16,.72);color:#eff8ff;font-size:12px}
      .tn29-actions{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:8px;margin-top:11px}.tn29-actions button{min-height:46px;border-radius:11px;font-size:9px;font-weight:950;letter-spacing:.04em}
      .tn29-start{border:1px solid rgba(72,216,164,.48);background:linear-gradient(135deg,rgba(18,99,72,.74),rgba(8,50,39,.78));color:#c9f9e4}
      .tn29-close{border:1px solid rgba(82,184,230,.42);background:rgba(15,57,80,.68);color:#c8efff}.tn29-stop{border:1px solid rgba(255,107,117,.43);background:rgba(87,27,32,.66);color:#ffc8cd}
      .tn29-actions button:disabled{opacity:.38}.tn29-msg{margin-top:10px;padding:10px 11px;border:1px solid rgba(82,137,175,.2);border-radius:10px;color:#91cae4;font-size:10px;line-height:1.45}
      .tn29-msg.error{color:#ffb1b7;border-color:rgba(239,93,103,.32)}.tn29-note{margin:9px 0 0;color:#73899b;font-size:8px;line-height:1.5}
      .tn29-last{margin-top:10px;color:#7f98aa;font-size:9px;line-height:1.45}.tn29-real-label{display:inline-block;margin-left:7px;padding:3px 6px;border-radius:999px;border:1px solid rgba(255,100,108,.35);color:#ff9aa1;font-size:7px;font-weight:900;letter-spacing:.08em}
      @media(max-width:700px){.tn29-grid{grid-template-columns:1fr 1fr}.tn29-form{grid-template-columns:1fr 1fr}.tn29-actions{grid-template-columns:1fr}}
      @media(max-width:390px){.tn29-form{grid-template-columns:1fr}.tn29-head h2{font-size:22px}}
    `;
    document.head.appendChild(style);
  }

  function cardHTML() {
    return `
      <section class="tn29-card" id="observer-testnet-v29">
        <div class="tn29-head">
          <div>
            <div class="tn29-kicker">FAKE MONEY · REAL EXCHANGE API</div>
            <h2>Binance Spot Testnet</h2>
            <p>Observer opens and closes real Testnet orders with virtual BTC/USDT. No real-money key is used.</p>
          </div>
          <div class="tn29-badge" id="tn29-badge">CHECKING</div>
        </div>
        <div class="tn29-body">
          <div class="tn29-grid">
            <div class="tn29-kpi"><span>MODE</span><b id="tn29-mode">OFF</b></div>
            <div class="tn29-kpi"><span>STATE</span><b id="tn29-state">—</b></div>
            <div class="tn29-kpi"><span>TESTNET USDT</span><b id="tn29-usdt">—</b></div>
            <div class="tn29-kpi"><span>BOT POSITION</span><b id="tn29-position">—</b></div>
            <div class="tn29-kpi"><span>SESSION P/L</span><b id="tn29-pnl">—</b></div>
            <div class="tn29-kpi"><span>FILLS</span><b id="tn29-fills">—</b></div>
          </div>

          <div class="tn29-form">
            <label>Max one order, USDT<input id="tn29-max-order" type="number" min="1" max="100" step="1" value="10"></label>
            <label>Max test fills<input id="tn29-max-fills" type="number" min="1" max="100" step="1" value="20"></label>
            <label>Run time, minutes<input id="tn29-max-minutes" type="number" min="5" max="360" step="5" value="120"></label>
            <label>Confirmation<input id="tn29-confirm" type="text" autocomplete="off" placeholder="type TESTNET"></label>
          </div>

          <div class="tn29-actions">
            <button class="tn29-start" id="tn29-start" type="button">START TESTNET TRADING</button>
            <button class="tn29-close" id="tn29-close" type="button">CLOSE TEST POSITION</button>
            <button class="tn29-stop" id="tn29-stop" type="button">STOP & FLATTEN</button>
          </div>

          <div class="tn29-msg" id="tn29-msg">Checking Testnet connection…</div>
          <div class="tn29-last" id="tn29-last"></div>
          <p class="tn29-note">Separate Render secrets required: BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET. Testnet state/checkpoints and fills are backed up to the database.</p>
        </div>
      </section>`;
  }

  function init() {
    const root = document.getElementById("observer-console");
    if (!root) return;
    installStyle();

    const liveCard = document.querySelector(".observer-canary-card");
    if (liveCard) {
      liveCard.open = false;
      const title = liveCard.querySelector("summary b");
      if (title && !title.dataset.tn29) {
        title.dataset.tn29 = "1";
        title.insertAdjacentHTML("beforeend", '<span class="tn29-real-label">REAL MONEY</span>');
      }
    }

    if (!document.getElementById("observer-testnet-v29")) {
      const anchor = liveCard || document.querySelector(".observer-kpis");
      anchor.insertAdjacentHTML("beforebegin", cardHTML());
    }

    const csrf = root.dataset.csrf;
    const $ = (id) => document.getElementById(id);
    let busy = false;

    const fmt = (v, digits=2) => {
      const n = Number(v);
      return Number.isFinite(n) ? n.toLocaleString(undefined, {maximumFractionDigits: digits}) : "—";
    };

    function render(t) {
      const badge = $("tn29-badge");
      if (!t.credentials_ready) {
        badge.textContent = "KEYS MISSING";
        badge.className = "tn29-badge error";
      } else if (!t.ok) {
        badge.textContent = "API ERROR";
        badge.className = "tn29-badge error";
      } else if (t.active) {
        badge.textContent = "RUNNING";
        badge.className = "tn29-badge running";
      } else {
        badge.textContent = "READY";
        badge.className = "tn29-badge ready";
      }

      $("tn29-mode").textContent = t.active ? "TESTNET LIVE" : "OFF";
      $("tn29-state").textContent = `${t.state_id || "—"} · ${t.action || "—"}`;
      $("tn29-usdt").textContent = t.usdt == null ? "—" : fmt(t.usdt, 2);
      $("tn29-position").textContent = t.bot_position_value_usdt == null
        ? "—"
        : `${fmt(t.bot_position_btc, 8)} BTC · ${fmt(t.bot_position_value_usdt, 2)} USDT`;
      const pnl = $("tn29-pnl");
      pnl.textContent = t.session_pnl_usdt == null ? "—" : `${Number(t.session_pnl_usdt) >= 0 ? "+" : ""}${fmt(t.session_pnl_usdt, 2)} USDT`;
      pnl.className = Number(t.session_pnl_usdt || 0) >= 0 ? "pos" : "neg";
      $("tn29-fills").textContent = `${t.fills || 0}/${t.max_fills || "—"}`;

      const msg = $("tn29-msg");
      if (!t.credentials_ready) {
        msg.textContent = "Add BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET in Render → Environment.";
        msg.className = "tn29-msg error";
      } else if (!t.ok) {
        msg.textContent = `Testnet API error: ${t.reason || "unknown"}`;
        msg.className = "tn29-msg error";
      } else if (t.active) {
        msg.textContent = `${t.position_open ? "POSITION OPEN" : "FLAT"} · max order ${t.max_order_usdt || "—"} USDT · auto-stop ${t.max_minutes || "—"} min`;
        msg.className = "tn29-msg";
      } else {
        msg.textContent = "Ready. Type TESTNET and start fake-money exchange trading.";
        msg.className = "tn29-msg";
      }

      if (t.latest_trade) {
        const x = t.latest_trade;
        $("tn29-last").textContent = `Last fill: ${x.action || "—"} · ${x.cummulative_quote_qty || x.notional_usdt || "—"} USDT · order ${x.order_id || "—"}`;
      } else {
        $("tn29-last").textContent = "No Testnet fills in this session yet.";
      }

      $("tn29-start").disabled = busy || t.active || !t.credentials_ready || !t.ok;
      $("tn29-close").disabled = busy || !t.position_open;
      $("tn29-stop").disabled = busy || !t.active;

      // The ordinary top STOP/RESTART can kill any child process, but during
      // TESTNET we want the safe path that first flattens and saves Testnet.
      const topStop = document.getElementById("btn-stop");
      const topRestart = document.getElementById("btn-restart");
      if (topStop) topStop.disabled = !!t.active;
      if (topRestart) topRestart.disabled = !!t.active;
    }

    async function status() {
      try {
        const r = await fetch("/observer/testnet/api/status", {headers: {"Accept": "application/json"}, cache: "no-store"});
        const data = await r.json();
        if (data.testnet) render(data.testnet);
      } catch (err) {
        const msg = $("tn29-msg");
        msg.textContent = `Status error: ${err.message}`;
        msg.className = "tn29-msg error";
      }
    }

    async function act(kind) {
      if (busy) return;
      busy = true;
      const routes = {
        start: ["/observer/testnet/api/start", {
          max_order_usdt: $("tn29-max-order").value,
          max_fills: $("tn29-max-fills").value,
          max_minutes: $("tn29-max-minutes").value,
          confirm: $("tn29-confirm").value.trim()
        }],
        close: ["/observer/testnet/api/close", {}],
        stop: ["/observer/testnet/api/stop", {}]
      };
      const [url, body] = routes[kind];
      const msg = $("tn29-msg");
      msg.textContent = kind === "start" ? "Starting Binance Spot Testnet…" : "Processing…";
      msg.className = "tn29-msg";
      try {
        const r = await fetch(url, {
          method: "POST",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf
          },
          body: JSON.stringify(body)
        });
        const data = await r.json();
        msg.textContent = data.message || (data.ok ? "OK" : "Blocked");
        msg.className = "tn29-msg" + (data.ok ? "" : " error");
        if (data.testnet) render(data.testnet);
      } catch (err) {
        msg.textContent = err.message;
        msg.className = "tn29-msg error";
      } finally {
        busy = false;
        await status();
      }
    }

    $("tn29-start").addEventListener("click", () => act("start"));
    $("tn29-close").addEventListener("click", () => act("close"));
    $("tn29-stop").addEventListener("click", () => act("stop"));

    status();
    window.setInterval(status, 8000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, {once: true});
  } else {
    init();
  }
})();
