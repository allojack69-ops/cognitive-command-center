
import json, random, itertools
from pathlib import Path

BASE = Path(__file__).resolve().parent
BOT_BANK = json.loads((BASE/"data"/"bot_stress_v0_1.json").read_text(encoding="utf-8"))
STATE_BANK = json.loads((BASE/"data"/"human_ai_state_v1.json").read_text(encoding="utf-8"))

def bot_item_map():
    return {x["id"]: x for x in BOT_BANK["items"]}

def state_items():
    out=[]
    for p in STATE_BANK["matched_pairs"]:
        for x in p["items"]:
            y=dict(x); y["family"]="STATE"; y["pair_id"]=p["pair_id"]; y["construct"]=p["construct"]; out.append(y)
    for x in STATE_BANK["efp_items"]:
        y=dict(x); y["family"]="EFP"; out.append(y)
    for x in STATE_BANK["grp_base"]:
        y=dict(x); y["family"]="GRP"; out.append(y)
    return out

STATE_ITEMS = state_items()
STATE_MAP = {x["id"]:x for x in STATE_ITEMS}

def constrained_bot_order(seed):
    ids=[x["id"] for x in BOT_BANK["items"]]
    pair_lookup={}
    for p in BOT_BANK["matched_pairs"]:
        pair_lookup[p["a"]]=p["pair_id"]; pair_lookup[p["b"]]=p["pair_id"]
    fam={x["id"]:x.get("family","") for x in BOT_BANK["items"]}
    rng=random.Random(seed)
    for _ in range(12000):
        order=ids[:]; rng.shuffle(order)
        pos={x:i for i,x in enumerate(order)}
        if not all(abs(pos[p["a"]]-pos[p["b"]])>=5 for p in BOT_BANK["matched_pairs"]):
            continue
        if any(x.startswith("PX") for x in order[:4]):
            continue
        if pos.get("PX2",0) < 8:
            continue
        if any(fam[order[i]]==fam[order[i+1]]==fam[order[i+2]] for i in range(len(order)-2)):
            continue
        return order
    # deterministic fallback with pair spacing
    return ["MR1A","EM1A","HY1A","MR2A","EM2A","D1","HY2A","D2","EM1B","PX1",
            "MR1B","D3","HY1B","PX2","MR2B","D4","HY2B","PX3","EM2B","PX4"]

def constrained_state_order(seed):
    """18-item participant-seeded order with hard invariants.
    First five are easy, diagnostic items are later, and every matched pair
    is separated by at least five positions.
    """
    rng=random.Random(seed)
    pair_ids=[p["pair_id"] for p in STATE_BANK["matched_pairs"]]
    easy_pair_ids=["SD1","SD2","SD3","SD4"]
    medium_pair_ids=["SD5","SD6"]

    orientation={}
    for pair_id in pair_ids:
        members=[pair_id+"A",pair_id+"B"]
        rng.shuffle(members)
        orientation[pair_id]=(members[0],members[1])

    arr=[None]*18
    easy_pairs=easy_pair_ids[:]; rng.shuffle(easy_pairs)
    medium_pairs=medium_pair_ids[:]; rng.shuffle(medium_pairs)
    early_pair_order=easy_pairs+medium_pairs
    early_slots=[0,1,3,4,6,8]
    early_pos={}
    for slot,pair_id in zip(early_slots,early_pair_order):
        arr[slot]=orientation[pair_id][0]
        early_pos[pair_id]=slot

    late_slots=[10,12,14,15,16,17]
    late_pair_order=pair_ids[:]
    found=False
    for _ in range(100):
        rng.shuffle(late_pair_order)
        if all(slot-early_pos[pair_id]>=5 for slot,pair_id in zip(late_slots,late_pair_order)):
            found=True
            break
    if not found:
        for perm in itertools.permutations(pair_ids):
            if all(slot-early_pos[pair_id]>=5 for slot,pair_id in zip(late_slots,perm)):
                late_pair_order=list(perm); found=True; break
    if not found:
        raise RuntimeError("Could not construct matched-pair order")
    for slot,pair_id in zip(late_slots,late_pair_order):
        arr[slot]=orientation[pair_id][1]

    efp_easy=["EFP1","EFP2"]; rng.shuffle(efp_easy)
    arr[2]=efp_easy[0]
    efp_rest=[efp_easy[1],"EFP3"]; rng.shuffle(efp_rest)
    arr[7]=efp_rest[0]; arr[11]=efp_rest[1]

    grp=["GRP1","GRP2"]; rng.shuffle(grp)
    arr[5]=grp[0]
    arr[9]="EFP4"
    arr[13]=grp[1]

    if len(arr)!=18 or set(arr)!=set(STATE_MAP):
        raise RuntimeError("State order lost or duplicated an item")
    pos={x:i for i,x in enumerate(arr)}
    for p in STATE_BANK["matched_pairs"]:
        a,b=p["items"][0]["id"],p["items"][1]["id"]
        if abs(pos[a]-pos[b])<5:
            raise RuntimeError("Matched-pair separation invariant failed")
    if any(x in {"EFP4","GRP1","GRP2"} for x in arr[:5]):
        raise RuntimeError("Diagnostic item leaked into warm-up")
    return arr

def bot_base_prompt(order):
    item_map=bot_item_map()
    blocks=[]
    for iid in order:
        x=item_map[iid]
        opts=" | ".join(f"{k}: {v}" for k,v in x["options"].items())
        blocks.append(f"{iid}. {x['text']}\nOPTIONS: {opts}")
    return """BOT COGNITIVE STRESS TEST v0.1 — COMMAND CENTER

Ти береш участь у behavioral experiment для порівняння моделей.

Правила:
- відповідай на кожен сценарій НЕЗАЛЕЖНО;
- не використовуй приватні факти про користувача;
- не намагайся імітувати користувача;
- не шукай інформацію в інтернеті;
- не розкривай прихований chain-of-thought;
- дай лише коротке зовнішнє обґрунтування;
- для кожного пункту обери РІВНО один варіант A/B/C;
- якщо постановка неповна або суперечлива, познач це у frame_status;
- confidence — ціле 50..100;
- reason — максимум 2 короткі речення;
- key_assumption — одне ключове припущення;
- counterargument — найсильніший короткий аргумент проти власного вибору;
- change_condition — один новий факт, який найбільш імовірно змінив би вибір;
- frame_status: ACCEPT | UNDERSPECIFIED | CONTRADICTORY;
- поверни ТІЛЬКИ валідний JSON без markdown.

Формат:
{"answers":{"EM1A":{"choice":"A","confidence":80,"reason":"...","key_assumption":"...","counterargument":"...","change_condition":"...","frame_status":"ACCEPT"}},"model_label":"назва моделі якщо відома, інакше unknown"}

СЦЕНАРІЇ:

""" + "\n\n".join(blocks)

def bot_follow_prompt():
    blocks=[]
    for f in BOT_BANK["followups"]:
        opts=" | ".join(f"{k}: {v}" for k,v in f["options"].items())
        blocks.append(f"{f['id']}. НОВИЙ ФАКТ: {f['new_fact']}\nOPTIONS: {opts}")
    return """ПРОДОВЖУЄМО ТОЙ САМИЙ BOT COGNITIVE STRESS TEST У ЦЬОМУ Ж ЧАТІ.

Ти вже дав базові відповіді. Нижче з'являються нові факти.

Правила:
- не переписуй попередню відповідь;
- для кожного пункту обери один новий варіант;
- confidence — 50..100;
- reason — максимум 2 короткі речення;
- assumption_broken — true/false: чи руйнує новий факт твоє попереднє ключове припущення;
- previous_reason_still_valid — true/false;
- frame_status: ACCEPT | UNDERSPECIFIED | CONTRADICTORY;
- поверни ТІЛЬКИ валідний JSON без markdown.

Формат:
{"updates":{"MR2B":{"choice":"A","confidence":90,"reason":"...","assumption_broken":true,"previous_reason_still_valid":false,"frame_status":"ACCEPT"}}}

НОВІ ФАКТИ:

""" + "\n\n".join(blocks)

def state_base_prompt(order):
    blocks=[]
    for iid in order:
        x=STATE_MAP[iid]
        opts=" | ".join(f"{k}: {v}" for k,v in x["options"].items())
        blocks.append(f"{iid}. {x['human_text']}\nOPTIONS: {opts}")
    return """Ти береш участь у behavioral experiment.

Відповідай на кожну ситуацію НЕЗАЛЕЖНО і природно, виходячи з того, як ти міркуєш у своєму поточному персоналізованому стані.

Важливо:
- це має бути НОВИЙ звичайний чат у звичному особистому акаунті користувача;
- не використовуй відповіді людини з людської частини тесту;
- не намагайся імітувати користувача;
- не розкривай приватні факти;
- для кожного пункту обери рівно один варіант;
- confidence — 50..100;
- reason — максимум одне коротке речення;
- поверни ТІЛЬКИ валідний JSON без markdown.

Формат:
{"answers":{"SD1A":{"choice":"A","confidence":80,"reason":"..."}},"model_label":"назва моделі якщо відома, інакше unknown"}

СИТУАЦІЇ:

""" + "\n\n".join(blocks)

def state_follow_prompt():
    blocks=[]
    for g in STATE_BANK["grp_base"]:
        opts=" | ".join(f"{k}: {v}" for k,v in g["update_options"].items())
        blocks.append(f"{g['id']}. НОВИЙ ФАКТ: {g['surprise']}\nOPTIONS: {opts}")
    return """Продовжуємо той самий behavioral experiment у ЦЬОМУ Ж чаті.

Ти щойно дав початкові відповіді. Тепер з'явилась нова інформація.
Для кожного пункту обери один варіант після нового факту.
confidence — 50..100; reason — максимум одне коротке речення.
Поверни ТІЛЬКИ валідний JSON без markdown.

Формат:
{"updates":{"GRP1":{"choice":"B","confidence":85,"reason":"..."}}}

""" + "\n\n".join(blocks)
