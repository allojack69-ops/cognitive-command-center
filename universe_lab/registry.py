PROJECTS = [
    {
        "id":"bot_stress","number":"01","title":"BOT COGNITIVE STRESS",
        "subtitle":"20 scenarios + 6 follow-up. JSON-validated AI reasoning under controlled cognitive pressure.",
        "endpoint":"bot_new","kind":"AI test","status":"LIVE","audience":"public",
        "cta":"Test my AI"
    },
    {
        "id":"human_ai","number":"02","title":"HUMAN ↔ AI PAIR",
        "subtitle":"You answer first; then your personalized AI answers the matched protocol. Compare the pair.",
        "endpoint":"hai_new","kind":"Human–AI","status":"LIVE","audience":"public",
        "cta":"Start pair test"
    },
    {
        "id":"grp","number":"03","title":"COGNITIVE DYNAMICS / GRP",
        "subtitle":"32-step randomized reset-washout association experiment using the shared anonymous participant identity.",
        "endpoint":"universe_grp.home","kind":"Human cognition","status":"LIVE","audience":"public",
        "cta":"Run GRP"
    },
    {
        "id":"history","number":"04","title":"HISTORY INTERVENTION",
        "subtitle":"Same model and target, controlled difference in prior history. Tests history-dependent behavior.",
        "endpoint":"history_probe.new_pair","kind":"Causality","status":"LAB","audience":"lab",
        "cta":"Open protocol"
    },
    {
        "id":"benchmarks","number":"05","title":"EXTERNAL BENCHMARKS",
        "subtitle":"Compare model behavior against authorized human distributions and versioned research benchmark packs.",
        "endpoint":"benchmarks","kind":"Benchmark","status":"LAB","audience":"lab",
        "cta":"Open benchmarks"
    },
    {
        "id":"observer_wapi","number":"06","title":"OBSERVER WAPI",
        "subtitle":"Response-timing engine: person, history, state, input and person×nonverbal interaction.",
        "endpoint":"universe_observer.wapi_home","kind":"Dataset engine","status":"ENGINE","audience":"research",
        "cta":"Open engine"
    },
    {
        "id":"mor_observer","number":"07","title":"MOR / MODEL GEOMETRY",
        "subtitle":"State, relation, memory, corridor and transition research exports from the MOR observer system.",
        "endpoint":"universe_observer.mor_home","kind":"MOR","status":"RESEARCH","audience":"research",
        "cta":"Open MOR"
    },
    {
        "id":"mor_mesh","number":"08","title":"MOR MESH FIELD LAB",
        "subtitle":"Android two-node encrypted mesh field tests with verified HELLO, delivery and ACK lifecycle.",
        "endpoint":"universe_mesh.home","kind":"Field system","status":"FIELD","audience":"research",
        "cta":"Open field lab"
    },
    {
        "id":"robus","number":"09","title":"ROBUS / ADAPTIVE ROBUSTNESS",
        "subtitle":"Multi-scale robustness decomposition: hidden interactions, critical boundaries, hysteresis, blind regions and EFP loss.",
        "endpoint":"universe_robus.home","kind":"Robustness","status":"ENGINE","audience":"research",
        "cta":"Open ROBUS"
    },
]


def projects_for(audience):
    return [p for p in PROJECTS if p.get("audience") == audience]


PUBLIC_PROJECTS = projects_for("public")
LAB_PROJECTS = projects_for("lab")
RESEARCH_PROJECTS = projects_for("research")
