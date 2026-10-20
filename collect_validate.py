from __future__ import annotations
import csv, json, re, ssl, sys, unicodedata, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
OUT = BASE / "validation-output"
OUT.mkdir(exist_ok=True)
DETAIL_URL = "https://www.cbf.com.br/futebol-brasileiro/tabelas/campeonato-brasileiro/serie-a/2026?documento=Tabela%20Detalhada"
TABLE_URL = "https://www.cbf.com.br/futebol-brasileiro/tabelas/campeonato-brasileiro/serie-a/2026"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/bra.1/scoreboard?dates=2026&limit=500"
BASELINE = BASE / ".github/data/serie_a_2026_baseline.csv"
HEADERS = {"User-Agent": "brasileirao-serie-a-cbf-public-data-check/1.0", "Accept": "application/json,text/html,*/*"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    aliases = {
        "atleticomineiro": "atleticomg", "atleticomg": "atleticomg",
        "athleticoparanaense": "athleticopr", "athleticopr": "athleticopr",
        "redbullbragantino": "rbbragantino", "rbbragantino": "rbbragantino",
        "santosfc": "santos", "vascodagamasaf": "vasco", "vascodagama": "vasco",
        "coritibasaf": "coritiba",
    }
    return aliases.get(s, s)


def get_json(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} fetching {url}")
        return json.loads(r.read())


def collect_cbf(page, url: str):
    response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
    if response is None or response.status != 200:
        raise RuntimeError(f"CBF page did not return HTTP 200: {url}")
    # This is the public browser-rendered detailed fixtures table, not a private API.
    page.wait_for_function("""() => [...document.querySelectorAll('tr')].filter(r => /Ref:\\s*\\d+\\s+Rodada:/.test(r.innerText)).length >= 380""", timeout=90000)
    rows = page.locator("tr").evaluate_all("""rows => rows.map(row => {
      const cells = [...row.querySelectorAll('td')].map(c => c.innerText.trim().replace(/\\s+/g, ' '));
      return cells.length >= 3 ? cells : null;
    }).filter(Boolean)""")
    records = []
    for cells in rows:
        first = cells[0]
        hm = re.search(r"Ref:\s*(\d+)\s+Rodada:\s*(\d+)", first)
        if not hm:
            continue
        ref, rnd = hm.group(1), int(hm.group(2))
        matchup = cells[1]
        scored = re.match(r"(.+?)\s+(\d+)\s+x\s+(\d+)\s+(.+)$", matchup)
        if scored:
            home, hg, ag, away = scored.groups()
        else:
            unscored = re.match(r"(.+?)\s+x\s+(.+)$", matchup)
            if not unscored:
                raise RuntimeError(f"Could not parse CBF fixture row Ref {ref}: {cells}")
            home, away = unscored.groups()
            hg = ag = ""
        home = re.sub(r"\s+\([A-Z]{2}\)$", "", home).strip()
        away = re.sub(r"\s+\([A-Z]{2}\)$", "", away).strip()
        date_text = cells[2]
        dm = re.search(r"(\d{2})/(\d{2})/(\d{4})", date_text)
        date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else "TBD"
        tm = re.search(r"às\s+(\d{2})h(\d{2})", date_text)
        kickoff = f"{tm.group(1)}:{tm.group(2)}" if tm else ""
        records.append({"cbf_id": ref, "round": rnd, "home": home, "hg": hg, "ag": ag,
                        "away": away, "date": date, "kickoff": kickoff,
                        "status": "finished" if hg != "" else ("date_tbd" if date == "TBD" else "scheduled")})
    # The CBF page can expose repeated hidden responsive rows; fixture references are authoritative.
    unique = {}
    for r in records:
        if r["cbf_id"] in unique and unique[r["cbf_id"]] != r:
            raise RuntimeError(f"Conflicting duplicate CBF Ref {r['cbf_id']}")
        unique[r["cbf_id"]] = r
    return list(unique.values())


def collect_table(page):
    response = page.goto(TABLE_URL, wait_until="domcontentloaded", timeout=90000)
    if response is None or response.status != 200:
        raise RuntimeError("CBF standings page did not return HTTP 200")
    page.wait_for_function("""() => [...document.querySelectorAll('tr')].filter(r => r.querySelector('a[href*="/times/campeonato-brasileiro/serie-a/2026/"]')).length >= 20""", timeout=90000)
    rows = page.locator("tr").evaluate_all("""rows => rows.map(row => {
      const a = row.querySelector('a[href*="/times/campeonato-brasileiro/serie-a/2026/"]');
      const td = [...row.querySelectorAll('td')].map(c => c.innerText.trim());
      return a && td.length >= 9 ? {name:a.innerText.trim(), cells:td} : null;
    }).filter(Boolean)""")
    table = {}
    for r in rows:
        c = r["cells"]
        try:
            vals = [int(re.sub(r"[^0-9-]", "", x)) for x in c[1:9]]
        except Exception as e:
            raise RuntimeError(f"Could not parse CBF table row: {r}") from e
        if len(vals) != 8:
            raise RuntimeError(f"CBF table row had unexpected columns: {r}")
        pts, played, w, d, l, gf, ga, gd = vals
        table[norm(r["name"])] = {"name": r["name"], "played": played, "w": w, "d": d, "l": l,
                                  "gf": gf, "ga": ga, "gd": gd, "points": pts}
    if len(table) != 20:
        raise RuntimeError(f"Expected 20 unique CBF standings rows; got {len(table)}")
    return table


def read_baseline():
    if not BASELINE.exists():
        raise RuntimeError(f"Missing checked-in historical comparison baseline: {BASELINE}")
    with BASELINE.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_espn():
    payload = get_json(ESPN_URL)
    events = payload.get("events", [])
    out = []
    for e in events:
        try:
            comp = e["competitions"][0]
            home = next(x for x in comp["competitors"] if x["homeAway"] == "home")
            away = next(x for x in comp["competitors"] if x["homeAway"] == "away")
            out.append({"espn_id": e["id"], "date": e["date"][:10],
                        "home": home["team"]["displayName"], "away": away["team"]["displayName"],
                        "hg": str(home.get("score", "")), "ag": str(away.get("score", "")),
                        "completed": bool(e["status"]["type"].get("completed"))})
        except (KeyError, StopIteration, TypeError):
            continue
    return out


def calc_standings(fixtures):
    st = defaultdict(lambda: Counter())
    for m in fixtures:
        if m["hg"] == "":
            continue
        h, a, hg, ag = norm(m["home"]), norm(m["away"]), int(m["hg"]), int(m["ag"])
        for team, gf, ga, won in ((h, hg, ag, hg > ag), (a, ag, hg, ag > hg)):
            st[team]["played"] += 1; st[team]["gf"] += gf; st[team]["ga"] += ga
            st[team]["w" if won else "d" if hg == ag else "l"] += 1
            st[team]["points"] += 3 if won else 1 if hg == ag else 0
            st[team]["gd"] = st[team]["gf"] - st[team]["ga"]
    return {k: dict(v) for k, v in st.items()}


def main():
    started = datetime.now(timezone.utc).isoformat()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="pt-BR", user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
        fixtures = collect_cbf(page, DETAIL_URL)
        official_table = collect_table(page)
        browser.close()
    baseline = read_baseline()
    espn = read_espn()

    round_counts = Counter(m["round"] for m in fixtures)
    keys = [(m["round"], norm(m["home"]), norm(m["away"])) for m in fixtures]
    if len(fixtures) != 380 or set(round_counts) != set(range(1, 39)) or any(round_counts[r] != 10 for r in range(1, 39)):
        raise RuntimeError(f"Expected exactly 380 fixtures and 10 per rounds 1-38; total={len(fixtures)}, counts={dict(round_counts)}")
    if len(keys) != len(set(keys)) or len({m["cbf_id"] for m in fixtures}) != 380:
        raise RuntimeError("Duplicate fixture key or CBF reference detected")
    bidx = {(int(m["round"]), norm(m["home"]), norm(m["away"])): m for m in baseline}
    if len(bidx) != 380 or set(keys) != set(bidx):
        raise RuntimeError("CBF fixture keys do not exactly match the checked-in 380-row baseline")
    baseline_changes = []
    for m in fixtures:
        old = bidx[(m["round"], norm(m["home"]), norm(m["away"]))]
        if old["date"] != m["date"]:
            baseline_changes.append({"round":m["round"],"home":m["home"],"old_date":old["date"],"cbf_date":m["date"]})
        if old.get("status") == "finished" and m["status"] == "finished" and (old.get("hg") != m["hg"] or old.get("ag") != m["ag"]):
            baseline_changes.append({"round":m["round"],"home":m["home"],"old_score":f"{old.get('hg')}-{old.get('ag')}","cbf_score":f"{m['hg']}-{m['ag']}"})
        elif old.get("status") != "finished" and m["status"] == "finished":
            baseline_changes.append({"round":m["round"],"home":m["home"],"newly_finished_score":f"{m['hg']}-{m['ag']}","old_status":old.get("status")})
    finished = [m for m in fixtures if m["status"] == "finished"]
    espn_idx = defaultdict(list)
    for e in espn:
        espn_idx[(norm(e["home"]), norm(e["away"]))].append(e)
    crosschecks, espn_mismatches = [], []
    for m in finished:
        candidates = espn_idx[(norm(m["home"]), norm(m["away"]))]
        matches = [e for e in candidates if e["completed"] and e["hg"] == m["hg"] and e["ag"] == m["ag"]]
        item = {"round":m["round"],"cbf_ref":m["cbf_id"],"home":m["home"],"cbf_score":f"{m['hg']}-{m['ag']}","away":m["away"],"espn_count":len(matches),"espn_ids":[e["espn_id"] for e in matches]}
        crosschecks.append(item)
        if len(matches) != 1:
            espn_mismatches.append(item)

    computed = calc_standings(fixtures)
    table_diffs = {}
    for team in set(official_table) | set(computed):
        if team not in official_table or team not in computed:
            table_diffs[team] = {"issue":"team missing in one table", "cbf":official_table.get(team),"computed":computed.get(team)}
            continue
        fields = ["played","points","w","d","l","gf","ga","gd"]
        diff = {f:{"computed":computed[team].get(f,0),"cbf":official_table[team].get(f)} for f in fields if computed[team].get(f,0) != official_table[team].get(f)}
        if diff: table_diffs[team] = diff

    now = datetime.now(timezone.utc).isoformat()
    report = {"retrieved_at_utc":now,"cbf_detailed_url":DETAIL_URL,"cbf_standings_url":TABLE_URL,"independent_results_url":ESPN_URL,
              "collection_method":"Playwright Chromium rendered public CBF pages; OS trust includes only the pinned, fingerprint-verified CBF intermediate; TLS and hostname verification remain enabled",
              "fixtures":len(fixtures),"round_counts":dict(sorted(round_counts.items())),"duplicate_fixture_keys":len(keys)-len(set(keys)),
              "finished":len(finished),"scheduled":sum(m['status']=='scheduled' for m in fixtures),"date_tbd":sum(m['status']=='date_tbd' for m in fixtures),
              "baseline_rows":len(baseline),"baseline_key_coverage":"PASS" if len(bidx)==380 and set(keys)==set(bidx) else "FAIL",
              "baseline_changes":baseline_changes,"espn_events":len(espn),"espn_completed_events":sum(e['completed'] for e in espn),
              "espn_exact_final_matches":len(crosschecks)-len(espn_mismatches),"cbf_finished_crosschecked":len(finished),"espn_mismatches":espn_mismatches,
              "cbf_standings_teams":len(official_table),"recomputed_standings_teams":len(computed),"standings_differences":table_diffs,
              "all_checks_pass":not espn_mismatches and not table_diffs and len(fixtures)==380 and len(finished)>0}
    (OUT/"validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (OUT/"cbf_fixtures.json").write_text(json.dumps(fixtures,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (OUT/"cbf_standings.json").write_text(json.dumps(official_table,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (OUT/"espn_crosschecks.json").write_text(json.dumps(crosschecks,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with (OUT/"cbf_fixtures.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(fixtures[0]));w.writeheader();w.writerows(fixtures)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if espn_mismatches:
        raise SystemExit(f"FAIL: {len(espn_mismatches)} final CBF scores could not be exactly independently matched to ESPN")
    if table_diffs:
        raise SystemExit(f"FAIL: CBF standings do not match independently recomputed results for {len(table_diffs)} team(s); no panel update is permitted")
    print("PASS: full 38x10 fixtures, ESPN final results, and CBF standings reconciled; safe to consider publication.")

if __name__ == "__main__":
    main()
