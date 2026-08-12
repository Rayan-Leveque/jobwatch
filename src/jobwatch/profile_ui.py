"""Page de profil facultatif pour personnaliser les lettres de motivation."""

from __future__ import annotations

import html

from jobwatch.profile import MAX_PROFILE_FIELD_LENGTH, ProfileDetails, ProfilePreferences
from jobwatch.seniority import SENIORITY_LEVELS
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
) -> str:
    preferences = preferences or ProfilePreferences()
    intro = (
        "Vos catégories sont prêtes. Choisissez maintenant les offres que vous voulez voir "
        "et si vous souhaitez utiliser les brouillons de lettres."
        if welcome
        else "Ajustez ce que Jobwatch vous montre et les outils de candidature que vous utilisez."
    )
    minimum_options = "".join(
        f'<option value="{value}"{" selected" if value == preferences.seniority_min else ""}>'
        f"{html.escape(label)}</option>"
        for value, label in SENIORITY_LEVELS
    )
    maximum_options = "".join(
        f'<option value="{value}"{" selected" if value == preferences.seniority_max else ""}>'
        f"{html.escape(label)}</option>"
        for value, label in SENIORITY_LEVELS
    )
    yes_checked = " checked" if preferences.cover_letters_enabled else ""
    no_checked = "" if preferences.cover_letters_enabled else " checked"
    disabled = "" if preferences.cover_letters_enabled else " disabled"
    exclusion_label = (
        f"{excluded_count} offre{'s' if excluded_count != 1 else ''} explicitement hors plage "
        f"{'sont masquées' if excluded_count != 1 else 'est masquée'} actuellement."
    )
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb">
<title>jobwatch · Profil de candidature</title>
{_csrf_head(csrf_token)}
<style>
:root {{ color-scheme:light; --bg:#f3f1eb; --surface:#fffefa; --surface2:#f8f6ef;
  --fg:#191b1f; --muted:#686d76; --line:rgba(29,31,35,.15); --accent:#42752d;
  --danger:#b63c54; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif; }}
* {{ box-sizing:border-box }} body {{ margin:0; min-height:100vh; color:var(--fg); background:
  radial-gradient(circle at 18% 0,rgba(112,82,200,.13),transparent 34%),var(--bg); }}
.shell {{ width:min(100%,760px); margin:0 auto; padding:24px 16px 48px }}
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
form {{ display:grid; gap:13px }} fieldset {{ min-width:0; margin:0; border:0; padding:0 }}
.preferences {{ display:grid; gap:17px; padding:18px; border:1px solid var(--line);
  border-radius:17px; background:var(--surface) }} .preferences h2 {{ margin:0; font-size:1.2rem }}
.preferences p {{ margin:0; color:var(--muted); font-size:.82rem; line-height:1.5 }}
.range-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px }}
.select-field {{ display:grid; gap:7px; min-width:0; color:var(--muted); font-size:.78rem;
  font-weight:750 }} select {{ width:100%; min-width:0; padding:12px 13px; border:1px solid var(--line);
  border-radius:12px; color:var(--fg); background:var(--surface2); font:inherit }}
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
@media (max-width:520px) {{ .top {{ align-items:flex-start }} .range-grid {{ grid-template-columns:1fr }}
  .actions {{ justify-content:stretch }}
  .status {{ flex-basis:100% }} .skip,button {{ flex:1; justify-content:center }} }}
</style></head><body><main class="shell">
  <div class="top"><a class="back" href="/">← Tableau de bord</a>
    <div class="account">{html.escape(email)}<br>Espace {html.escape(workspace_slug)}</div></div>
  <p class="eyebrow">Vos préférences</p><h1>Des offres adaptées à votre recherche.</h1>
  <p class="intro">{html.escape(intro)}</p>
  <p class="note">Tous les champs sont facultatifs. Jobwatch utilise seulement des informations
    que vous avez fournies et ne doit jamais inventer une expérience.</p>
  <form id="profile-form">
    <section class="preferences" aria-labelledby="seniority-heading">
      <h2 id="seniority-heading">Niveaux de séniorité recherchés</h2>
      <p>Les offres sans niveau explicite restent visibles. Jobwatch ne déduit jamais un niveau
        absent. {html.escape(exclusion_label)}</p>
      <div class="range-grid">
        <label class="select-field" for="seniority_min">Du niveau
          <select id="seniority_min" name="seniority_min">{minimum_options}</select></label>
        <label class="select-field" for="seniority_max">Au niveau
          <select id="seniority_max" name="seniority_max">{maximum_options}</select></label>
      </div>
    </section>
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
    </fieldset>
    <div class="actions"><p class="status" id="status" aria-live="polite"></p>
      <a class="skip" href="/">{'Passer pour l’instant' if welcome else 'Annuler'}</a>
      <button id="save" type="submit">Enregistrer mon profil</button></div>
  </form>
  <p class="privacy">Ces informations restent dans la base SQLite de cet espace. Elles sont
    envoyées au modèle de rédaction uniquement quand vous demandez une lettre.</p>
</main><script>
const letterFields=document.getElementById('letter-fields');
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
    const response=await fetch('/profile', {{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(payload)}});
    const data=await response.json().catch(()=>({{}}));
    if (!response.ok) throw new Error(data.error || 'Enregistrement impossible.');
    status.textContent='Profil enregistré.'; window.setTimeout(()=>location.href='/',500);
  }} catch (error) {{ status.textContent=error.message || 'Enregistrement impossible.';
    status.classList.add('error'); button.disabled=false; }}
}});
</script></body></html>"""
