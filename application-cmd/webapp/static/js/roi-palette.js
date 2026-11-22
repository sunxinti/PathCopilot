/**
 * PathCopilot ROI palette controller.
 *
 * Important: this module changes presentation only. The analysis pipeline keeps
 * its canonical red / blue / green level identifiers and cached data unchanged.
 */
(function () {
    'use strict';

    const STORAGE_KEY = 'pathcopilot.roiPalette.v1';
    const LEVEL_ALIASES = {
        level1: 'level1', red: 'level1', '10': 'level1', '10x': 'level1',
        level2: 'level2', blue: 'level2', '40': 'level2', '40x': 'level2',
        level3: 'level3', green: 'level3', '160': 'level3', '160x': 'level3'
    };

    const PRESETS = Object.freeze({
        classic: Object.freeze({
            level1: '#FF0000',
            level2: '#0066FF',
            level3: '#00CC00'
        }),
        okabeIto: Object.freeze({
            level1: '#D55E00',
            level2: '#0072B2',
            level3: '#CC79A7'
        }),
        softAccessible: Object.freeze({
            level1: '#C75B39',
            level2: '#356FA3',
            level3: '#8264A5'
        }),
        highContrast: Object.freeze({
            level1: '#111827',
            level2: '#0077B6',
            level3: '#F59E0B'
        })
    });

    const DEFAULT_STATE = Object.freeze({
        preset: 'classic',
        colors: { ...PRESETS.classic }
    });

    function normalizeHex(value, fallback) {
        const input = String(value || '').trim();
        const short = /^#([0-9a-f]{3})$/i.exec(input);
        if (short) {
            return '#' + short[1].split('').map(char => char + char).join('').toUpperCase();
        }
        const full = /^#([0-9a-f]{6})$/i.exec(input);
        return full ? '#' + full[1].toUpperCase() : fallback;
    }

    function normalizeLevel(value) {
        const key = String(value || '').trim().toLowerCase();
        if (LEVEL_ALIASES[key]) return LEVEL_ALIASES[key];
        if (key.includes('160') || key.includes('green')) return 'level3';
        if (key.includes('40') || key.includes('blue')) return 'level2';
        return 'level1';
    }

    function hexToRgb(hex) {
        const normalized = normalizeHex(hex, '#000000').slice(1);
        return [
            parseInt(normalized.slice(0, 2), 16),
            parseInt(normalized.slice(2, 4), 16),
            parseInt(normalized.slice(4, 6), 16)
        ];
    }

    function readState() {
        try {
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
            if (!saved || typeof saved !== 'object') return { ...DEFAULT_STATE, colors: { ...DEFAULT_STATE.colors } };
            const preset = saved.preset === 'custom' || PRESETS[saved.preset] ? saved.preset : DEFAULT_STATE.preset;
            const base = preset === 'custom' ? DEFAULT_STATE.colors : PRESETS[preset];
            return {
                preset,
                colors: {
                    level1: normalizeHex(saved.colors?.level1, base.level1),
                    level2: normalizeHex(saved.colors?.level2, base.level2),
                    level3: normalizeHex(saved.colors?.level3, base.level3)
                }
            };
        } catch (error) {
            console.warn('[ROI palette] Could not read saved palette:', error);
            return { ...DEFAULT_STATE, colors: { ...DEFAULT_STATE.colors } };
        }
    }

    let state = readState();

    function saveState() {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        } catch (error) {
            console.warn('[ROI palette] Could not persist palette:', error);
        }
    }

    function applyCssVariables() {
        const root = document.documentElement;
        ['level1', 'level2', 'level3'].forEach(level => {
            const color = state.colors[level];
            root.style.setProperty(`--roi-${level}`, color);
            root.style.setProperty(`--roi-${level}-rgb`, hexToRgb(color).join(', '));
        });
        root.dataset.roiPalette = state.preset;
    }

    const OVERLAY_SELECTOR = [
        '.roi-overlay',
        '.phase-overlay',
        '.collab-region-overlay'
    ].join(', ');

    function refreshRenderedElement(element) {
        if (!(element instanceof HTMLElement)) return;

        const rawLevel = element.dataset.roiLevel || element.dataset.level;
        const level = normalizeLevel(rawLevel);
        const color = state.colors[level];
        element.dataset.roiLevel = level;

        if (!element.matches(OVERLAY_SELECTOR)) return;

        // Some Agent/Collaborative overlays carry inline `!important` colors.
        // Update the live DOM directly so an already rendered WSI changes
        // immediately, without re-running the pathology analysis.
        element.style.setProperty('--roi-current-color', color);
        element.style.setProperty('border-color', color, 'important');
        element.style.setProperty('outline-color', color, 'important');

        const isFilledOverlay =
            element.classList.contains('phase-overlay') ||
            element.classList.contains('collab-region-overlay');
        if (isFilledOverlay) {
            element.style.setProperty('background-color', withAlpha(color, 0.12), 'important');
        }

        element.querySelectorAll('.roi-overlay-label, .phase-overlay-label').forEach(label => {
            label.style.setProperty('background-color', color, 'important');
        });

        const levelLabel = getLevelLabel(level);
        const suffix = element.dataset.roiId ? ` ROI ${element.dataset.roiId}` : ' ROI';
        element.setAttribute('aria-label', `${levelLabel}${suffix}`);
        element.title = levelLabel;
    }

    function refreshRenderedElements() {
        document.querySelectorAll('[data-roi-level], [data-level]').forEach(element => {
            const rawLevel = element.dataset.roiLevel || element.dataset.level;
            const level = normalizeLevel(rawLevel);
            element.dataset.roiLevel = level;
            refreshRenderedElement(element);
        });

        document.querySelectorAll('[data-roi-color-output]').forEach(output => {
            const level = normalizeLevel(output.dataset.roiColorOutput);
            output.textContent = state.colors[level];
        });
    }

    let refreshFrame = 0;
    function scheduleRefresh() {
        if (refreshFrame) return;
        refreshFrame = window.requestAnimationFrame(() => {
            refreshFrame = 0;
            refreshRenderedElements();
        });
    }

    function observeNewOverlays() {
        if (!document.body || window.__pathcopilotRoiPaletteObserver) return;
        const observer = new MutationObserver(mutations => {
            const hasRelevantAddition = mutations.some(mutation =>
                Array.from(mutation.addedNodes).some(node =>
                    node instanceof HTMLElement &&
                    (node.matches?.('[data-roi-level], [data-level], .roi-overlay, .phase-overlay, .collab-region-overlay') ||
                     node.querySelector?.('[data-roi-level], [data-level], .roi-overlay, .phase-overlay, .collab-region-overlay'))
                )
            );
            if (hasRelevantAddition) scheduleRefresh();
        });
        observer.observe(document.body, { childList: true, subtree: true });
        window.__pathcopilotRoiPaletteObserver = observer;
    }

    function syncSettingsControls() {
        document.querySelectorAll('input[name="roiPalettePreset"]').forEach(input => {
            input.checked = input.value === state.preset;
            input.closest('.roi-palette-option')?.classList.toggle('is-selected', input.checked);
        });
        ['level1', 'level2', 'level3'].forEach(level => {
            const input = document.querySelector(`[data-roi-custom-color="${level}"]`);
            if (input) input.value = state.colors[level];
        });
        const customControls = document.getElementById('roiCustomColorControls');
        if (customControls) customControls.hidden = state.preset !== 'custom';
        refreshRenderedElements();
    }

    function setStatus(message) {
        const status = document.getElementById('roiPaletteStatus');
        if (!status) return;
        status.textContent = message;
        status.classList.add('is-visible');
        window.clearTimeout(setStatus.timer);
        setStatus.timer = window.setTimeout(() => status.classList.remove('is-visible'), 1800);
    }

    function choosePreset(preset, announce = true) {
        if (preset === 'custom') {
            state = { preset: 'custom', colors: { ...state.colors } };
        } else if (PRESETS[preset]) {
            state = { preset, colors: { ...PRESETS[preset] } };
        } else {
            return;
        }
        saveState();
        applyCssVariables();
        syncSettingsControls();
        if (announce) {
            setStatus((window.__lang || 'zh') === 'en'
                ? 'Applied to current and future ROIs'
                : '已同步应用到当前及后续 ROI');
        }
        window.dispatchEvent(new CustomEvent('pathcopilot:roi-palette-change', { detail: getState() }));
    }

    function setCustomColor(level, value) {
        const normalizedLevel = normalizeLevel(level);
        state = {
            preset: 'custom',
            colors: {
                ...state.colors,
                [normalizedLevel]: normalizeHex(value, state.colors[normalizedLevel])
            }
        };
        saveState();
        applyCssVariables();
        syncSettingsControls();
        setStatus((window.__lang || 'zh') === 'en'
            ? 'Custom palette applied to current and future ROIs'
            : '自定义配色已同步应用到当前及后续 ROI');
        window.dispatchEvent(new CustomEvent('pathcopilot:roi-palette-change', { detail: getState() }));
    }

    function bindSettingsControls() {
        if (document.documentElement.dataset.roiPaletteBound === 'true') {
            syncSettingsControls();
            return;
        }
        document.documentElement.dataset.roiPaletteBound = 'true';
        document.addEventListener('change', event => {
            const presetInput = event.target.closest?.('input[name="roiPalettePreset"]');
            if (presetInput) choosePreset(presetInput.value);

            const colorInput = event.target.closest?.('[data-roi-custom-color]');
            if (colorInput) setCustomColor(colorInput.dataset.roiCustomColor, colorInput.value);
        });
        observeNewOverlays();
        syncSettingsControls();
    }

    function getState() {
        return { preset: state.preset, colors: { ...state.colors } };
    }

    function getColor(level) {
        return state.colors[normalizeLevel(level)];
    }

    function getLevelLabel(level) {
        const normalized = normalizeLevel(level);
        const labels = {
            level1: 'Level 1 · 10×',
            level2: 'Level 2 · 40×',
            level3: 'Level 3 · 160×'
        };
        return labels[normalized];
    }

    function withAlpha(color, alpha) {
        const [r, g, b] = hexToRgb(color);
        return `rgba(${r}, ${g}, ${b}, ${Math.max(0, Math.min(1, Number(alpha)))})`;
    }

    applyCssVariables();
    window.addEventListener('pathcopilot:roi-palette-change', scheduleRefresh);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindSettingsControls, { once: true });
    } else {
        bindSettingsControls();
    }

    window.PathCopilotRoiPalette = Object.freeze({
        presets: PRESETS,
        getState,
        getColor,
        getLevelLabel,
        normalizeLevel,
        withAlpha,
        choosePreset,
        setCustomColor,
        apply: applyCssVariables,
        refresh: refreshRenderedElements,
        bindSettingsControls,
        syncSettingsControls
    });
})();
