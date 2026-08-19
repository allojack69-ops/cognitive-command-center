
import os
os.environ["DATABASE_URL"]="sqlite:///:memory:"
os.environ["COOKIE_SECURE"]="0"
os.environ["ADMIN_KEY"]="test"
from app import app
from studies import constrained_bot_order, BOT_BANK, constrained_state_order, STATE_MAP

def test_orders():
    o=constrained_bot_order(123)
    assert len(o)==20 and len(set(o))==20
    pos={x:i for i,x in enumerate(o)}
    for p in BOT_BANK["matched_pairs"]:
        assert abs(pos[p["a"]]-pos[p["b"]])>=5
    s=constrained_state_order(123)
    assert len(s)==18 and set(s)==set(STATE_MAP)

def test_home():
    c=app.test_client()
    r=c.get("/")
    assert r.status_code==200
    assert b"COGNITIVE COMMAND CENTER" in r.data
