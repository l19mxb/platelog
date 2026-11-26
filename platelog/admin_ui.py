"""Shared Admin navigation, fields, contextual help and responsive layout."""

from html import escape

SECTIONS = {
    "axis-cameras": ("Axis cameras", "Connect cameras and configure how they send vehicle events."),
    "registered-vehicles": ("Registered vehicles", "Import and maintain the list used to recognise registered vehicles."),
    "alarms": ("Alarm rules", "Choose what happens when a vehicle is read."),
    "audible-alerts": ("Audible alerts", "Choose the sounds staff hear for new vehicle reads."),
    "csv-reports": ("Daily reports", "Schedule a CSV report for the previous calendar day."),
    "retention": ("Data & retention", "Manage stored passages, photos and automatic cleanup."),
    "access": ("Access & security", "Control staff sign-in and the administrator password."),
    "system": ("System", "Manage display time, listener ports and service operation."),
}


def section_url(key: str) -> str:
    return "/admin/alarms" if key == "alarms" else f"/admin?section={key}"


def help_tip(key: str, label: str, text: str) -> str:
    return (f'<span class="help-wrap"><button type="button" class="help-button" '
            f'aria-label="Help: {escape(label)}" aria-describedby="help-{escape(key)}" data-help>?</button>'
            f'<span id="help-{escape(key)}" class="help-tip" role="tooltip" hidden>{escape(text)}</span></span>')


def field(key: str, label: str, control: str, help_text: str = "", *, wide: bool = False) -> str:
    identity = f"field-{escape(key)}"
    for tag in ("input", "select", "textarea"):
        if f"<{tag}" in control:
            control = control.replace(f"<{tag}", f'<{tag} id="{identity}"', 1)
            break
    tip = help_tip(key, label, help_text) if help_text else ""
    return (f'<div class="field{" field-wide" if wide else ""}"><div class="field-label">'
            f'<label for="{identity}">{escape(label)}</label>{tip}</div>{control}</div>')


def admin_shell(section: str, content: str, csrf: str, *, message: str = "", error: bool = False,
                actions: str = "", heading: str = "", description: str | None = None) -> str:
    section = section if section in SECTIONS else "axis-cameras"
    title, intro = SECTIONS[section]
    title = heading or title
    intro = description if description is not None else intro
    links, options = [], []
    for key, (label, _) in SECTIONS.items():
        current = ' aria-current="page"' if key == section else ""
        links.append(f'<a href="{section_url(key)}"{current}>{escape(label)}</a>')
        options.append(f'<option value="{section_url(key)}"{" selected" if key == section else ""}>{escape(label)}</option>')
    notice = (f'<p class="notice{" error" if error else ""}" role="{"alert" if error else "status"}">{escape(message)}</p>'
              if message else "")
    return f'''<div class="admin-shell"><aside class="admin-sidebar"><h2>Administration</h2>
        <nav aria-label="Admin settings">{"".join(links)}</nav>
        <div class="mobile-admin-nav"><label for="admin-section-nav">Settings section</label>
        <select id="admin-section-nav" data-admin-nav>{"".join(options)}</select></div>
        <form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{escape(csrf)}">
        <button class="secondary" type="submit">Sign out</button></form></aside>
        <div class="admin admin-content"><div class="admin-heading"><div><h2>{escape(title)}</h2>
        <p class="section-intro">{escape(intro)}</p></div>{actions}</div>{notice}{content}</div></div>'''


CSS = """
.admin-page { background: #fff; }
.admin-page main { padding: 0; }
.admin-page main > header { display:flex; align-items:center; justify-content:space-between; gap:16px;
    padding:10px 28px; margin:0; background:#172638; color:#fff; min-height:44px; }
.admin-page main > header h1 { font-size:28px; letter-spacing:-.035em; }
.admin-page .header-top > div { display:flex; align-items:center; gap:24px; }
.admin-page .header-top p { color:#e4edf5; font-size:15px; padding-left:24px; border-left:1px solid #5c6b7b; }
.admin-page .tabs { margin:0; border:0; gap:12px; }
.admin-page .tabs a { color:#fff; padding:7px 16px; border:0; border-radius:5px; font-size:15px; }
.admin-page .tabs a[aria-current] { background:#194b79; }
.admin-shell { display:grid; grid-template-columns:232px minmax(0,1fr); min-height:calc(100dvh - 64px); }
.admin-sidebar { background:#f4f7fa; padding:24px 12px; border-right:1px solid #d3dce5; }
.admin-sidebar h2 { font-size:19px; margin:0 12px 20px; }
.admin-sidebar nav { display:grid; gap:4px; }
.admin-sidebar nav a { display:block; padding:10px 12px; font-size:15px; color:#172638; text-decoration:none; border-radius:5px; }
.admin-sidebar nav a:hover { background:#e3ebf4; }
.admin-sidebar nav a[aria-current] { background:#194b79; color:white; font-weight:650; }
.admin-sidebar form { margin:28px 12px 0; }
.mobile-admin-nav { display:none; }
.admin.admin-content { max-width:none; min-width:0; padding:24px 32px 44px; font-size:15px; }
.admin-content .admin-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:24px; }
.admin-heading h2 { font-size:28px; line-height:1.2; letter-spacing:-.025em; margin:0; }
.admin .section-intro { color:#4b5d70; margin:6px 0 0; }
.admin-content :is(h3,h4) { margin:24px 0 10px; font-size:19px; line-height:1.4; }
.admin-content > section:first-of-type > h3:first-child { margin-top:0; }
.admin-content section + section { margin-top:28px; padding-top:20px; border-top:1px solid #d3dce5; }
.admin-content p { max-width:85ch; }
.admin-content form { display:flex; flex-wrap:wrap; gap:16px; align-items:flex-end; margin-top:16px; }
.admin-content .form-grid, .admin-content .camera-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px 24px; }
.admin-content .field { display:flex; flex-direction:column; min-width:0; gap:6px; }
.admin-content .field-label { display:flex; align-items:center; gap:7px; min-height:22px; }
.admin-content .field-label label { display:inline; font-weight:600; font-size:14px; }
.admin-content .field-wide, .admin-content .form-actions, .admin-content .form-note { grid-column:1/-1; }
.admin-content :is(input:not([type="checkbox"]):not([type="hidden"]),select,textarea) {
    box-sizing:border-box; min-width:0; width:100%; max-width:100%; padding:9px 11px; border:1px solid #9eacb9;
    border-radius:5px; background:#fff; color:#172638; font:15px/1.35 system-ui,sans-serif; }
.admin-content input[type="number"] { width:100%; }
.admin-content input[type="checkbox"] { width:17px; height:17px; accent-color:#194b79; flex-shrink:0; }
.admin-content .enable { flex-direction:row; align-items:center; gap:10px; font-weight:500; }
.admin-content .enable > label.enable { width:auto; }
.admin-content .enable .help-wrap { margin-left:0; }
.admin-content button, .admin-content .camera-add, .admin-sidebar button { font:600 14px/1.4 system-ui,sans-serif; padding:9px 14px; }
.admin-content .camera-add { display:inline-block; white-space:nowrap; }
.admin-page .secondary, .admin-content .sound-tests button { background:#fff; color:#194b79; border:1px solid #9eacb9; }
.admin-page button:hover, .admin-content .camera-add:hover { filter:brightness(.95); }
.admin-content .form-actions { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.admin-content .form-note { color:#4b5d70; font-size:14px; margin:0; }
.admin-content .notice { background:#edf4fa; padding:12px 14px; border-left:3px solid #194b79; margin:0 0 20px; }
.admin-content .notice.error { border-color:#a32c2c; color:#7c2323; background:#fff2f2; }
.admin-content .notice:empty { display:none; }
.admin-content .camera-heading { margin-bottom:12px; }
.admin-content .camera-heading h3 { margin:0; }
.admin-content .camera-editor, .admin-content .rule-editor { margin-top:20px; padding:18px 20px; background:#fff; border:1px solid #c3cfda; border-radius:6px; }
.admin-content :is(.camera-editor,.rule-editor) > :is(h3,h4):first-child { margin-top:0; }
.admin-content .camera-table { margin-top:12px; }
.admin-content .camera-table :is(td,th) { font-size:14px; padding:11px 12px; }
.admin-content table code { font-size:12px; overflow-wrap:anywhere; white-space:normal; }
.admin-content .camera-table code { max-width:40ch; display:inline-block; }
.admin-content .camera-password input { width:100%; }
.admin-content .alarm-rule-form { display:block; }
.admin-content .alarm-action-fields { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px 24px;
    padding:0; margin:20px 0 0; border:0; min-width:0; }
.admin-content .alarm-action-fields legend { font-size:18px; font-weight:650; margin-bottom:10px; }
.admin-content .alarm-rule-form textarea { min-width:0; resize:vertical; }
.admin-content .alarm-rule-form > .form-actions { margin-top:18px; }
.admin-content [hidden] { display:none !important; }
.admin-content .subtabs { display:flex; gap:6px; border-bottom:1px solid #c3cfda; margin-bottom:24px; }
.admin-content .subtabs a { padding:9px 14px; text-decoration:none; border-bottom:3px solid transparent; color:#4b5d70; font-weight:600; }
.admin-content .subtabs a[aria-current] { color:#194b79; border-bottom-color:#194b79; }
.admin-content .smtp-warning { color:#8b3c17; }
.admin-content .storage-summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; padding:14px 0; }
.admin-content .storage-summary div { display:grid; gap:3px; }
.admin-content .storage-summary dt { color:#4b5d70; font-size:13px; }
.admin-content .storage-summary dd { font-size:20px; font-weight:600; margin:0; }
.admin-content .danger-zone { border:1px solid #e4bcbc; border-radius:6px; padding:16px 20px; margin-top:28px; }
.admin-content .danger-zone h3 { margin-top:0; }
.admin-content .danger { background:#a32c2c; }
.admin-content .activity-list { padding:0; list-style:none; }
.admin-content .activity-list li { padding:10px 0; border-bottom:1px solid #e3ebf4; }
.admin-content .activity-list time { display:block; color:#4b5d70; font-size:13px; }
.admin-content details > summary { cursor:pointer; font-weight:600; color:#194b79; }
.admin-content .review-table { max-width:100%; }
.admin-content .rule-summary { color:#4b5d70; display:block; font-size:13px; }
.help-wrap { display:inline-flex; position:relative; align-items:center; flex-shrink:0; }
button.help-button { display:inline-flex; justify-content:center; align-items:center; padding:0; width:19px; height:19px;
    border:1px solid #64778a; border-radius:50%; background:transparent; color:#40576e; font:600 12px/1 system-ui,sans-serif; cursor:help; }
.help-tip { position:fixed; z-index:1000; box-sizing:border-box; width:max-content; max-width:min(320px,calc(100vw - 24px));
    background:#172638; color:#fff; border-radius:5px; padding:10px 12px; font:400 13px/1.5 system-ui,sans-serif;
    box-shadow:0 3px 10px #17263826; white-space:normal; pointer-events:auto; }
.help-tip[hidden] { display:none; }
.admin-page :focus-visible { outline:3px solid #4385bf; outline-offset:3px; }
.admin-login { padding:32px 24px; max-width:30rem; margin:32px auto; }
.admin-login h2 { font-size:26px; }
.admin-login form { display:grid; gap:16px; }
@media (max-width:1050px) { .admin-shell { grid-template-columns:200px minmax(0,1fr); }
    .admin.admin-content { padding:24px; } }
@media (max-width:760px) {
    .admin-page main > header { padding:10px 16px; gap:8px; }
    .admin-page .header-top p { display:none; }
    .admin-page .tabs { gap:4px; }
    .admin-page .tabs a { padding:7px 10px; }
    .admin-shell { display:block; }
    .admin-sidebar { display:flex; align-items:center; gap:12px; padding:12px 16px; border:0; border-bottom:1px solid #d3dce5; }
    .admin-sidebar h2, .admin-sidebar nav { display:none; }
    .admin-sidebar form { margin:0; padding-top:17px; }
    .mobile-admin-nav { display:grid; flex:1; min-width:0; gap:4px; color:#4b5d70; font-size:12px; }
    .mobile-admin-nav select { width:100%; min-width:0; padding:8px; color:#172638; background:white; border:1px solid #9eacb9;
        border-radius:5px; font:14px system-ui,sans-serif; }
    .admin.admin-content { padding:22px 16px 36px; }
    .admin-heading h2 { font-size:25px; }
    .admin-content .admin-heading { gap:12px; }
    .admin-content .camera-editor, .admin-content .rule-editor { padding:16px; }
    .admin-content .subtabs a { padding:8px 10px; font-size:14px; }
    .admin-content .storage-summary { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
@media (max-width:600px) {
    .admin-content .form-grid, .admin-content .camera-form, .admin-content .alarm-action-fields { grid-template-columns:minmax(0,1fr); }
}
"""

JS = """
(() => {
    const nav = document.querySelector('[data-admin-nav]');
    nav?.addEventListener('change', () => { location.href = nav.value; });
    // Older documentation links still select the right Admin section.
    if (document.querySelector('.admin-shell') && location.pathname === '/admin' && !location.search.includes('section=')) {
        const aliases = {'axis-cameras':'axis-cameras','camera-editor':'axis-cameras',
            'registered-vehicles':'registered-vehicles','registered-refresh':'registered-vehicles',
            'audible-alerts':'audible-alerts','csv-reports':'csv-reports','register-access':'access'};
        const section = aliases[location.hash.slice(1)];
        if (section) { const url = new URL(location.href); url.searchParams.set('section', section); location.replace(url.href); }
    }
    let open, pinned = false;
    function closeHelp() {
        if (open) open.querySelector('[role="tooltip"]').hidden = true;
        open = undefined; pinned = false;
    }
    function showHelp(wrap) {
        if (open !== wrap) closeHelp();
        open = wrap;
        const button = wrap.querySelector('[data-help]');
        const tip = wrap.querySelector('[role="tooltip"]');
        tip.hidden = false;
        const target = button.getBoundingClientRect(), box = tip.getBoundingClientRect();
        tip.style.left = `${Math.max(12, Math.min(target.left, innerWidth - box.width - 12))}px`;
        const below = target.bottom + 8;
        tip.style.top = `${Math.max(8, below + box.height < innerHeight - 8 ? below : target.top - box.height - 8)}px`;
    }
    // Delegate so help also works in live-updated table headers.
    document.addEventListener('pointerover', event => {
        const wrap = event.target.closest?.('.help-wrap');
        if (wrap && !wrap.contains(event.relatedTarget)) showHelp(wrap);
    });
    document.addEventListener('pointerout', event => {
        const wrap = event.target.closest?.('.help-wrap');
        if (wrap && !wrap.contains(event.relatedTarget) && !pinned && document.activeElement !== wrap.querySelector('[data-help]')) closeHelp();
    });
    document.addEventListener('focusin', event => {
        const button = event.target.closest?.('[data-help]');
        if (button) showHelp(button.closest('.help-wrap'));
    });
    document.addEventListener('focusout', event => {
        if (open && event.target === open.querySelector('[data-help]')) closeHelp();
    });
    document.addEventListener('click', event => {
        const button = event.target.closest?.('[data-help]');
        if (!button) return;
        const wrap = button.closest('.help-wrap');
        if (open === wrap && pinned) closeHelp(); else { showHelp(wrap); pinned = true; }
    });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') closeHelp(); });
    document.addEventListener('pointerdown', event => { if (open && !open.contains(event.target)) closeHelp(); });
    addEventListener('resize', closeHelp);
    addEventListener('scroll', closeHelp, true);
    for (const form of document.querySelectorAll('[data-rule-editor]')) {
        const action = form.querySelector('[name="action"]');
        function update() {
            for (const group of form.querySelectorAll('[data-alarm-action]')) {
                const active = action.value === 'both' || action.value === group.dataset.alarmAction;
                group.hidden = !active;
                group.disabled = !active;
            }
        }
        action.addEventListener('change', update); update();
    }
    const security = document.querySelector('#field-smtp-security');
    if (security) {
        const warning = document.querySelector('[data-smtp-plain-warning]');
        function updateSecurity() { warning.hidden = security.value !== 'none'; }
        security.addEventListener('change', updateSecurity); updateSecurity();
    }
})();
"""
