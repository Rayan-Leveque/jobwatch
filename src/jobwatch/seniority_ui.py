"""Balisage et script partagés du sélecteur de séniorité à deux poignées."""

from __future__ import annotations

import html
import json

from jobwatch.seniority import SENIORITY_LEVELS

SENIORITY_RANGE_CSS = """\
.dual-range { position:relative; height:42px; margin:10px 10px 0 }
.range-rail,.range-selection { position:absolute; top:18px; right:13px; left:13px; height:6px;
  border-radius:999px; background:var(--line) }
.range-selection { left:var(--range-left); right:auto; width:var(--range-width); background:var(--green, var(--accent)) }
.range-input { position:absolute; inset:0; width:100%; height:42px; margin:0; padding:0;
  border:0; appearance:none; -webkit-appearance:none; background:transparent; pointer-events:none }
.range-input::-webkit-slider-runnable-track { height:6px; background:transparent }
.range-input::-moz-range-track { height:6px; background:transparent }
.range-input::-webkit-slider-thumb { width:26px; height:26px; margin-top:-10px; border:3px solid var(--surface);
  border-radius:50%; appearance:none; -webkit-appearance:none; background:var(--green, var(--accent));
  box-shadow:0 0 0 1px var(--green, var(--accent)),0 3px 9px rgba(25,27,31,.22); pointer-events:auto; cursor:grab }
.range-input::-moz-range-thumb { width:20px; height:20px; border:3px solid var(--surface);
  border-radius:50%; background:var(--green, var(--accent)); box-shadow:0 0 0 1px var(--green, var(--accent)),0 3px 9px rgba(25,27,31,.22);
  pointer-events:auto; cursor:grab }
.range-labels { position:relative; height:28px; margin:0 10px }
.range-labels span { position:absolute; top:8px; left:var(--level-position); width:max-content;
  color:var(--muted); font-size:.6875rem; line-height:1.2; text-align:center;
  white-space:nowrap; transform:translateX(-50%) }
.range-labels span::before { content:""; position:absolute; bottom:calc(100% + 5px); left:50%;
  width:2px; height:6px; border-radius:2px; background:var(--line); transform:translateX(-50%) }
@media (max-width:360px) { .dual-range,.range-labels { margin-left:0; margin-right:0; } }
"""


def seniority_level_labels_html(range_max: int) -> str:
    tick_labels = ("Stage", "Altern.", "Junior", "Interm.", "Senior", "Lead")
    return "".join(
        f'<span data-level="{value}" style="--level-position:calc('
        f'{value / range_max * 100}% + {13 - value / range_max * 26}px)">'
        f"{html.escape(tick_labels[value])}</span>"
        for value, _label in SENIORITY_LEVELS
    )


def seniority_sync_script(
    *,
    min_id: str,
    max_id: str,
    range_id: str = "seniority-range",
    summary_id: str = "seniority-summary",
) -> str:
    labels = json.dumps([label for _value, label in SENIORITY_LEVELS], ensure_ascii=False)
    return f"""const seniorityLabels={labels};
const seniorityMin=document.getElementById('{min_id}');
const seniorityMax=document.getElementById('{max_id}');
const syncSeniority=(changed)=>{{
  if(Number(seniorityMin.value)>Number(seniorityMax.value)) {{
    if(changed===seniorityMin) seniorityMax.value=seniorityMin.value;
    else seniorityMin.value=seniorityMax.value;
  }}
  const low=Number(seniorityMin.value),high=Number(seniorityMax.value),steps=seniorityLabels.length-1;
  const range=document.getElementById('{range_id}');
  const lowRatio=low/steps,spanRatio=(high-low)/steps;
  range.style.setProperty('--range-left',`calc(${{lowRatio*100}}% + ${{13-lowRatio*26}}px)`);
  range.style.setProperty('--range-width',`calc(${{spanRatio*100}}% - ${{spanRatio*26}}px)`);
  document.getElementById('{summary_id}').textContent=low===high
    ? seniorityLabels[low] : `${{seniorityLabels[low]}} à ${{seniorityLabels[high]}}`;
}};
[seniorityMin,seniorityMax].forEach(input=>input.addEventListener('input',()=>syncSeniority(input)));
syncSeniority();"""
