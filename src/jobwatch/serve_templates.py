"""Gabarits de page du tableau de bord : chrome HTML/CSS/JS statique.

Coquilles de page extraites de jobwatch.serve : shell HTML, styles et scripts
non dépendants des données (le rendu des données vit dans jobwatch.serve_render).
"""

from __future__ import annotations

import html


def _csrf_head(token: str) -> str:
    if not token:
        return ""
    escaped = html.escape(token, quote=True)
    return f"""<meta name="csrf-token" content="{escaped}">
<script>
(function () {{
  const originalFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {{
    const options = Object.assign({{}}, init || {{}});
    const method = String(options.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {{
      const headers = new Headers(options.headers || {{}});
      headers.set('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
      options.headers = headers;
    }}
    return originalFetch(input, options);
  }};
}})();
</script>"""


_BUG_REPORT_DIALOG = """\
<div class="bug-report-overlay" id="bug-report-overlay" hidden>
  <section class="bug-report-dialog" role="dialog" aria-modal="true"
    aria-labelledby="bug-report-title">
    <button class="bug-report-close" type="button" data-bug-report-close
      aria-label="Fermer">×</button>
    <p class="eyebrow">Un problème ?</p>
    <h2 id="bug-report-title">Signaler un bug</h2>
    <p class="bug-report-intro">Décrivez simplement ce qui s'est passé. La page et le
      navigateur seront joints automatiquement.</p>
    <form id="bug-report-form">
      <label class="doc-label" for="bug-report-message">Que s'est-il passé ?</label>
      <textarea class="apply-input bug-report-message" id="bug-report-message"
        maxlength="4000" required placeholder="Ex. J'ai appuyé sur… et rien ne s'est passé."></textarea>
      <div class="bug-report-actions">
        <button class="card-action" type="button" data-bug-report-close>Annuler</button>
        <button class="card-action bug-report-submit" type="submit">Envoyer</button>
      </div>
      <p class="bug-report-status" id="bug-report-status" aria-live="polite"></p>
    </form>
  </section>
</div>"""


_BUG_REPORT_JS = """\
(function () {
  const overlay = document.getElementById('bug-report-overlay');
  const form = document.getElementById('bug-report-form');
  if (!overlay || !form) return;
  const message = document.getElementById('bug-report-message');
  const status = document.getElementById('bug-report-status');
  const submit = form.querySelector('.bug-report-submit');
  let previousFocus = null;
  const open = trigger => {
    previousFocus = trigger;
    status.textContent = '';
    status.classList.remove('is-error', 'is-success');
    overlay.hidden = false;
    document.body.classList.add('modal-open');
    requestAnimationFrame(() => message.focus());
  };
  const close = () => {
    overlay.hidden = true;
    document.body.classList.remove('modal-open');
    if (previousFocus) previousFocus.focus();
  };
  document.querySelectorAll('[data-bug-report-open]').forEach(button => {
    button.addEventListener('click', () => open(button));
  });
  overlay.querySelectorAll('[data-bug-report-close]').forEach(button => {
    button.addEventListener('click', close);
  });
  overlay.addEventListener('click', event => {
    if (event.target === overlay) close();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !overlay.hidden) close();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const text = message.value.trim();
    if (!text) {
      status.textContent = 'Décrivez le problème rencontré.';
      status.classList.add('is-error');
      return;
    }
    submit.disabled = true;
    status.textContent = 'Envoi…';
    status.classList.remove('is-error', 'is-success');
    try {
      const response = await fetch('/bug-report', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          message: text,
          page: location.pathname + location.search,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || 'Envoi impossible.');
      message.value = '';
      status.textContent = 'Merci, le signalement a bien été envoyé.';
      status.classList.add('is-success');
      setTimeout(close, 1200);
    } catch (error) {
      status.textContent = error.message || 'Envoi impossible.';
      status.classList.add('is-error');
    } finally {
      submit.disabled = false;
    }
  });
})();
"""


def _login_form(email: str = "", error: str = "") -> str:
    error_html = f'<p class="auth-error">{html.escape(error)}</p>' if error else ""
    return f"""{error_html}
<form method="post" action="/login">
  <label>Email<input type="email" name="email" autocomplete="username" required
    value="{html.escape(email, quote=True)}"></label>
  {_password_field("password", "Mot de passe", "current-password")}
  <button type="submit">Se connecter</button>
</form>"""


def _invite_form(token: str, error: str = "") -> str:
    error_html = f'<p class="auth-error">{html.escape(error)}</p>' if error else ""
    action = f"/invite/{html.escape(token, quote=True)}"
    return f"""{error_html}
<p class="auth-intro">Choisissez un mot de passe d'au moins 8 caractères.</p>
<form method="post" action="{action}">
  {_password_field("password", "Mot de passe", "new-password", minlength=8)}
  {_password_field("password_confirmation", "Confirmation", "new-password", minlength=8)}
  <button type="submit">Créer mon compte</button>
</form>"""


def _password_field(
    name: str, label: str, autocomplete: str, *, minlength: int | None = None
) -> str:
    minimum = f' minlength="{minlength}"' if minlength is not None else ""
    field_id = f"auth-{name}"
    return f"""<label>{html.escape(label)}<span class="password-field">
    <input id="{field_id}" type="password" name="{name}" autocomplete="{autocomplete}"
      {minimum} required>
    <button class="password-toggle" type="button" data-password-target="{field_id}"
      aria-label="Afficher le mot de passe" aria-pressed="false">
      <svg class="eye-show" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/>
        <circle cx="12" cy="12" r="2.5"/></svg>
      <svg class="eye-hide" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" aria-hidden="true"><path d="m3 3 18 18M10.6 6.2A10.8 10.8 0 0 1 12 6c6 0 9.5 6 9.5 6a16 16 0 0 1-2.3 3M6.3 6.3C3.9 8 2.5 12 2.5 12s3.5 6 9.5 6c1.7 0 3.2-.5 4.5-1.2"/></svg>
    </button></span></label>"""


def _auth_page(title: str, body: str, *, workspace_slug: str | None = None) -> str:
    workspace = (
        f'<p class="auth-workspace">Espace {html.escape(workspace_slug)}</p>'
        if workspace_slug
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb"><title>jobwatch · {html.escape(title)}</title>
<style>
:root {{ color-scheme:light; font-family:Inter,ui-sans-serif,system-ui,sans-serif; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
  color:#191b1f; background:radial-gradient(circle at 20% 0,#e5def7 0,transparent 35%),#f3f1eb; }}
.auth-card {{ width:min(100%,430px); padding:32px; border:1px solid rgba(29,31,35,.12);
  border-radius:24px; background:#fffefa; box-shadow:0 24px 70px rgba(52,46,34,.13); }}
.auth-brand {{ color:#42752d; font-size:.78rem; font-weight:800; letter-spacing:.16em;
  text-transform:uppercase; }}
.auth-workspace {{ margin:8px 0 0; color:#686d76; font-size:.78rem; font-weight:700; }}
h1 {{ margin:12px 0 24px; font-size:clamp(1.8rem,8vw,2.5rem); line-height:1.05; }}
form {{ display:grid; gap:18px; }}
label {{ display:grid; gap:8px; color:#686d76; font-size:.86rem; font-weight:650; }}
input {{ width:100%; padding:13px 14px; border:1px solid rgba(29,31,35,.17);
  border-radius:12px; color:#191b1f; background:#f8f6ef; font:inherit; }}
input:focus {{ outline:2px solid #42752d; outline-offset:2px; }}
.password-field {{ position:relative; display:block; }}
.password-field input {{ padding-right:48px; }}
button {{ margin-top:4px; padding:14px 18px; border:0; border-radius:12px;
  color:#fff; background:#42752d; font:inherit; font-weight:800; cursor:pointer; }}
.password-toggle {{ position:absolute; top:50%; right:5px; width:38px; height:38px;
  margin:0; padding:9px; transform:translateY(-50%); color:#686d76; background:transparent; }}
.password-toggle:hover {{ color:#191b1f; background:rgba(29,31,35,.06); }}
.password-toggle svg {{ width:20px; height:20px; }}
.password-toggle .eye-hide {{ display:none; }}
.password-toggle[aria-pressed="true"] .eye-show {{ display:none; }}
.password-toggle[aria-pressed="true"] .eye-hide {{ display:block; }}
.auth-intro {{ color:#686d76; line-height:1.5; }}
.auth-error {{ padding:12px 14px; border-radius:12px; color:#8f2940;
  background:rgba(182,60,84,.10); }}
.auth-link {{ display:inline-flex; padding:12px 16px; border-radius:12px; color:#fff;
  background:#42752d; font-weight:750; text-decoration:none; }}
</style></head><body><main class="auth-card"><div class="auth-brand">jobwatch</div>{workspace}
<h1>{html.escape(title)}</h1>{body}</main><script>
document.querySelectorAll('[data-password-target]').forEach(button => {{
  button.addEventListener('click', () => {{
    const input = document.getElementById(button.dataset.passwordTarget);
    const visible = input.type === 'text';
    input.type = visible ? 'password' : 'text';
    button.setAttribute('aria-pressed', visible ? 'false' : 'true');
    button.setAttribute('aria-label', visible ? 'Afficher le mot de passe' : 'Masquer le mot de passe');
  }});
}});
</script></body></html>"""


_CSS = """\
:root {
  color-scheme:dark;
  --bg:#090b10; --bg-deep:#06070a; --surface:#11141c; --surface-2:#171b25;
  --surface-hover:#1c2130; --fg:#f3f5f8; --muted:#9198a8; --muted-2:#6e7585;
  --line:rgba(255,255,255,.085); --line-strong:rgba(255,255,255,.16);
  --accent:#b9f46f; --accent-ink:#17210d; --accent-soft:rgba(185,244,111,.12);
  --violet:#ab91ff; --violet-soft:rgba(171,145,255,.12);
  --amber:#ffbe63; --amber-soft:rgba(255,190,99,.12);
  --blue:#72b7ff; --blue-soft:rgba(114,183,255,.13);
  --danger:#ff879b; --danger-soft:rgba(255,135,155,.12);
  --shadow:0 22px 60px rgba(0,0,0,.30);
  --card-shadow:0 8px 28px rgba(0,0,0,.16);
  --radius-xl:24px; --radius-lg:18px; --radius-md:14px;
}
html[data-theme="light"] {
  color-scheme:light;
  --bg:#f3f1eb; --bg-deep:#ebe7de; --surface:#fffefa; --surface-2:#f8f6ef;
  --surface-hover:#f4f1e8; --fg:#191b1f; --muted:#686d76; --muted-2:#858990;
  --line:rgba(29,31,35,.095); --line-strong:rgba(29,31,35,.17);
  --accent:#42752d; --accent-ink:#fff; --accent-soft:rgba(66,117,45,.10);
  --violet:#7052c8; --violet-soft:rgba(112,82,200,.09);
  --amber:#9a5d08; --amber-soft:rgba(154,93,8,.09);
  --blue:#1269ad; --blue-soft:rgba(18,105,173,.09);
  --danger:#b63c54; --danger-soft:rgba(182,60,84,.10);
  --shadow:0 22px 55px rgba(52,46,34,.12);
  --card-shadow:0 7px 24px rgba(52,46,34,.07);
}
* { box-sizing:border-box }
html { min-height:100%; background:var(--bg); scroll-behavior:smooth }
body { min-height:100vh; margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  -webkit-font-smoothing:antialiased; -webkit-tap-highlight-color:transparent }
button, input { font:inherit }
button, summary, a { -webkit-tap-highlight-color:transparent }
.ambient { position:fixed; inset:0; overflow:hidden; pointer-events:none; z-index:0 }
.ambient::before { content:""; position:absolute; width:480px; height:480px;
  top:-250px; left:50%; transform:translateX(-50%); border-radius:50%;
  background:radial-gradient(circle, rgba(171,145,255,.22), transparent 67%);
  filter:blur(12px) }
.ambient::after { content:""; position:absolute; width:320px; height:320px;
  top:260px; right:-240px; border-radius:50%;
  background:radial-gradient(circle, rgba(185,244,111,.11), transparent 70%) }
html[data-theme="light"] .ambient::before { background:radial-gradient(circle, rgba(112,82,200,.13), transparent 67%) }
html[data-theme="light"] .ambient::after { background:radial-gradient(circle, rgba(66,117,45,.10), transparent 70%) }
.shell { position:relative; z-index:1; width:min(100%, 760px); margin:0 auto;
  padding:max(18px, env(safe-area-inset-top)) calc(16px + env(safe-area-inset-right))
  calc(40px + env(safe-area-inset-bottom)) calc(16px + env(safe-area-inset-left)) }
.topbar { min-height:48px; display:flex; align-items:center; justify-content:space-between;
  gap:16px; margin-bottom:30px }
.identity { display:flex; align-items:center; gap:11px; min-width:0 }
.monogram { width:38px; height:38px; display:grid; place-items:center; border-radius:12px;
  color:var(--accent-ink); background:var(--accent); font-size:.9rem; font-weight:850;
  letter-spacing:-.04em; box-shadow:0 0 0 5px var(--accent-soft) }
.identity-copy { display:flex; flex-direction:column; line-height:1.18 }
.identity-name { font-weight:750; letter-spacing:-.015em }
.identity-sub { margin-top:3px; color:var(--muted); font-size:.73rem; letter-spacing:.02em }
.theme-toggle { position:relative; width:48px; height:48px; flex:0 0 48px; border:1px solid var(--line);
  border-radius:15px; color:var(--fg); background:color-mix(in srgb, var(--surface) 86%, transparent);
  box-shadow:var(--card-shadow); cursor:pointer; transition:transform .2s ease, background .2s ease,
  border-color .2s ease }
.theme-toggle:active { transform:scale(.94) }
.theme-toggle svg { position:absolute; inset:0; margin:auto; width:20px; height:20px;
  transition:opacity .2s ease, transform .35s cubic-bezier(.2,.8,.2,1) }
.icon-sun { opacity:0; transform:rotate(-70deg) scale(.6) }
.icon-moon { opacity:1; transform:rotate(0) scale(1) }
html[data-theme="light"] .icon-sun { opacity:1; transform:rotate(0) scale(1) }
html[data-theme="light"] .icon-moon { opacity:0; transform:rotate(70deg) scale(.6) }
.theme-toggle:focus-visible, .clear-search:focus-visible,
summary:focus-visible, a:focus-visible { outline:3px solid var(--violet); outline-offset:3px }
.card-toggle:focus-visible { outline:3px solid var(--violet); outline-offset:-4px }
.hero { margin-bottom:22px }
.eyebrow { margin:0 0 10px; color:var(--accent); font-size:.69rem; font-weight:800;
  letter-spacing:.16em; text-transform:uppercase }
h1 { max-width:560px; margin:0; font-size:clamp(2.25rem, 10vw, 4.6rem); line-height:.98;
  font-weight:810; letter-spacing:-.065em }
h1 span { color:var(--muted-2); font-weight:620 }
.hero-meta { display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin:18px 0 0;
  color:var(--muted); font-size:.78rem }
.live-dot { width:7px; height:7px; border-radius:50%; background:var(--accent);
  box-shadow:0 0 0 5px var(--accent-soft) }
.track-tabs { display:grid; grid-template-columns:repeat(2, 1fr); gap:6px; margin:20px 0 0;
  padding:5px; border:1px solid var(--line); border-radius:var(--radius-md);
  background:var(--surface) }
.track-tab { display:grid; place-items:center; min-height:40px; padding:0 10px;
  border-radius:calc(var(--radius-md) - 5px); color:var(--muted); font-size:.73rem;
  font-weight:790; letter-spacing:.05em; text-transform:uppercase; text-decoration:none;
  transition:color .18s ease, background .18s ease }
.track-tab.active { color:var(--accent-ink); background:var(--accent);
  box-shadow:0 0 0 4px var(--accent-soft) }
.manage-link { display:inline-flex; margin-top:18px; color:var(--accent); font-size:.76rem;
  font-weight:780; text-decoration:none }
.manage-links { display:flex; flex-wrap:wrap; gap:8px 18px }
.profile-prompt { display:flex; align-items:center; justify-content:space-between; gap:14px;
  margin:18px 0 0; padding:15px 16px; border:1px solid var(--line); border-radius:var(--radius-md);
  background:var(--surface); box-shadow:var(--card-shadow) }
.profile-prompt div { min-width:0; display:grid; gap:3px }
.profile-prompt strong { font-size:.82rem }
.profile-prompt span { color:var(--muted); font-size:.73rem }
.profile-prompt a { flex:none; color:var(--accent); font-size:.74rem; font-weight:790;
  text-decoration:none }
.stats { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin:24px 0 22px }
.stat { min-width:0; padding:14px 13px 13px; border:1px solid var(--line); border-radius:var(--radius-md);
  background:linear-gradient(145deg, var(--surface-2), var(--surface)); box-shadow:var(--card-shadow) }
.stat-value { display:block; font-size:1.55rem; line-height:1; font-weight:790; letter-spacing:-.05em }
.stat-label { display:block; margin-top:8px; color:var(--muted); font-size:.67rem; line-height:1.25;
  letter-spacing:.045em; text-transform:uppercase }
.stat-new .stat-value { color:var(--accent) }
.stat-seen .stat-value { color:var(--blue) }
.stat-applied .stat-value { color:var(--violet) }
.search-dock { position:sticky; top:calc(env(safe-area-inset-top) + 8px); z-index:20;
  margin:0 -6px 26px; padding:6px; border:1px solid transparent; border-radius:20px;
  transition:background .2s ease, border-color .2s ease, box-shadow .2s ease }
.search-dock.stuck { border-color:var(--line); background:color-mix(in srgb, var(--bg) 84%, transparent);
  box-shadow:var(--shadow); -webkit-backdrop-filter:blur(18px); backdrop-filter:blur(18px) }
.search-box { position:relative; display:flex; align-items:center; min-height:54px;
  border:1px solid var(--line-strong); border-radius:16px; background:var(--surface);
  box-shadow:var(--card-shadow); overflow:hidden; transition:border-color .2s ease, box-shadow .2s ease }
.search-box:focus-within { border-color:color-mix(in srgb, var(--violet) 70%, transparent);
  box-shadow:0 0 0 4px var(--violet-soft), var(--card-shadow) }
.search-icon { width:21px; height:21px; flex:none; margin-left:16px; color:var(--muted) }
#q { width:100%; height:52px; min-width:0; padding:0 8px 0 11px; border:0; outline:0;
  color:var(--fg); background:transparent; font-size:16px; -webkit-appearance:none; appearance:none }
#q::placeholder { color:var(--muted-2); opacity:1 }
#q::-webkit-search-cancel-button { display:none }
.clear-search { display:none; width:44px; height:44px; flex:0 0 44px; margin-right:4px;
  border:0; border-radius:12px; color:var(--muted); background:transparent; cursor:pointer }
.clear-search.visible { display:grid; place-items:center }
.clear-search svg { width:18px; height:18px }
.search-status { min-height:18px; margin:7px 11px 0; color:var(--muted); font-size:.72rem }
.section { margin:0 0 14px; border:1px solid var(--line); border-radius:var(--radius-xl);
  background:color-mix(in srgb, var(--surface) 82%, transparent); box-shadow:var(--card-shadow);
  overflow:hidden }
.section > summary { min-height:72px; display:flex; align-items:center; justify-content:space-between;
  gap:14px; padding:12px 16px; list-style:none; cursor:pointer; user-select:none }
.section > summary::-webkit-details-marker { display:none }
.summary-copy { min-width:0; display:flex; align-items:center; gap:12px }
.section-dot { width:10px; height:10px; flex:0 0 10px; border-radius:50%;
  background:var(--muted-2); box-shadow:0 0 0 5px rgba(145,152,168,.09) }
.section-new .section-dot { background:var(--accent); box-shadow:0 0 0 5px var(--accent-soft) }
.section-priority .section-dot { background:var(--amber); box-shadow:0 0 0 5px var(--amber-soft) }
.section-seen .section-dot { background:var(--blue); box-shadow:0 0 0 5px var(--blue-soft) }
.section-later .section-dot { background:var(--amber); box-shadow:0 0 0 5px var(--amber-soft) }
.section-discarded .section-dot { background:var(--danger); box-shadow:0 0 0 5px var(--danger-soft) }
.section-applied .section-dot { background:var(--violet); box-shadow:0 0 0 5px var(--violet-soft) }
.section-title, .section-subtitle { display:block }
.section-title { overflow:hidden; color:var(--fg); font-size:.94rem; font-weight:740;
  letter-spacing:-.015em; text-overflow:ellipsis; white-space:nowrap }
.section-subtitle { margin-top:2px; color:var(--muted); font-size:.7rem }
.summary-tail { display:flex; align-items:center; gap:11px; flex:none }
.count { min-width:30px; height:30px; padding:0 8px; display:inline-grid; place-items:center;
  border:1px solid var(--line); border-radius:10px; color:var(--muted); background:var(--surface-2);
  font-size:.71rem; font-weight:750; font-variant-numeric:tabular-nums }
.chevron { width:9px; height:9px; border-right:2px solid var(--muted); border-bottom:2px solid var(--muted);
  transform:rotate(45deg) translate(-2px,-2px); transition:transform .25s ease }
.section:not([open]) .chevron { transform:rotate(-45deg) translate(-1px,-1px) }
.card-list { display:grid; gap:8px; padding:0 8px 8px }
.row { position:relative; min-height:88px; padding:15px 14px 14px 17px; border:1px solid var(--line);
  border-radius:var(--radius-lg); background:var(--surface-2); overflow:hidden;
  box-shadow:0 1px 0 rgba(255,255,255,.025) inset; transition:transform .18s ease,
  border-color .18s ease, background .18s ease, opacity .22s ease }
.row::before { content:""; position:absolute; inset:13px auto 13px 0; width:3px; border-radius:0 4px 4px 0;
  background:var(--muted-2) }
.row.row-new::before { background:var(--accent) }
.row.row-seen::before { background:var(--blue) }
.row.row-later::before { background:var(--amber) }
.row.row-discarded::before { background:var(--danger) }
.row.row-applied::before { background:var(--violet) }
.row.row-removing { opacity:0; transform:translateX(18px) }
.row .body { position:relative; z-index:2; min-width:0 }
.card-topline { display:flex; align-items:flex-start; justify-content:space-between; gap:10px }
.card-badges { display:flex; flex:none; align-items:center; gap:7px }
.summary-chevron { width:9px; height:9px; margin:0 4px 4px 0; border-right:2px solid var(--muted);
  border-bottom:2px solid var(--muted); transform:rotate(45deg); transition:transform .2s ease }
.company { min-width:0; overflow-wrap:anywhere; color:var(--fg); font-size:.74rem; line-height:1.35;
  font-weight:810; letter-spacing:.065em; text-transform:uppercase }
.role { max-width:620px; margin-top:5px; overflow-wrap:anywhere; color:var(--fg); font-size:.98rem;
  line-height:1.34; font-weight:590; letter-spacing:-.018em }
.pill { flex:none; min-height:22px; display:inline-flex; align-items:center; padding:2px 8px;
  border:1px solid var(--line); border-radius:999px; font-size:.6rem; line-height:1;
  font-weight:800; letter-spacing:.07em; text-transform:uppercase }
.pill.applied { color:var(--blue); border-color:color-mix(in srgb, var(--blue) 38%, transparent);
  background:var(--blue-soft) }
.pill.follow_up { color:var(--amber); border-color:color-mix(in srgb, var(--amber) 38%, transparent);
  background:var(--amber-soft) }
.pill.interview { color:var(--violet); border-color:color-mix(in srgb, var(--violet) 35%, transparent);
  background:var(--violet-soft) }
.pill.offer { color:var(--accent); border-color:color-mix(in srgb, var(--accent) 38%, transparent);
  background:var(--accent-soft) }
.pill.rejected { color:var(--danger); border-color:color-mix(in srgb, var(--danger) 38%, transparent);
  background:var(--danger-soft) }
.pill.unknown { color:var(--muted); background:var(--surface) }
.pill.fit.high { color:var(--accent); border-color:color-mix(in srgb, var(--accent) 38%, transparent);
  background:var(--accent-soft) }
.pill.fit.medium { color:var(--amber); border-color:color-mix(in srgb, var(--amber) 38%, transparent);
  background:var(--amber-soft) }
.pill.fit.low { color:var(--muted); border-color:var(--line); background:var(--surface) }
.meta { display:flex; align-items:center; flex-wrap:wrap; gap:2px 6px; margin-top:11px;
  color:var(--muted); font-size:.72rem; line-height:1.4; overflow-wrap:anywhere }
.platform { min-height:24px; display:inline-flex; align-items:center; padding:2px 8px; border-radius:999px;
  color:var(--blue); background:var(--blue-soft); font-size:.64rem; font-weight:760; letter-spacing:.015em }
.offer-link { position:relative; z-index:3; width:34px; height:34px; flex:0 0 34px;
  display:grid; place-items:center; border:1px solid var(--line); border-radius:11px;
  color:var(--fg); background:var(--surface); pointer-events:auto; text-decoration:none;
  transition:border-color .15s ease, background .15s ease }
.offer-link svg { width:16px; height:16px }
.note { margin:12px 0 0; padding:11px 12px; border:1px dashed var(--line-strong);
  border-radius:12px; color:var(--muted); background:color-mix(in srgb, var(--surface) 55%, transparent);
  font-size:.75rem; overflow-wrap:anywhere }
.card-reader { position:relative; z-index:3; margin:13px 13px 0; pointer-events:auto }
.reader-tabs { display:flex; gap:5px;
  padding:4px; border:1px solid var(--line); border-radius:12px; background:var(--surface) }
.reader-tab { flex:1 1 0; min-width:0; min-height:36px; padding:0 8px; overflow:hidden; border:0;
  border-radius:8px; color:var(--muted); background:transparent; font-size:.68rem; font-weight:740;
  letter-spacing:.01em; text-overflow:ellipsis; white-space:nowrap; cursor:pointer;
  transition:color .15s ease, background .15s ease, box-shadow .15s ease }
.reader-tab[aria-expanded="true"] { color:var(--fg); background:var(--surface-hover);
  box-shadow:0 1px 4px rgba(0,0,0,.09) }
.reader-tab.letter-ok { color:var(--violet) }
.reader-tab.letter-failed { color:var(--danger) }
.reader-tab.letter-running, .reader-tab.letter-queued { color:var(--violet) }
.reader-tab:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
.summary-panel { position:relative; z-index:2; margin:10px 0 1px; padding:13px 0 12px;
  border-top:1px solid var(--line) }
.summary-panel[hidden] { display:none }
.summary-title { color:var(--accent); font-size:.68rem; font-weight:820; letter-spacing:.09em;
  text-transform:uppercase }
.summary-panel ul { margin:9px 0 0; padding-left:19px; color:var(--muted); font-size:.76rem }
.summary-panel li + li { margin-top:6px }
.summary-provenance { margin:7px 0 0; color:var(--muted); font-size:.67rem; line-height:1.35 }
.summary-provenance.limited { color:var(--amber) }
.content-unavailable { margin:12px 0 0; padding:10px 11px; border:1px solid var(--amber-soft);
  border-radius:10px; color:var(--amber); background:var(--amber-soft); font-size:.7rem; line-height:1.4 }
.summary-fields { display:grid; gap:5px; margin:10px 0 2px }
.summary-field { display:flex; gap:8px; align-items:baseline; font-size:.76rem }
.sf-label { flex:none; min-width:132px; color:var(--muted-2); font-size:.63rem;
  font-weight:800; letter-spacing:.07em; text-transform:uppercase }
.sf-value { color:var(--fg); overflow-wrap:anywhere }
.summary-field.sf-empty .sf-value { color:var(--muted-2); font-style:italic }
@media (max-width:370px) { .sf-label { min-width:104px } }
.content-toggle { position:relative; z-index:3; margin:12px 13px 0; padding:0 14px;
  display:flex; width:calc(100% - 26px); justify-content:center; align-items:center;
  gap:7px; min-height:38px; border:1px solid var(--line); border-radius:11px;
  color:var(--fg); background:var(--surface); font-size:.74rem; font-weight:740;
  letter-spacing:.02em; cursor:pointer; pointer-events:auto;
  transition:border-color .15s ease, background .15s ease, box-shadow .15s ease }
.content-toggle .summary-chevron { margin:0 }
.content-toggle[aria-expanded="true"] { background:var(--surface-hover);
  box-shadow:0 1px 4px rgba(0,0,0,.09) }
.content-toggle[aria-expanded="true"] .summary-chevron { transform:rotate(225deg) }
.content-toggle:focus-visible { outline:3px solid var(--violet); outline-offset:-4px }
@media (hover:hover) {
  .content-toggle:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
.content-panel { position:relative; z-index:2; margin:10px 0 1px; padding:12px 0;
  border-top:1px solid var(--line); color:var(--muted); font-size:.76rem;
  line-height:1.55; overflow-wrap:anywhere }
.content-panel[hidden] { display:none }
.content-panel p { margin:0 0 10px }
.content-panel p:last-child { margin-bottom:0 }
.content-panel .md-heading { margin:14px 0 6px; color:var(--fg); font-weight:800; font-size:.82rem }
.content-panel .md-heading:first-child { margin-top:0 }
.content-panel ul, .content-panel ol { margin:0 0 10px; padding-left:19px }
.content-panel li + li { margin-top:4px }
.content-panel strong { color:var(--fg) }
.content-panel a { color:var(--accent) }
.content-panel hr { margin:16px 0; border:0; border-top:1px solid var(--line) }
.content-panel .md-heading + hr { margin-top:6px }
.card-actions { position:relative; z-index:3; display:flex; flex-wrap:wrap; gap:8px;
  margin:12px 13px 0; pointer-events:auto }
.card-action { min-height:38px; padding:0 14px; display:inline-flex; align-items:center;
  border:1px solid var(--line); border-radius:11px; background:var(--surface); color:var(--fg);
  font-size:.71rem; font-weight:700; letter-spacing:.02em; cursor:pointer;
  transition:border-color .15s ease, background .15s ease }
.action-later { color:var(--amber) }
.action-discard { color:var(--danger) }
.action-apply { color:var(--accent) }
.card-action:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
@media (hover:hover) {
  .card-action:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
.apply-form { position:relative; z-index:3; display:grid; grid-template-columns:minmax(0, 1fr);
  gap:8px; margin:12px 13px 0; pointer-events:auto }
.apply-form[hidden] { display:none }
.apply-input { min-height:44px; padding:0 12px; border:1px solid var(--line-strong);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:16px }
.apply-input::placeholder { color:var(--muted-2); opacity:1 }
.apply-input:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
.apply-submit { justify-self:start }
.doc-field { display:grid; grid-template-columns:minmax(0, 1fr); gap:6px; min-width:0;
  padding:8px; border:1px dashed var(--line-strong);
  border-radius:11px; transition:border-color .15s ease, background .15s ease }
.doc-field.doc-dragover { border-color:var(--accent); background:var(--accent-soft) }
.doc-label { color:var(--muted); font-size:.68rem; font-weight:700; letter-spacing:.03em;
  text-transform:uppercase }
.doc-row { display:flex; gap:8px; min-width:0 }
.doc-select { flex:1; min-width:0 }
.doc-icon-btn { flex:0 0 44px; width:44px; min-height:44px; display:grid; place-items:center;
  padding:0; border:1px solid var(--line); border-radius:11px; color:var(--fg);
  background:var(--surface); cursor:pointer;
  transition:border-color .15s ease, background .15s ease, opacity .15s ease }
.doc-icon-btn svg { width:19px; height:19px }
.doc-icon-btn:disabled { opacity:.4; cursor:default }
.doc-icon-btn:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
@media (hover:hover) {
  .doc-icon-btn:not(:disabled):hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
.doc-label-prompt { display:flex; gap:8px }
.doc-label-prompt[hidden] { display:none }
.action-draft { color:var(--violet) }
.draft-form { position:relative; z-index:3; display:grid; grid-template-columns:minmax(0, 1fr);
  gap:8px; margin:10px 0 0; pointer-events:auto }
.draft-form[hidden] { display:none }
.draft-submit { justify-self:start }
.draft-area { position:relative; z-index:3; display:flex; align-items:center; flex-wrap:wrap;
  gap:9px; pointer-events:auto; color:var(--muted); font-size:.75rem }
.draft-area:not(:empty) { margin-bottom:10px }
.letter-panel { margin:10px 0 1px; padding:12px 0 2px; border-top:1px solid var(--line) }
.letter-panel[hidden] { display:none }
.letter-panel > .action-draft { margin-top:2px }
.action-edit-body { color:var(--violet); margin-bottom:8px }
.action-edit-body[hidden] { display:none }
.body-editor { position:relative; z-index:3; display:grid; gap:8px; margin:0 0 10px }
.body-editor[hidden] { display:none }
.body-editor-textarea { width:100%; min-height:180px; resize:vertical;
  font-family:inherit; line-height:1.5 }
.body-editor-actions { display:flex; gap:8px }
.body-editor-status { margin:0; color:var(--muted); font-size:.75rem }
.body-editor-status.error { color:var(--danger) }
/* avancement des lettres : ancré dans la barre du haut, jamais posé sur le
   contenu - un panneau flottant paraissait perdu à droite sur grand écran et
   mordait la carte dès que la fenêtre rétrécissait */
.batch-badge-wrap { position:relative; display:flex }
.batch-badge-wrap[hidden] { display:none }
.batch-badge { display:flex; align-items:center; gap:7px; height:38px; padding:0 12px 0 9px;
  border:1px solid var(--line-strong); border-radius:999px; background:var(--surface);
  color:var(--fg); font-size:.8rem; font-weight:650; cursor:pointer;
  transition:background .15s ease }
.batch-badge:hover { background:var(--surface-hover) }
.batch-ring { width:19px; height:19px; flex:none; border-radius:50%;
  background:conic-gradient(var(--violet) calc(var(--batch-progress, 0) * 1%),
    var(--line-strong) 0) }
/* l'anneau reste gris tant qu'aucune lettre n'est finie : la pulsation dit
   que le lot tourne, là où un pourcentage à 0 paraîtrait figé */
.batch-ring:not(.batch-ring-done) { animation:batch-pulse 1.7s ease-in-out infinite }
@keyframes batch-pulse { 50% { opacity:.45 } }
.batch-ring::after { content:""; display:block; width:11px; height:11px; margin:4px;
  border-radius:50%; background:var(--surface) }
.batch-badge:hover .batch-ring::after { background:var(--surface-hover) }
.batch-ring-done { background:var(--accent) }
.batch-panel { position:absolute; z-index:30; right:0; top:calc(100% + 9px);
  width:max-content; max-width:min(250px, calc(100vw - 32px)); padding:13px 15px;
  border:1px solid var(--line); border-radius:var(--radius-md);
  background:var(--surface-2); box-shadow:var(--shadow); text-align:left }
.batch-panel[hidden] { display:none }
.batch-panel p { margin:0; font-size:.82rem; line-height:1.4 }
.batch-panel-note { margin-top:3px; color:var(--muted) }
.draft-spinner { width:14px; height:14px; flex:none; border:2px solid var(--line-strong);
  border-top-color:var(--violet); border-radius:50%; animation:draft-spin .8s linear infinite }
@keyframes draft-spin { to { transform:rotate(360deg) } }
.draft-error { margin:0; color:var(--danger); overflow-wrap:anywhere }
.draft-error a, .letter-links a { position:relative; z-index:3; pointer-events:auto }
.draft-warning { margin:0 0 8px; color:var(--amber); overflow-wrap:anywhere }
.letter-page { display:block; width:100%; max-width:560px; margin:0 auto 10px;
  border:1px solid var(--line-strong); border-radius:10px; background:#fff }
.letter-links { margin:4px 0 8px; text-align:center; font-size:.72rem }
.swipe-fab { height:48px; flex:none; display:flex; align-items:center; justify-content:center;
  gap:8px; padding:0 13px; border:1px solid var(--line); border-radius:15px; color:var(--violet);
  background:color-mix(in srgb, var(--surface) 86%, transparent); box-shadow:var(--card-shadow);
  font-weight:760; text-decoration:none;
  transition:transform .2s ease, background .2s ease, border-color .2s ease }
.swipe-fab svg { width:20px; height:20px }
.swipe-fab:active { transform:scale(.94) }
.swipe-fab-count { min-width:20px; height:20px;
  display:grid; place-items:center; padding:0 5px; border-radius:999px;
  color:var(--accent-ink); background:var(--accent); font-size:.62rem; font-weight:820;
  font-variant-numeric:tabular-nums; }
@media (hover:hover) { .swipe-fab:hover { background:var(--surface-hover) } }
.topbar-tools { display:flex; align-items:center; gap:10px }
.logout-button { height:48px; display:flex; align-items:center; justify-content:center; gap:8px;
  padding:0 13px; border:1px solid var(--line); border-radius:15px; color:var(--fg);
  background:color-mix(in srgb, var(--surface) 86%, transparent); box-shadow:var(--card-shadow);
  font-weight:700; cursor:pointer; }
.logout-button svg { width:19px; height:19px; }
.swipe-popup { position:fixed; inset:0; z-index:60; display:flex; align-items:flex-end;
  justify-content:center; padding:16px; background:rgba(0,0,0,.45);
  -webkit-backdrop-filter:blur(6px); backdrop-filter:blur(6px);
  opacity:0; transition:opacity .25s ease }
.swipe-popup[hidden] { display:none }
.swipe-popup.visible { opacity:1 }
.swipe-popup-card { width:min(100%, 420px);
  margin-bottom:max(8px, env(safe-area-inset-bottom));
  padding:26px 22px 20px; border:1px solid var(--line-strong);
  border-radius:var(--radius-xl); background:var(--surface); box-shadow:var(--shadow);
  text-align:center; transform:translateY(24px); transition:transform .28s cubic-bezier(.2,.8,.2,1) }
.swipe-popup.visible .swipe-popup-card { transform:translateY(0) }
.swipe-popup-icon { width:54px; height:54px; margin:0 auto 12px; display:grid;
  place-items:center; border-radius:17px; color:var(--violet); background:var(--violet-soft);
  box-shadow:0 0 0 6px color-mix(in srgb, var(--violet) 6%, transparent) }
.swipe-popup-icon svg { width:26px; height:26px }
.swipe-popup-card h2 { margin:0 0 4px; font-size:1.3rem; font-weight:790; letter-spacing:-.03em }
.swipe-popup-card p { margin:0 0 18px; color:var(--muted); font-size:.86rem }
.swipe-popup-actions { display:grid; gap:8px }
.swipe-popup-go { display:flex; align-items:center; justify-content:center; min-height:50px;
  border-radius:14px; color:var(--accent-ink); background:var(--accent); font-size:.9rem;
  font-weight:790; text-decoration:none; box-shadow:0 0 0 5px var(--accent-soft) }
.swipe-popup-later { min-height:44px; border:0; border-radius:12px; color:var(--muted);
  background:transparent; font-size:.8rem; font-weight:700; cursor:pointer }
.swipe-popup-later:focus-visible, .swipe-popup-go:focus-visible {
  outline:3px solid var(--violet); outline-offset:2px }
@media (min-width:620px) { .swipe-popup { align-items:center } }
.undo-toast { display:flex; align-items:center; justify-content:space-between; gap:12px;
  color:var(--muted); font-size:.78rem }
.undo-toast .undo-btn { flex:none; min-height:38px; padding:0 14px; border:1px solid var(--line);
  border-radius:11px; color:var(--fg); background:var(--surface); font-size:.72rem; font-weight:700;
  cursor:pointer; transition:border-color .15s ease, background .15s ease }
.undo-toast .undo-btn:focus-visible { outline:3px solid var(--violet); outline-offset:2px }
@media (hover:hover) {
  .undo-toast .undo-btn:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
.empty-note { margin:0; padding:14px 13px; border:1px dashed var(--line-strong);
  border-radius:13px; color:var(--muted); background:var(--surface-2);
  font-size:.75rem; overflow-wrap:anywhere }
.no-results { margin:0 0 14px; padding:26px 18px; border:1px dashed var(--line-strong);
  border-radius:var(--radius-lg); color:var(--muted); text-align:center }
.no-results strong { display:block; margin-bottom:3px; color:var(--fg) }
.footer { display:flex; align-items:center; justify-content:center; flex-wrap:wrap; gap:7px;
  margin-top:24px; color:var(--muted-2); font-size:.68rem; text-align:center }
.bug-report-button { display:inline-flex; align-items:center; justify-content:center; min-height:34px;
  padding:0 10px; border:1px solid var(--line); border-radius:10px; color:var(--muted);
  background:var(--surface); font:inherit; font-weight:720; cursor:pointer;
  transition:color .15s ease, background .15s ease, border-color .15s ease }
.bug-report-button:hover { color:var(--fg); background:var(--surface-hover) }
.bug-report-button:focus-visible, .bug-report-close:focus-visible {
  outline:3px solid var(--violet); outline-offset:2px }
.bug-report-overlay { position:fixed; inset:0; z-index:100; display:flex; align-items:flex-end;
  justify-content:center; padding:16px; background:rgba(10,12,16,.5);
  -webkit-backdrop-filter:blur(6px); backdrop-filter:blur(6px) }
.bug-report-overlay[hidden] { display:none }
.bug-report-dialog { position:relative; width:min(100%, 520px); padding:22px;
  border:1px solid var(--line-strong); border-radius:var(--radius-xl); color:var(--fg);
  background:var(--surface); box-shadow:var(--shadow) }
.bug-report-dialog h2 { margin:4px 34px 7px 0; font-size:1.35rem; letter-spacing:-.025em }
.bug-report-close { position:absolute; top:12px; right:12px; width:38px; height:38px;
  border:0; border-radius:10px; color:var(--muted); background:transparent;
  font-size:1.45rem; line-height:1; cursor:pointer }
.bug-report-close:hover { color:var(--fg); background:var(--surface-hover) }
.bug-report-intro { margin:0 0 16px; color:var(--muted); font-size:.78rem; line-height:1.5 }
#bug-report-form { display:grid; grid-template-columns:minmax(0, 1fr) }
.bug-report-message { width:100%; min-height:130px; margin-top:7px; resize:vertical;
  line-height:1.45 }
.bug-report-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:12px }
.bug-report-submit { color:var(--blue) }
.bug-report-submit:disabled { opacity:.5; cursor:default }
.bug-report-status { min-height:18px; margin:10px 0 0; color:var(--muted);
  font-size:.72rem; text-align:right }
.bug-report-status.is-error { color:var(--danger) }
.bug-report-status.is-success { color:var(--accent) }
body.modal-open { overflow:hidden }
@media (min-width:620px) { .bug-report-overlay { align-items:center } }
a { color:var(--blue) }
@media (hover:hover) {
  .theme-toggle:hover, .clear-search:hover { background:var(--surface-hover) }
  .row:hover { transform:translateY(-1px); border-color:var(--line-strong); background:var(--surface-hover) }
  .offer-link:hover { border-color:var(--line-strong); background:var(--surface-hover) }
}
@media (min-width:620px) {
  .shell { padding-left:24px; padding-right:24px }
  .topbar { margin-bottom:42px }
  .stats { gap:12px }
  .stat { padding:18px }
  .section > summary { padding-left:20px; padding-right:20px }
  .card-list { padding:0 10px 10px; gap:10px }
  .row { padding:18px 18px 17px 21px }
}
@media (max-width:619px) {
  .topbar { flex-wrap:wrap; }
  .topbar-tools { width:100%; }
  .swipe-fab { flex:1; }
  .logout-button { margin-left:auto; }
  .profile-prompt { align-items:flex-start; flex-direction:column }
}
@media (max-width:370px) {
  .stat { padding:12px 9px }
  .stat-value { font-size:1.35rem }
  .stat-label { font-size:.59rem }
  .section-subtitle { display:none }
}
@media (prefers-reduced-motion:reduce) {
  *, *::before, *::after { scroll-behavior:auto !important; animation:none !important; transition:none !important }
}
"""


_BATCH_BADGE_JS = """\
(function () {
  const wrap = document.getElementById('batch-badge-wrap');
  if (!wrap) return;
  const badge = document.getElementById('batch-badge');
  const ring = document.getElementById('batch-ring');
  const count = document.getElementById('batch-badge-count');
  const panel = document.getElementById('batch-panel');
  const line1 = document.getElementById('batch-panel-line1');
  const line2 = document.getElementById('batch-panel-line2');
  const track = document.body.dataset.track || 'engineer';
  let timer = null;

  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  const poll = () => fetch(`/draft/batch/status?track=${track}`)
    .then(resp => resp.ok ? resp.json() : null)
    .then(payload => {
      if (!payload) return;
      const active = payload.queued + payload.running;
      const done = payload.ok + payload.failed;
      const failed = payload.failed ? ` · ${payload.failed} échec(s)` : '';
      if (active > 0) {
        wrap.hidden = false;
        ring.classList.remove('batch-ring-done');
        ring.style.setProperty('--batch-progress', Math.round(100 * done / (done + active)));
        count.textContent = String(active);
        line1.textContent = `${active} lettre(s) en cours`;
        line2.textContent = `${payload.ok} prête(s)${failed}`;
        if (!timer) timer = setInterval(poll, 3000);
        return;
      }
      stop();
      if (wrap.hidden) return;          // rien n'a tourné pendant cette visite
      ring.classList.add('batch-ring-done');
      ring.style.setProperty('--batch-progress', 100);
      count.textContent = String(payload.ok);
      line1.textContent = `Terminé · ${payload.ok} lettre(s) prête(s)`;
      line2.textContent = `à joindre depuis les cartes${failed}`;
    })
    .catch(() => {});

  badge.addEventListener('click', () => {
    const open = badge.getAttribute('aria-expanded') === 'true';
    badge.setAttribute('aria-expanded', open ? 'false' : 'true');
    panel.hidden = open;
  });
  document.addEventListener('click', event => {
    if (panel.hidden || wrap.contains(event.target)) return;
    badge.setAttribute('aria-expanded', 'false');
    panel.hidden = true;
  });

  window.jwBatchBadge = {
    poll,
    start: () => {
      wrap.hidden = false;
      ring.classList.remove('batch-ring-done');
      ring.style.setProperty('--batch-progress', 0);
      count.textContent = '…';
      line1.textContent = 'Génération lancée…';
      line2.textContent = '';
      if (!timer) timer = setInterval(poll, 3000);
      poll();
    },
  };
  poll();
})();
"""


_JS = """\
(function () {
  const root = document.documentElement;
  const themeToggle = document.getElementById('theme-toggle');
  const themeColor = document.getElementById('theme-color');
  const syncThemeUI = () => {
    const isLight = root.dataset.theme === 'light';
    themeToggle.setAttribute('aria-label', isLight ? 'Passer au thème sombre' : 'Passer au thème clair');
    themeToggle.setAttribute('aria-pressed', isLight ? 'true' : 'false');
    themeColor.setAttribute('content', isLight ? '#f3f1eb' : '#090b10');
  };
  syncThemeUI();
  themeToggle.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('jw-theme', root.dataset.theme); } catch (_) {}
    syncThemeUI();
  });

  const q = document.getElementById('q');
  const clearSearch = document.getElementById('clear-search');
  const searchStatus = document.getElementById('search-status');
  const noResults = document.getElementById('no-results');
  const searchDock = document.getElementById('search-dock');
  const details = [...document.querySelectorAll('.section')];
  const rows = [...document.querySelectorAll('.row')];
  // Résumé, annonce et lettre partagent un lecteur : une seule vue reste ouverte.
  document.addEventListener('click', event => {
    const button = event.target.closest('.reader-tab');
    if (!button) return;
    const reader = button.closest('.card-reader');
    const expanded = button.getAttribute('aria-expanded') === 'true';
    reader.querySelectorAll('.reader-tab').forEach(other => {
      const panel = document.getElementById(other.getAttribute('aria-controls'));
      const keepOpen = other === button && !expanded;
      other.setAttribute('aria-expanded', keepOpen ? 'true' : 'false');
      if (panel) panel.hidden = !keepOpen;
    });
  });
  const readSession = (key, fallback) => {
    try { return sessionStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
  };
  const writeSession = (key, value) => {
    try { sessionStorage.setItem(key, value); } catch (_) {}
  };
  const normalize = value => value.toLocaleLowerCase('fr').normalize('NFD')
    .replace(/[\\u0300-\\u036f]/g, '');
  let saved = {};
  try { saved = JSON.parse(readSession('jw-open', '{}')) || {}; } catch (_) {}
  q.value = readSession('jw-q', '');
  details.forEach((d, i) => {
    const key = d.dataset.section;
    const stored = saved[key] !== undefined ? saved[key] : saved[i];
    d.dataset.open = stored !== undefined ? String(stored)
      : d.dataset.default === '1' ? '1' : '0';
    d.open = d.dataset.open === '1';
    d.addEventListener('toggle', () => {
      if (q.value.trim()) return;
      d.dataset.open = d.open ? '1' : '0';
      saved[key] = d.dataset.open;
      writeSession('jw-open', JSON.stringify(saved));
    });
  });
  const apply = () => {
    const rawNeedle = q.value.trim();
    const needle = normalize(rawNeedle);
    let shownTotal = 0;
    rows.forEach(r => {
      // data-search est rendu côté serveur et déjà normalisé : il ne contient
      // que société, poste, lieu, plateforme et recherche. Lire textContent
      // ferait entrer les <option> des menus de documents dans le filtre.
      const visible = !needle || (r.dataset.search || '').includes(needle);
      r.hidden = !visible;
      if (visible) shownTotal += 1;
    });
    details.forEach(d => {
      const sectionRows = [...d.querySelectorAll('.row')];
      const shown = sectionRows.filter(r => !r.hidden).length;
      d.open = needle ? shown > 0 : d.dataset.open === '1';
      const c = d.querySelector('.count');
      if (c) c.textContent = needle ? `${shown}/${sectionRows.length}` : `${sectionRows.length}`;
    });
    clearSearch.classList.toggle('visible', Boolean(rawNeedle));
    noResults.hidden = !needle || shownTotal > 0;
    searchStatus.textContent = needle
      ? `${shownTotal} sur ${rows.length} offre${shownTotal === 1 ? '' : 's'}`
      : `${rows.length} offres`;
  };
  q.addEventListener('input', () => {
    writeSession('jw-q', q.value);
    apply();
  });
  q.addEventListener('keydown', e => {
    if (e.key === 'Escape' && q.value) {
      e.preventDefault(); q.value = ''; writeSession('jw-q', ''); apply();
    }
  });
  clearSearch.addEventListener('click', () => {
    q.value = ''; writeSession('jw-q', ''); apply(); q.focus();
  });
  const observeDock = () => searchDock.classList.toggle('stuck', searchDock.getBoundingClientRect().top
    <= parseFloat(getComputedStyle(searchDock).top) + 1);
  document.addEventListener('scroll', observeDock, {passive:true});
  observeDock();
  apply();

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const UNDO_WINDOW_MS = 7000;
  const REMOVE_MS = 260;
  const updateSectionCount = section => {
    if (!section) return;
    const count = section.querySelector('.count');
    if (count && !q.value.trim()) count.textContent = String(section.querySelectorAll('.row').length);
  };
  // Minuterie plutôt que transitionend : quand le système saute la transition
  // (iOS en économie d'énergie) sans exposer prefers-reduced-motion,
  // transitionend ne se déclenche jamais et la carte resterait figée.
  const removeRow = (row, done) => {
    if (reduceMotion) { done(); return; }
    row.classList.add('row-removing');
    setTimeout(done, REMOVE_MS);
  };
  const showToast = (row, message, onUndo) => {
    const toast = document.createElement('article');
    toast.className = 'row undo-toast';
    const label = document.createElement('span');
    label.textContent = message;
    toast.append(label);
    let undoBtn = null;
    if (onUndo) {
      undoBtn = document.createElement('button');
      undoBtn.type = 'button';
      undoBtn.className = 'undo-btn';
      undoBtn.textContent = 'Annuler';
      toast.append(undoBtn);
    }
    row.replaceWith(toast);
    rows.splice(rows.indexOf(row), 1, toast);
    updateSectionCount(toast.closest('.section'));
    const timer = setTimeout(() => {
      const section = toast.closest('.section');
      toast.remove();
      rows.splice(rows.indexOf(toast), 1);
      updateSectionCount(section);
    }, UNDO_WINDOW_MS);
    if (undoBtn) undoBtn.addEventListener('click', () => {
      clearTimeout(timer);
      onUndo();
    });
  };
  const showUndo = (row, matchId, prevState) => {
    showToast(row, 'Retirée du tableau de bord.', () => {
      fetch(`/match/${matchId}/restore`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({state: prevState}),
      }).then(resp => {
        if (resp.ok) location.reload();
      });
    });
  };
  const actionButtons = [...document.querySelectorAll('.action-later, .action-discard')];
  actionButtons.forEach(button => {
    button.addEventListener('click', () => {
      const row = button.closest('.row');
      const matchId = button.dataset.matchId;
      const action = button.dataset.action;
      const prevState = button.dataset.prevState;
      fetch(`/match/${matchId}/${action}`, {method: 'POST'}).then(resp => {
        if (!resp.ok) return;
        removeRow(row, () => showUndo(row, matchId, prevState));
      });
    });
  });
  // Un seul formulaire d'action reste ouvert à la fois dans une carte.
  [...document.querySelectorAll('.action-apply, .action-draft')].forEach(button => {
    button.addEventListener('click', () => {
      const expanded = button.getAttribute('aria-expanded') === 'true';
      const form = document.getElementById(button.getAttribute('aria-controls'));
      if (!expanded) {
        [...button.closest('.row').querySelectorAll('.action-apply, .action-draft')]
          .forEach(other => {
            if (other === button) return;
            other.setAttribute('aria-expanded', 'false');
            const otherForm = document.getElementById(other.getAttribute('aria-controls'));
            if (otherForm) otherForm.hidden = true;
          });
      }
      button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      if (form) form.hidden = expanded;
    });
  });
  [...document.querySelectorAll('.apply-form')].forEach(form => {
    form.addEventListener('submit', event => {
      event.preventDefault();
      const row = form.closest('.row');
      const matchId = form.dataset.matchId;
      const toId = value => value ? Number(value) : null;
      fetch(`/match/${matchId}/apply`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          cv_library_id: toId(form.elements.cv_library_id.value),
          cover_letter_library_id: toId(form.elements.cover_letter_library_id.value),
        }),
      }).then(resp => {
        if (!resp.ok) return;
        removeRow(row, () => showToast(row, 'Candidature enregistrée.'));
      });
    });
  });

  const RUNNING_HTML = '<span class="draft-spinner" aria-hidden="true"></span>'
    + '<span>Génération de la lettre en cours…</span>';
  const DRAFT_POLL_MS = 3000;
  const draftPolls = new Map();
  const setDraftArea = (matchId, html, status) => {
    const area = document.getElementById(`draft-area-${matchId}`);
    if (!area) return;
    area.innerHTML = html;
    area.dataset.status = status;
    const tab = document.querySelector(`.letter-toggle[aria-controls="letter-panel-${matchId}"]`);
    if (tab) {
      tab.classList.remove('letter-empty', 'letter-queued', 'letter-running', 'letter-failed', 'letter-ok');
      tab.classList.add(`letter-${status || 'empty'}`);
      const label = tab.querySelector('.reader-tab-label');
      if (label) label.textContent = status === 'ok' ? 'Lettre · prête'
        : status === 'failed' ? 'Lettre · échec'
        : status === 'queued' || status === 'running' ? 'Lettre · en cours' : 'Lettre';
    }
    const compose = document.querySelector(`.action-draft[aria-controls="draft-form-${matchId}"]`);
    if (compose) {
      compose.hidden = status === 'queued' || status === 'running';
      compose.textContent = status === 'ok' ? 'Régénérer la lettre'
        : status === 'failed' ? 'Réessayer' : 'Générer la lettre';
    }
    const editBtn = document.querySelector(`.action-edit-body[data-match-id="${matchId}"]`);
    if (editBtn) editBtn.hidden = status !== 'ok';
  };
  const registerCoverLetter = (matchId, libraryId, label) => {
    const value = String(libraryId);
    [...document.querySelectorAll('select[data-doc-type="cover_letter"]')].forEach(select => {
      let option = [...select.options].find(o => o.value === value);
      if (!option) {
        option = document.createElement('option');
        option.value = value;
        select.append(option);
      }
      option.textContent = label;
    });
    const own = document.querySelector(`#apply-form-${matchId} select[data-doc-type="cover_letter"]`);
    if (own) {
      own.value = value;
      own.dispatchEvent(new Event('change'));
    }
  };
  const pollDraft = matchId => {
    if (draftPolls.has(matchId)) return;
    const timer = setInterval(() => {
      fetch(`/match/${matchId}/draft/status`)
        .then(resp => resp.ok ? resp.json() : null)
        .then(payload => {
          if (!payload) return;
          if (payload.status !== 'running' && payload.status !== 'queued') {
            clearInterval(timer);
            draftPolls.delete(matchId);
          }
          setDraftArea(matchId, payload.html, payload.status);
          if (payload.status === 'ok' && payload.library_id) {
            registerCoverLetter(matchId, payload.library_id, payload.library_label);
          }
        })
        .catch(() => {});
    }, DRAFT_POLL_MS);
    draftPolls.set(matchId, timer);
  };
  [...document.querySelectorAll('.draft-area[data-status="running"], .draft-area[data-status="queued"]')]
    .forEach(area => pollDraft(area.dataset.matchId));
  [...document.querySelectorAll('.draft-form')].forEach(form => {
    const select = form.elements.cv_library_id;
    const track = form.dataset.track;
    if (select) {
      let savedCv = null;
      try { savedCv = localStorage.getItem(`jw-cv-${track}`); } catch (_) {}
      if (savedCv && [...select.options].some(o => o.value === savedCv)) select.value = savedCv;
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (!select) return;
      const matchId = form.dataset.matchId;
      try { localStorage.setItem(`jw-cv-${track}`, select.value); } catch (_) {}
      fetch(`/match/${matchId}/draft`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          cv_library_id: Number(select.value),
          instruction: form.elements.instruction.value.trim(),
          track,
        }),
      }).then(resp => {
        if (resp.ok) {
          form.hidden = true;
          const btn = document.querySelector(`[aria-controls="draft-form-${matchId}"]`);
          if (btn) btn.setAttribute('aria-expanded', 'false');
          setDraftArea(matchId, RUNNING_HTML, 'running');
          pollDraft(matchId);
          return null;
        }
        return resp.json().catch(() => null);
      }).then(payload => {
        if (payload && payload.error) {
          const area = document.getElementById(`draft-area-${matchId}`);
          if (!area) return;
          area.dataset.status = 'failed';
          area.textContent = '';
          const p = document.createElement('p');
          p.className = 'draft-error';
          p.textContent = payload.error;
          area.append(p);
        }
      }).catch(() => {});
    });
  });

  [...document.querySelectorAll('.action-edit-body')].forEach(button => {
    button.addEventListener('click', () => {
      const matchId = button.dataset.matchId;
      const editor = document.getElementById(`body-editor-${matchId}`);
      if (!editor) return;
      const textarea = editor.querySelector('.body-editor-textarea');
      const status = editor.querySelector('.body-editor-status');
      status.textContent = ''; status.classList.remove('error');
      editor.hidden = false; button.hidden = true;
      textarea.disabled = true; textarea.value = 'Chargement…';
      fetch(`/match/${matchId}/letter/body`)
        .then(resp => resp.json().then(data => ({ok: resp.ok, data})))
        .then(({ok, data}) => {
          textarea.disabled = false;
          if (!ok) {
            textarea.value = '';
            status.textContent = data.error || 'Erreur';
            status.classList.add('error');
            return;
          }
          textarea.value = data.body;
        })
        .catch(() => {
          textarea.disabled = false; textarea.value = '';
          status.textContent = 'Erreur réseau';
          status.classList.add('error');
        });
    });
  });
  const closeBodyEditor = editor => {
    editor.hidden = true;
    const matchId = editor.id.replace('body-editor-', '');
    const openBtn = document.querySelector(`.action-edit-body[data-match-id="${matchId}"]`);
    if (openBtn) openBtn.hidden = false;
  };
  [...document.querySelectorAll('.body-editor-cancel')].forEach(button => {
    button.addEventListener('click', () => closeBodyEditor(button.closest('.body-editor')));
  });
  [...document.querySelectorAll('.body-editor-save')].forEach(button => {
    button.addEventListener('click', () => {
      const editor = button.closest('.body-editor');
      const matchId = editor.id.replace('body-editor-', '');
      const textarea = editor.querySelector('.body-editor-textarea');
      const status = editor.querySelector('.body-editor-status');
      button.disabled = true;
      status.textContent = 'Enregistrement…'; status.classList.remove('error');
      fetch(`/match/${matchId}/letter/body`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({body: textarea.value}),
      }).then(resp => resp.json().then(data => ({ok: resp.ok, data})))
        .then(({ok, data}) => {
          button.disabled = false;
          if (!ok) {
            status.textContent = data.error || 'Échec de la compilation.';
            status.classList.add('error');
            return;
          }
          setDraftArea(matchId, data.html, data.status);
          closeBodyEditor(editor);
          if (data.library_id) registerCoverLetter(matchId, data.library_id, data.library_label);
        })
        .catch(() => {
          button.disabled = false;
          status.textContent = 'Erreur réseau';
          status.classList.add('error');
        });
    });
  });

  const swipePopup = document.getElementById('swipe-popup');
  if (swipePopup) {
    const popupKey = `jw-swipe-popup-${swipePopup.dataset.track}`;
    const dismiss = () => {
      swipePopup.classList.remove('visible');
      setTimeout(() => { swipePopup.hidden = true; }, 260);
      writeSession(popupKey, '1');
    };
    if (!readSession(popupKey, '')) {
      swipePopup.hidden = false;
      requestAnimationFrame(() => swipePopup.classList.add('visible'));
    }
    swipePopup.querySelector('.swipe-popup-later').addEventListener('click', dismiss);
    swipePopup.addEventListener('click', e => { if (e.target === swipePopup) dismiss(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !swipePopup.hidden) dismiss();
    });
  }

  [...document.querySelectorAll('.doc-preview-btn')].forEach(btn => {
    const select = btn.closest('.doc-row').querySelector('select');
    const sync = () => { btn.disabled = !select.value; };
    select.addEventListener('change', sync);
    sync();
    btn.addEventListener('click', () => {
      if (select.value) window.open(`/documents/${select.value}`, '_blank', 'noopener');
    });
  });

  const readAsBase64 = file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  const pendingUploads = new WeakMap();
  const startUpload = (field, file) => {
    pendingUploads.set(field, file);
    const prompt = field.querySelector('.doc-label-prompt');
    const labelInput = field.querySelector('.doc-label-input');
    labelInput.value = '';
    prompt.hidden = false;
    labelInput.focus();
  };
  const finishUpload = async field => {
    const file = pendingUploads.get(field);
    if (!file) return;
    const docType = field.dataset.docType;
    const labelInput = field.querySelector('.doc-label-input');
    const select = field.querySelector('.doc-select');
    const contentBase64 = await readAsBase64(file);
    const resp = await fetch('/documents', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        filename: file.name,
        type: docType,
        label: labelInput.value.trim(),
        content_base64: contentBase64,
      }),
    });
    if (!resp.ok) return;
    const entry = await resp.json();
    const option = document.createElement('option');
    option.value = String(entry.id);
    option.textContent = entry.label;
    select.append(option);
    select.value = String(entry.id);
    select.dispatchEvent(new Event('change'));
    pendingUploads.delete(field);
    field.querySelector('.doc-label-prompt').hidden = true;
  };
  [...document.querySelectorAll('.doc-field')].forEach(field => {
    const fileInput = field.querySelector('.doc-file-input');
    field.querySelector('.doc-upload-btn').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) startUpload(field, fileInput.files[0]);
      fileInput.value = '';
    });
    field.querySelector('.doc-label-confirm').addEventListener('click', () => finishUpload(field));
    field.querySelector('.doc-label-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); finishUpload(field); }
    });
    field.addEventListener('dragover', e => { e.preventDefault(); field.classList.add('doc-dragover'); });
    field.addEventListener('dragleave', () => field.classList.remove('doc-dragover'));
    field.addEventListener('drop', e => {
      e.preventDefault();
      field.classList.remove('doc-dragover');
      const file = e.dataTransfer.files[0];
      if (file) startUpload(field, file);
    });
  });
})();
"""


_TRACK_TABS = (
    ("engineer", "/", "Ingénieur IA"),
    ("project", "/po", "Chef de projet / PO"),
)


def _track_nav(track: str) -> str:
    if track == "all":
        return ""
    links = []
    for key, href, label in _TRACK_TABS:
        current = ' aria-current="page"' if key == track else ""
        links.append(
            f'<a class="track-tab{" active" if key == track else ""}" '
            f'href="{href}"{current}>{html.escape(label)}</a>'
        )
    return f'<nav class="track-tabs" aria-label="Piste métier">{"".join(links)}</nav>'


def _page_template(
    *, body, total, new_count, seen_count, applied_count, stamp, track,
    category_link="", profile_link="", profile_prompt="", swipe_fab="", swipe_popup="",
    batch_badge="", csrf_token="", identity_sub="",
) -> str:
    logout_button = (
        '<button class="logout-button" type="button" aria-label="Déconnexion" '
        'onclick="fetch(\'/logout\',{method:\'POST\'}).then(()=>location.href=\'/login\')">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"/>'
        '<path d="m15 8 4 4-4 4M9 12h10"/></svg><span>Déconnexion</span></button>'
        if csrf_token
        else ""
    )
    identity_copy = html.escape(identity_sub) if identity_sub else "Suivi de vos offres"
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb" id="theme-color">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>jobwatch · tableau de bord</title>
{_csrf_head(csrf_token)}
<script>
(function () {{
  try {{
    const saved = localStorage.getItem('jw-theme');
    document.documentElement.dataset.theme = saved === 'dark' ? 'dark' : 'light';
  }} catch (_) {{
    document.documentElement.dataset.theme = 'light';
  }}
}})();
</script>
<style>
{_CSS}</style></head><body data-track="{track}">
<div class="ambient" aria-hidden="true"></div>
<div class="shell">
  <header>
    <div class="topbar">
      <div class="identity">
        <div class="monogram" aria-hidden="true">JW</div>
        <div class="identity-copy"><span class="identity-name">jobwatch</span>
          <span class="identity-sub">{identity_copy}</span></div>
      </div>
      <div class="topbar-tools">
      {swipe_fab}
      {batch_badge}
      <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Passer au thème clair">
        <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="3.7"/><path d="M12 2v2.1M12 19.9V22M4.93 4.93l1.49 1.49M17.58 17.58l1.49 1.49M2 12h2.1M19.9 12H22M4.93 19.07l1.49-1.49M17.58 6.42l1.49-1.49"/>
        </svg>
        <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M20.2 15.1A8.4 8.4 0 0 1 8.9 3.8 8.5 8.5 0 1 0 20.2 15.1Z"/>
        </svg>
      </button>
      {logout_button}
      </div>
    </div>
    <div class="hero">
      <p class="eyebrow">Tableau de bord</p>
      <h1>Vos offres,<br><span>sous contrôle.</span></h1>
      <p class="hero-meta"><span class="live-dot" aria-hidden="true"></span>
        Mis à jour le {stamp}</p>
    </div>
    {_track_nav(track)}
    <div class="manage-links">{category_link}{profile_link}</div>
    {profile_prompt}
    <div class="stats" aria-label="Vue d'ensemble">
      <div class="stat stat-new"><span class="stat-value">{new_count}</span><span class="stat-label">Nouveaux matchs</span></div>
      <div class="stat stat-seen"><span class="stat-value">{seen_count}</span><span class="stat-label">Vus</span></div>
      <div class="stat stat-applied"><span class="stat-value">{applied_count}</span><span class="stat-label">Candidatures</span></div>
    </div>
  </header>
  <div class="search-dock" id="search-dock">
    <div class="search-box">
      <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>
      </svg>
      <input id="q" type="search" aria-label="Filtrer les offres"
        placeholder="Entreprise, poste, lieu, recherche…" autocomplete="off" enterkeyhint="search">
      <button class="clear-search" id="clear-search" type="button" aria-label="Effacer la recherche">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <circle cx="12" cy="12" r="8"/><path d="m9 9 6 6M15 9l-6 6"/>
        </svg>
      </button>
    </div>
    <div class="search-status" id="search-status" aria-live="polite">{total} offres</div>
  </div>
  <main>
{body}
    <div class="no-results" id="no-results" hidden><strong>Aucune offre trouvée</strong>
      Essayez un autre mot-clé.</div>
  </main>
  <footer class="footer"><span>Données locales · base SQLite jobwatch</span>
    <button class="bug-report-button" type="button" data-bug-report-open>Signaler un bug</button>
  </footer>
</div>
{swipe_popup}
{_BUG_REPORT_DIALOG}
<script>
{_JS}
{_BUG_REPORT_JS}
{_BATCH_BADGE_JS}</script></body></html>
"""


_SWIPE_CSS = """\
.swipe-shell { position:relative; z-index:1; display:flex; flex-direction:column;
  width:min(100%, 560px); min-height:100vh; min-height:100dvh; margin:0 auto;
  padding:max(14px, env(safe-area-inset-top)) calc(14px + env(safe-area-inset-right))
  calc(14px + env(safe-area-inset-bottom)) calc(14px + env(safe-area-inset-left)) }
.swipe-top { display:flex; align-items:center; justify-content:space-between; gap:10px;
  margin-bottom:12px }
.swipe-back { display:inline-flex; align-items:center; min-height:44px; padding:0 13px;
  border:1px solid var(--line); border-radius:11px; color:var(--fg); background:var(--surface);
  font-size:.76rem; font-weight:700; text-decoration:none }
.swipe-count { color:var(--muted); font-size:.8rem; font-weight:700;
  font-variant-numeric:tabular-nums }
.swipe-stage { position:relative; flex:1; min-height:340px }
.swipe-stage[hidden] { display:none }
.swipe-card { position:absolute; inset:0; display:none; flex-direction:column;
  padding:18px 16px 14px; border:1px solid var(--line-strong); border-radius:var(--radius-xl);
  background:var(--surface); box-shadow:var(--shadow); overflow:hidden; touch-action:pan-y }
.swipe-card.top { display:flex; z-index:2 }
.swipe-card.next { display:flex; z-index:1; transform:scale(.955) translateY(10px);
  opacity:.55; pointer-events:none }
.swipe-card.leaving { display:flex; z-index:3; pointer-events:none;
  transition:transform .28s ease, opacity .28s ease }
.swipe-card-scroll { flex:1; min-height:0; overflow-y:auto; -webkit-overflow-scrolling:touch;
  scrollbar-width:none }
.swipe-card-scroll::-webkit-scrollbar { display:none }
.swipe-card .content-panel { margin:10px 0 0; padding:12px 0 0; pointer-events:auto }
.swipe-card .content-toggle { margin:12px 0 0; width:100% }
.swipe-summary { margin:14px 0 0; padding:12px 12px 11px; border:1px dashed var(--line-strong);
  border-radius:12px }
.swipe-summary ul { margin:8px 0 0; padding-left:18px; color:var(--muted); font-size:.8rem }
.swipe-summary li + li { margin-top:5px }
.swipe-stamp { position:absolute; top:16px; padding:6px 12px; border:2px solid;
  border-radius:10px; font-size:.78rem; font-weight:850; letter-spacing:.06em;
  text-transform:uppercase; opacity:0; pointer-events:none; background:var(--surface) }
.stamp-right { left:12px; color:var(--accent); border-color:var(--accent);
  transform:rotate(-12deg) }
.stamp-left { right:12px; color:var(--danger); border-color:var(--danger);
  transform:rotate(12deg) }
.swipe-controls { display:flex; align-items:center; justify-content:center; gap:16px;
  padding:16px 0 4px }
.swipe-controls[hidden] { display:none }
.swipe-btn { width:64px; height:64px; display:grid; place-items:center; padding:0;
  border:1px solid var(--line-strong); border-radius:50%; background:var(--surface);
  box-shadow:var(--card-shadow); cursor:pointer;
  transition:transform .15s ease, background .15s ease, opacity .15s ease }
.swipe-btn svg { width:26px; height:26px }
.swipe-btn:active { transform:scale(.9) }
.swipe-btn:disabled { opacity:.35; cursor:default }
.swipe-btn:focus-visible { outline:3px solid var(--violet); outline-offset:3px }
.swipe-btn-no { color:var(--danger) }
.swipe-btn-yes { color:var(--accent) }
.swipe-btn-undo { width:50px; height:50px; color:var(--muted) }
.swipe-btn-undo svg { width:20px; height:20px }
@media (hover:hover) { .swipe-btn:not(:disabled):hover { background:var(--surface-hover) } }
.swipe-done { padding:22px 18px; border:1px solid var(--line); border-radius:var(--radius-xl);
  background:var(--surface); box-shadow:var(--card-shadow) }
.swipe-done[hidden] { display:none }
.swipe-done h2 { margin:0 0 6px; font-size:1.35rem; font-weight:790; letter-spacing:-.03em }
.done-stats { margin:0 0 16px; color:var(--muted); font-size:.85rem }
.batch-form { display:grid; grid-template-columns:minmax(0, 1fr); gap:10px }
.batch-btn { justify-self:start; gap:5px; color:var(--violet) }
.batch-btn:disabled { opacity:.45; cursor:default }
.done-back { display:inline-flex; margin-top:18px; text-decoration:none }
.swipe-support { display:flex; justify-content:center; padding-top:8px }
"""


_SWIPE_JS = """\
(function () {
  const stage = document.getElementById('swipe-stage');
  const cards = [...document.querySelectorAll('.swipe-card')];
  const countEl = document.getElementById('swipe-count');
  const controls = document.getElementById('swipe-controls');
  const done = document.getElementById('swipe-done');
  const undoBtn = document.getElementById('swipe-undo');
  const track = document.body.dataset.track;
  const backHref = document.body.dataset.backHref || '/';
  const pendingInitial = Number(document.body.dataset.pending || '0');
  const total = cards.length;
  let index = 0;
  const history = [];
  const session = {right: 0, left: 0};

  const showDone = () => {
    stage.hidden = true;
    controls.hidden = true;
    done.hidden = false;
    document.getElementById('done-right').textContent = String(session.right);
    document.getElementById('done-left').textContent = String(session.left);
    const batchBtn = document.getElementById('batch-btn');
    if (batchBtn && !batchBtn.dataset.started) {
      const pending = pendingInitial + session.right;
      document.getElementById('batch-count').textContent = String(pending);
      batchBtn.disabled = pending === 0;
    }
  };
  const hideDone = () => {
    stage.hidden = false;
    controls.hidden = false;
    done.hidden = true;
  };
  const render = () => {
    cards.forEach((card, i) => {
      card.classList.toggle('top', i === index);
      card.classList.toggle('next', i === index + 1);
    });
    countEl.textContent = `${Math.min(index + 1, total)} / ${total}`;
    undoBtn.disabled = history.length === 0;
    if (index >= total) showDone();
  };

  const act = dir => {
    if (index >= total) return;
    const card = cards[index];
    const action = dir === 'right' ? 'later' : 'discard';
    fetch(`/match/${card.dataset.matchId}/${action}`, {method: 'POST'}).then(resp => {
      if (!resp.ok) location.reload();
    });
    history.push({index, dir});
    session[dir] += 1;
    index += 1;
    card.classList.remove('top');
    card.classList.add('leaving');
    const sign = dir === 'right' ? 1 : -1;
    requestAnimationFrame(() => {
      card.style.transform = `translateX(${sign * 130}%) rotate(${sign * 14}deg)`;
      card.style.opacity = '0';
    });
    setTimeout(() => {
      card.classList.remove('leaving');
      card.style.transform = '';
      card.style.opacity = '';
    }, 300);
    render();
  };

  const undo = () => {
    const last = history.pop();
    if (!last) return;
    if (index >= total) hideDone();
    const card = cards[last.index];
    session[last.dir] -= 1;
    index = last.index;
    fetch(`/match/${card.dataset.matchId}/restore`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({state: 'new'}),
    }).then(resp => {
      if (!resp.ok) location.reload();
    });
    render();
  };

  document.getElementById('swipe-no').addEventListener('click', () => act('left'));
  document.getElementById('swipe-yes').addEventListener('click', () => act('right'));
  undoBtn.addEventListener('click', undo);
  document.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight') { e.preventDefault(); act('right'); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); act('left'); }
    else if (e.key === 'ArrowUp' || e.key === 'u') { e.preventDefault(); undo(); }
  });

  [...document.querySelectorAll('.swipe-content-toggle')].forEach(button => {
    button.addEventListener('click', () => {
      const expanded = button.getAttribute('aria-expanded') === 'true';
      const panel = document.getElementById(button.getAttribute('aria-controls'));
      button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
      if (panel) panel.hidden = expanded;
    });
  });

  let drag = null;
  const THRESHOLD = 80;
  const DIRECTION_THRESHOLD = 8;
  stage.addEventListener('pointerdown', e => {
    if (index >= total) return;
    const card = cards[index];
    if (!card.contains(e.target) || e.target.closest('a, button')) return;
    drag = {id: e.pointerId, x: e.clientX, y: e.clientY, dx: 0, horizontal: false, card};
  });
  stage.addEventListener('pointermove', e => {
    if (!drag || e.pointerId !== drag.id) return;
    drag.dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (!drag.horizontal) {
      if (Math.max(Math.abs(drag.dx), Math.abs(dy)) < DIRECTION_THRESHOLD) return;
      if (Math.abs(dy) >= Math.abs(drag.dx)) { drag = null; return; }
      drag.horizontal = true;
      drag.card.setPointerCapture(e.pointerId);
    }
    drag.card.style.transform = `translateX(${drag.dx}px) rotate(${drag.dx / 22}deg)`;
    const fade = Math.min(1, Math.max(0, (Math.abs(drag.dx) - 30) / 60));
    drag.card.querySelector('.stamp-right').style.opacity = drag.dx > 0 ? fade : 0;
    drag.card.querySelector('.stamp-left').style.opacity = drag.dx < 0 ? fade : 0;
  });
  const endDrag = e => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.horizontal) { drag = null; return; }
    const {card, dx} = drag;
    drag = null;
    card.querySelector('.stamp-right').style.opacity = '';
    card.querySelector('.stamp-left').style.opacity = '';
    if (dx > THRESHOLD) act('right');
    else if (dx < -THRESHOLD) act('left');
    else {
      card.style.transition = 'transform .2s ease';
      card.style.transform = '';
      setTimeout(() => { card.style.transition = ''; }, 220);
    }
  };
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);

  const batchBtn = document.getElementById('batch-btn');
  if (batchBtn) {
    const select = document.getElementById('batch-cv');
    let savedCv = null;
    try { savedCv = localStorage.getItem(`jw-cv-${track}`); } catch (_) {}
    if (savedCv && [...select.options].some(o => o.value === savedCv)) select.value = savedCv;
    batchBtn.addEventListener('click', () => {
      if (!select.value) return;
      try { localStorage.setItem(`jw-cv-${track}`, select.value); } catch (_) {}
      batchBtn.disabled = true;
      batchBtn.dataset.started = '1';
      fetch('/draft/batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({track, cv_library_id: Number(select.value)}),
      }).then(resp => {
        if (!resp.ok) { batchBtn.disabled = false; delete batchBtn.dataset.started; return; }
        if (window.jwBatchBadge) window.jwBatchBadge.start();
        window.location.assign(backHref);
      }).catch(() => {
        batchBtn.disabled = false;
        delete batchBtn.dataset.started;
      });
    });
  }

  render();
})();
"""


def _swipe_page_template(
    *, track, cards, total, pending, batch, back_href, batch_badge="", csrf_token=""
) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f3f1eb">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>jobwatch · tri des offres</title>
{_csrf_head(csrf_token)}
<script>
(function () {{
  try {{
    const saved = localStorage.getItem('jw-theme');
    document.documentElement.dataset.theme = saved === 'dark' ? 'dark' : 'light';
  }} catch (_) {{
    document.documentElement.dataset.theme = 'light';
  }}
}})();
</script>
<style>
{_CSS}{_SWIPE_CSS}</style></head>
<body data-track="{track}" data-pending="{pending}" data-back-href="{back_href}">
<div class="ambient" aria-hidden="true"></div>
<div class="swipe-shell">
  <div class="swipe-top">
    <a class="swipe-back" href="{back_href}">← Tableau de bord</a>
    {batch_badge}
    <span class="swipe-count" id="swipe-count">1 / {total}</span>
  </div>
  <div class="swipe-stage" id="swipe-stage">
{cards}
  </div>
  <div class="swipe-controls" id="swipe-controls">
    <button class="swipe-btn swipe-btn-no" id="swipe-no" type="button" aria-label="Écarter (flèche gauche)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
    </button>
    <button class="swipe-btn swipe-btn-undo" id="swipe-undo" type="button" aria-label="Annuler le dernier tri (flèche haut)" disabled>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>
    </button>
    <button class="swipe-btn swipe-btn-yes" id="swipe-yes" type="button" aria-label="À candidater (flèche droite)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 12.5 5 5 10-11"/></svg>
    </button>
  </div>
  <section class="swipe-done" id="swipe-done" hidden aria-live="polite">
    <h2>Tri terminé</h2>
    <p class="done-stats"><span id="done-right">0</span> à candidater · <span id="done-left">0</span> écartée(s)</p>
    {batch}
    <a class="card-action done-back" href="{back_href}">← Retour au tableau de bord</a>
  </section>
  <div class="swipe-support">
    <button class="bug-report-button" type="button" data-bug-report-open>Signaler un bug</button>
  </div>
</div>
{_BUG_REPORT_DIALOG}
<script>
{_SWIPE_JS}
{_BUG_REPORT_JS}
{_BATCH_BADGE_JS}</script></body></html>
"""
