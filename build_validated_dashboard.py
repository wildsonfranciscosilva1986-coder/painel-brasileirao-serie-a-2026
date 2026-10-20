"""Build the public dashboard only from the fully reconciled CBF collector output."""
import csv
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
OUT = BASE / "validation-output"
report_path = OUT / "validation.json"
fixtures_path = OUT / "cbf_fixtures.json"
if not report_path.exists() or not fixtures_path.exists():
    raise SystemExit("FAIL-CLOSED: collector validation outputs are missing")
report = json.loads(report_path.read_text(encoding="utf-8"))
fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
def norm(name):
    value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "", value)
    aliases = {"atleticomineiro": "atleticomg", "atleticomg": "atleticomg",
               "athleticoparanaense": "athleticopr", "athleticopr": "athleticopr",
               "redbullbragantino": "rbbragantino", "rbbragantino": "rbbragantino",
               "santosfc": "santos", "vascodagamasaf": "vasco", "vascodagama": "vasco",
               "coritibasaf": "coritiba"}
    return aliases.get(value, value)

round_counts = Counter(int(m["round"]) for m in fixtures)
keys = [(int(m["round"]), m["home"], m["away"]) for m in fixtures]
checks = {
    "all_checks_pass": report.get("all_checks_pass") is True,
    "exactly_380_fixtures": len(fixtures) == 380 and report.get("fixtures") == 380,
    "10_fixtures_each_round": set(round_counts) == set(range(1, 39)) and all(round_counts[r] == 10 for r in range(1, 39)),
    "unique_fixture_keys": len(keys) == len(set(keys)),
    "unique_cbf_refs": len({str(m["cbf_id"]) for m in fixtures}) == 380,
    "CBF_versus_ESPN_all_finals": report.get("cbf_finished_crosschecked") == report.get("finished") == report.get("espn_exact_final_matches") and not report.get("espn_mismatches"),
    "all_20_standings_reconciled": report.get("cbf_standings_teams") == 20 and report.get("recomputed_standings_teams") == 20 and not report.get("standings_differences"),
    "baseline_coverage": report.get("baseline_key_coverage") == "PASS",
}
failed = [name for name, ok in checks.items() if not ok]
if failed:
    raise SystemExit("FAIL-CLOSED: refusing to build/publish: " + ", ".join(failed))

# Keep the human-readable club labels in the established title case, while the
# collector's raw CBF names remain untouched in validation-output evidence.
baseline_path = BASE / ".github/data/serie_a_2026_baseline.csv"
with baseline_path.open(encoding="utf-8-sig", newline="") as f:
    baseline = list(csv.DictReader(f))
labels = {(int(row["round"]), norm(row["home"]), norm(row["away"])): (row["home"], row["away"]) for row in baseline}
if len(labels) != 380:
    raise SystemExit("FAIL-CLOSED: cannot canonicalize labels without all 380 baseline fixtures")
for match in fixtures:
    label_key = (int(match["round"]), norm(match["home"]), norm(match["away"]))
    if label_key not in labels:
        raise SystemExit("FAIL-CLOSED: CBF fixture has no canonical baseline label")
    match["home"], match["away"] = labels[label_key]

csv_path = BASE / "serie_a_2026_results_verified.csv"
with csv_path.open("w", encoding="utf-8", newline="") as f:
    fields = ["round", "date", "kickoff", "home", "hg", "ag", "away", "status", "cbf_id"]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for m in fixtures:
        w.writerow({"round": m["round"], "date": m["date"], "kickoff": m["kickoff"],
                    "home": m["home"], "hg": m["hg"], "ag": m["ag"], "away": m["away"],
                    "status": m["status"], "cbf_id": m["cbf_id"]})

# The builder uses America/Sao_Paulo for its displayed cutoff and writes the full interactive HTML.
result = subprocess.run([sys.executable, str(BASE / "build_serie_a_dashboard.py")], cwd=BASE)
if result.returncode:
    raise SystemExit("FAIL-CLOSED: dashboard builder failed")
generated = BASE / "serie_a_2026_painel_atualizado.html"
if not generated.exists() or generated.stat().st_size < 50000:
    raise SystemExit("FAIL-CLOSED: generated dashboard is missing or unexpectedly small")
html = generated.read_text(encoding="utf-8")
if "__DATA__" in html or "__CUTOFF__" in html or str(report["finished"]) not in html:
    raise SystemExit("FAIL-CLOSED: generated dashboard lacks completed-data/cutoff substitutions")
(BASE / "index.html").write_text(html, encoding="utf-8")
now = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M BRT")
print(f"SAFE TO PUBLISH: {len(fixtures)} fixtures; {report['finished']} CBF/ESPN-matched finals; 20/20 standings; built {now}.")
