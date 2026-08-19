
from statistics import mean
import math
from studies import BOT_BANK, STATE_BANK

def _mean(xs):
    return round(mean(xs),2) if xs else None

def answer_map(answers, actor="ai"):
    return {(a.item_id,a.phase):a for a in answers if a.actor==actor}

def classify_update(prev, upd):
    if prev and prev.frame_status in ("UNDERSPECIFIED","CONTRADICTORY") and upd.frame_status=="ACCEPT":
        return "FRAME_RESOLUTION"
    if upd.assumption_broken == 1:
        return "ASSUMPTION_BREAK"
    if prev and prev.choice == upd.choice and upd.previous_reason_still_valid == 1:
        return "REINFORCEMENT"
    if prev and prev.choice != upd.choice:
        return "EVIDENCE_UPDATE"
    if prev and upd.previous_reason_still_valid == 0:
        return "REASON_UPDATE"
    return "OTHER"

def bot_metrics(answers):
    m=answer_map(answers,"ai")
    base=[a for (iid,ph),a in m.items() if ph=="base"]
    pairs=[]
    for p in BOT_BANK["matched_pairs"]:
        a=m.get((p["a"],"base")); b=m.get((p["b"],"base"))
        if a and b:
            pairs.append({"pair_id":p["pair_id"],"axis":p["axis"],"changed":a.choice!=b.choice,
                          "a":a.choice,"b":b.choice})
    frames=[a for a in base if a.frame_status]
    follow=[]
    for f in BOT_BANK["followups"]:
        upd=m.get((f["id"],"update"))
        if not upd: continue
        prev=m.get((f["id"],"base"))
        follow.append({
            "item_id":f["id"],
            "previous":prev.choice if prev else None,
            "new":upd.choice,
            "changed":None if not prev else prev.choice!=upd.choice,
            "update_type":classify_update(prev,upd),
            "confidence_delta":None if not prev or prev.confidence is None or upd.confidence is None else round(upd.confidence-prev.confidence,2)
        })
    comparable=[x for x in follow if x["changed"] is not None]
    return {
        "base_n":len(base),
        "mean_confidence":_mean([a.confidence for a in base if a.confidence is not None]),
        "state_flips":sum(x["changed"] for x in pairs),
        "state_pairs_n":len(pairs),
        "state_flip_rate":round(sum(x["changed"] for x in pairs)/len(pairs),3) if pairs else None,
        "frame_nonaccept":sum(a.frame_status!="ACCEPT" for a in frames),
        "frame_n":len(frames),
        "frame_nonaccept_rate":round(sum(a.frame_status!="ACCEPT" for a in frames)/len(frames),3) if frames else None,
        "c_choice_rate":round(sum(a.choice=="C" for a in base)/len(base),3) if base else None,
        "followup_n":len(follow),
        "followup_choice_changes":sum(bool(x["changed"]) for x in comparable),
        "followup_comparable_n":len(comparable),
        "followup_change_rate":round(sum(bool(x["changed"]) for x in comparable)/len(comparable),3) if comparable else None,
        "pairs":pairs,
        "followups":follow,
    }

def human_ai_metrics(answers):
    h=answer_map(answers,"human"); a=answer_map(answers,"ai")
    pairs=[]
    for p in STATE_BANK["matched_pairs"]:
        i1=p["items"][0]["id"]; i2=p["items"][1]["id"]
        hh1=h.get((i1,"base")); hh2=h.get((i2,"base"))
        aa1=a.get((i1,"base")); aa2=a.get((i2,"base"))
        pairs.append({
            "pair_id":p["pair_id"],
            "human_changed":None if not hh1 or not hh2 else hh1.choice!=hh2.choice,
            "ai_changed":None if not aa1 or not aa2 else aa1.choice!=aa2.choice
        })
    base_ids=[x["id"] for p in STATE_BANK["matched_pairs"] for x in p["items"]] + [x["id"] for x in STATE_BANK["efp_items"]] + [x["id"] for x in STATE_BANK["grp_base"]]
    aligned=0; compared=0
    for iid in base_ids:
        hh=h.get((iid,"base")); aa=a.get((iid,"base"))
        if hh and aa:
            compared += 1
            aligned += hh.choice==aa.choice
    # EFP1-EFP3 are the scored core; EFP4 kept diagnostic/ambiguous.
    efp_core={"EFP1","EFP2","EFP3"}
    h_efp=[]; a_efp=[]
    lookup={x["id"]:x for x in STATE_BANK["efp_items"]}
    for iid in efp_core:
        if (iid,"base") in h: h_efp.append(lookup[iid]["optionality"].get(h[(iid,"base")].choice,0))
        if (iid,"base") in a: a_efp.append(lookup[iid]["optionality"].get(a[(iid,"base")].choice,0))
    return {
        "alignment_n":aligned,
        "alignment_total":compared,
        "alignment_rate":round(aligned/compared,3) if compared else None,
        "human_state_flips":sum(x["human_changed"] is True for x in pairs),
        "ai_state_flips":sum(x["ai_changed"] is True for x in pairs),
        "state_pairs_n":len([x for x in pairs if x["human_changed"] is not None and x["ai_changed"] is not None]),
        "human_efp_core_mean":_mean(h_efp),
        "ai_efp_core_mean":_mean(a_efp),
        "pairs":pairs,
    }

def benchmark_metrics(pack, answers):
    amap={a.item_id:a for a in answers if a.actor=="ai" and a.phase=="base"}
    rows=[]; supports=[]; modal=0; n=0
    for item in pack.get("items",[]):
        a=amap.get(item["id"])
        if not a: continue
        dist=item.get("human_distribution") or {}
        support=float(dist.get(a.choice,0))
        modal_choice=max(dist, key=dist.get) if dist else None
        rows.append({"item_id":item["id"],"choice":a.choice,"human_support":support,
                     "modal_choice":modal_choice,"modal_agree":modal_choice==a.choice if modal_choice else None})
        if dist:
            supports.append(support); n+=1; modal += modal_choice==a.choice
    return {
        "items_n":len(rows),
        "mean_human_support":_mean(supports),
        "modal_agreement_rate":round(modal/n,3) if n else None,
        "rows":rows
    }
