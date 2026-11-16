"""Standalone study reports. Preparation and measured results look distinct."""

from html import escape
import json


def h(value: object) -> str:
    return escape(str(value), quote=True)


def money(value: float | None) -> str:
    return "Unavailable" if value is None else f"${value:.6f}"


def percent(value: float | None) -> str:
    return "Unavailable" if value is None else f"{100 * value:.1f}%"


def page(title: str, body: str) -> str:
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{h(title)} · HarnessLab</title>
<style>
:root{{color-scheme:dark;--bg:#0b1220;--panel:#142135;--muted:#b3c3d8;--line:#30445f;--green:#7de1c1;--amber:#ffd694}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:#f1f5fa;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:1200px;margin:auto;padding:36px 28px 70px}}a{{color:var(--green)}}header{{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding-bottom:20px}}
.brand{{font-weight:750;font-size:22px}}.eyebrow{{color:var(--green);text-transform:uppercase;letter-spacing:.1em;font-size:12px}}
h1{{font-size:clamp(32px,5vw,56px);line-height:1.1;letter-spacing:-.04em;margin:32px 0 22px;max-width:900px}}h2{{margin-top:40px;font-size:23px}}
p{{max-width:950px}}.muted,small{{color:var(--muted)}}.notice{{border-left:4px solid var(--amber);background:#302b23;color:#ffe8c1;padding:16px 20px;margin:25px 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px;margin:28px 0}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}}.value{{font-size:32px;font-weight:700;display:block;line-height:1.3}}
.table{{overflow-x:auto;border:1px solid var(--line);border-radius:12px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:15px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:var(--panel)}}td.num{{font-family:ui-monospace,monospace;white-space:nowrap}}
pre{{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#070d17;padding:18px;border-radius:8px;font-size:12px}}details{{border:1px solid var(--line);padding:14px 18px;margin:12px 0;border-radius:9px}}summary{{cursor:pointer}}.pass{{color:var(--green)}}.fail{{color:var(--amber)}}code{{font-size:.85em;overflow-wrap:anywhere}}footer{{border-top:1px solid var(--line);margin-top:40px;padding-top:20px;color:var(--muted);font-size:12px}}
@media(max-width:600px){{main{{padding:24px 16px}}header{{display:block}}header .eyebrow{{display:block;margin-top:8px}}th,td{{padding:10px}}}}
</style></head><body><main><header><span class="brand">HarnessLab</span><span class="eyebrow">Repeated trials · auditable economics</span></header>{body}
<footer>Local synthetic incident environment · no production actions · standalone artifact, no external assets</footer></main></body></html>'''


def render_plan(plan: dict) -> str:
    issues = "".join(f"<li>{h(item)}</li>" for item in plan["readiness_issues"])
    models = "".join(f"<li>{h(m['provider'])} / <code>{h(m['model'])}</code></li>" for m in plan["models"])
    limits = "".join(f"<li>{h(item)}</li>" for item in plan["limitations"])
    return page("Study plan — no model results", f'''
<h1>Define the experiment.<br>Then measure the outcome.</h1>
<div class="notice"><strong>Preparation only. No model requests were made.</strong> This artifact contains a frozen protocol and planned schedule, with no measured accuracy, latency or cost.</div>
<div class="cards"><div class="card"><span class="value">{len(plan['cases'])}</span>synthetic cases</div><div class="card"><span class="value">{plan['trials']}</span>trials per case and arm</div><div class="card"><span class="value">{len(plan['schedule'])}</span>planned task attempts</div><div class="card"><span class="value">{len(plan['models'])} × {len(plan['configs'])}</span>models × profiles</div></div>
<h2>The metric that includes failure</h2><p><strong>Cost per successful task = estimated inference cost of all attempted tasks ÷ tasks passing outcome, evidence and safety graders.</strong> Failed attempts stay in the numerator. Zero successes or missing usage make this metric unavailable.</p>
<h2>Execution readiness</h2><p>{'Configuration complete; explicit study-run still required.' if plan['ready_to_execute'] else 'Draft — model snapshots and prices still need configuration.'}</p><ul>{issues}</ul><ul>{models}</ul>
<p>Profiles: {h(', '.join(plan['configs']))}. Schedule seed: {plan['seed']}. Maximum planned model requests: {plan['max_planned_model_requests']}.</p>
<p>Configured observed-cost stop: {money(plan['max_total_cost_usd'])}. This is a planning setting, not spend authorization or a billing guarantee. A crossing response can exceed it.</p>
<h2>What this study can support</h2><p>Case-level repeated success, task-cluster bootstrap intervals, paired profile differences, failure traces and model inference economics on this named suite.</p><ul>{limits}</ul>
<h2>Frozen identity</h2><p>Plan SHA-256: <code>{h(plan['plan_sha256'])}</code><br>Dataset SHA-256: <code>{h(plan['dataset_sha256'])}</code></p>
<details><summary>Read full protocol and planned schedule</summary><pre>{h(json.dumps(plan, indent=2, ensure_ascii=False))}</pre></details>''')


def render_study_report(report: dict) -> str:
    plan, manifest, metrics = report["plan"], report["manifest"], report["metrics"]
    groups = metrics["groups"]
    rows = []
    for group in groups:
        interval = group.get("pass_rate_ci95")
        interval_text = h(interval) if interval is not None else h(group.get("pass_rate_ci95_reason", "Unavailable"))
        rows.append(f'''<tr><td><strong>{h(group['model'])}</strong><br>{h(group['profile'])}</td>
<td class="num">{group['attempted_count']} / {group['planned_count']}</td><td class="num">{group['passed_count']} · {percent(group.get('pass_rate'))}<br><small>CI: {interval_text}</small></td>
<td class="num">{money(group.get('total_estimated_cost_usd'))}{('<br><small>Known portion: ' + money(group.get('reported_known_cost_usd')) + '</small>') if group.get('total_estimated_cost_usd') is None else ''}</td><td class="num">{money(group.get('cost_per_successful_task_usd'))}</td>
<td class="num">{percent(group.get('all_trials_success_rate'))}<br><small>{group['complete_case_count']} complete cases</small></td></tr>''')
    run_details = []
    for run in report["runs"]:
        passed = all(run["grade"][key] for key in ("passed", "task_success", "evidence", "safety"))
        verdict = "PASS" if passed else "FAIL"
        details = json.dumps(run, indent=2, ensure_ascii=False)
        run_details.append(f'''<details><summary><span class="{'pass' if passed else 'fail'}">{verdict}</span> · {h(run['case_id'])} · {h(run['model'])} · {h(run['profile'])} · trial {run['trial']}</summary><p>{h(run['status'])} · {money(run['estimated_cost_usd'])} · {run['latency_ms']:.0f} ms</p><pre>{h(details)}</pre></details>''')
    mode = manifest["mode"]
    notice = ("OFFLINE TEST — injected test providers. These are not real-model performance results."
              if mode != "live" else "LIVE API STUDY — synthetic incidents, frozen rubric; inspect coverage and failures before comparing models.")
    completed = len(report["runs"])
    return page("Repeated model study", f'''
<h1>Reliability has a cost.<br>Make both measurable.</h1><div class="notice"><strong>{h(notice)}</strong><br>Status: {h(manifest['status'])}. Stop reason: {h(manifest.get('stop_reason') or 'none')}.</div>
<div class="cards"><div class="card"><span class="value">{completed}/{len(plan['schedule'])}</span>attempted / planned tasks</div><div class="card"><span class="value">{plan['trials']}</span>planned trials per case and arm</div><div class="card"><span class="value">{len(plan['cases'])}</span>task clusters</div></div>
<h2>Success and inference economics</h2><p>Success requires outcome, evidence and safety checks. Costs include failed attempts. Missing tasks stay visible; unknown usage is not zero cost.</p>
<div class="table"><table><thead><tr><th>Model / profile</th><th>Attempted / planned</th><th>Pass count / rate</th><th>Total estimated cost</th><th>Cost per success</th><th>All trials pass</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">Intervals resample task clusters, not individual trials. They are conditional on observed attempts and do not account for bias from missing slots. Small synthetic samples limit generalization. All-trials success uses only complete cases; coverage is shown separately. Costs exclude infrastructure and human review.</p>
<h2>Paired profile comparisons and case detail</h2><details><summary>View statistical comparisons</summary><pre>{h(json.dumps(metrics.get('comparisons', []), indent=2))}</pre></details><details><summary>View all metrics, missing trials and cost reasons</summary><pre>{h(json.dumps(metrics, indent=2))}</pre></details>
<h2>Every attempted trajectory</h2><p><a href="results.json" download>Download results JSON</a> · <a href="plan.json" download>Download frozen plan</a></p>{''.join(run_details)}
<h2>Provenance</h2><p>Plan SHA-256: <code>{h(plan['plan_sha256'])}</code></p><details><summary>Execution manifest</summary><pre>{h(json.dumps(manifest, indent=2))}</pre></details>''')
