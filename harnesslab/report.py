"""Dependency-free, offline HTML report. All run-controlled text is escaped."""

from __future__ import annotations

from html import escape
import json
from typing import Any


def _h(value: Any) -> str:
    return escape(str(value), quote=True)


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _money(value: float | None) -> str:
    return "Unknown" if value is None else f"${value:.6f}"


def render_report(report: dict[str, Any]) -> str:
    """Render a self-contained report without CDNs, fonts, or remote requests."""
    manifest = report["manifest"]
    summaries = report["summaries"]
    runs = report["runs"]
    scripted = manifest["mode"] == "scripted"
    scope = "Runtime contract evaluation" if scripted else "Live model evaluation"
    notice = (
        "Scripted decisions isolate harness behavior. These results do not measure LLM intelligence, "
        "real-world accuracy, or production readiness."
        if scripted else
        "Live model results on a small synthetic suite. Inspect failures and traces before interpreting "
        "aggregate scores. Provider costs are estimates only when rates are configured."
    )
    providers = ", ".join(f"{p['name']} / {p['model']}" for p in manifest["providers"])
    cards = []
    comparison = []
    for summary in summaries:
        profile = summary["profile"]
        rate = _percent(summary["pass_rate"])
        featured = " featured" if profile == "hardened" else ""
        cards.append(f"""
        <article class="metric-card{featured}">
          <div class="eyebrow">{_h(profile)}</div>
          <div class="metric-value">{rate}<span>contract pass</span></div>
          <div class="meter" role="img" aria-label="{rate} of runs passed"><i style="width:{rate}"></i></div>
          <p>{summary['passed']} / {summary['runs']} runs satisfy every grader</p>
          <dl class="mini-metrics">
            <div><dt>Task outcome</dt><dd>{_percent(summary['task_success_rate'])}</dd></div>
            <div><dt>Evidence</dt><dd>{_percent(summary['evidence_rate'])}</dd></div>
            <div><dt>Safety</dt><dd>{_percent(summary['safety_rate'])}</dd></div>
          </dl>
        </article>""")
        comparison.append(f"""
        <tr><th scope="row">{_h(profile)}</th>
          <td>{summary['mean_latency_ms']:.1f} ms</td><td>{summary['p95_latency_ms']:.1f} ms</td>
          <td>{summary['input_tokens']:,} / {summary['output_tokens']:,}{'' if summary.get('usage_complete', True) else ' (partial)'}</td>
          <td>{_money(summary['estimated_cost_usd'])}</td>
          <td>{_percent(summary['all_trials_success_rate'])}</td></tr>""")

    run_rows = []
    for run in runs:
        grade = run["grade"]
        verdict = "pass" if grade["passed"] else "fail"
        indicators = "".join(
            f'<span class="check {"yes" if grade[key] else "no"}">{label}: {"pass" if grade[key] else "fail"}</span>'
            for key, label in (("task_success", "Outcome"), ("evidence", "Evidence"), ("safety", "Safety"))
        )
        reasons = "".join(f"<li>{_h(reason)}</li>" for reason in grade["reasons"])
        tags = "".join(f'<span class="tag">{_h(tag)}</span>' for tag in run.get("tags", []))
        result = json.dumps(run.get("result"), indent=2, ensure_ascii=False)
        event_rows = "".join(
            f'<li><div class="event-head"><span class="event-number">{_h(event.get("seq"))}</span>'
            f'<strong>{_h(event.get("type"))}</strong><time>{_h(event.get("timestamp", ""))}</time></div>'
            f'<pre>{_h(json.dumps(event.get("data"), ensure_ascii=False, indent=2))}</pre></li>'
            for event in run["events"]
        )
        run_rows.append(f"""
        <details class="run" data-profile="{_h(run['profile'])}" data-verdict="{verdict}">
          <summary>
            <span class="verdict {verdict}">{verdict.upper()}</span>
            <span class="run-name">{_h(run['case_id'])}<small>{_h(run['profile'])} · trial {run['trial']}</small></span>
            <span class="run-status">{_h(run['status'])}</span>
            <span class="run-time">{run['latency_ms']:.1f} ms</span>
            <span class="expand" aria-hidden="true">+</span>
          </summary>
          <div class="run-body">
            <p class="question">{_h(run['question'])}</p>
            <div class="tags">{tags}</div><div class="checks">{indicators}</div>
            {f'<ul class="reasons">{reasons}</ul>' if reasons else '<p class="quiet">All declared checks passed for this fixture.</p>'}
            <dl class="run-meta"><div><dt>Run ID</dt><dd>{_h(run['run_id'])}</dd></div>
            <div><dt>Model calls / steps</dt><dd>{run['model_calls']} / {run['steps']}</dd></div>
            <div><dt>Tokens in / out</dt><dd>{run['input_tokens']} / {run['output_tokens']}</dd></div>
            <div><dt>Estimated cost</dt><dd>{_money(run.get('estimated_cost_usd'))}</dd></div></dl>
            <h3>Final result</h3><pre>{_h(result)}</pre>
            <details class="trace"><summary>Inspect {len(run['events'])} trace events</summary>
              <ol class="events">{event_rows}</ol>
            </details>
          </div>
        </details>""")

    options = "".join(f'<option value="{_h(s["profile"])}">{_h(s["profile"])}</option>' for s in summaries)
    limits = "".join(f"<li>{_h(item)}</li>" for item in manifest["limitations"])
    manifest_json = _h(json.dumps(manifest, indent=2, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>HarnessLab | Evaluation report</title>
<style>
:root{{--bg:#0b1220;--panel:#121e30;--border:#28374c;--ink:#edf5ff;--muted:#a5b7ce;--teal:#64e3c0;--amber:#ffbd7a;--red:#ffa3ac;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,Segoe UI,sans-serif}}a{{color:var(--teal);text-underline-offset:4px}}button,input,select{{font:inherit}}:focus-visible{{outline:3px solid var(--teal);outline-offset:4px}}.skip{{position:absolute;left:24px;top:-100px}}.skip:focus{{top:10px;background:var(--bg);z-index:2}}
.container{{max-width:1280px;margin:auto;padding:0 40px}}header{{border-bottom:1px solid var(--border)}}.topbar{{display:flex;align-items:center;justify-content:space-between;gap:20px;min-height:86px}}.brand{{font-weight:750;letter-spacing:-.6px;font-size:23px;display:flex;align-items:center;gap:12px}}.brand-mark{{width:32px;height:32px;display:grid;place-items:center;border:1px solid var(--teal);color:var(--teal);font:700 18px var(--mono);border-radius:7px}}.badge{{border:1px solid var(--border);border-radius:99px;padding:5px 12px;color:var(--muted);font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase}}
main{{padding-bottom:64px}}.hero{{padding:55px 0 28px}}.eyebrow{{font:11px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--teal)}}h1{{font-size:clamp(34px,5vw,56px);line-height:1.1;letter-spacing:-2px;margin:15px 0 16px;max-width:900px}}.lead{{font-size:18px;color:var(--muted);max-width:770px;margin:0}}.hero-meta{{display:flex;gap:22px;flex-wrap:wrap;margin-top:24px;font:12px var(--mono);color:var(--muted)}}.hero-meta strong{{color:var(--ink)}}.notice{{border:1px solid #66513b;background:#261f1b;color:#ffd2a5;padding:15px 20px;border-radius:10px;margin:8px 0 30px;font-size:14px}}.notice strong{{color:var(--amber)}}
.section-head{{display:flex;align-items:end;justify-content:space-between;gap:18px;margin:35px 0 17px}}h2{{font-size:23px;letter-spacing:-.5px;line-height:1.25;margin:0}}.section-head p{{margin:7px 0 0;color:var(--muted);font-size:13px}}.section-index{{color:var(--teal);font:12px var(--mono);margin-right:12px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}}.metric-card{{background:var(--panel);border:1px solid var(--border);padding:23px;border-radius:13px}}.metric-card.featured{{border-color:#3b9f8a;background:linear-gradient(135deg,#132b32,#121e30 75%)}}.metric-card .eyebrow{{color:var(--muted);letter-spacing:.07em}}.featured .eyebrow{{color:var(--teal)}}.metric-value{{font-size:48px;font-weight:700;line-height:1.1;letter-spacing:-2px;margin:18px 0 16px}}.metric-value span{{display:block;font:12px system-ui;letter-spacing:0;color:var(--muted);margin-top:8px}}.meter{{height:5px;border-radius:5px;background:#29384a;overflow:hidden}}.meter i{{height:100%;display:block;background:var(--teal)}}.metric-card p{{font-size:12px;color:var(--muted);margin:12px 0 20px}}.mini-metrics{{margin:0;padding-top:15px;border-top:1px solid var(--border)}}.mini-metrics div{{display:flex;justify-content:space-between;font-size:12px;margin-top:5px}}dt{{color:var(--muted)}}dd{{margin:0;font-family:var(--mono)}}
.table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:12px}}table{{width:100%;border-collapse:collapse;white-space:nowrap;text-align:left}}thead{{background:#142033;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}th,td{{padding:16px 20px;border-bottom:1px solid var(--border)}}tbody th{{font-weight:550}}tbody td{{font:12px var(--mono)}}tbody tr:last-child>*{{border-bottom:0}}.footnote{{font-size:12px;color:var(--muted);margin:12px 0 0}}
.filters{{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:18px 0}}.filters label{{font-size:13px;color:var(--muted)}}select{{background:var(--panel);color:var(--ink);border:1px solid var(--border);padding:9px 35px 9px 12px;border-radius:7px;margin-left:8px}}input[type=checkbox]{{accent-color:var(--teal);width:16px;height:16px;vertical-align:middle;margin:0 6px 0 0}}.run-count{{margin-left:auto;color:var(--muted);font:12px var(--mono)}}.run{{border:1px solid var(--border);border-radius:9px;margin:9px 0;background:#0f1929}}.run>summary{{display:grid;grid-template-columns:60px minmax(160px,1fr) 165px 80px 18px;gap:18px;align-items:center;cursor:pointer;list-style:none;padding:18px 20px}}summary::-webkit-details-marker{{display:none}}.run-name{{font-weight:600}}.run-name small{{display:block;color:var(--muted);font:11px var(--mono);margin-top:5px}}.verdict{{font:10px var(--mono);padding:4px 7px;border-radius:4px;text-align:center;letter-spacing:.07em}}.verdict.pass{{color:var(--teal);background:#14382f}}.verdict.fail{{color:var(--amber);background:#3a2c22}}.run-status,.run-time{{font:11px var(--mono);color:var(--muted)}}.run-time{{text-align:right}}.expand{{color:var(--muted);font-size:20px}}.run[open] .expand{{transform:rotate(45deg)}}.run[open]>summary{{border-bottom:1px solid var(--border)}}.run-body{{padding:22px 25px}}.question{{margin:0 0 14px}}.tags,.checks{{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}}.tag{{font:10px var(--mono);color:var(--muted);border:1px solid var(--border);padding:3px 8px;border-radius:4px}}.check{{font-size:12px;padding:3px 9px;border-radius:4px}}.check.yes{{background:#15382f;color:var(--teal)}}.check.no{{background:#3c2831;color:var(--red)}}.reasons{{color:var(--amber);font-size:13px;padding-left:20px}}.quiet{{color:var(--muted);font-size:13px}}.run-meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:22px 0}}.run-meta dt{{font-size:11px}}.run-meta dd{{font-size:11px;overflow-wrap:anywhere;margin-top:4px}}h3{{font-size:13px;margin-top:25px}}pre{{background:#080f1b;border:1px solid #202e42;padding:15px;border-radius:6px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.6 var(--mono);color:#c5d6ec;max-height:450px}}.trace>summary,.manifest>summary{{cursor:pointer;font-size:13px;color:var(--teal);padding:10px 0}}.events{{list-style:none;margin:10px 0 0;padding:0 0 0 15px;border-left:1px solid var(--border)}}.events li{{margin:15px 0 24px}}.event-head{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12px}}.event-number{{font:10px var(--mono);color:var(--teal)}}.event-head time{{margin-left:auto;font:10px var(--mono);color:var(--muted)}}.empty{{padding:35px;color:var(--muted);text-align:center;border:1px dashed var(--border);border-radius:9px}}[hidden]{{display:none!important}}
.lineage{{display:grid;grid-template-columns:1fr 1fr;gap:25px;background:var(--panel);border:1px solid var(--border);padding:24px;border-radius:12px}}.lineage dt{{font-size:11px;margin:12px 0 4px}}.lineage dt:first-child{{margin-top:0}}.lineage dd{{font-size:11px;overflow-wrap:anywhere}}.limitations{{font-size:13px;color:var(--muted);padding-left:18px;margin:0}}.limitations li{{margin-bottom:8px}}.manifest{{margin-top:20px}}footer{{border-top:1px solid var(--border);padding:22px 0;color:var(--muted);font-size:12px}}.footer-row{{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}}
@media(max-width:700px){{.container{{padding:0 20px}}.hero{{padding-top:35px}}.hero-meta{{gap:12px}}.run>summary{{grid-template-columns:48px minmax(120px,1fr) 16px;gap:12px;padding:15px}}.run-status,.run-time{{display:none}}.run-body{{padding:18px}}.lineage{{grid-template-columns:1fr}}.run-count{{width:100%;margin-left:0}}.topbar{{min-height:74px}}.badge{{font-size:9px}}.section-head{{align-items:start}}}}
@media print{{:root{{--bg:white;--panel:#f5f7fa;--border:#ccc;--ink:#111;--muted:#444;--teal:#166750;--amber:#794200}}body{{font-size:11px}}.filters,.expand,.skip,.trace{{display:none}}.metric-card,.lineage,.run{{break-inside:avoid}}.hero{{padding-top:20px}}h1{{font-size:32px}}.notice{{background:#fff8e8;color:#633700}}pre{{background:#f3f5f8;color:#222}}}}
</style></head><body>
<a href="#main" class="skip">Skip to results</a>
<header><div class="container topbar"><div class="brand"><span class="brand-mark" aria-hidden="true">H</span>HarnessLab</div><span class="badge">{_h(manifest['mode'])} · evaluation artifact</span></div></header>
<main id="main" class="container">
<section class="hero"><div class="eyebrow">Evidence over intuition</div><h1>Make agent reliability<br>inspectable.</h1>
<p class="lead">{scope}. Compare harness controls against the same incident scenarios, then follow every result back to its execution trace.</p>
<div class="hero-meta"><span><strong>{manifest['case_count']}</strong> cases</span><span><strong>{len(summaries)}</strong> configurations</span><span><strong>{len(runs)}</strong> runs</span><span>{_h(providers)}</span></div></section>
<aside class="notice"><strong>{'Scripted evaluation' if scripted else 'Evaluation scope'}.</strong> {_h(notice)}</aside>
<section aria-labelledby="comparison-title"><div class="section-head"><div><h2 id="comparison-title"><span class="section-index">01</span>Configuration comparison</h2><p>A run passes only when outcome, evidence, and safety checks all pass.</p></div></div><div class="cards">{''.join(cards)}</div></section>
<section aria-labelledby="resources-title"><div class="section-head"><div><h2 id="resources-title"><span class="section-index">02</span>Resources &amp; consistency</h2><p>Observed wall time and token usage. Costs remain unknown without configured rates.</p></div></div>
<div class="table-wrap"><table><thead><tr><th scope="col">Configuration</th><th scope="col">Mean latency</th><th scope="col">p95 latency</th><th scope="col">Tokens in / out</th><th scope="col">Est. cost</th><th scope="col">All-trials pass</th></tr></thead><tbody>{''.join(comparison)}</tbody></table></div>
<p class="footnote">p95 uses nearest rank. All-trials pass is the fraction of cases that passed every trial. Repeated scripted trials do not add statistical evidence. Partial token usage is a lower bound after an interrupted or unreported request.</p></section>
<section aria-labelledby="runs-title"><div class="section-head"><div><h2 id="runs-title"><span class="section-index">03</span>Inspect the evidence</h2><p>Expand a run to examine its result, grading reasons, and ordered trace.</p></div><a href="results.json" download>Download JSON</a></div>
<div class="filters"><label for="profile-filter">Configuration<select id="profile-filter"><option value="all">All configurations</option>{options}</select></label><label><input id="failed-filter" type="checkbox">Failures only</label><span id="run-count" class="run-count" aria-live="polite">{len(runs)} runs shown</span></div>
<div id="runs">{''.join(run_rows)}</div><p id="empty" class="empty" hidden>No runs match these filters.</p></section>
<section aria-labelledby="lineage-title"><div class="section-head"><div><h2 id="lineage-title"><span class="section-index">04</span>Lineage &amp; limits</h2><p>Reproduce the configuration. Keep the conclusion within the evidence.</p></div></div>
<div class="lineage"><dl><dt>Dataset SHA-256</dt><dd>{_h(manifest['dataset_sha256'])}</dd><dt>Execution ID</dt><dd>{_h(manifest['execution_id'])}</dd><dt>Started (UTC)</dt><dd>{_h(manifest['started_at'])}</dd><dt>Environment</dt><dd>HarnessLab {_h(manifest['harnesslab_version'])} · Python {_h(manifest['python'])}<br>{_h(manifest['platform'])}</dd><dt>Durable trace database</dt><dd>{_h(manifest['trace_database'])}</dd></dl><ul class="limitations">{limits}</ul></div>
<details class="manifest"><summary>View complete execution manifest</summary><pre>{manifest_json}</pre></details></section>
</main><footer><div class="container footer-row"><span>HarnessLab · Applied AI reliability engineering</span><span>Standalone artifact · no remote assets or tracking</span></div></footer>
<script>
const profile = document.getElementById('profile-filter');
const failed = document.getElementById('failed-filter');
const rows = Array.from(document.querySelectorAll('.run'));
function filterRuns() {{
  let visible = 0;
  for (const row of rows) {{
    row.hidden = (profile.value !== 'all' && row.dataset.profile !== profile.value) || (failed.checked && row.dataset.verdict !== 'fail');
    if (!row.hidden) visible++;
  }}
  document.getElementById('run-count').textContent = visible + ' of ' + rows.length + ' runs shown';
  document.getElementById('empty').hidden = visible !== 0;
}}
profile.addEventListener('change', filterRuns);
failed.addEventListener('change', filterRuns);
</script></body></html>"""
