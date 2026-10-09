import json
import sys
import time

sys.path.insert(0, sys.argv[1])
import fastsim as fs

rows = [json.loads(l) for l in open(f"{sys.argv[1]}/to_sim.jsonl")]
res = {
    json.loads(l)["sheet_id"] + json.loads(l)["grp"]: json.loads(l)
    for l in open(f"{sys.argv[1]}/sim_out.jsonl")
}
ok = bad = miss = 0
t = time.time()
for r in rows:
    s = r["sheet"]
    d = s["generated_at"][:10]
    o = fs.simulate(s["ticker"], d, s["exit"]["rules"])
    ref = res.get(s["sheet_id"] + r["grp"])
    if ref is None or ref["exit_reason"] == "data_missing":
        if o is not None and ref is not None:
            miss += 1
        continue
    if o is None:
        miss += 1
        continue
    same = (
        o[2] == ref["exit_reason"]
        and abs(o[1] - float(ref["pnl_pct"])) < 1e-6
        and o[0] == ref["exit_date"]
    )
    if same:
        ok += 1
    else:
        bad += 1
        if bad <= 8:
            print(
                s["sheet_id"],
                o,
                ref["exit_reason"],
                ref["pnl_pct"],
                ref["exit_date"],
                ref.get("coverage"),
            )
print(f"일치 {ok} 불일치 {bad} 누락 {miss}  {time.time() - t:.1f}s")
