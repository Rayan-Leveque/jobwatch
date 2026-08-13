"""Page de profil facultatif pour personnaliser les lettres de motivation."""

from __future__ import annotations

import html
import sqlite3

from jobwatch.onboarding import CareerIntent
from jobwatch.profile import MAX_PROFILE_FIELD_LENGTH, ProfileDetails, ProfilePreferences
from jobwatch.seniority import SENIORITY_LEVELS
from jobwatch.seniority_ui import seniority_level_labels_html, seniority_sync_script
from jobwatch.serve_templates import _csrf_head


def _field(
    name: str,
    label: str,
    help_text: str,
    placeholder: str,
    value: str,
    *,
    rows: int = 3,
) -> str:
    return f"""<label class="field" for="{name}">
      <span>{html.escape(label)}</span>
      <small>{html.escape(help_text)}</small>
      <textarea id="{name}" name="{name}" rows="{rows}"
        maxlength="{MAX_PROFILE_FIELD_LENGTH}"
        placeholder="{html.escape(placeholder, quote=True)}">{html.escape(value)}</textarea>
    </label>"""


def render_profile(
    details: ProfileDetails,
    csrf_token: str,
    *,
    preferences: ProfilePreferences | None = None,
    email: str,
    workspace_slug: str,
    welcome: bool = False,
    excluded_count: int = 0,
    cv_documents: list[sqlite3.Row] | None = None,
    career_intents: list[CareerIntent] | None = None,
) -> str:
    preferences = preferences or ProfilePreferences()
    intro = (
        "Vos catégories sont prêtes. Choisissez maintenant les offres que vous voulez voir "
        "et si vous souhaitez utiliser les brouillons de lettres."
        if welcome
        else "Ajustez ce que Jobwatch vous montre et les outils de candidature que vous utilisez."
    )
    range_max = len(SENIORITY_LEVELS) - 1
    level_labels = seniority_level_labels_html(range_max)
    yes_checked = " checked" if preferences.cover_letters_enabled else ""
    no_checked = "" if preferences.cover_letters_enabled else " checked"
    disabled = "" if preferences.cover_letters_enabled else " disabled"
    exclusion_label = (
        f"{excluded_count} offre{'s' if excluded_count != 1 else ''} explicitement hors plage "
        f"{'sont masquées' if excluded_count != 1 else 'est masquée'} actuellement."
    )
    cv_items = "".join(
        f'<li data-document-id="{int(row["id"])}"><span>{html.escape(str(row["label"]))}</span>'
        f'<a href="/documents/{int(row["id"])}" target="_blank" rel="noopener">Ouvrir</a></li>'
        for row in (cv_documents or [])
    )
    if not cv_items:
        cv_items = '<li class="empty-document">Aucun CV enregistré.</li>'
    intent_items = "".join(
        '<li><strong>' + html.escape(intent.label) + '</strong><div class="keyword-list">'
        + "".join(f"<span>{html.escape(keyword)}</span>" for keyword in intent.keywords)
        + "</div></li>"
        for intent in (career_intents or [])
    )
    if not intent_items:
        intent_items = '<li class="empty-category">Aucune catégorie enregistrée.</li>'
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb">
<title>jobwatch · Options</title>
{_csrf_head(csrf_token)}
<style>
:root {{ color-scheme:light; --bg:#f3f1eb; --surface:#fffefa; --surface2:#f8f6ef;
  --fg:#191b1f; --muted:#686d76; --line:rgba(29,31,35,.15); --accent:#42752d;
  --danger:#b63c54; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif; }}
* {{ box-sizing:border-box }} body {{ margin:0; min-height:100vh; color:var(--fg); background:
  radial-gradient(circle at 18% 0,rgba(112,82,200,.13),transparent 34%),var(--bg); }}
.shell {{ width:min(100%,1040px); margin:0 auto; padding:24px 16px 48px }}
.top {{ display:flex; align-items:center; justify-content:space-between; gap:14px; margin-bottom:38px }}
.brand {{ font-size:.78rem; font-weight:850; letter-spacing:.15em; text-transform:uppercase;
  color:var(--accent) }} .account {{ min-width:0; color:var(--muted); font-size:.72rem;
  text-align:right; overflow-wrap:anywhere }}
.back {{ display:inline-flex; min-height:44px; align-items:center; padding:0 13px; border:1px solid var(--line);
  border-radius:12px; color:var(--fg); background:var(--surface); font-size:.78rem;
  font-weight:740; text-decoration:none }}
.eyebrow {{ margin:0 0 10px; color:var(--accent); font-size:.69rem; font-weight:820;
  letter-spacing:.16em; text-transform:uppercase }} h1 {{ margin:0; max-width:650px;
  font-size:clamp(2.1rem,9vw,4rem); line-height:1; letter-spacing:-.055em }}
.intro {{ max-width:620px; margin:18px 0 28px; color:var(--muted); line-height:1.6 }}
.note {{ margin:0 0 20px; padding:14px 16px; border:1px solid rgba(66,117,45,.2);
  border-radius:15px; color:#355f26; background:rgba(66,117,45,.08); font-size:.8rem }}
.account-actions {{ display:flex; align-items:center; justify-content:flex-end; flex-wrap:wrap; gap:8px }}
.logout {{ min-height:36px; padding:0 10px; border:1px solid var(--line); color:var(--muted);
  background:var(--surface); font-size:.72rem; font-weight:740 }}
.categories {{ display:grid; gap:14px; padding:18px; border:1px solid var(--line);
  border-radius:17px; background:var(--surface) }}
.categories-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px }}
.categories-head div {{ min-width:0 }} .categories h2 {{ margin:0 0 5px; font-size:1.2rem }}
.categories p {{ margin:0; color:var(--muted); font-size:.82rem; line-height:1.5 }}
.categories-head > a {{ min-height:42px; display:inline-flex; align-items:center; flex:none; padding:0 13px;
  border:1px solid var(--line); border-radius:11px; color:var(--accent); font-size:.76rem;
  font-weight:780; text-decoration:none }}
.category-list {{ display:grid; gap:8px; margin:0; padding:0; list-style:none }}
.category-list > li {{ display:grid; gap:8px; min-width:0; padding:12px; border:1px solid var(--line);
  border-radius:12px; background:var(--surface2) }} .category-list strong {{ font-size:.82rem }}
.keyword-list {{ display:flex; flex-wrap:wrap; gap:5px }} .keyword-list span {{ max-width:100%; padding:4px 7px;
  border-radius:999px; color:var(--muted); background:var(--surface); font-size:.68rem;
  overflow-wrap:anywhere }} .category-list .empty-category {{ color:var(--muted); font-size:.78rem }}
.documents {{ display:grid; gap:14px; padding:18px; border:1px solid var(--line); border-radius:17px;
  background:var(--surface) }} .documents-head {{ display:flex; align-items:flex-start;
  justify-content:space-between; gap:16px }} .documents h2 {{ margin:0 0 5px; font-size:1.2rem }}
.documents p {{ margin:0; color:var(--muted); font-size:.82rem; line-height:1.5 }}
.upload-button {{ min-height:42px; flex:none; padding:0 13px }}
.document-list {{ display:grid; gap:7px; margin:0; padding:0; list-style:none }}
.document-list li {{ min-width:0; min-height:42px; display:flex; align-items:center;
  justify-content:space-between; gap:12px; padding:8px 11px; border:1px solid var(--line);
  border-radius:11px; background:var(--surface2); font-size:.78rem }}
.document-list li span {{ min-width:0; overflow-wrap:anywhere }} .document-list li a {{ flex:none;
  color:var(--accent); font-weight:760; text-decoration:none }}
.document-list .empty-document {{ color:var(--muted); justify-content:flex-start }}
.upload-status {{ margin:0!important; color:var(--muted); font-size:.76rem!important }}
.upload-status.error {{ color:var(--danger) }}
form {{ display:grid; gap:13px }} fieldset {{ min-width:0; margin:0; border:0; padding:0 }}
.settings-layout {{ display:grid; grid-template-columns:190px minmax(0,1fr); gap:18px; align-items:start }}
.settings-nav {{ position:sticky; top:18px; display:grid; gap:5px; padding:7px; border:1px solid var(--line);
  border-radius:16px; background:var(--surface) }}
.settings-tab {{ min-height:44px; padding:0 12px; border:0; border-radius:10px; color:var(--muted);
  background:transparent; font-size:.78rem; font-weight:760; text-align:left; cursor:pointer }}
.settings-tab[aria-selected="true"] {{ color:var(--accent); background:rgba(66,117,45,.10) }}
.settings-content {{ min-width:0 }} .settings-panel {{ display:grid; gap:13px }}
.settings-panel[hidden] {{ display:none }} .panel-heading {{ margin:0 2px 4px }}
.panel-heading h2 {{ margin:0 0 5px; font-size:1.35rem }} .panel-heading p {{ margin:0;
  color:var(--muted); font-size:.82rem; line-height:1.5 }}
.security-card {{ display:grid; gap:16px; padding:18px; border:1px solid var(--line);
  border-radius:17px; background:var(--surface) }} .security-row {{ display:flex; align-items:center;
  justify-content:space-between; gap:16px }} .security-row strong {{ display:block; font-size:.82rem }}
.security-row span {{ display:block; margin-top:3px; color:var(--muted); font-size:.75rem;
  overflow-wrap:anywhere }} .security-card .logout {{ min-height:42px; flex:none; color:var(--danger) }}
.preferences {{ display:grid; gap:17px; padding:18px; border:1px solid var(--line);
  border-radius:17px; background:var(--surface) }} .preferences h2 {{ margin:0; font-size:1.2rem }}
.preferences p {{ margin:0; color:var(--muted); font-size:.82rem; line-height:1.5 }}
.range-summary {{ margin-top:2px!important; color:var(--fg)!important; font-weight:790 }}
.dual-range {{ position:relative; height:42px; margin:10px 10px 0 }}
.range-rail,.range-selection {{ position:absolute; top:18px; right:13px; left:13px; height:6px;
  border-radius:999px; background:var(--line) }}
.range-selection {{ left:var(--range-left); right:auto; width:var(--range-width); background:var(--accent) }}
.range-input {{ position:absolute; inset:0; width:100%; height:42px; margin:0; padding:0;
  appearance:none; -webkit-appearance:none; background:transparent; pointer-events:none }}
.range-input::-webkit-slider-runnable-track {{ height:6px; background:transparent }}
.range-input::-moz-range-track {{ height:6px; background:transparent }}
.range-input::-webkit-slider-thumb {{ width:26px; height:26px; margin-top:-10px; border:3px solid var(--surface);
  border-radius:50%; appearance:none; -webkit-appearance:none; background:var(--accent);
  box-shadow:0 0 0 1px var(--accent),0 3px 9px rgba(25,27,31,.22); pointer-events:auto; cursor:grab }}
.range-input::-moz-range-thumb {{ width:20px; height:20px; border:3px solid var(--surface);
  border-radius:50%; background:var(--accent); box-shadow:0 0 0 1px var(--accent),0 3px 9px rgba(25,27,31,.22);
  pointer-events:auto; cursor:grab }}
.range-input:focus-visible {{ outline:none }} .range-input:focus-visible::-webkit-slider-thumb {{ outline:3px solid rgba(112,82,200,.28); outline-offset:3px }}
.range-labels {{ position:relative; height:38px }}
.range-labels span {{ position:absolute; top:8px; left:var(--level-position); width:max-content;
  max-width:18%; color:var(--muted); font-size:.62rem; line-height:1.2; text-align:center;
  overflow-wrap:anywhere; transform:translateX(-50%) }}
.range-labels span::before {{ content:""; position:absolute; bottom:calc(100% + 5px); left:50%;
  width:2px; height:6px; border-radius:2px; background:var(--line); transform:translateX(-50%) }}
.range-labels span:first-child {{ text-align:left; transform:none }}
.range-labels span:first-child::before {{ left:0; transform:none }}
.range-labels span:last-child {{ text-align:right; transform:translateX(-100%) }}
.range-labels span:last-child::before {{ right:0; left:auto; transform:none }}
.radio-group {{ display:grid; gap:8px }} .radio-label {{ display:flex; align-items:flex-start; gap:10px;
  padding:12px; border:1px solid var(--line); border-radius:12px; background:var(--surface2) }}
.radio-label input {{ margin-top:3px; accent-color:var(--accent) }} .radio-label span {{ line-height:1.4 }}
.letter-fields {{ display:grid; gap:13px }} .letter-fields:disabled {{ opacity:.55 }}
.section-heading {{ margin:12px 2px 0 }} .section-heading h2 {{ margin:0 0 5px; font-size:1.2rem }}
.section-heading p {{ margin:0; color:var(--muted); font-size:.82rem; line-height:1.5 }}
.field {{ display:grid; gap:7px; padding:17px;
  border:1px solid var(--line); border-radius:17px; background:var(--surface) }}
.field > span {{ font-weight:790 }} .field small {{ color:var(--muted); line-height:1.45 }}
textarea {{ width:100%; min-width:0; resize:vertical; padding:13px 14px; border:1px solid var(--line);
  border-radius:12px; color:var(--fg); background:var(--surface2); font:inherit; line-height:1.5 }}
textarea:focus {{ outline:3px solid rgba(112,82,200,.24); border-color:#7052c8 }}
.actions {{ position:sticky; bottom:12px; display:flex; align-items:center; justify-content:flex-end;
  flex-wrap:wrap; gap:10px; margin-top:8px; padding:11px; border:1px solid var(--line);
  border-radius:16px; background:rgba(255,254,250,.94); backdrop-filter:blur(12px) }}
.skip {{ min-height:46px; display:inline-flex; align-items:center; padding:0 15px; color:var(--muted);
  font-size:.8rem; font-weight:740; text-decoration:none }} button {{ min-height:46px; padding:0 18px;
  border:0; border-radius:12px; color:#fff; background:var(--accent); font:inherit;
  font-weight:820; cursor:pointer }} button:disabled {{ opacity:.55 }}
.status {{ flex:1 1 180px; margin:0; color:var(--muted); font-size:.76rem }}
.status.error {{ color:var(--danger) }} .privacy {{ margin:22px 4px 0; color:var(--muted);
  font-size:.72rem; line-height:1.5 }}
@media (max-width:700px) {{ .settings-layout {{ grid-template-columns:1fr }}
  .settings-nav {{ position:static; grid-template-columns:repeat(4,minmax(0,1fr)) }}
  .settings-tab {{ padding:0 7px; font-size:.7rem; text-align:center }} }}
@media (max-width:520px) {{ .top {{ align-items:flex-start }}
  .categories-head,.documents-head {{ align-items:flex-start; flex-direction:column }}
  .actions {{ justify-content:stretch }}
  .status {{ flex-basis:100% }} .skip,button {{ flex:1; justify-content:center }} }}
</style></head><body><main class="shell">
  <div class="top"><a class="back" href="/">← Tableau de bord</a>
    <div class="account-actions"><div class="account">{html.escape(email)}<br>Espace {html.escape(workspace_slug)}</div></div></div>
  <p class="eyebrow">Options</p><h1>Adaptez Jobwatch à votre recherche.</h1>
  <p class="intro">{html.escape(intro)}</p>
  <p class="note">Tous les champs sont facultatifs. Jobwatch utilise seulement des informations
    que vous avez fournies et ne doit jamais inventer une expérience.</p>
  <form id="profile-form"><div class="settings-layout">
    <nav class="settings-nav" role="tablist" aria-label="Catégories d’options">
      <button class="settings-tab" type="button" role="tab" aria-selected="true" aria-controls="panel-recherche" data-settings-tab="recherche">Recherche</button>
      <button class="settings-tab" type="button" role="tab" aria-selected="false" aria-controls="panel-cv" data-settings-tab="cv">CV</button>
      <button class="settings-tab" type="button" role="tab" aria-selected="false" aria-controls="panel-lettres" data-settings-tab="lettres">Lettres</button>
      <button class="settings-tab" type="button" role="tab" aria-selected="false" aria-controls="panel-securite" data-settings-tab="securite">Sécurité</button>
    </nav><div class="settings-content">
    <div class="settings-panel" id="panel-recherche" role="tabpanel" data-settings-panel="recherche">
      <div class="panel-heading"><h2>Recherche</h2><p>Définissez les postes et les niveaux que Jobwatch doit vous montrer.</p></div>
    <section class="categories" aria-labelledby="categories-heading">
      <div class="categories-head"><div><h2 id="categories-heading">Mes catégories</h2>
        <p>Voici les métiers et les mots-clés utilisés pour classer vos offres.</p></div>
        <a href="/onboarding?edit=1">Modifier</a></div>
      <ul class="category-list">{intent_items}</ul>
    </section>
    <section class="preferences" aria-labelledby="seniority-heading">
      <h2 id="seniority-heading">Niveaux de séniorité recherchés</h2>
      <p>Les offres sans niveau explicite restent visibles. Jobwatch ne déduit jamais un niveau
        absent. {html.escape(exclusion_label)}</p>
      <p class="range-summary" id="seniority-summary" aria-live="polite"></p>
      <div class="dual-range" id="seniority-range">
        <span class="range-rail" aria-hidden="true"></span><span class="range-selection" aria-hidden="true"></span>
        <input class="range-input" id="seniority_min" name="seniority_min" type="range" min="0"
          max="{range_max}" step="1" value="{preferences.seniority_min}" aria-label="Niveau minimum">
        <input class="range-input" id="seniority_max" name="seniority_max" type="range" min="0"
          max="{range_max}" step="1" value="{preferences.seniority_max}" aria-label="Niveau maximum">
      </div><div class="range-labels" aria-hidden="true">{level_labels}</div>
    </section></div>
    <div class="settings-panel" id="panel-cv" role="tabpanel" data-settings-panel="cv" hidden>
      <div class="panel-heading"><h2>CV</h2><p>Gérez les documents proposés lors de vos candidatures.</p></div>
    <section class="documents" aria-labelledby="documents-heading">
      <div class="documents-head"><div><h2 id="documents-heading">Mes CV</h2>
        <p>Ajoutez les PDF que vous voulez retrouver lors de vos candidatures.</p></div>
        <button class="upload-button" id="upload-cv" type="button">Ajouter un CV</button></div>
      <input id="cv-file" type="file" accept="application/pdf,.pdf" hidden>
      <ul class="document-list" id="cv-list">{cv_items}</ul>
      <p class="upload-status" id="upload-status" aria-live="polite"></p>
    </section></div>
    <div class="settings-panel" id="panel-lettres" role="tabpanel" data-settings-panel="lettres" hidden>
      <div class="panel-heading"><h2>Lettres de motivation</h2><p>Choisissez si vous utilisez ce parcours et personnalisez sa rédaction.</p></div>
    <section class="preferences" aria-labelledby="letters-heading">
      <h2 id="letters-heading">Génération de lettres de motivation</h2>
      <p>Ce choix masque ou restaure tout le parcours de génération. Vos brouillons et vos
        informations personnelles ne sont jamais supprimés.</p>
      <div class="radio-group" role="radiogroup" aria-labelledby="letters-heading">
        <label class="radio-label"><input type="radio" name="cover_letters_enabled"
          value="true"{yes_checked}><span>Oui, afficher la génération de lettres</span></label>
        <label class="radio-label"><input type="radio" name="cover_letters_enabled"
          value="false"{no_checked}><span>Non, masquer ce parcours pour le moment</span></label>
      </div>
    </section>
    <div class="section-heading"><h2>Personnalisation des lettres</h2>
      <p id="letter-fields-note">Ces informations restent enregistrées lorsque la génération est désactivée.</p></div>
    <fieldset class="letter-fields" id="letter-fields"{disabled}>
      {_field('motivations', 'Vos motivations', 'Ce qui vous attire dans un poste, un secteur ou une mission.', 'Ex. construire des produits IA utiles et proches des équipes métier', details.motivations)}
      {_field('targets', 'Postes et entreprises ciblés', 'Les rôles, environnements ou types d’entreprises que vous privilégiez.', 'Ex. AI Engineer, équipes produit en croissance, secteur mobilité', details.targets)}
      {_field('highlights', 'Projets et réalisations à valoriser', 'Uniquement des faits vrais que la lettre peut mettre en avant.', 'Ex. projet RAG déployé, résultat mesurable, rôle précis dans une équipe', details.highlights, rows=5)}
      {_field('preferred_tone', 'Ton préféré', 'La manière dont vous souhaitez vous exprimer.', 'Ex. direct, chaleureux, concret, sans superlatifs', details.preferred_tone, rows=2)}
      {_field('constraints_text', 'Contraintes à respecter', 'Ce que la lettre doit prendre en compte ou ne pas affirmer.', 'Ex. disponible à partir d’octobre, mobilité limitée à Paris', details.constraints_text)}
      {_field('reusable_details', 'Informations personnelles réutilisables', 'Des détails vrais utiles dans plusieurs candidatures.', 'Ex. engagement associatif, langue de travail, intérêt durable pour un domaine', details.reusable_details, rows=4)}
    </fieldset></div>
    <div class="settings-panel" id="panel-securite" role="tabpanel" data-settings-panel="securite" hidden>
      <div class="panel-heading"><h2>Sécurité</h2><p>Consultez le compte connecté et fermez sa session.</p></div>
      <section class="security-card"><div class="security-row"><div><strong>Compte connecté</strong>
        <span>{html.escape(email)} · Espace {html.escape(workspace_slug)}</span></div>
        <button class="logout" id="logout" type="button">Se déconnecter</button></div></section>
    </div></div></div>
    <div class="actions"><p class="status" id="status" aria-live="polite"></p>
      <a class="skip" href="/">{'Passer pour l’instant' if welcome else 'Annuler'}</a>
      <button id="save" type="submit">Enregistrer mes options</button></div>
  </form>
  <p class="privacy">Ces informations restent dans la base SQLite de cet espace. Elles sont
    envoyées au modèle de rédaction uniquement quand vous demandez une lettre.</p>
</main><script>
const letterFields=document.getElementById('letter-fields');
const settingsTabs=[...document.querySelectorAll('[data-settings-tab]')];
const showSettingsPanel=(name,updateHash=true)=>{{
  const selected=settingsTabs.some(tab=>tab.dataset.settingsTab===name) ? name : 'recherche';
  settingsTabs.forEach(tab=>tab.setAttribute('aria-selected',String(tab.dataset.settingsTab===selected)));
  document.querySelectorAll('[data-settings-panel]').forEach(panel=>{{
    panel.hidden=panel.dataset.settingsPanel!==selected;
  }});
  if(updateHash) history.replaceState(null,'',`#${{selected}}`);
}};
settingsTabs.forEach(tab=>tab.addEventListener('click',()=>showSettingsPanel(tab.dataset.settingsTab)));
showSettingsPanel(location.hash.slice(1)||'recherche',false);
const cvFile=document.getElementById('cv-file');
const uploadStatus=document.getElementById('upload-status');
document.getElementById('upload-cv').addEventListener('click',()=>cvFile.click());
cvFile.addEventListener('change',async()=>{{
  const file=cvFile.files[0]; if(!file) return;
  uploadStatus.classList.remove('error');
  if(file.size>10*1024*1024) {{ uploadStatus.textContent='Ce fichier dépasse 10 Mo.';
    uploadStatus.classList.add('error'); return; }}
  uploadStatus.textContent='Import du CV…';
  try {{
    const content=await new Promise((resolve,reject)=>{{ const reader=new FileReader();
      reader.onload=()=>resolve(String(reader.result).split(',',2)[1]); reader.onerror=reject;
      reader.readAsDataURL(file); }});
    const response=await fetch('/documents',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{filename:file.name,label:file.name,type:'cv',content_base64:content}})}});
    const data=await response.json().catch(()=>({{}}));
    if(!response.ok) throw new Error(data.error||'Import impossible.');
    document.querySelector('#cv-list .empty-document')?.remove();
    const item=document.createElement('li'); item.dataset.documentId=data.id;
    const label=document.createElement('span'); label.textContent=data.label;
    const link=document.createElement('a'); link.href=`/documents/${{data.id}}`; link.target='_blank';
    link.rel='noopener'; link.textContent='Ouvrir'; item.append(label,link);
    document.getElementById('cv-list').append(item); uploadStatus.textContent='CV ajouté.'; cvFile.value='';
  }} catch(error) {{ uploadStatus.textContent=error.message||'Import impossible.';
    uploadStatus.classList.add('error'); }}
}});
{seniority_sync_script(min_id='seniority_min', max_id='seniority_max')}
document.getElementById('logout').addEventListener('click', async event => {{
  event.currentTarget.disabled=true;
  try {{ const response=await fetch('/logout',{{method:'POST'}});
    if (!response.ok) throw new Error(); location.href='/login';
  }} catch (_) {{ event.currentTarget.disabled=false; }}
}});
const syncLetterFields=()=>{{
  const enabled=document.querySelector('[name="cover_letters_enabled"]:checked').value==='true';
  letterFields.disabled=!enabled;
}};
document.querySelectorAll('[name="cover_letters_enabled"]').forEach(input =>
  input.addEventListener('change', syncLetterFields));
document.getElementById('profile-form').addEventListener('submit', async event => {{
  event.preventDefault(); const button=document.getElementById('save');
  const status=document.getElementById('status'); button.disabled=true;
  status.textContent='Enregistrement…'; status.classList.remove('error');
  const payload=Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.seniority_min=Number(payload.seniority_min);
  payload.seniority_max=Number(payload.seniority_max);
  payload.cover_letters_enabled=payload.cover_letters_enabled==='true';
  try {{
    const response=await fetch('/options', {{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(payload)}});
    const data=await response.json().catch(()=>({{}}));
    if (!response.ok) throw new Error(data.error || 'Enregistrement impossible.');
    status.textContent='Options enregistrées.'; window.setTimeout(()=>location.href='/',500);
  }} catch (error) {{ status.textContent=error.message || 'Enregistrement impossible.';
    status.classList.add('error'); button.disabled=false; }}
}});
</script></body></html>"""
