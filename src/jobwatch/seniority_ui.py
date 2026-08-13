"""Balisage et script partagés du sélecteur de séniorité à deux poignées."""

from __future__ import annotations

import html
import json

from jobwatch.seniority import SENIORITY_LEVELS


def seniority_level_labels_html(range_max: int) -> str:
    return "".join(
        f'<span data-level="{value}" style="--level-position:calc('
        f'{value / range_max * 100}% + {13 - value / range_max * 26}px)">'
        f"{html.escape(label)}</span>"
        for value, label in SENIORITY_LEVELS
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
