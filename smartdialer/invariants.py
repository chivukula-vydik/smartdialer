"""Cross-table invariants. Must hold between any two committed transactions."""

ACTIVE = "('QUEUED','RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED')"


def check(db):
    errs = []
    q = db.q
    for r in q("SELECT id, state, call_id FROM agents WHERE state IN ('RESERVED','DIALING','CONNECTED')"):
        call = db.one("SELECT state, agent_id FROM calls WHERE id=?", (r["call_id"],)) if r["call_id"] else None
        if call is None:
            errs.append(f"agent {r['id']} {r['state']} without call")
        elif call["agent_id"] != r["id"]:
            errs.append(f"agent {r['id']} -> call {r['call_id']} which points to {call['agent_id']}")
        elif r["state"] == "CONNECTED" and call["state"] != "CONNECTED":
            errs.append(f"agent {r['id']} CONNECTED but call {call['state']}")
    for r in q(f"SELECT id, agent_id, state FROM calls WHERE state IN {ACTIVE} AND agent_id IS NOT NULL"):
        a = db.one("SELECT state, call_id FROM agents WHERE id=?", (r["agent_id"],))
        if a is None or a["call_id"] != r["id"] or a["state"] not in ("RESERVED", "DIALING", "CONNECTED"):
            errs.append(f"call {r['id']} {r['state']} bound to agent {r['agent_id']} in "
                        f"{None if a is None else (a['state'], a['call_id'])}")
    for r in q(f"SELECT borrower_id, COUNT(*) n FROM calls WHERE state IN {ACTIVE} GROUP BY borrower_id HAVING n>1"):
        errs.append(f"borrower {r['borrower_id']} has {r['n']} active calls")
    for r in q(f"SELECT c.id, b.status FROM calls c JOIN borrowers b ON b.id=c.borrower_id "
               f"WHERE c.state IN {ACTIVE} AND (b.status!='CLAIMED' OR b.last_call_id!=c.id)"):
        errs.append(f"active call {r['id']} but borrower is {r['status']}")
    for r in q("SELECT agent_id, COUNT(*) n FROM calls WHERE state='CONNECTED' GROUP BY agent_id HAVING n>1"):
        errs.append(f"agent {r['agent_id']} connected to {r['n']} calls")
    return errs
