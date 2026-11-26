"""Build app/reference/models.json (brands and models) from the questionnaire.

Run once per questionnaire version, from the backend folder:
    textutil -convert txt -stdout "<questionnaire>.docx" > /tmp/q.txt
    .venv/bin/python scripts/build_model_list.py /tmp/q.txt

Reporting models are the purchase list (E8). CNG versions roll into their petrol twin.
Models that appear only in the considered list (E5) are added so consideration can be reported.
"""
import json
import re
import sys
from pathlib import Path

# Purchase names that can't be matched by spelling alone: purchase name -> considered-list (E5) name
MANUAL = {"Honda City": "City", "Tiguan R-Line": "Tiguan", "Aircross X": "C3 Aircross", "C3 CNG": "C3"}
SPECIAL_E5 = {"127", "128"}  # "I did not consider any other model", "Other (Please specify)"

is_code = re.compile(r"\d+(_\d+)?").fullmatch


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", s.lower())


def block(lines, start, end):
    a = next(i for i, l in enumerate(lines) if l.startswith(start))
    b = next(i for i, l in enumerate(lines) if l.startswith(end))
    return [l for l in lines[a:b] if l]


def main(path: str) -> None:
    lines = [l.strip() for l in Path(path).read_text().split("\n")]

    seg = block(lines, "E5. Which top three", "E7. What were")
    i, brand, e5 = seg.index("Dealership Visit") + 1, None, []
    while i < len(seg):
        s = seg[i]
        nxt = seg[i + 1] if i + 1 < len(seg) else ""
        if not is_code(s) and not is_code(nxt):
            brand, i = s, i + 1
        elif not is_code(s) and is_code(nxt):
            e5.append({"brand": brand, "name": s, "code": nxt})
            i += 2
            while i < len(seg) and is_code(seg[i]):
                i += 1
        else:
            i += 1
    e5 = [x for x in e5 if x["code"] not in SPECIAL_E5]
    by_norm = {norm(x["name"]): x for x in e5}

    seg = [l for l in block(lines, "E8. Model that you", "E9. Please let us know the variant") if not l.startswith(("Scripter", "E8."))]
    e8 = [seg[i] for i in range(len(seg) - 1) if not is_code(seg[i]) and is_code(seg[i + 1])]

    models, problems = {}, []
    for name in e8:
        base = MANUAL.get(name) or re.sub(r"\s+CNG$", "", name)
        if norm(base) in by_norm:
            hits = [by_norm[norm(base)]]
        else:
            parts = [p.strip() for p in base.split("/")]
            hits = [by_norm[norm(p)] for p in parts if norm(p) in by_norm]
        if not hits:
            problems.append(name)
            continue
        key = tuple(sorted(h["code"] for h in hits))
        m = models.setdefault(key, {"name": base if len(hits) > 1 or name == base else hits[0]["name"],
                                    "brand": hits[0]["brand"], "e5_codes": list(key), "names": []})
        m["names"].append(name)
    used = {c for m in models.values() for c in m["e5_codes"]}
    for x in e5:
        if x["code"] not in used:
            models[(x["code"],)] = {"name": x["name"], "brand": x["brand"], "e5_codes": [x["code"]], "names": [x["name"]]}

    out = {"brands": sorted({m["brand"] for m in models.values()}),
           "models": sorted(models.values(), key=lambda m: (m["brand"], m["name"]))}
    target = Path(__file__).resolve().parent.parent / "app" / "reference" / "models.json"
    target.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(out['models'])} models, {len(out['brands'])} brands written to {target}")
    if problems:
        print("Purchase names with no match (add to MANUAL):", problems)


if __name__ == "__main__":
    main(sys.argv[1])
