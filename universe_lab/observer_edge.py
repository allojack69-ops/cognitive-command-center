
import hashlib
import json
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from db import Event, SessionLocal
from . import observer_control as oc

bp = Blueprint("universe_observer_edge", __name__, url_prefix="/observer/edge")

COMMAND_EVENT = "observer_edge_command_v1"
HEARTBEAT_EVENT = "observer_edge_heartbeat_v1"
STATUS_EVENT = "observer_edge_status_v1"
TRADE_EVENT = "observer_edge_trade_v1"
CHECKPOINT_EVENT = "observer_edge_checkpoint_v1"
AGENT_VERSION = "1.0"
BOOTSTRAP_TOKEN_HASH = "533dfddcdbbe010162fdb2d0f4ff6bdb959bd8880fd5fa6c6b88d2581db3063d"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _payload(event):
    try:
        return json.loads(event.payload_json or "{}")
    except Exception:
        return {}


def _prune(db, event_type, keep):
    rows = db.scalars(
        select(Event)
        .where(Event.event_type == event_type)
        .order_by(Event.created_at.desc(), Event.id.desc())
        .offset(keep)
    ).all()
    for row in rows:
        db.delete(row)


def _admin_require():
    oc._require_admin()
    oc._check_csrf()


def _agent_authorized():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    if not token:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return bool(
        BOOTSTRAP_TOKEN_HASH
        and secrets.compare_digest(digest, BOOTSTRAP_TOKEN_HASH)
    )


def _require_agent():
    if not _agent_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


def _latest(db, event_type):
    return db.scalar(
        select(Event)
        .where(Event.event_type == event_type)
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(1)
    )


def _snapshot():
    with SessionLocal() as db:
        heartbeat = _latest(db, HEARTBEAT_EVENT)
        status = _latest(db, STATUS_EVENT)
        trade = _latest(db, TRADE_EVENT)
        checkpoint = _latest(db, CHECKPOINT_EVENT)
        commands = db.scalars(
            select(Event)
            .where(Event.event_type == COMMAND_EVENT)
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(40)
        ).all()

        age = None
        online = False
        if heartbeat and heartbeat.created_at:
            created = heartbeat.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = max(
                0.0,
                (datetime.now(timezone.utc) - created).total_seconds(),
            )
            online = age <= 18.0

        pending = []
        for row in commands:
            p = _payload(row)
            if p.get("status", "pending") == "pending":
                pending.append({"event_id": row.id, **p})

        cp = _payload(checkpoint) if checkpoint else {}
        return {
            "ok": True,
            "version": AGENT_VERSION,
            "online": online,
            "heartbeat_age_s": age,
            "heartbeat": _payload(heartbeat) if heartbeat else {},
            "status": _payload(status) if status else {},
            "latest_trade": _payload(trade) if trade else None,
            "checkpoint": {
                "exists": bool(checkpoint),
                "saved_at": cp.get("saved_at"),
                "state_id": cp.get("state_id"),
            },
            "pending_commands": list(reversed(pending)),
        }


def _augment_live_snapshot(data):
    status_payload = data.get("status")
    if not isinstance(status_payload, dict):
        return data

    runtime_snapshot = status_payload.get("runtime_status")
    if not isinstance(runtime_snapshot, dict) or not runtime_snapshot:
        return data

    local_logs = status_payload.get("runtime_log_tail")
    if not isinstance(local_logs, list):
        local_logs = []
    local_logs = [str(x).rstrip("\n") for x in local_logs[-100:]]

    try:
        observer = oc._summarize(
            runtime_snapshot,
            "termux:observer_status.json",
        )
        current = runtime_snapshot.get("current_state") or {}
        states = (
            (runtime_snapshot.get("recent") or {}).get("states")
            or []
        )
        observer["opportunity"] = oc._entry_opportunity(
            current,
            states,
            local_logs,
        )
        data["observer"] = observer
        data["logs"] = local_logs
    except Exception as exc:
        data["summary_error"] = (
            f"{type(exc).__name__}: {exc}"
        )[:240]
        return data

    started_at = status_payload.get("started_at")
    uptime_sec = None
    if started_at:
        try:
            started = datetime.fromisoformat(
                str(started_at).replace("Z", "+00:00")
            )
            uptime_sec = max(
                0,
                int(
                    (datetime.now(timezone.utc) - started)
                    .total_seconds()
                ),
            )
        except Exception:
            pass

    data["process"] = {
        "pid": status_payload.get("pid"),
        "alive": bool(
            data.get("online")
            and status_payload.get("active")
        ),
        "started_at": started_at,
        "uptime_sec": uptime_sec,
        "runtime_dir": (
            "termux:~/cognitive-command-center/"
            "observer_runtime"
        ),
        "runtime_dir_exists": True,
        "command": ["termux-agent", "observer_runtime"],
        "engine_attached": True,
    }
    return data


@bp.get("/status")
def admin_status():
    oc._require_admin()
    return jsonify(_augment_live_snapshot(_snapshot()))


@bp.post("/command")
def create_command():
    _admin_require()
    data = request.get_json(silent=True) or {}
    command = str(data.get("command") or "").lower().strip()
    if command not in {"start", "stop", "close", "status"}:
        return jsonify({"ok": False, "error": "invalid command"}), 400

    if command == "start":
        live = _snapshot()
        live_status = live.get("status") or {}
        pending = live.get("pending_commands") or []
        if live.get("online") and live_status.get("active"):
            return jsonify({
                "ok": False,
                "error": "already_running",
                "message": "Testnet runtime is already running.",
            }), 409
        if any(item.get("command") == "start" for item in pending):
            return jsonify({
                "ok": False,
                "error": "start_already_queued",
                "message": "START is already queued.",
            }), 409

    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    if command == "start":
        try:
            max_order = float(params.get("max_order_usdt", 10))
            max_fills = int(params.get("max_fills", 20))
            max_minutes = int(params.get("max_minutes", 120))
        except Exception:
            return jsonify({"ok": False, "error": "invalid limits"}), 400

        if not (1 <= max_order <= 25):
            return jsonify({"ok": False, "error": "max_order_usdt must be 1..25"}), 400
        if not (1 <= max_fills <= 100):
            return jsonify({"ok": False, "error": "max_fills must be 1..100"}), 400
        if not (5 <= max_minutes <= 360):
            return jsonify({"ok": False, "error": "max_minutes must be 5..360"}), 400

        params = {
            "max_order_usdt": max_order,
            "max_fills": max_fills,
            "max_minutes": max_minutes,
            "testnet_only": True,
        }

    payload = {
        "command_id": uuid4().hex,
        "command": command,
        "params": params,
        "status": "pending",
        "created_at": _now_iso(),
    }

    with SessionLocal() as db:
        row = Event(
            event_type=COMMAND_EVENT,
            payload_json=json.dumps(payload),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        event_id = row.id
        _prune(db, COMMAND_EVENT, 100)
        db.commit()

    return jsonify({"ok": True, "event_id": event_id, **payload})


@bp.get("/agent/poll")
def agent_poll():
    denied = _require_agent()
    if denied:
        return denied

    with SessionLocal() as db:
        rows = db.scalars(
            select(Event)
            .where(Event.event_type == COMMAND_EVENT)
            .order_by(Event.created_at.asc(), Event.id.asc())
            .limit(100)
        ).all()
        commands = []
        for row in rows:
            p = _payload(row)
            if p.get("status", "pending") == "pending":
                commands.append({"event_id": row.id, **p})

    return jsonify({
        "ok": True,
        "server_time": _now_iso(),
        "commands": commands[-20:],
    })


@bp.post("/agent/report")
def agent_report():
    denied = _require_agent()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "heartbeat").lower()
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    payload = {
        **payload,
        "received_at": _now_iso(),
        "agent_version": AGENT_VERSION,
    }
    ack = data.get("ack") if isinstance(data.get("ack"), list) else []

    event_map = {
        "heartbeat": HEARTBEAT_EVENT,
        "status": STATUS_EVENT,
        "trade": TRADE_EVENT,
        "checkpoint": CHECKPOINT_EVENT,
    }
    event_type = event_map.get(kind)
    if not event_type:
        return jsonify({"ok": False, "error": "invalid report kind"}), 400

    with SessionLocal() as db:
        db.add(Event(
            event_type=event_type,
            payload_json=json.dumps(payload, ensure_ascii=False),
        ))

        ack_ids = set()
        for item in ack:
            try:
                ack_ids.add(int(item))
            except Exception:
                pass

        if ack_ids:
            rows = db.scalars(
                select(Event).where(
                    Event.id.in_(ack_ids),
                    Event.event_type == COMMAND_EVENT,
                )
            ).all()
            for row in rows:
                p = _payload(row)
                p["status"] = "acked"
                p["acked_at"] = _now_iso()
                row.payload_json = json.dumps(p, ensure_ascii=False)

        keep = {
            HEARTBEAT_EVENT: 120,
            STATUS_EVENT: 120,
            TRADE_EVENT: 500,
            CHECKPOINT_EVENT: 12,
        }[event_type]
        _prune(db, event_type, keep)
        db.commit()

    return jsonify({"ok": True})


EDGE_PANEL = r'''
<section class="card edge-node-card" id="edge-node-card">
  <style>
    .edge-node-card{margin:28px 0;border-color:rgba(100,220,255,.32);background:rgba(3,18,29,.78)}
    .edge-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
    .edge-head h2{margin:.15rem 0}
    .edge-badge{border:1px solid #486070;border-radius:999px;padding:8px 13px;font-weight:800;font-size:.82rem}
    .edge-badge.online{color:#7ef0bd;border-color:#2f8d68}
    .edge-badge.offline{color:#ffcf7b;border-color:#846331}
    .edge-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:20px 0}
    .edge-grid>div{border:1px solid rgba(130,180,215,.22);border-radius:16px;padding:14px}
    .edge-grid span{display:block;font-size:.72rem;letter-spacing:.08em;color:#91a5ba;text-transform:uppercase}
    .edge-grid b{display:block;margin-top:6px;font-size:1.15rem}
    .edge-controls{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
    .edge-controls button{min-height:52px;border-radius:14px;font-weight:800}
    .edge-start{background:#0c6048;color:white}
    .edge-close{background:#12324a;color:white}
    .edge-stop{background:#6a2028;color:white}
    .edge-note{font-size:.82rem;color:#94a8bb;margin-top:12px;word-break:break-word}
    @media(max-width:640px){
      .edge-controls{grid-template-columns:1fr}
      .edge-grid{grid-template-columns:1fr 1fr}
    }
  </style>
  <div class="edge-head">
    <div>
      <div class="eyebrow">ACTIVE TESTNET PATH · PHONE EXECUTION NODE</div>
      <h2>Termux Observer Agent</h2>
      <p class="muted">Website controls it. Your phone talks directly to Binance Spot Testnet. No real money.</p>
    </div>
    <span class="edge-badge offline" id="edge-badge">OFFLINE</span>
  </div>
  <div class="edge-grid">
    <div><span>MODE</span><b id="edge-mode">OFF</b></div>
    <div><span>STATE</span><b id="edge-state">—</b></div>
    <div><span>TESTNET USDT</span><b id="edge-usdt">—</b></div>
    <div><span>BOT POSITION</span><b id="edge-position">—</b></div>
    <div><span>SESSION P/L</span><b id="edge-pnl">—</b></div>
    <div><span>FILLS</span><b id="edge-fills">0</b></div>
  </div>
  <div class="edge-controls">
    <button class="edge-start" id="edge-start" type="button">▶ START · 120 MIN</button>
    <button class="edge-close" id="edge-close" type="button">CLOSE TEST POSITION</button>
    <button class="edge-stop" id="edge-stop" type="button">■ STOP & FLATTEN</button>
  </div>
  <div class="edge-note" id="edge-note">Waiting for phone heartbeat.</div>
</section>
<script>
(() => {
  const root=document.getElementById('observer-console'); if(!root)return;
  const csrf=root.dataset.csrf; const $=id=>document.getElementById(id);
  const post=async(url,body)=>{
    const r=await fetch(url,{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify(body||{})
    });
    const j=await r.json();
    if(!r.ok||!j.ok)throw new Error(j.error||j.message||('HTTP '+r.status));
    return j;
  };
  const fmt=n=>{
    n=Number(n);
    return Number.isFinite(n)
      ? n.toLocaleString(undefined,{maximumFractionDigits:4})
      : '—';
  };
  async function refresh(){
    try{
      const r=await fetch('/observer/edge/status',{cache:'no-store'});
      const j=await r.json();
      const s=j.status||{},a=s.account||{};
      $('edge-badge').textContent=j.online?'ONLINE':'OFFLINE';
      $('edge-badge').className='edge-badge '+(j.online?'online':'offline');
      $('edge-mode').textContent=s.active?'TESTNET LIVE':'OFF';
      $('edge-state').textContent=s.state_id||'—';
      $('edge-usdt').textContent=fmt(a.usdt);
      $('edge-position').textContent=s.bot_position_btc!=null
        ? fmt(s.bot_position_btc)+' BTC':'—';
      $('edge-pnl').textContent=s.session_pnl_usdt!=null
        ? fmt(s.session_pnl_usdt)+' USDT':'—';
      $('edge-fills').textContent=s.fills??0;
      $('edge-note').textContent=j.online
        ? 'Phone heartbeat '+Math.round(j.heartbeat_age_s||0)+'s ago · DB backup '+(j.checkpoint.exists?'ACTIVE':'WARMUP')+' · queued '+j.pending_commands.length
        : 'Phone agent offline. The local daemon will reconnect automatically when running.';
    }catch(e){
      $('edge-note').textContent='Edge status error: '+e.message;
    }
  }
  $('edge-start').onclick=async()=>{
    try{
      await post('/observer/edge/command',{
        command:'start',
        params:{max_order_usdt:10,max_fills:20,max_minutes:120}
      });
      $('edge-note').textContent='START queued for phone.';
    }catch(e){$('edge-note').textContent=e.message}
  };
  $('edge-close').onclick=async()=>{
    try{
      await post('/observer/edge/command',{command:'close'});
      $('edge-note').textContent='CLOSE queued.';
    }catch(e){$('edge-note').textContent=e.message}
  };
  $('edge-stop').onclick=async()=>{
    try{
      await post('/observer/edge/command',{command:'stop'});
      $('edge-note').textContent='STOP & FLATTEN queued.';
    }catch(e){$('edge-note').textContent=e.message}
  };
  refresh();
  setInterval(refresh,3000);
})();
</script>
'''


@bp.after_app_request
def inject_edge_panel(response):
    if request.path != "/observer/control":
        return response
    try:
        if not response.content_type or "text/html" not in response.content_type:
            return response
        html = response.get_data(as_text=True)
        marker = '<section class="observer-kpis">'
        if marker in html and 'id="edge-node-card"' not in html:
            html = html.replace(marker, EDGE_PANEL + "\n" + marker, 1)
            response.set_data(html)
            response.headers.pop("Content-Length", None)
    except Exception:
        pass
    return response

