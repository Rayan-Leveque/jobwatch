"""Page HTML autonome du parcours d'onboarding candidat."""

from __future__ import annotations

import html
import json

from jobwatch.onboarding import MAX_INTENTS


def _script_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_onboarding(
    csrf_token: str,
    initial_intents: list[dict[str, object]] | None = None,
    cv_library_ids: list[int] | None = None,
) -> str:
    csrf = html.escape(csrf_token, quote=True)
    editing = initial_intents is not None
    initial_data = _script_json(
        {
            "editing": editing,
            "intents": initial_intents or [],
            "cvLibraryIds": cv_library_ids or [],
        }
    )
    choice_hidden = " hidden" if editing else ""
    intent_hidden = "" if editing else " hidden"
    eyebrow = "Vos catégories" if editing else "Étape 3 · Vos objectifs"
    heading = "Gérez vos catégories" if editing else "Quels postes recherchez-vous ?"
    lead = (
        "Ajoutez, renommez ou retirez une catégorie. Les changements s’appliqueront à vos "
        "prochaines découvertes."
        if editing
        else "L’IA a lu vos CV, mais ces catégories ne sont que des propositions. Renommez-les, "
        "corrigez les mots-clés ou ajoutez une autre direction."
    )
    confirm_label = "Enregistrer mes catégories" if editing else "Confirmer et lancer la découverte"
    back_link = '<a class="back-link" href="/">← Tableau de bord</a>' if editing else ""
    mode_back_hidden = " hidden" if editing else ""
    max_intents = MAX_INTENTS
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb"><meta name="csrf-token" content="{csrf}">
<title>jobwatch · Votre recherche</title>
<style>
:root {{ color-scheme:light; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --bg:#f3f1eb; --surface:#fffefa; --surface-2:#f8f6ef; --fg:#191b1f;
  --muted:#686d76; --line:rgba(29,31,35,.12); --green:#42752d; --violet:#7052c8; }}
* {{ box-sizing:border-box }}
body {{ margin:0; min-height:100vh; color:var(--fg); background:var(--bg); }}
.ambient {{ position:fixed; inset:0; pointer-events:none;
  background:radial-gradient(circle at 20% 0,rgba(112,82,200,.13),transparent 34%); }}
main {{ position:relative; width:min(100%,720px); margin:auto; padding:28px 18px 70px; }}
.brand {{ display:flex; align-items:center; gap:10px; color:var(--green); font-size:.78rem;
  font-weight:850; letter-spacing:.14em; text-transform:uppercase; }}
.brand-mark {{ display:grid; place-items:center; width:34px; height:34px; border-radius:11px;
  color:#fff; background:var(--green); letter-spacing:0; }}
.steps {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin:34px 0 28px; }}
.step {{ height:5px; border-radius:999px; background:rgba(29,31,35,.10); }}
.step.active {{ background:var(--green); }}
.eyebrow {{ margin:0 0 8px; color:var(--green); font-size:.78rem; font-weight:800;
  letter-spacing:.13em; text-transform:uppercase; }}
h1 {{ max-width:620px; margin:0; font-size:clamp(2.2rem,9vw,4.2rem); line-height:.98;
  letter-spacing:-.055em; }}
.lead {{ max-width:590px; margin:18px 0 30px; color:var(--muted); font-size:1.03rem; line-height:1.6; }}
.panel {{ padding:22px; border:1px solid var(--line); border-radius:22px;
  background:var(--surface); box-shadow:0 16px 45px rgba(52,46,34,.08); }}
.action-panel {{ padding-bottom:17px; }}
.choice-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
.choice {{ min-width:0; padding:22px; border:1px solid var(--line); border-radius:17px;
  color:var(--fg); background:var(--surface-2); text-align:left; }}
.choice:hover {{ border-color:rgba(66,117,45,.45); background:rgba(66,117,45,.06); }}
.choice strong {{ display:block; margin-bottom:7px; font-size:1.05rem; }}
.choice span {{ display:block; color:var(--muted); font-size:.88rem; line-height:1.45; }}
.drop {{ display:grid; place-items:center; min-height:210px; padding:24px; border:1.5px dashed
  rgba(66,117,45,.35); border-radius:17px; background:rgba(66,117,45,.045); text-align:center; }}
.drop.drag {{ border-color:var(--green); background:rgba(66,117,45,.09); }}
.drop-content {{ position:relative; display:block; }}
.drop-copy {{ min-width:0; }}
.drop-icon {{ position:absolute; top:50%; right:calc(100% + 18px); display:grid; place-items:center;
  width:52px; height:52px; transform:translateY(-50%);
  border-radius:16px; color:var(--green); background:rgba(66,117,45,.11); }}
.drop-icon svg {{ display:block; width:25px; height:25px; }}
.drop strong {{ display:block; font-size:1.05rem; }}
.drop-hint {{ display:block; margin-top:5px; color:var(--muted); font-size:.86rem; }}
button {{ border:0; font:inherit; cursor:pointer; }}
.wizard-action {{ display:inline-flex; justify-content:center; align-items:center; min-height:40px;
  padding:8px 12px; border-radius:11px; font-size:.84rem; font-weight:750; }}
.primary {{ margin-top:18px; color:#fff; background:var(--green); }}
.primary:disabled {{ opacity:.48; cursor:wait; }}
.file-list {{ display:flex; flex-wrap:wrap; justify-content:center; gap:8px; margin:14px 0 0; }}
.file-name {{ padding:7px 10px; border-radius:9px; color:var(--green);
  background:rgba(66,117,45,.09); font-size:.84rem; font-weight:700; }}
.status {{ margin:16px 0 0; color:var(--muted); text-align:center; }}
.status:empty {{ margin-top:0; }}
.status.error {{ color:#a72f49; }}
[hidden] {{ display:none !important; }}
.intent-list {{ display:grid; gap:14px; }}
.intent {{ padding:18px; border:1px solid var(--line); border-radius:17px; background:var(--surface-2); }}
.intent-head {{ display:flex; gap:10px; align-items:center; }}
input {{ width:100%; padding:12px 13px; border:1px solid var(--line); border-radius:11px;
  color:var(--fg); background:var(--surface); font:inherit; }}
.intent-label {{ font-size:1rem; font-weight:750; }}
.remove {{ flex:none; width:38px; height:38px; border-radius:10px; color:var(--muted);
  background:transparent; font-size:1.3rem; }}
.field-label {{ display:block; margin:14px 0 7px; color:var(--muted); font-size:.78rem;
  font-weight:750; }}
.intent-name-label {{ margin-top:0; }}
.actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }}
.actions .primary {{ margin-top:0; }}
.secondary {{ border:1px solid var(--line); color:var(--fg); background:var(--surface);
  box-shadow:0 5px 16px rgba(52,46,34,.06); font-size:.84rem; font-weight:750; }}
.analysis-note {{ margin:0 0 18px; color:var(--muted); line-height:1.5; }}
.mode-back {{ margin:0 0 18px; border:1px solid var(--line); color:var(--fg);
  background:var(--surface); box-shadow:0 5px 16px rgba(52,46,34,.06); }}
.mode-back:hover {{ border-color:rgba(66,117,45,.45); background:rgba(66,117,45,.06); }}
.back-link {{ display:inline-flex; margin:0 0 26px; color:var(--muted); font-size:.82rem;
  font-weight:700; text-decoration:none; }}
@media (max-width:520px) {{ .panel {{ padding:16px; }} .choice-grid {{ grid-template-columns:1fr; }} }}
</style></head><body><div class="ambient"></div><main>
<div class="brand"><span class="brand-mark">JW</span>jobwatch</div>
{back_link}
<div class="steps"><span class="step active"></span><span class="step" id="step-2"></span>
  <span class="step" id="step-3"></span></div>
<section id="choice-step"{choice_hidden}>
  <p class="eyebrow">Étape 1 · Votre départ</p>
  <h1>Comment voulez-vous commencer ?</h1>
  <p class="lead">jobwatch surveille les nouvelles offres correspondant aux postes qui vous
  intéressent et les rassemble dans un seul tableau de bord. Vous pouvez partir de vos CV ou
  définir directement vos catégories de postes.</p>
  <div class="panel choice-grid">
    <button class="choice" id="choose-cv" type="button"><strong>Importer mes CV</strong>
      <span>Ajoutez un ou plusieurs PDF et laissez l’IA suggérer des catégories de postes.</span></button>
    <button class="choice" id="choose-manual" type="button"><strong>Créer mes catégories</strong>
      <span>Renseignez vous-même les métiers et mots-clés à surveiller.</span></button>
  </div>
</section>
<section id="upload-step" hidden>
  <p class="eyebrow">Étape 2 · Vos CV</p>
  <h1>Ajoutez un ou plusieurs CV.</h1>
  <p class="lead">Ils restent privés dans votre instance. jobwatch les analyse ensemble pour
  vous proposer des catégories de postes que vous pourrez entièrement modifier.</p>
  <button class="wizard-action mode-back" id="back-to-choice-upload" type="button"{mode_back_hidden}>← Modifier mon choix</button>
  <div class="panel action-panel">
    <label class="drop" id="drop-zone" for="cv-file"><span class="drop-content">
      <span class="drop-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 3v12m-4-4 4 4 4-4"/><path d="M5 20h14"/></svg></span>
      <span class="drop-copy"><strong>Déposez vos CV ici</strong>
      <span class="drop-hint">Un ou plusieurs PDF · 10 Mo maximum chacun</span></span></span></label>
    <input id="cv-file" type="file" accept="application/pdf,.pdf" multiple hidden>
    <div class="file-list" id="file-list" hidden></div>
    <button class="wizard-action primary" id="analyze" type="button" disabled>Analyser mes CV</button>
    <p class="status" id="upload-status" aria-live="polite"></p>
  </div>
</section>
<section id="intent-step"{intent_hidden}>
  <p class="eyebrow">{eyebrow}</p>
  <h1>{heading}</h1>
  <p class="lead" id="intent-lead">{lead}</p>
  <button class="wizard-action mode-back" id="back-to-choice-intents" type="button"{mode_back_hidden}>← Modifier mon choix</button>
  <div class="panel action-panel">
    <p class="analysis-note">jobwatch cherchera les offres correspondant aux mots-clés de chaque
    catégorie. Toutes les offres seront réunies dans votre tableau de bord, avec leur catégorie.</p>
    <div class="intent-list" id="intent-list"></div>
    <div class="actions"><button class="wizard-action secondary" id="add-intent" type="button">+ Ajouter une catégorie</button>
      <button class="wizard-action primary" id="confirm" type="button">{confirm_label}</button></div>
    <p class="status" id="intent-status" aria-live="polite"></p>
  </div>
</section>
</main><script id="initial-data" type="application/json">{initial_data}</script><script>
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const initialData = JSON.parse(document.getElementById('initial-data').textContent);
const fileInput = document.getElementById('cv-file');
const fileList = document.getElementById('file-list');
const analyze = document.getElementById('analyze');
const uploadStatus = document.getElementById('upload-status');
const drop = document.getElementById('drop-zone');
let cvLibraryIds = initialData.cvLibraryIds || [];
let selectedFiles = [];

const addFiles = files => {{
  const errors = [];
  for (const file of files) {{
  if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {{
    errors.push(`${{file.name}} n’est pas un fichier PDF.`); continue;
  }}
  if (file.size > 10 * 1024 * 1024) {{
    errors.push(`${{file.name}} dépasse 10 Mo.`); continue;
  }}
  const duplicate = selectedFiles.some(item => item.name === file.name && item.size === file.size);
  if (!duplicate) selectedFiles.push(file);
  }}
  fileList.replaceChildren(...selectedFiles.map(file => {{
    const item = document.createElement('span'); item.className = 'file-name'; item.textContent = file.name;
    return item;
  }}));
  fileList.hidden = !selectedFiles.length; analyze.disabled = !selectedFiles.length;
  uploadStatus.textContent = errors.join(' '); uploadStatus.classList.toggle('error', Boolean(errors.length));
}};
fileInput.addEventListener('change', () => addFiles(fileInput.files));
['dragenter','dragover'].forEach(name => drop.addEventListener(name, event => {{
  event.preventDefault(); drop.classList.add('drag');
}}));
['dragleave','drop'].forEach(name => drop.addEventListener(name, event => {{
  event.preventDefault(); drop.classList.remove('drag');
}}));
drop.addEventListener('drop', event => addFiles(event.dataTransfer.files));
const base64 = file => new Promise((resolve, reject) => {{
  const reader = new FileReader(); reader.onerror = reject;
  reader.onload = () => resolve(String(reader.result).split(',', 2)[1]);
  reader.readAsDataURL(file);
}});
const post = (url, payload) => fetch(url, {{method:'POST', headers:{{
  'Content-Type':'application/json', 'X-CSRF-Token':csrf}}, body:JSON.stringify(payload)
}}).then(async response => {{
  const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Erreur'); return data;
}});

analyze.addEventListener('click', async () => {{
  if (!selectedFiles.length) return;
  analyze.disabled = true; uploadStatus.textContent = 'Envoi du CV…';
  try {{
    cvLibraryIds = [];
    for (const file of selectedFiles) {{
      const uploaded = await post('/documents', {{filename:file.name, label:file.name, type:'cv',
        content_base64:await base64(file)}});
      cvLibraryIds.push(uploaded.id);
    }}
    uploadStatus.textContent = 'Analyse du profil en cours…';
    const result = await post('/onboarding/analyze', {{cv_library_ids:cvLibraryIds}});
    showIntents(result.intents, true);
  }} catch (error) {{ uploadStatus.textContent = error.message; uploadStatus.classList.add('error');
    analyze.disabled = false; }}
}});

const showIntents = (intents, fromCv) => {{
  renderIntents(intents); document.getElementById('choice-step').hidden = true;
  document.getElementById('upload-step').hidden = true;
  document.getElementById('intent-step').hidden = false;
  document.getElementById('intent-lead').textContent = initialData.editing
    ? 'Ajoutez, renommez ou retirez une catégorie. Les changements s’appliqueront à vos prochaines découvertes.'
    : fromCv
      ? 'L’IA a lu vos CV, mais ces catégories ne sont que des propositions. Renommez-les, corrigez les mots-clés ou ajoutez une autre direction.'
      : 'Créez une ou plusieurs catégories, puis indiquez les intitulés et mots-clés à surveiller.';
  document.getElementById('step-2').classList.add('active');
  document.getElementById('step-3').classList.add('active'); window.scrollTo(0,0);
}};
document.getElementById('choose-cv').addEventListener('click', () => {{
  document.getElementById('choice-step').hidden = true;
  document.getElementById('upload-step').hidden = false;
  document.getElementById('step-2').classList.add('active'); window.scrollTo(0,0);
}});
const returnToChoice = () => {{
  document.getElementById('choice-step').hidden = false;
  document.getElementById('upload-step').hidden = true;
  document.getElementById('intent-step').hidden = true;
  document.getElementById('step-2').classList.remove('active');
  document.getElementById('step-3').classList.remove('active');
  window.scrollTo(0,0);
}};
document.getElementById('back-to-choice-upload').addEventListener('click', returnToChoice);
document.getElementById('back-to-choice-intents').addEventListener('click', returnToChoice);
document.getElementById('choose-manual').addEventListener('click', () =>
  showIntents([{{label:'',keywords:[],exclude:[]}}], false));

const intentCard = intent => {{
  const card = document.createElement('div'); card.className = 'intent';
  card.innerHTML = `<label class="field-label intent-name-label">Nom de la catégorie</label>
    <div class="intent-head"><input class="intent-label" aria-label="Nom de la catégorie">
    <button class="remove" type="button" aria-label="Supprimer la catégorie">×</button></div>
    <label class="field-label">Intitulés de poste et mots-clés, séparés par des virgules</label>
    <input class="keywords" aria-label="Mots-clés">
    <label class="field-label">À exclure, séparés par des virgules</label>
    <input class="exclude" aria-label="Termes exclus"></div>`;
  if (Number.isInteger(intent.id)) card.dataset.intentId = String(intent.id);
  card.querySelector('.intent-label').value = intent.label || '';
  card.querySelector('.keywords').value = (intent.keywords || []).join(', ');
  card.querySelector('.exclude').value = (intent.exclude || []).join(', ');
  card.querySelector('.remove').addEventListener('click', () => card.remove());
  return card;
}};
const renderIntents = intents => {{
  const list = document.getElementById('intent-list'); list.replaceChildren();
  intents.forEach(intent => list.append(intentCard(intent)));
}};
const MAX_INTENTS = {max_intents};
document.getElementById('add-intent').addEventListener('click', () => {{
  const list = document.getElementById('intent-list');
  const status = document.getElementById('intent-status');
  if (list.querySelectorAll('.intent').length >= MAX_INTENTS) {{
    status.textContent = `${{MAX_INTENTS}} catégories maximum.`;
    status.classList.add('error'); return;
  }}
  status.textContent = ''; status.classList.remove('error');
  list.append(intentCard({{label:'',keywords:[],exclude:[]}}));
}});
document.getElementById('confirm').addEventListener('click', async event => {{
  const button = event.currentTarget; const status = document.getElementById('intent-status');
  const split = value => value.split(',').map(item => item.trim()).filter(Boolean);
  const intents = [...document.querySelectorAll('.intent')].map(card => ({{
    id:card.dataset.intentId ? Number(card.dataset.intentId) : null,
    label:card.querySelector('.intent-label').value.trim(),
    keywords:split(card.querySelector('.keywords').value),
    exclude:split(card.querySelector('.exclude').value),
  }})).filter(intent => intent.label && intent.keywords.length);
  if (!intents.length) {{ status.textContent = 'Ajoutez au moins une catégorie avec un mot-clé.';
    status.classList.add('error'); return; }}
  button.disabled = true; status.textContent = 'Enregistrement de vos catégories…';
  status.classList.remove('error');
  try {{ await post('/onboarding/complete', {{cv_library_ids:cvLibraryIds, intents}});
    location.href = '/'; }} catch (error) {{ status.textContent = error.message;
    status.classList.add('error'); button.disabled = false; }}
}});
if (initialData.editing) showIntents(initialData.intents, false);
</script></body></html>"""
