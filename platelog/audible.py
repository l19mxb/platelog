"""Browser chimes for newly received vehicle passages."""

from html import escape

from .admin_ui import help_tip


DEFAULT_SETTINGS = {"registered": False, "unregistered": False}

REGISTER_CONTROL = """<div id="register-audio" class="register-audio" hidden>
    <button type="button" data-enable-audio aria-pressed="false">Enable sound</button>
    <span data-audio-status role="status">New reads from the selected camera.</span></div>"""


def settings_form(csrf: str, settings: dict) -> str:
    known = " checked" if settings["registered"] else ""
    unknown = " checked" if settings["unregistered"] else ""
    registered_help = help_tip("sound-registered", "Registered vehicle sound", "A soft success chime for a fresh read that matches Registered vehicles. The plate log's camera filter applies.")
    unregistered_help = help_tip("sound-unregistered", "Unregistered vehicle sound", "A sharper four-pulse alert for a fresh read absent from Registered vehicles. An empty registered list, hidden tab or duplicate replay will not sound.")
    return f"""<section id="audible-alerts"><h3>Vehicle sounds</h3>
        <form class="form-grid" method="post" action="/admin/audible-alerts">
        <input type="hidden" name="csrf" value="{escape(csrf)}">
        <div class="enable field-wide"><label class="enable"><input type="checkbox" name="registered" value="yes"{known}> Registered vehicle · success chime</label>{registered_help}</div>
        <div class="enable field-wide"><label class="enable"><input type="checkbox" name="unregistered" value="yes"{unknown}> Unregistered vehicle · alert chime</label>{unregistered_help}</div>
        <div class="form-actions"><button type="submit">Save sounds</button></div></form>
        <div class="sound-tests"><button type="button" data-test-audio="registered">Test success sound</button>
        <button type="button" data-test-audio="unregistered">Test alert sound</button>
        <span data-audio-test-status role="status"></span></div>
        <p>Click Enable sound at each desk. Sounds play while the plate log tab is visible.</p></section>"""


JS = """
(() => {
    let audio;
    let active = false;
    let generation = 0;
    let started;
    let settingsKey;
    let pending = false;
    let reconnecting = true;
    let lastPoll = 0;
    const playing = new Set();
    const heard = new Map();
    const control = document.querySelector('#register-audio');
    const button = control?.querySelector('[data-enable-audio]');
    const status = control?.querySelector('[data-audio-status]');
    const camera = document.querySelector('#register-search select[name="camera"]');

    async function enableAudio() {
        const Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) throw new Error('Audio is unavailable in this browser.');
        audio ??= new Context();
        await audio.resume();
        if (audio.state !== 'running') throw new Error('Click Enable sound to allow audio.');
    }

    function chime(kind, offset = 0) {
        const success = kind === 'registered';
        const notes = success ? [660, 880] : [880, 1320, 880, 1320];
        const begins = audio.currentTime + 0.02 + offset;
        notes.forEach((frequency, index) => {
            const at = begins + index * (success ? 0.16 : 0.125);
            const oscillator = audio.createOscillator();
            const gain = audio.createGain();
            oscillator.type = success ? 'sine' : 'square';
            oscillator.frequency.value = frequency;
            gain.gain.setValueAtTime(0, at);
            gain.gain.linearRampToValueAtTime(success ? 0.18 : 0.16, at + (success ? 0.015 : 0.006));
            gain.gain.exponentialRampToValueAtTime(0.001, at + (success ? 0.14 : 0.10));
            oscillator.connect(gain);
            gain.connect(audio.destination);
            oscillator.start(at);
            oscillator.stop(at + (success ? 0.15 : 0.11));
            playing.add(oscillator);
            oscillator.onended = () => { playing.delete(oscillator); oscillator.disconnect(); gain.disconnect(); };
        });
    }

    for (const test of document.querySelectorAll('[data-test-audio]')) {
        test.addEventListener('click', async () => {
            const feedback = document.querySelector('[data-audio-test-status]');
            try {
                await enableAudio();
                chime(test.dataset.testAudio);
                feedback.textContent = test.dataset.testAudio === 'registered' ? 'Success sound played.' : 'Alert sound played.';
            } catch (error) {
                feedback.textContent = error.message;
            }
        });
    }
    if (!control) return;

    function reset() {
        generation++;
        started = undefined;
        reconnecting = true;
        heard.clear();
        for (const oscillator of playing) oscillator.stop();
        playing.clear();
    }

    function mute() {
        active = false;
        reset();
        if (audio) void audio.suspend();
        button.textContent = 'Enable sound';
        button.setAttribute('aria-pressed', 'false');
        status.textContent = 'Sound off.';
    }

    button.addEventListener('click', async () => {
        if (active) { mute(); return; }
        button.disabled = true;
        try {
            await enableAudio();
            active = true;
            reset();
            button.textContent = 'Mute sound';
            button.setAttribute('aria-pressed', 'true');
            status.textContent = 'Sound on for new reads.';
            await poll();
        } catch (error) {
            status.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    async function poll() {
        if (pending || document.hidden) return;
        pending = true;
        const version = generation;
        const selected = camera?.value || '';
        const query = new URLSearchParams({ camera: selected });
        if (active && !reconnecting && started !== undefined) query.set('since', String(started));
        try {
            const response = await fetch('/register/audible-alerts?' + query, { signal: AbortSignal.timeout(8000) });
            if (!response.ok) throw new Error('Sign in or check the connection to resume sound.');
            const result = await response.json();
            if (version !== generation || document.hidden) return;
            const changed = settingsKey !== JSON.stringify(result.settings);
            settingsKey = JSON.stringify(result.settings);
            const available = result.settings.registered || result.settings.unregistered;
            control.hidden = !available;
            if (!available) {
                mute();
                return;
            }
            if (!active) {
                status.textContent = 'New reads from the selected camera.';
                return;
            }
            if (audio.state !== 'running') {
                mute();
                status.textContent = 'Click Enable sound to resume audio.';
                return;
            }
            if (!result.has_registered_list) {
                started = result.server_ms;
                status.textContent = 'Load a registered vehicle list to use sound.';
                return;
            }
            const baseline = changed || reconnecting || started === undefined || performance.now() - lastPoll > 15000;
            if (baseline) {
                started = result.server_ms;
                heard.clear();
                status.textContent = 'Sound on for new reads.';
            } else {
                for (const [id, received] of heard) {
                    if (received < result.server_ms - 60000) heard.delete(id);
                }
                const fresh = result.events.filter(event => !heard.has(event.id));
                fresh.forEach(event => heard.set(event.id, event.received_ms));
                fresh.forEach((event, index) => chime(event.kind, index * 0.55));
                if (fresh.length) {
                    const last = fresh[fresh.length - 1];
                    status.textContent = last.kind === 'registered' ? 'Registered vehicle · success sound played.' : 'Unregistered vehicle · alert sound played.';
                }
            }
            reconnecting = false;
            lastPoll = performance.now();
        } catch (error) {
            reconnecting = true;
            if (active) status.textContent = error.message;
        } finally {
            pending = false;
        }
    }

    camera?.addEventListener('change', () => { reset(); void poll(); });
    document.querySelector('[data-clear-search]')?.addEventListener('click', () => { reset(); void poll(); });
    document.addEventListener('visibilitychange', () => {
        reset();
        if (document.hidden) {
            if (active) {
                void audio.suspend();
                status.textContent = 'Sound paused while this tab is hidden.';
            }
        } else if (active) {
            void audio.resume().then(poll).catch(() => {
                mute();
                status.textContent = 'Click Enable sound to resume audio.';
            });
        } else {
            void poll();
        }
    });
    window.addEventListener('pagehide', mute);
    window.setInterval(poll, 5000);
    void poll();
})();
"""
