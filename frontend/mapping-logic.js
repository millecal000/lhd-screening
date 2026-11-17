// --- Disclaimer (persisted for the session) ---
(function () {
    const STORAGE_KEY = 'lhdi_disclaimer_accepted';
    const overlay = document.getElementById('disclaimer-overlay');
    if (!overlay) return;
    if (sessionStorage.getItem(STORAGE_KEY) === '1') {
        overlay.style.display = 'none';
    }
    const agreeBtn = document.getElementById('disclaimer-agree');
    if (agreeBtn) {
        agreeBtn.addEventListener('click', () => {
            sessionStorage.setItem(STORAGE_KEY, '1');
            overlay.style.display = 'none';
        });
    }
})();

// 1. Initialize Map with multiple base layers
let forecastChart;
let ratingCurvesChart;
let srcChart;
const _srcCache = new Map();
const _forecastCache = new Map();
let allDams = [];
let _forecastState = null; // { allPoints, hasSafetyRange, qMin, qMax, damName }
let markers = L.layerGroup();
// river/state hold the lowercased/uppercased values renderMarkers() matches against;
// the *Display companions hold the original text the filter chips show to the user.
let activeRiverFilter = { river: '', riverDisplay: '', state: '', stateDisplay: '' };
let fatalityOnlyFilter = false;

// Dams the LHDI review has flagged as non-LHD or removed - shared by every place in this
// file that decides whether a dam is eligible to be shown or searched (damPassesChips(),
// _damMatchesRiverFilter(), and the desktop free-text search), so they can't drift apart.
const EXCLUDED_REVIEW_STATUSES = new Set([
    'Removed',
    'Confirmed not a LHD',
    'Appears to not be LHD'
]);

// damPassesChips() is the single predicate both the map (renderMarkers) and the search
// bar's live suggestion lists filter through, so "what the map shows" and "what you can
// search for" never drift apart as chips are added/removed.
function damPassesChips(dam) {
    if (EXCLUDED_REVIEW_STATUSES.has((dam.Review_Status || '').trim())) return false;
    if (activeRiverFilter.river) {
        const gnis   = (dam.GNIS_Name       || '').toLowerCase();
        const stream = (dam['River/Stream'] || '').toLowerCase();
        const river  = (dam.River           || '').toLowerCase();
        if (!gnis.includes(activeRiverFilter.river) && !stream.includes(activeRiverFilter.river) && !river.includes(activeRiverFilter.river)) return false;
    }
    if (activeRiverFilter.state && (dam['State Abbreviation'] || '').toUpperCase() !== activeRiverFilter.state) return false;
    if (fatalityOnlyFilter && (parseInt(dam.NumberOfFatalities) || 0) === 0) return false;
    return true;
}

// Display-name resolver: prefer OSM_Name; fall back to Dam_Name; finally a placeholder.
// Also returns whether the underlying Dam_Name was "generic" (so callers can decide
// search/UI behavior).
const GENERIC_NAME_RE = /^(wade unspecified|low-?head dam( \(\d+\))?|lhd|added-?\d*|control structure( ?#?\d+)?|dam|unnamed|n\/?a)$/i;
function isGenericName(n) {
    if (!n) return true;
    return GENERIC_NAME_RE.test(String(n).trim());
}
function displayName(dam) {
    const osm = (dam.OSM_Name || '').trim();
    if (osm) return osm;
    const orig = (dam.Dam_Name || '').trim();
    if (orig && !isGenericName(orig)) return orig;
    if (orig) return orig; // keep generic placeholder visible if no better option
    return 'Unnamed Dam';
}
// Safely embed a JS string literal inside an HTML double-quoted attribute (onclick=...).
// JSON.stringify handles JS-string escaping (\, ", control chars), then we HTML-encode
// any chars that would break the attribute.
function jsAttrLiteral(s) {
    return JSON.stringify(String(s))
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors'
});

const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community'
});

const terrain = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
    attribution: 'Map data: &copy; OpenStreetMap contributors, SRTM | Map style: &copy; OpenTopoMap (CC-BY-SA)'
});

const map = L.map('map', {
    preferCanvas: true,
    layers: [osm] // Default layer
}).setView([39.82, -98.57], 4);

const NHD_BLUE = [0, 102, 204, 255];   // R,G,B,A 0-255

function getStrahlerFilter(zoom) {
    // Note: If fields mismatch on Esri's end, change "StreamOrde" to "StreamOrder"
    if (zoom >= 16) return "StreamOrde >= 1"; // Show everything (headwaters)
    if (zoom >= 14) return "StreamOrde >= 2";
    if (zoom >= 12) return "StreamOrde >= 3";
    if (zoom >= 10) return "StreamOrde >= 4";
    if (zoom >= 8)  return "StreamOrde >= 5";
    if (zoom >= 6)  return "StreamOrde >= 6";
    return "StreamOrde >= 7";                 // Only major rivers at global view
}

// Define base maps for the control toggle
const baseMaps = {
    "Street Map": osm,
    "Satellite": satellite,
    "Terrain": terrain
};

// Add the background maps button (Layers Control), Placing the Layer Controls before the nhdFLowlines so they can be filtered as well
// Not added to the map yet - it's attached below the dam/river search button once that control exists (see layerControl.addTo(map) further down)
const layerControl = L.control.layers(baseMaps, null, { position: 'topleft' });

// 1. Initialize using L.esri.featureLayer on the Flowlines sublayer (ID 2)
const nhdFlowlines = L.esri.featureLayer({
    url: 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/NHDPlusV21/FeatureServer/2',
    outFields: ['StreamOrde'], // Just pulling in the StreamOrde Attribute decreases lag time of the flowlines while zooming
    opacity: 1.0,
    where: getStrahlerFilter(4), // Initialize using the map's starting zoom layer constraint
    // simplifyFactor/precision ask the server to decimate and round the returned stream
    // vertices instead of sending full-resolution geometry - the flowlines dataset is very
    // vertex-dense, and this cuts payload size and canvas render cost substantially with no
    // visible difference at map scale.
    simplifyFactor: 0.5,
    precision: 5,
    style: function () {
        return {
            color: `rgba(${NHD_BLUE.join(',')})`,
            weight: 2
        };
    },
    attribution: 'Hydrography &copy; USGS NHD'
})

// 2. Dynamically change the client-side/server-side query as you zoom
function updateFlowlineFilters() {
    const currentZoom = map.getZoom();
    const sqlWhere = getStrahlerFilter(currentZoom);
    
    // Feature Layers use setWhere instead of setLayerDefs
    nhdFlowlines.setWhere(sqlWhere);
}

// 3. Listen for zoom events to update visibility
map.on('zoomend', updateFlowlineFilters);

 // 4. Add L.esri.featureLayer to the Layer Control Filter
layerControl.addOverlay(nhdFlowlines, "Flowlines");

// SVG icons for top-left toolbar buttons
const ICON_LOCATION = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;"><circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="3" fill="currentColor"></circle><line x1="12" y1="1" x2="12" y2="5"></line><line x1="12" y1="19" x2="12" y2="23"></line><line x1="1" y1="12" x2="5" y2="12"></line><line x1="19" y1="12" x2="23" y2="12"></line></svg>';
const ICON_FILTER = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"></polygon></svg>';
const ICON_SEARCH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>';
const ICON_LAYERS = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>';

// Abbreviation -> full name, used for the "State Abbreviation" search-bar filter category
// (lets someone type either "WY" or "Wyoming" and find the same suggestion).
const US_STATES = [
    ['AL','Alabama'],['AK','Alaska'],['AZ','Arizona'],['AR','Arkansas'],['CA','California'],
    ['CO','Colorado'],['CT','Connecticut'],['DE','Delaware'],['DC','District of Columbia'],
    ['FL','Florida'],['GA','Georgia'],['ID','Idaho'],['IL','Illinois'],['IN','Indiana'],
    ['IA','Iowa'],['KS','Kansas'],['KY','Kentucky'],['LA','Louisiana'],['ME','Maine'],
    ['MD','Maryland'],['MA','Massachusetts'],['MI','Michigan'],['MN','Minnesota'],
    ['MS','Mississippi'],['MO','Missouri'],['MT','Montana'],['NE','Nebraska'],['NV','Nevada'],
    ['NH','New Hampshire'],['NJ','New Jersey'],['NM','New Mexico'],['NY','New York'],
    ['NC','North Carolina'],['ND','North Dakota'],['OH','Ohio'],['OK','Oklahoma'],
    ['OR','Oregon'],['PA','Pennsylvania'],['RI','Rhode Island'],['SC','South Carolina'],
    ['SD','South Dakota'],['TN','Tennessee'],['TX','Texas'],['UT','Utah'],['VT','Vermont'],
    ['VA','Virginia'],['WA','Washington'],['WV','West Virginia'],['WI','Wisconsin'],['WY','Wyoming']
];

// Geolocation: zoom to user's current location
const GeolocationControl = L.Control.extend({
    options: { position: 'topleft' },
    onAdd: function() {
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
        const button = L.DomUtil.create('a', '', container);
        button.href = '#';
        button.title = 'Show my location';
        button.setAttribute('aria-label', 'Show my location');
        button.innerHTML = ICON_LOCATION;

        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.on(button, 'click', (e) => {
            L.DomEvent.preventDefault(e);
            if (!navigator.geolocation) {
                alert('Geolocation is not supported by your browser.');
                return;
            }
            navigator.geolocation.getCurrentPosition(
            (pos) => map.flyTo([pos.coords.latitude, pos.coords.longitude], 13, { duration: 1.5 }),
                (err) => alert('Unable to retrieve your location: ' + err.message)
            );
        });
        return container;
    }
});
map.addControl(new GeolocationControl());

// Search & filter bar. Both variants below share the same underlying filter state
// (activeRiverFilter / fatalityOnlyFilter / damPassesChips()) so the map's markers, the
// active-filters badge, and cross-links (state-boundary clicks, Escape key, legend) behave
// identically no matter which one is on screen:
//  - Mobile (<=768px): chip-based bar with a funnel menu and live typeahead (River/State/
//    Fatality chips + free text), docked next to the hamburger menu.
//  - Desktop (>768px): the original filter-icon button + dropdown panel (search results,
//    River/State text filters with an Apply/Clear button, fatality checkbox), floating over
//    the map like the other toolbar buttons - mirrors the pre-makeover main-branch UI.
const SearchControl = L.Control.extend({
    options: { position: 'topleft' },
    onAdd: function() {
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
        container.style.position = 'relative';

        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.disableScrollPropagation(container);

        let mode = null; // 'mobile' | 'desktop'
        let cleanupCurrent = null;

        function sync() {
            const isMobile = window.innerWidth <= 768;
            const nextMode = isMobile ? 'mobile' : 'desktop';
            if (nextMode === mode) return;
            if (cleanupCurrent) cleanupCurrent();
            container.innerHTML = '';
            container.classList.toggle('sc-container', isMobile);
            container.style.zIndex = isMobile ? '900' : ''; // Mobile bar sits above the layers control
            cleanupCurrent = isMobile ? buildMobileSearchUI(container) : buildDesktopSearchUI(container);
            mode = nextMode;
        }

        sync();
        window.addEventListener('resize', sync);

        return container;
    }
});

// --- Mobile filter UI: chip-based bar with funnel menu + typeahead ------------------------
function buildMobileSearchUI(container) {
        const bar = L.DomUtil.create('div', 'sc-bar', container);

        const searchIcon = L.DomUtil.create('span', 'sc-icon-search', bar);
        searchIcon.innerHTML = ICON_SEARCH;

        const chipsInput = L.DomUtil.create('div', 'sc-chips-input', bar);
        chipsInput.addEventListener('click', (e) => { if (e.target === chipsInput) input.focus(); });

        const input = L.DomUtil.create('input', 'sc-input', chipsInput);
        input.id = 'globalSearchInput';
        input.type = 'text';
        input.autocomplete = 'off';

        const funnelBtn = L.DomUtil.create('button', 'sc-funnel-btn', bar);
        funnelBtn.type = 'button';
        funnelBtn.title = 'Choose what to filter by';
        funnelBtn.setAttribute('aria-label', 'Choose what to filter by');
        funnelBtn.innerHTML = ICON_FILTER;

        const categoryMenu = L.DomUtil.create('div', 'sc-category-menu', container);
        const suggestions = L.DomUtil.create('div', 'sc-suggestions', container);
        suggestions.id = 'globalSearchResults';

        // --- Filter categories ----------------------------------------------------
        const CATEGORIES = [
            { key: 'general',  label: 'Dam / City / State', placeholder: 'Search dams, city, state...' },
            { key: 'river',    label: 'River Name',          placeholder: 'Search river name...' },
            { key: 'state',    label: 'State Abbreviation',  placeholder: 'Search state...' },
            { key: 'fatality', label: 'Show only fatality sites' }
        ];
        let mode = 'general';

        function setMode(key) {
            mode = key;
            const cat = CATEGORIES.find(c => c.key === key) || CATEGORIES[0];
            input.placeholder = cat.placeholder || 'Search dams, city, state...';
            hideCategoryMenu();
            hideSuggestions();
            setTimeout(() => input.focus(), 0);
        }

        // --- Floating-panel positioning (shared by the category menu & suggestions) --
        // Anchors under the bar at its live on-screen position (fixed, not absolute) and
        // caps its height, so on phones it reads as a dropdown card rather than covering
        // the whole screen - the map stays visible below it.
        function layoutFloater(el, maxHeight) {
            const rect = bar.getBoundingClientRect();
            const isMobile = window.innerWidth <= 768;
            el.style.position = 'fixed';
            el.style.top = (rect.bottom + 6) + 'px';
            if (isMobile) {
                el.style.left = '12px';
                el.style.right = '12px';
                el.style.width = 'auto';
            } else {
                el.style.left = rect.left + 'px';
                el.style.right = 'auto';
                el.style.width = Math.max(rect.width, 260) + 'px';
            }
            el.style.maxHeight = maxHeight;
            el.style.zIndex = '1150';
            el.style.overflowY = 'auto';
        }

        // --- Category dropdown menu -------------------------------------------------
        function renderCategoryMenu() {
            categoryMenu.innerHTML = '';
            const header = document.createElement('div');
            header.className = 'sc-suggestions-header';
            header.textContent = 'Filter Options';
            categoryMenu.appendChild(header);
            CATEGORIES.forEach(cat => {
                // Blue highlight = this is the currently selected filter option (input mode,
                // or - for fatality, which has no input mode - already toggled on).
                // Checkmark = that filter has actually started filtering (i.e. has a chip
                // showing in the search bar), not just that it's the option being viewed.
                const isFiltering = cat.key === 'fatality' ? fatalityOnlyFilter
                    : cat.key === 'river' ? !!activeRiverFilter.river
                    : cat.key === 'state' ? !!activeRiverFilter.state
                    : false;
                const isSelected = cat.key === mode || (cat.key === 'fatality' && isFiltering);
                const item = document.createElement('div');
                item.className = 'sc-category-item' + (isSelected ? ' sc-category-item-checked' : '');
                item.innerHTML = `<span class="sc-category-check">${isFiltering ? '✓' : ''}</span><span>${cat.label}</span>`;
                item.onclick = () => {
                    if (cat.key === 'fatality') {
                        fatalityOnlyFilter = !fatalityOnlyFilter;
                        renderChips();
                        renderMarkers();
                        hideCategoryMenu();
                    } else {
                        setMode(cat.key);
                    }
                };
                categoryMenu.appendChild(item);
            });
        }

        function showCategoryMenu() {
            hideSuggestions();
            renderCategoryMenu();
            layoutFloater(categoryMenu, '50vh');
            categoryMenu.style.display = 'block';
        }
        function hideCategoryMenu() { categoryMenu.style.display = 'none'; }

        L.DomEvent.on(funnelBtn, 'click', (e) => {
            L.DomEvent.preventDefault(e);
            if (categoryMenu.style.display === 'block') hideCategoryMenu(); else showCategoryMenu();
        });

        // --- Chips -------------------------------------------------------------------
        function makeChip(label, onRemove) {
            const chip = document.createElement('span');
            chip.className = 'sc-chip';
            const text = document.createElement('span');
            text.className = 'sc-chip-label';
            text.textContent = label;
            const close = document.createElement('span');
            close.className = 'sc-chip-remove';
            close.textContent = '×';
            close.setAttribute('role', 'button');
            close.setAttribute('aria-label', `Remove ${label} filter`);
            close.onclick = (e) => { e.stopPropagation(); onRemove(); };
            chip.appendChild(text);
            chip.appendChild(close);
            return chip;
        }

        function renderChips() {
            chipsInput.querySelectorAll('.sc-chip').forEach(c => c.remove());
            const chips = [];
            if (activeRiverFilter.state) {
                chips.push(makeChip(`State: ${activeRiverFilter.stateDisplay}`, () => {
                    activeRiverFilter.state = ''; activeRiverFilter.stateDisplay = '';
                    if (typeof window.clearStateHighlight === 'function') window.clearStateHighlight();
                    renderChips(); renderMarkers();
                }));
            }
            if (activeRiverFilter.river) {
                chips.push(makeChip(`River: ${activeRiverFilter.riverDisplay}`, () => {
                    activeRiverFilter.river = ''; activeRiverFilter.riverDisplay = '';
                    renderChips(); renderMarkers();
                }));
            }
            if (fatalityOnlyFilter) {
                chips.push(makeChip('Fatality Sites Only', () => {
                    fatalityOnlyFilter = false;
                    renderChips(); renderMarkers();
                }));
            }
            chips.forEach(c => chipsInput.insertBefore(c, input));
        }

        // --- Suggestions / autocomplete ----------------------------------------------
        function hideSuggestions() { suggestions.style.display = 'none'; suggestions.innerHTML = ''; }

        function suggestionHeader(text) {
            const h = document.createElement('div');
            h.className = 'sc-suggestions-header';
            h.textContent = text;
            return h;
        }

        function showFloaterList(rows, emptyMsg) {
            suggestions.innerHTML = '';
            if (rows.length === 0) {
                suggestions.appendChild(suggestionHeader(emptyMsg));
            } else {
                rows.forEach(row => suggestions.appendChild(row));
            }
            layoutFloater(suggestions, 'min(60vh, 320px)');
            suggestions.style.display = 'block';
        }

        function showGeneralSuggestions(val) {
            const matches = allDams.filter(d => damPassesChips(d) && (
                (d.Dam_Name || '').toLowerCase().includes(val) ||
                (d.OSM_Name || '').toLowerCase().includes(val) ||
                (d.City || '').toLowerCase().includes(val) ||
                (d['State Abbreviation'] || '').toLowerCase().includes(val)
            ));

            const rows = [];
            if (matches.length > 0) {
                rows.push(suggestionHeader(`Showing top ${Math.min(10, matches.length)} of ${matches.length} results`));
            }
            matches.slice(0, 10).forEach(dam => {
                const row = document.createElement('div');
                row.className = 'sc-suggestion-row';
                const place = (dam.City && dam.City.trim()) || (dam['County Name'] && dam['County Name'].trim()) || '';
                const loc = [place, dam['State Abbreviation']].filter(Boolean).join(', ');
                row.innerHTML = `<strong>${displayName(dam)}</strong><br><span class="sc-suggestion-sub">${loc}</span>`;
                row.onclick = () => {
                    const lat = parseFloat(dam.Latitude);
                    const lng = parseFloat(dam.Longitude);
                    if (!isNaN(lat) && !isNaN(lng)) {
                        map.flyTo([lat, lng], 14, { duration: 1.5 });
                        markers.eachLayer(l => { if (l.getLatLng().lat === lat && l.getLatLng().lng === lng) l.openPopup(); });
                    }
                    input.value = '';
                    hideSuggestions();
                };
                rows.push(row);
            });
            showFloaterList(rows, 'No matching dams found');
        }

        function showRiverSuggestions(val) {
            const seen = new Map(); // lowercase river name -> original-cased display text
            allDams.forEach(d => {
                if (activeRiverFilter.state && (d['State Abbreviation'] || '').toUpperCase() !== activeRiverFilter.state) return;
                if (fatalityOnlyFilter && (parseInt(d.NumberOfFatalities) || 0) === 0) return;
                [d.GNIS_Name, d['River/Stream'], d.River].forEach(name => {
                    const trimmed = (name || '').trim();
                    if (!trimmed) return;
                    const lower = trimmed.toLowerCase();
                    if (val && !lower.startsWith(val)) return;
                    if (!seen.has(lower)) seen.set(lower, trimmed);
                });
            });
            const options = Array.from(seen.values()).sort((a, b) => a.localeCompare(b)).slice(0, 8);
            const rows = options.map(opt => {
                const row = document.createElement('div');
                row.className = 'sc-suggestion-row';
                row.textContent = opt;
                row.onclick = () => {
                    activeRiverFilter.river = opt.toLowerCase();
                    activeRiverFilter.riverDisplay = opt;
                    input.value = '';
                    setMode('general');
                    renderChips();
                    renderMarkers();
                };
                return row;
            });
            showFloaterList(rows, 'No matching rivers');
        }

        function showStateSuggestions(val) {
            const options = US_STATES.filter(([abbr, name]) =>
                !val || abbr.toLowerCase().startsWith(val) || name.toLowerCase().startsWith(val)
            ).slice(0, 8);
            const rows = options.map(([abbr, name]) => {
                const row = document.createElement('div');
                row.className = 'sc-suggestion-row';
                row.innerHTML = `<strong>${abbr}</strong> <span class="sc-suggestion-sub">${name}</span>`;
                row.onclick = () => {
                    activeRiverFilter.state = abbr;
                    activeRiverFilter.stateDisplay = abbr;
                    input.value = '';
                    setMode('general');
                    renderChips();
                    renderMarkers();
                    if (typeof window.highlightStateBoundary === 'function') window.highlightStateBoundary(abbr);
                };
                return row;
            });
            showFloaterList(rows, 'No matching states');
        }

        input.addEventListener('focus', () => {
            hideCategoryMenu();
            const val = input.value.trim().toLowerCase();
            // River/State are typeahead-only: no browse-everything list until you start typing.
            if (mode === 'river') { if (val) showRiverSuggestions(val); else hideSuggestions(); }
            else if (mode === 'state') { if (val) showStateSuggestions(val); else hideSuggestions(); }
            else if (val.length >= 2) showGeneralSuggestions(val);
        });

        input.addEventListener('input', () => {
            const val = input.value.trim().toLowerCase();
            if (mode === 'river') { if (val) showRiverSuggestions(val); else hideSuggestions(); return; }
            if (mode === 'state') { if (val) showStateSuggestions(val); else hideSuggestions(); return; }
            if (val.length < 2) { hideSuggestions(); return; }
            showGeneralSuggestions(val);
        });

        input.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            const raw = input.value.trim();
            if (!raw) return;
            if (mode === 'river') {
                activeRiverFilter.river = raw.toLowerCase();
                activeRiverFilter.riverDisplay = raw;
                input.value = '';
                setMode('general');
                renderChips();
                renderMarkers();
            } else if (mode === 'state') {
                const match = US_STATES.find(([abbr, name]) => abbr.toLowerCase() === raw.toLowerCase() || name.toLowerCase() === raw.toLowerCase());
                if (!match) return;
                activeRiverFilter.state = match[0];
                activeRiverFilter.stateDisplay = match[0];
                input.value = '';
                setMode('general');
                renderChips();
                renderMarkers();
                if (typeof window.highlightStateBoundary === 'function') window.highlightStateBoundary(match[0]);
            }
        });

        function onResize() {
            if (categoryMenu.style.display === 'block') layoutFloater(categoryMenu, '50vh');
            if (suggestions.style.display === 'block') layoutFloater(suggestions, 'min(60vh, 320px)');
        }
        window.addEventListener('resize', onResize);

        // --- External API used elsewhere (state-boundary clicks, Escape key) --------
        window.setStateFilter = (abbr) => {
            activeRiverFilter.state = abbr.toUpperCase();
            activeRiverFilter.stateDisplay = abbr.toUpperCase();
            renderChips();
            renderMarkers();
        };
        window.clearStateFilterChip = () => {
            activeRiverFilter.state = '';
            activeRiverFilter.stateDisplay = '';
            renderChips();
            renderMarkers();
        };
        window.getStateFilter = () => activeRiverFilter.state;
        window.clearAllFilters = () => {
            activeRiverFilter = { river: '', riverDisplay: '', state: '', stateDisplay: '' };
            fatalityOnlyFilter = false;
            input.value = '';
            setMode('general');
            renderChips();
            renderMarkers();
        };
        window.openSearchPanel = () => setTimeout(() => input.focus(), 0);
        window.closeSearchPanel = () => { hideCategoryMenu(); hideSuggestions(); };

        renderChips();

        return () => window.removeEventListener('resize', onResize);
}

// --- Desktop filter UI: filter-icon button + dropdown panel (search results, River/State
// text filters with Apply/Clear, fatality checkbox) - mirrors the pre-makeover main branch.
function buildDesktopSearchUI(container) {
        const button = L.DomUtil.create('a', '', container);
        button.href = '#';
        button.title = 'Search dams';
        button.setAttribute('aria-label', 'Search dams');
        button.innerHTML = ICON_FILTER;

        const panel = L.DomUtil.create('div', '', container);
        panel.style.display = 'block'; // Start opened on non-mobile screens (this function only runs when !isMobile)
        panel.style.position = 'absolute';
        panel.style.top = '0';
        panel.style.left = 'calc(100% + 6px)';
        panel.style.background = 'white';
        panel.style.padding = '8px';
        panel.style.borderRadius = '4px';
        panel.style.boxShadow = '0 1px 5px rgba(0,0,0,0.4)';
        panel.style.minWidth = '240px';

        const input = L.DomUtil.create('input', '', panel);
        input.id = 'globalSearchInput';
        input.type = 'text';
        input.placeholder = 'Search dams, city, state...';
        input.style.width = '100%';
        input.style.padding = '6px';
        input.style.boxSizing = 'border-box';
        input.style.border = '1px solid #ccc';
        input.style.borderRadius = '3px';

        const resultsDiv = L.DomUtil.create('div', '', panel);
        resultsDiv.id = 'globalSearchResults';
        resultsDiv.style.maxHeight = '240px';
        resultsDiv.style.overflowY = 'auto';
        resultsDiv.style.marginTop = '6px';

        L.DomEvent.on(button, 'click', (e) => {
            L.DomEvent.preventDefault(e);
            const isOpen = panel.style.display === 'block';
            panel.style.display = isOpen ? 'none' : 'block';
            if (!isOpen) setTimeout(() => input.focus(), 0);
        });

        input.addEventListener('input', (e) => {
            const val = e.target.value.toLowerCase();
            resultsDiv.innerHTML = '';

            // Wait for at least 2 characters before querying
            if (val.length < 2) return;

            // Find all matches across the dataset
            const matches = allDams.filter(d => damPassesChips(d) && (
                (d.Dam_Name || '').toLowerCase().includes(val) ||
                (d.OSM_Name || '').toLowerCase().includes(val) ||
                (d.City || '').toLowerCase().includes(val) ||
                (d['State Abbreviation'] || '').toLowerCase().includes(val)
            ));

            const countHeader = document.createElement('div');
            countHeader.style.cssText = 'font-size: 11px; font-weight: bold; color: #7f8c8d; padding: 4px 5px 6px; border-bottom: 1px solid #eee; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.03em;';

            if (matches.length === 0) {
                countHeader.textContent = 'No matching dams found';
                resultsDiv.appendChild(countHeader);
                return;
            } else {
                const shown = Math.min(10, matches.length);
                countHeader.textContent = `Showing top ${shown} of ${matches.length} results`;
                resultsDiv.appendChild(countHeader);
            }

            matches.slice(0, 10).forEach(dam => {
                const div = document.createElement('div');
                div.style.padding = '6px 8px';
                div.style.cursor = 'pointer';
                div.style.borderBottom = '1px solid #f9f9f9';
                div.style.fontSize = '12px';
                div.style.borderRadius = '3px';
                div.style.transition = 'background-color 0.15s ease';

                const place = (dam.City && dam.City.trim()) || (dam['County Name'] && dam['County Name'].trim()) || '';
                const loc = [place, dam['State Abbreviation']].filter(Boolean).join(', ');

                div.innerHTML = `<strong>${displayName(dam)}</strong><br><span style="color:#7f8c8d; font-size: 11px;">${loc}</span>`;

                div.onmouseover = () => div.style.backgroundColor = '#f0f4f8';
                div.onmouseout = () => div.style.backgroundColor = 'transparent';

                div.onclick = () => {
                    const lat = parseFloat(dam.Latitude);
                    const lng = parseFloat(dam.Longitude);
                    if (!isNaN(lat) && !isNaN(lng)) {
                        map.flyTo([lat, lng], 14, { duration: 1.5 });
                        markers.eachLayer(l => {
                            if (l.getLatLng().lat === lat && l.getLatLng().lng === lng) l.openPopup();
                        });
                    }
                    input.value = displayName(dam);
                    panel.style.display = 'none';
                };
                resultsDiv.appendChild(div);
            });
        });

        // --- River Filter section ---
        const divider = L.DomUtil.create('hr', '', panel);
        divider.style.cssText = 'margin:8px 0;border:none;border-top:1px solid #ddd;';

        const riverLabel = L.DomUtil.create('div', '', panel);
        riverLabel.style.cssText = 'font-size:11px;font-weight:bold;color:#555;margin-bottom:5px;letter-spacing:0.03em;';
        riverLabel.textContent = 'FILTER BY RIVER';

        const riverInput = L.DomUtil.create('input', '', panel);
        riverInput.id = 'globalRiverInput';
        riverInput.type = 'text';
        riverInput.placeholder = 'River name (e.g. South Platte)';
        riverInput.style.cssText = 'width:100%;padding:6px;box-sizing:border-box;border:1px solid #ccc;border-radius:3px;margin-bottom:4px;font-size:12px;';
        riverInput.value = activeRiverFilter.riverDisplay || '';

        const stateInput = L.DomUtil.create('input', '', panel);
        stateInput.id = 'globalStateInput';
        stateInput.type = 'text';
        stateInput.maxLength = 2;
        stateInput.placeholder = 'State abbr. (e.g. CO)';
        stateInput.style.cssText = 'width:100%;padding:6px;box-sizing:border-box;border:1px solid #ccc;border-radius:3px;margin-bottom:6px;font-size:12px;text-transform:uppercase;';
        stateInput.value = activeRiverFilter.stateDisplay || '';

        const btnRow = L.DomUtil.create('div', '', panel);
        btnRow.style.cssText = 'display:flex;gap:4px;margin-bottom:4px;';

        const applyBtn = L.DomUtil.create('button', '', btnRow);
        applyBtn.id = 'globalApplyFilterBtn';
        applyBtn.textContent = 'Apply Filter';
        applyBtn.style.cssText = 'flex:1;padding:6px;background:#3498db;color:white;border:none;border-radius:3px;cursor:pointer;font-size:12px;font-weight:600;';

        const clearBtn = L.DomUtil.create('button', '', btnRow);
        clearBtn.textContent = 'Clear';
        clearBtn.style.cssText = 'padding:6px 10px;background:#95a5a6;color:white;border:none;border-radius:3px;cursor:pointer;font-size:12px;';

        const filterStatus = L.DomUtil.create('div', '', panel);
        filterStatus.style.cssText = 'font-size:11px;color:#666;min-height:15px;';

        const filterDivider = L.DomUtil.create('hr', '', panel);
        filterDivider.style.cssText = 'margin:8px 0;border:none;border-top:1px solid #ddd;';

        const optionsLabel = L.DomUtil.create('div', '', panel);
        optionsLabel.style.cssText = 'font-size:11px;font-weight:bold;color:#555;margin-bottom:5px;letter-spacing:0.03em;';
        optionsLabel.textContent = 'FILTER OPTIONS';

        const checkboxLabel = L.DomUtil.create('label', '', panel);
        checkboxLabel.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;user-select:none;';

        const fatalityCheckbox = L.DomUtil.create('input', '', checkboxLabel);
        fatalityCheckbox.type = 'checkbox';
        fatalityCheckbox.id = 'fatalityFilter';
        fatalityCheckbox.style.cursor = 'pointer';
        fatalityCheckbox.checked = fatalityOnlyFilter;

        const checkboxText = document.createTextNode('Show only fatality sites');
        checkboxLabel.appendChild(checkboxText);

        // fatalityOnlyFilter is the same global damPassesChips()/renderMarkers()/badge read,
        // so toggling this checkbox behaves identically to the mobile chip menu's option.
        fatalityCheckbox.addEventListener('change', () => {
            fatalityOnlyFilter = fatalityCheckbox.checked;
            renderMarkers();
        });

        function _damMatchesRiverFilter(d, rq, sq) {
            if (EXCLUDED_REVIEW_STATUSES.has((d.Review_Status || '').trim())) return false;
            if (!d['State Abbreviation']) return false;
            if (rq) {
                const gnis   = (d.GNIS_Name        || '').toLowerCase();
                const stream = (d['River/Stream']  || '').toLowerCase();
                const river  = (d.River            || '').toLowerCase();
                if (!gnis.includes(rq) && !stream.includes(rq) && !river.includes(rq)) return false;
            }
            if (sq && (d['State Abbreviation'] || '').toUpperCase() !== sq) return false;
            return true;
        }

        function applyRiverFilter() {
            const rq = riverInput.value.trim().toLowerCase();
            const sq = stateInput.value.trim().toUpperCase();
            activeRiverFilter = { river: rq, riverDisplay: riverInput.value.trim(), state: sq, stateDisplay: sq };
            renderMarkers();

            if (typeof window.highlightStateBoundary === 'function') {
                window.highlightStateBoundary(sq);
            }

            if (!rq && !sq) {
                filterStatus.textContent = '';
                return;
            }

            const matched = allDams.filter(d => _damMatchesRiverFilter(d, rq, sq));
            const withCoords = matched.filter(d => !isNaN(parseFloat(d.Latitude)) && !isNaN(parseFloat(d.Longitude)));

            filterStatus.textContent = `${withCoords.length} dam${withCoords.length !== 1 ? 's' : ''} matched`;

            if (withCoords.length > 0) {
                const bounds = L.latLngBounds(
                    withCoords.map(d => [parseFloat(d.Latitude), parseFloat(d.Longitude)])
                );
                map.flyToBounds(bounds, { padding: [60, 60], maxZoom: 13, duration: 1.5 });
            }
        }

        L.DomEvent.on(applyBtn, 'click', (e) => { L.DomEvent.preventDefault(e); applyRiverFilter(); });

        L.DomEvent.on(clearBtn, 'click', (e) => {
            L.DomEvent.preventDefault(e);
            riverInput.value = '';
            stateInput.value = '';
            activeRiverFilter = { river: '', riverDisplay: '', state: '', stateDisplay: '' };
            filterStatus.textContent = '';
            renderMarkers();
            if (typeof window.clearStateHighlight === 'function') {
                window.clearStateHighlight();
            }
        });

        L.DomEvent.on(riverInput, 'keydown', (e) => { if (e.key === 'Enter') applyRiverFilter(); });
        L.DomEvent.on(stateInput, 'keydown', (e) => { if (e.key === 'Enter') applyRiverFilter(); });

        // --- External API used elsewhere (state-boundary clicks, Escape key) --------
        window.setStateFilter = (abbr) => {
            activeRiverFilter.state = abbr.toUpperCase();
            activeRiverFilter.stateDisplay = abbr.toUpperCase();
            stateInput.value = activeRiverFilter.stateDisplay;
            renderMarkers();
        };
        window.clearStateFilterChip = () => {
            activeRiverFilter.state = '';
            activeRiverFilter.stateDisplay = '';
            stateInput.value = '';
            renderMarkers();
        };
        window.getStateFilter = () => activeRiverFilter.state;
        window.clearAllFilters = () => {
            activeRiverFilter = { river: '', riverDisplay: '', state: '', stateDisplay: '' };
            fatalityOnlyFilter = false;
            input.value = '';
            resultsDiv.innerHTML = '';
            riverInput.value = '';
            stateInput.value = '';
            fatalityCheckbox.checked = false;
            filterStatus.textContent = '';
            renderMarkers();
        };
        window.openSearchPanel = () => {
            panel.style.display = 'block';
            setTimeout(() => input.focus(), 0);
        };
        window.closeSearchPanel = () => { panel.style.display = 'none'; };

        return null;
}

const searchControlInstance = new SearchControl();
map.addControl(searchControlInstance);

// Attach the layers control underneath the search button, restyled to match
// the same leaflet-bar button size/icon treatment as the other toolbar buttons
layerControl.addTo(map);
const layersToggle = layerControl.getContainer().querySelector('.leaflet-control-layers-toggle');
if (layersToggle) {
    layerControl.getContainer().classList.add('leaflet-bar');
    layersToggle.style.background = 'none';
    layersToggle.innerHTML = ICON_LAYERS;
    layersToggle.title = 'Change map layers';
    layersToggle.setAttribute('aria-label', 'Change map layers');
}

// --- Layers control: click-to-expand/collapse on mobile instead of hover ---
// Leaflet expands this control on mouseenter/collapses on mouseleave by default. On
// touch screens there's no real hover, and iOS Safari's synthetic-hover-on-first-tap
// quirk can make it feel like you have to tap twice. Below 768px we strip that hover
// binding so opening the list is a deliberate tap on the icon; tapping elsewhere on the
// map still collapses it (that's wired to a map click listener, not hover, so it's
// unaffected). Desktop keeps the original hover behavior.
(function mobileLayersClickOnly() {
    const layersContainer = layerControl.getContainer();
    if (!layersContainer || !layersToggle) return;
    let hoverStripped = false;

    function applyMode() {
        const isMobile = window.innerWidth <= 768;
        if (isMobile && !hoverStripped) {
            L.DomEvent.off(layersContainer, { mouseenter: layerControl._expandSafely, mouseleave: layerControl.collapse }, layerControl);
            hoverStripped = true;
        } else if (!isMobile && hoverStripped) {
            L.DomEvent.on(layersContainer, { mouseenter: layerControl._expandSafely, mouseleave: layerControl.collapse }, layerControl);
            hoverStripped = false;
        }
    }

    applyMode();
    window.addEventListener('resize', applyMode);

    // Leaflet hides the toggle icon itself (display:none) while expanded, so there's no
    // "tap the icon again" option - and tapping elsewhere on the map to close isn't
    // discoverable. Add an explicit close button inside the expanded panel (mobile only;
    // see .leaflet-layers-close-btn in styles.css - desktop still closes via mouseleave).
    const layersSection = layersContainer.querySelector('.leaflet-control-layers-list');
    if (layersSection) {
        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'leaflet-layers-close-btn';
        closeBtn.setAttribute('aria-label', 'Close layers menu');
        closeBtn.innerHTML = '&times;';
        closeBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            layerControl.collapse();
        });
        layersSection.insertBefore(closeBtn, layersSection.firstChild);
    }
})();

// --- Dock the search/filter control next to the hamburger menu on mobile ---
// On phones the search button used to float over the map like the other toolbar
// buttons; opening its panel there ate most of the screen. Below 768px we move the
// same control (button + panel, untouched) into the nav bar so it sits beside the
// hamburger, Google-Maps-style, and its dropdown panel (see layoutSearchPanel above)
// stays capped in height so the map is still visible while filtering.
(function dockSearchControl() {
    const mobileSlot = document.getElementById('mobile-search-slot');
    const searchContainer = searchControlInstance.getContainer();
    const desktopCorner = searchContainer.parentNode; // .leaflet-top.leaflet-left
    if (!mobileSlot || !desktopCorner) return;

    function place() {
        const isMobile = window.innerWidth <= 768;
        const alreadyDocked = isMobile
            ? searchContainer.parentNode === mobileSlot
            : searchContainer.parentNode === desktopCorner;
        if (alreadyDocked) return;

        if (isMobile) {
            mobileSlot.appendChild(searchContainer);
        } else {
            desktopCorner.insertBefore(searchContainer, layerControl.getContainer());
        }
        // Moving the control across the breakpoint invalidates any open panel's
        // fixed-position coordinates, so close it rather than leave it misplaced.
        if (typeof window.closeSearchPanel === 'function') window.closeSearchPanel();
    }

    place();
    window.addEventListener('resize', place);
})();

// --- Active Filters Badge ---
const ActiveFiltersControl = L.Control.extend({
    options: { position: 'bottomleft' },
    onAdd: function() {
        const container = L.DomUtil.create('div', 'leaflet-control');
        container.id = 'activeFiltersBadge';
        container.style.cssText = 'display:none; background:rgba(255,255,255,0.95); padding:8px 12px; border-radius:4px; box-shadow:0 1px 5px rgba(0,0,0,0.4); font-size:12px; font-weight:bold; color:#2c3e50; cursor:pointer; border-left:4px solid #3498db; margin-bottom: 20px; transition: all 0.2s ease;';
        container.title = 'Click to clear all filters';
        
        L.DomEvent.disableClickPropagation(container);
        
        container.onmouseover = () => container.style.background = '#f8f9fa';
        container.onmouseout = () => container.style.background = 'rgba(255,255,255,0.95)';
        
        container.onclick = (e) => {
            L.DomEvent.stopPropagation(e);
            // Trigger the global escape key logic to clear everything
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        };
        
        return container;
    }
});
map.addControl(new ActiveFiltersControl());

window.updateActiveFiltersBadge = () => {
    const badge = document.getElementById('activeFiltersBadge');
    if (!badge) return;
    
    let active = [];
    if (activeRiverFilter.state) active.push(`State: ${activeRiverFilter.stateDisplay}`);
    if (activeRiverFilter.river) active.push(`River: ${activeRiverFilter.riverDisplay}`);
    if (fatalityOnlyFilter) active.push(`Fatalities Only`);
    
    if (active.length > 0) {
        badge.style.display = 'block';
        badge.innerHTML = `Active Filters: <span style="font-weight:normal; color:#555;">${active.join(' | ')}</span> <span style="color:#e74c3c; margin-left:12px; border-left: 1px solid #ccc; padding-left: 10px;">&times; Clear All</span>`;
    } else {
        badge.style.display = 'none';
    }
};

// 2. Load Dam Data
// --- dam table loader (Parquet) ---
// The dam table is data/full_lhd_website.parquet (it used to be a CSV read with
// d3.csv). Parquet stores text as UTF-8 and is about 5x smaller. It is read in
// the browser with hyparquet, a small dependency-free Parquet reader, pinned to
// one version and loaded only when needed (this file is a classic script, so a
// dynamic import() is used instead of an import statement).
const HYPARQUET_URL = 'https://cdn.jsdelivr.net/npm/hyparquet@1.32.0/+esm';

// One Parquet cell as the text d3.csv used to give, so the rest of this file
// (parseFloat, parseInt, string checks) works unchanged:
//   missing -> ''      whole-number columns arrive as BigInt -> '13080001'
//   numbers -> String  (78840.0 becomes '78840'; parseFloat gives the same value)
//   booleans -> 'True' / 'False' (how pandas wrote them in the CSV)
function damCellToText(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'bigint') return v.toString();
    if (typeof v === 'number') {
        if (Number.isNaN(v)) return '';
        if (v === Infinity) return 'inf';
        if (v === -Infinity) return '-inf';
        return String(v);
    }
    if (typeof v === 'boolean') return v ? 'True' : 'False';
    if (v instanceof Date) return v.toISOString();
    return String(v);
}

// Fetch the whole Parquet file (about 2 MB) and return one plain object per dam,
// keyed by column name with text values, like d3.csv returned.
async function loadDamTable(url) {
    const { parquetReadObjects } = await import(HYPARQUET_URL);
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status} loading ${url}`);
    const file = await resp.arrayBuffer();
    const rows = await parquetReadObjects({ file });
    return rows.map(r => {
        const out = {};
        for (const k of Object.keys(r)) out[k] = damCellToText(r[k]);
        return out;
    });
}
// --- end dam table loader ---

async function loadDams() {
    try {
        // Append a timestamp to the URL to force the browser to bypass its cache. This ensures that users always get the most up-to-date data without needing to manually clear their cache. In development, this is crucial to see changes immediately. In production, it guarantees that any updates to the dam data are reflected for all users without delay.
        allDams = await loadDamTable(`data/full_lhd_website.parquet?v=${new Date().getTime()}`);
        // NWM does not produce forecasts for Hawaii or Puerto Rico, so exclude those dams everywhere.
        const NON_CONUS_STATES = new Set(['HI', 'PR']);
        allDams = allDams.filter(d => !NON_CONUS_STATES.has((d['State Abbreviation'] || '').toUpperCase()));
        renderMarkers();
        console.log("Dam markers clustered and initialized.");
    } catch (err) { 
        console.error("Error loading the dam table (Parquet):", err); 
    }
}

// Helper to scale marker size based on zoom level
function getMarkerRadiusAndWeight() {
    const zoom = map.getZoom();
    if (zoom <= 5) return { radius: 3, weight: 0.5 };
    if (zoom <= 8) return { radius: 4, weight: 0.5 };
    if (zoom <= 11) return { radius: 6, weight: 1.0 };
    return { radius: 8, weight: 1.5 };
}

// Mobile-only invisible hit-target radius (px) - visible markers top out at radius 8
// (16px diameter), well under the ~44px touch-target guideline. Ignored on desktop,
// where a mouse pointer makes the small visible dot easy enough to click.
const MOBILE_HIT_RADIUS = 22;

// Dynamically resize markers whenever the map zoom level changes
map.on('zoomend', () => {
    const { radius, weight } = getMarkerRadiusAndWeight();
    markers.eachLayer(layer => {
        if (!layer.setRadius) return;
        if (layer.options.isHitTarget) {
            layer.setRadius(Math.max(radius, MOBILE_HIT_RADIUS));
            return;
        }
        layer.setRadius(radius);
        layer.setStyle({ weight: weight });
    });
});

// 3. Render Markers with Filter Logic
function renderMarkers() {
    markers.clearLayers();

    const { radius, weight } = getMarkerRadiusAndWeight();

    allDams.forEach(dam => {
        const lat = parseFloat(dam.Latitude);
        const lng = parseFloat(dam.Longitude);
        
        if (!isNaN(lat) && !isNaN(lng)) {
            // Filter out sites that don't have a US State abbreviation
            if (!dam["State Abbreviation"]) return;

            // Review-status exclusion + River/State/Fatality chip filters - see
            // damPassesChips() near the top of the file.
            if (!damPassesChips(dam)) return;

            const fatalities = parseInt(dam.NumberOfFatalities) || 0;

            // Backend writes the envelope in cms; NWPS forecasts are in cfs. Convert at parse.
            const CMS_TO_CFS = 35.3147;
            const qMinRaw = parseFloat(dam.Qmin_env) * CMS_TO_CFS;
            const qMaxRaw = parseFloat(dam.Qmax_env) * CMS_TO_CFS;

            // Rounded values for display only
            const _round = (v) => v >= 100 ? Math.round(v / 100) * 100 : Math.round(v);
            const qMinDisplay = _round(qMinRaw).toLocaleString();
            const qMaxDisplay = _round(qMaxRaw).toLocaleString();

            const hasComid = dam.Reach_ID !== undefined && dam.Reach_ID !== null && String(dam.Reach_ID).trim() !== '';
            const hasSafetyData = isFinite(qMinRaw) && isFinite(qMaxRaw) && hasComid;

            const _wspH = parseFloat(dam.Dam_Height_WSP_Ft);
            const _wspL = parseFloat(dam.Dam_Length_WSP_Ft);
            const hasNwmGeom = isFinite(_wspH) && _wspH > 0 && isFinite(_wspL) && _wspL > 0;

            // Determine marker color based on data priority tier
            let markerColor = '#7f8c8d'; // Default: Gray
            if (hasNwmGeom) {
                markerColor = '#e67e22'; // Priority 1: Orange (NWM height + width available)
            } else if (hasComid) {
                markerColor = '#2980b9'; // Priority 2: Blue (Live forecast available)
            }

            const place = (dam.City && dam.City.trim()) || (dam["County Name"] && dam["County Name"].trim()) || "Unknown location";
            const state = dam["State Abbreviation"] || "";
            const location = place + (state ? `, ${state}` : "");
            
            const marker = L.circleMarker([lat, lng], {
                radius: radius,
                fillColor: markerColor,
                color: '#1a252f', // Subtle dark border
                weight: weight,   // Scaled stroke
                opacity: 0.8,    // Slightly transparent stroke
                fillOpacity: 1  
            });

            const _displayName = displayName(dam);
            
            marker.bindTooltip(_displayName, { direction: 'top', offset: [0, -6] });
            
            let popupContent = `
                <div class="popup-content">
                    <strong>${_displayName}</strong><br>
                    <b>Location:</b> ${location}<br>
                    ${(fatalities > 0 && !hasSafetyData) ? `<b>Fatalities:</b> ${fatalities}<br>` : ''}
                    <hr>`;

            const heightFt = parseFloat(dam.Dam_Height_WSP_Ft);
            const lengthFt = parseFloat(dam.Dam_Length_WSP_Ft);

            const hasRatingCurves = isFinite(heightFt) && heightFt > 0
                && isFinite(lengthFt) && lengthFt > 0
                && hasComid;

            if (hasComid || hasRatingCurves) {
                if (hasSafetyData) {
                    popupContent += `<b>Dangerous Range:</b> ${qMinDisplay} - ${qMaxDisplay} cfs<br>`;
                    if (fatalities > 0) {
                        popupContent += `<b>Fatalities:</b> ${fatalities}<br>`;
                    }
                }
                const safetyArgs = hasSafetyData ? `${qMinRaw}, ${qMaxRaw}` : `null, null`;

                let onClickActions = ['openCombinedPanel()'];
                const summaryData = {
                    name: _displayName,
                    location,
                    heightFt: isFinite(heightFt) ? heightFt : null,
                    lengthFt: isFinite(lengthFt) ? lengthFt : null,
                    hasSafetyData,
                    qMinDisplay: hasSafetyData ? qMinDisplay : null,
                    qMaxDisplay: hasSafetyData ? qMaxDisplay : null,
                    fatalities,
                    hasComid,
                    hasRatingCurves,
                    reachId: hasComid ? String(dam.Reach_ID) : null,
                    qMinCms: hasSafetyData ? parseFloat(dam.Qmin_env) : null,
                    qMaxCms: hasSafetyData ? parseFloat(dam.Qmax_env) : null
                };
                onClickActions.push(`showSiteSummary(${jsAttrLiteral(JSON.stringify(summaryData))})`);
                if (hasSafetyData) onClickActions.push(`showDangerRangeSummary(${jsAttrLiteral(JSON.stringify(summaryData))})`);
                // Forecast graph disabled in Site Analysis (kept for easy re-enable):
                // if (hasComid) onClickActions.push(`checkForecast('${dam.Reach_ID}', ${safetyArgs}, ${jsAttrLiteral(_displayName)})`);
                if (hasRatingCurves) onClickActions.push(`showRatingCurves(${heightFt}, ${lengthFt}, '${dam.Reach_ID}', ${jsAttrLiteral(_displayName)})`);
                if (hasComid && !hasRatingCurves) onClickActions.push(`showSyntheticRatingCurve('${dam.Reach_ID}', ${jsAttrLiteral(_displayName)})`);
                if (hasComid) onClickActions.push(`showFlowDurationCurve('${dam.Reach_ID}', ${jsAttrLiteral(_displayName)}, ${safetyArgs})`);

                popupContent += `
                    <button class="btn-check" onclick="${onClickActions.join('; ')}">
                        Check Site Analysis
                    </button>`;
            } else {
                popupContent += `<i>No forecast or rating curve available for this site.</i>`;
            }

            popupContent += `</div>`;

            // Mobile: an invisible, larger circleMarker underneath the visible dot so the
            // tappable area meets touch-target guidelines without changing how the dot looks.
            // Added to the layer group first so it draws (and hit-tests) below the visible marker.
            if (window.innerWidth <= 768) {
                const hitMarker = L.circleMarker([lat, lng], {
                    radius: Math.max(radius, MOBILE_HIT_RADIUS),
                    stroke: false,
                    fill: true,
                    fillOpacity: 0,
                    isHitTarget: true
                });
                hitMarker.bindTooltip(_displayName, { direction: 'top', offset: [0, -6] });
                hitMarker.bindPopup(popupContent);
                // Forecast prefetch disabled along with the Site Analysis forecast graph:
                // if (hasComid) hitMarker.on('click', () => prefetchForecast(dam.Reach_ID));
                markers.addLayer(hitMarker);
            }

            marker.bindPopup(popupContent);
            // Forecast prefetch disabled along with the Site Analysis forecast graph:
            // if (hasComid) marker.on('click', () => prefetchForecast(dam.Reach_ID));
            markers.addLayer(marker);
        }
    });
    
    map.addLayer(markers); 

    if (typeof window.updateBoundaryZOrder === 'function') {
        window.updateBoundaryZOrder();
    }
    
    if (typeof window.updateActiveFiltersBadge === 'function') {
        window.updateActiveFiltersBadge();
    }
}

window.openCombinedPanel = () => {
    const cModal = document.getElementById('combinedModal');
    cModal.classList.add('is-open');

    if (window.innerWidth <= 768) {
        // Let the mobile @media rule size/position the panel full-screen;
        // clear any inline position from a prior desktop drag/resize.
        cModal.style.top = '';
        cModal.style.left = '';
        cModal.style.width = '';
        cModal.style.height = '';
        cModal.style.transform = '';
    } else if (cModal.style.transform !== 'none') {
        cModal.style.transform = 'none';
        cModal.style.top = '80px';
        cModal.style.left = 'calc(50% - 500px)';
    }
    document.getElementById('siteSummaryContainer').style.display = 'none';
    document.getElementById('dangerRangeContainer').style.display = 'none';
    document.getElementById('forecastContainer').style.display = 'none';
    document.getElementById('ratingCurvesContainer').style.display = 'none';
    document.getElementById('srcContainer').style.display = 'none';
    document.getElementById('fdcContainer').style.display = 'none';
};

// Static site facts shown in place of the (currently disabled) live forecast chart.
function showSiteSummary(dataStr) {
    const d = JSON.parse(dataStr);

    const rows = [`<b>Location:</b> ${d.location}`];
    if (d.heightFt != null) rows.push(`<b>Dam height:</b> ${d.heightFt.toFixed(1)} ft`);
    if (d.lengthFt != null) rows.push(`<b>Dam length:</b> ${d.lengthFt.toFixed(0)} ft`);
    if (d.hasSafetyData) rows.push(`<b>Dangerous flow range:</b> ${d.qMinDisplay} - ${d.qMaxDisplay} cfs`);
    if (d.fatalities > 0) rows.push(`<b>Known fatalities:</b> ${d.fatalities}`);
    if (d.reachId) rows.push(`<b>NHDPlus reach ID:</b> ${d.reachId}`);

    const analyses = [];
    if (d.hasRatingCurves) analyses.push('rating curves');
    else if (d.hasComid) analyses.push('synthetic rating curve');
    if (d.hasComid) analyses.push('flow duration curve');
    if (analyses.length) rows.push(`<b>Available analyses:</b> ${analyses.join(', ')}`);

    document.getElementById('siteSummaryContainer').style.display = 'flex';
    document.getElementById('siteSummaryHeader').innerHTML =
        `<strong>${d.name} &mdash; Site Summary</strong><br>${rows.join('<br>')}`;
}
window.showSiteSummary = showSiteSummary;

// Shown only for dams with a calculated dangerous flow range (Qmin_env/Qmax_env).
// Combines a plain-language hazard statement with the technical basis for the range,
// for readers who want to see how Qmin/Qmax were derived.
function showDangerRangeSummary(dataStr) {
    const d = JSON.parse(dataStr);
    if (!d.hasSafetyData) return;

    const fatalityNote = d.fatalities > 0
        ? ` This dam has ${d.fatalities} documented fatalit${d.fatalities === 1 ? 'y' : 'ies'}.`
        : '';

    const hazardStatement =
        `At discharges of <b>${d.qMinDisplay}–${d.qMaxDisplay} cfs</b>, the tailwater at this dam sits ` +
        `between the hydraulic jump's conjugate depth and its flip (boil) depth &mdash; the flow range in which ` +
        `a submerged recirculating roller can form at the base of the dam and trap a person underwater. ` +
        `Recreators should avoid this reach when streamflow falls in this range.${fatalityNote}`;

    const technicalRows = [];
    if (d.heightFt != null && d.lengthFt != null) {
        technicalRows.push(`Dam height (P): ${d.heightFt.toFixed(1)} ft &nbsp;·&nbsp; Dam length (L): ${d.lengthFt.toFixed(0)} ft`);
    }
    technicalRows.push(`Tailwater stage-discharge relationship: NOAA National Water Model synthetic rating curve, reach ${d.reachId}`);
    technicalRows.push(`Danger condition: tailwater depth y<sub>t</sub>(Q) between conjugate depth y₂(Q) and flip depth y<sub>flip</sub>(Q)`);
    if (d.qMinCms != null && d.qMaxCms != null && isFinite(d.qMinCms) && isFinite(d.qMaxCms)) {
        technicalRows.push(
            `Q<sub>min</sub> = ${d.qMinDisplay} cfs (${d.qMinCms.toFixed(1)} cms) &nbsp;·&nbsp; ` +
            `Q<sub>max</sub> = ${d.qMaxDisplay} cfs (${d.qMaxCms.toFixed(1)} cms)`
        );
    }

    document.getElementById('dangerRangeContainer').style.display = 'flex';
    document.getElementById('dangerRangeHeader').innerHTML =
        `<span style="color:red; font-weight:bold;">⚠️ WARNING: DANGER ZONE CALCULATED FOR THIS DAM ⚠️</span><br>` +
        `<strong>${d.name} &mdash; Danger Zone Summary</strong><br>` +
        `${hazardStatement}<br><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">` +
        `<u>How this range was calculated:</u><br>${technicalRows.join('<br>')}` +
        `</span>`;
}
window.showDangerRangeSummary = showDangerRangeSummary;

// 4. National Water Model Forecast (NOAA NWPS API, NHDPlus V2 COMID = Reach_ID)
// Medium-range only: ~10d 3-hourly ensemble mean + member spread as uncertainty band.
// Units: API returns ft³/s (cfs) — no conversion needed.

function prefetchForecast(comid) {
    comid = String(comid).replace(/\.0+$/, '');
    if (!comid || _forecastCache.has(comid)) return;
    _forecastCache.set(comid,
        fetch(`https://api.water.noaa.gov/nwps/v1/reaches/${comid}/streamflow?series=medium_range`)
            .then(r => r.json())
            .catch(() => null)
    );
}

async function checkForecast(comid, qMin, qMax, damName) {
    // Reach_ID arrives as a pandas-style float string ("10376596.0") — NWPS wants an integer.
    comid = String(comid).replace(/\.0+$/, '');
    const hasSafetyRange = qMin !== null && !isNaN(qMin) && qMax !== null && !isNaN(qMax);

    // FIX: Clear out the old header immediately so it doesn't distract the user while loading
    document.getElementById('statusDisplay').innerHTML = `<strong>${damName} Forecast</strong><br><span style="color:#888;">Loading forecast details...</span>`;

    // Show modal + spinner immediately
    document.getElementById('forecastContainer').style.display = 'flex';
    document.getElementById('forecastSpinner').style.display = 'block';
    document.getElementById('forecastChart').style.display = 'none';

    if (forecastChart) { forecastChart.destroy(); forecastChart = null; }

    try {
        const mrData = await (
            _forecastCache.get(comid) ??
            fetch(`https://api.water.noaa.gov/nwps/v1/reaches/${comid}/streamflow?series=medium_range`)
                .then(r => r.json())
        );

        const mrMean    = mrData.mediumRange?.mean?.data ?? [];
        const mrMembers = ['member1','member2','member3','member4','member5','member6']
            .map(k => mrData.mediumRange?.[k]?.data ?? []);

        // Floor current time to the nearest hour
        const nowFloor = new Date();
        nowFloor.setMinutes(0, 0, 0);
        const nowMs = nowFloor.getTime();

        const rawPoints = mrMean.map((p, i) => {
            const upper = Math.max(...mrMembers.map(m => m[i]?.flow ?? p.flow));
            const lower = Math.min(...mrMembers.map(m => m[i]?.flow ?? p.flow));
            return {
                validTime: p.validTime,
                validMs:   new Date(p.validTime).getTime(),
                flow:  p.flow,
                upper: Math.max(upper, p.flow),
                lower: Math.min(lower, p.flow)
            };
        });

        // Interpolate a synthetic point at the current floored hour
        const afterIdx = rawPoints.findIndex(p => p.validMs > nowMs);
        let allPoints;
        if (afterIdx > 0) {
            const p0 = rawPoints[afterIdx - 1];
            const p1 = rawPoints[afterIdx];
            const t  = (nowMs - p0.validMs) / (p1.validMs - p0.validMs);
            const lerp = (a, b) => a + t * (b - a);
            const synthetic = {
                validTime: nowFloor.toISOString(),
                validMs:   nowMs,
                flow:  lerp(p0.flow,  p1.flow),
                upper: lerp(p0.upper, p1.upper),
                lower: lerp(p0.lower, p1.lower)
            };
            allPoints = [synthetic, ...rawPoints.filter(p => p.validMs > nowMs)];
        } else {
            allPoints = rawPoints.filter(p => p.validMs >= nowMs);
        }

        if (allPoints.length === 0) {
            document.getElementById('forecastSpinner').style.display = 'none';
            document.getElementById('statusDisplay').innerHTML =
                `<strong>${damName}</strong><br>No forecast data returned for reach ${comid}.`;
            return;
        }

        _forecastState = { allPoints, hasSafetyRange, qMin, qMax, damName };

        const slider = document.getElementById('forecastSlider');
        const maxDays = Math.floor(
            (new Date(allPoints[allPoints.length - 1].validTime) - new Date(allPoints[0].validTime)) / 86400000
        );
        slider.max = maxDays;
        slider.value = Math.min(5, maxDays);
        document.getElementById('forecastSliderWrap').style.display = 'block';

        _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName, parseInt(slider.value));

    } catch (err) {
        console.error("NWM API Error:", err);
        document.getElementById('forecastSpinner').style.display = 'none';
        document.getElementById('statusDisplay').innerHTML =
            `<strong>${damName}</strong><br>Error fetching NWM forecast data.`;
    }
}

function _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName, days = 5) {
    const cutoff = new Date(allPoints[0].validTime).getTime() + days * 86400000;
    const points = allPoints.filter(p => new Date(p.validTime).getTime() <= cutoff);
    const allFlow  = points.map(p => p.flow);
    const allUpper = points.map(p => p.upper);
    const allLower = points.map(p => p.lower);
    const currentCfs = allFlow[0];
    const labels = points.map(p =>
        new Date(p.validTime).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' })
    );

    let statusText = `<strong>${damName} Forecast</strong><br>`;
    statusText += `Current Forecast: ${currentCfs != null ? currentCfs.toFixed(0) : 'N/A'} cfs`;

    if (hasSafetyRange) {
        statusText += ` | Dangerous Range: ${qMin.toFixed(0)}-${qMax.toFixed(0)} cfs<br>`;
        const isAnyDangerous = allFlow.some(v => v != null && v >= qMin && v <= qMax);
        statusText += isAnyDangerous
            ? `<span style="color:red; font-weight:bold;">⚠️ WARNING: DANGEROUS CONDITIONS FORECASTED ⚠️</span>`
            : `<span style="color:green; font-weight:bold;">✅ Status: Safe for Forecast Period</span>`;
    } else {
        statusText += `<br><span style="color:#555;">No dangerous flow range on record for this site.</span>`;
    }
    document.getElementById('statusDisplay').innerHTML = statusText;
    document.getElementById('forecastSpinner').style.display = 'none';
    document.getElementById('forecastChart').style.display = 'block';

    const datasets = [];
    if (hasSafetyRange) {
        // Red filled box for the legend, bounded by a thicker, dashed line
        datasets.push({
            label: 'Dangerous Flow Range',
            data: Array(allFlow.length).fill(qMax), 
            borderColor: '#e74c3c', 
            borderWidth: 3,             // Thicker line
            borderDash: [8, 4],         // Dashed formatting
            pointRadius: 0, 
            fill: {
                target: {value: qMin},
                above: 'rgba(231, 76, 60, 0.25)',
                below: 'rgba(231, 76, 60, 0.25)'
            },
            backgroundColor: 'rgba(231, 76, 60, 0.25)',
            pointStyle: 'rect'
        });
    }
    
    datasets.push(
        // Core National Water Model Forecast Trend line (cfs removed from label)
        { 
            label: 'NWM Forecast', 
            data: allFlow, 
            borderColor: '#000000', 
            backgroundColor: '#000000', 
            fill: false, 
            tension: 0.2, 
            borderWidth: 3, 
            pointRadius: 0,
            pointStyle: 'line'
        },
        // Upper Bound tracker representing Flow Uncertainty (Ensemble string stripped)
        { 
            label: 'Flow Uncertainty', 
            data: allUpper, 
            borderColor: '#3498db', 
            borderWidth: 1, 
            pointRadius: 0, 
            fill: '+1', // FIX: Forces the blue fill to stretch all the way down to the next dataset (Lower Bound)
            backgroundColor: 'rgba(52, 152, 219, 0.25)', 
            tension: 0.2,
            pointStyle: 'rect'
        },
        // Lower Bound tracker used explicitly to catch the bottom edge of the uncertainty fill
        { 
            label: 'Uncertainty Lower Bound', 
            data: allLower, 
            borderColor: '#3498db', 
            borderWidth: 1, 
            pointRadius: 0, 
            fill: false, 
            tension: 0.2,
            showLine: true,
            plugins: {
                legend: {
                    display: false 
                }
            }
        }
    );

    const validUppers = allUpper.filter(v => v != null && !isNaN(v));
    const maxForecast = validUppers.length > 0 ? Math.max(...validUppers) : 10;
    const yAxisMax = Math.max(maxForecast, 10) * 1.2;

    const ctx = document.getElementById('forecastChart').getContext('2d');
    if (forecastChart) forecastChart.destroy();
    forecastChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                filler: { propagate: true },
                legend: {
                    labels: {
                        usePointStyle: true,
                        filter: function(item) {
                            // Suppress the raw lower boundary line from appearing in the legend item array
                            return item.text !== 'Uncertainty Lower Bound';
                        }
                    }
                }
            },
            scales: {
                y: {
                    title: { display: true, text: 'Streamflow (cfs)' },
                    max: yAxisMax
                },
                x: { ticks: { maxTicksLimit: 10 } }
            }
        }
    });
}

// 4b. Rating curves (tailwater / conjugate / flip) computed client-side.
// Tailwater comes from the NWM synthetic rating curve (SRC) for the dam's reach.
// Display: Q in cfs on x-axis, depth in ft on y-axis.
async function showRatingCurves(heightFt, lengthFt, comid, damName) {
    document.getElementById('ratingCurvesContainer').style.display = 'flex';
    document.getElementById('ratingCurvesHeader').innerHTML =
        `<strong>${damName} Rating Curve</strong><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">Loading SRC…</span>`;

    const comidStr = String(comid).replace(/\.0+$/, '');
    const curve = await _loadSrcData(comidStr);

    if (!curve || !curve.discharge_cms || !curve.stage_m || curve.discharge_cms.length < 2) {
        document.getElementById('ratingCurvesHeader').innerHTML =
            `<strong>${damName} Rating Curve</strong><br>` +
            `<span style="color:#7f8c8d; font-size: 12px;">No SRC available for reach ${comidStr}.</span>`;
        return;
    }

    const srcQs  = curve.discharge_cms;
    const srcDs  = curve.stage_m;
    const qMaxCms = srcQs[srcQs.length - 1];
    const twFn = (Q) => window.LHDHydraulics.interpLinear(Q, srcQs, srcDs);

    const P = heightFt / window.LHDHydraulics.constants.M_TO_FT;
    const L = lengthFt / window.LHDHydraulics.constants.M_TO_FT;
    const CMS_TO_CFS = window.LHDHydraulics.constants.CMS_TO_CFS;
    const M_TO_FT = window.LHDHydraulics.constants.M_TO_FT;
    const ERROR_TOLERANCE_CFS = 0.0005;

    const qMinCms = srcQs[0];
    const { tailwater, conjugate, flip, dangerConj, dangerFlip } =
        window.LHDHydraulics.buildRatingCurvesFt(heightFt, lengthFt, twFn, qMaxCms, 500, qMinCms);

    const intersections = [];
    for (let i = 0; i < tailwater.length - 1; i++) {
        if (conjugate[i].y !== null && conjugate[i+1].y !== null && tailwater[i].y !== null && tailwater[i+1].y !== null) {
            const diff1 = tailwater[i].y - conjugate[i].y;
            const diff2 = tailwater[i+1].y - conjugate[i+1].y;
            if (diff1 * diff2 <= 0 && diff1 !== diff2) {
                const f_conj = (Qcfs) => {
                    const Q = Qcfs / CMS_TO_CFS;
                    const yt = twFn(Q);
                    const H = window.LHDHydraulics.weirHSimp(Q, L);
                    const y2 = window.LHDHydraulics.calcY2Simp(H, P);
                    if (yt === null || y2 === null) return null;
                    return (yt - y2) * M_TO_FT;
                };
                const exactX = window.LHDHydraulics.bisect(f_conj, tailwater[i].x * 0.99, tailwater[i+1].x * 1.01, ERROR_TOLERANCE_CFS);
                if (exactX !== null) {
                    const exactQ = exactX / CMS_TO_CFS;
                    const exactY = twFn(exactQ) * M_TO_FT;
                    intersections.push({x: exactX, y: exactY, label: 'Intersection (Tailwater & Conjugate)'});

                    const yf = window.LHDHydraulics.computeYFlipAdv(exactQ, L, P);
                    const exactYFlip = yf !== null ? yf * M_TO_FT : null;

                    if (exactYFlip !== null && exactY <= exactYFlip) {
                        dangerConj.push({ x: exactX, y: exactY });
                        dangerFlip.push({ x: exactX, y: exactYFlip });
                    }
                }
            }
        }
        if (flip[i].y !== null && flip[i+1].y !== null && tailwater[i].y !== null && tailwater[i+1].y !== null) {
            const diff1 = tailwater[i].y - flip[i].y;
            const diff2 = tailwater[i+1].y - flip[i+1].y;
            if (diff1 * diff2 <= 0 && diff1 !== diff2) {
                const f_flip = (Qcfs) => {
                    const Q = Qcfs / CMS_TO_CFS;
                    const yt = twFn(Q);
                    const yf = window.LHDHydraulics.computeYFlipAdv(Q, L, P);
                    if (yt === null || yf === null) return null;
                    return (yt - yf) * M_TO_FT;
                };
                const exactX = window.LHDHydraulics.bisect(f_flip, tailwater[i].x * 0.99, tailwater[i+1].x * 1.01, ERROR_TOLERANCE_CFS);
                if (exactX !== null) {
                    const exactQ = exactX / CMS_TO_CFS;
                    const exactY = twFn(exactQ) * M_TO_FT;
                    intersections.push({x: exactX, y: exactY, label: 'Intersection (Tailwater & Flip)'});

                    const H = window.LHDHydraulics.weirHSimp(exactQ, L);
                    const y2 = window.LHDHydraulics.calcY2Simp(H, P);
                    const exactYConj = y2 !== null ? y2 * M_TO_FT : null;

                    if (exactYConj !== null && exactY >= exactYConj) {
                        dangerConj.push({ x: exactX, y: exactYConj });
                        dangerFlip.push({ x: exactX, y: exactY });
                    }
                }
            }
        }
    }

    dangerConj.sort((a, b) => a.x - b.x);
    dangerFlip.sort((a, b) => a.x - b.x);

    document.getElementById('ratingCurvesHeader').innerHTML =
        `<strong>${damName} Rating Curve</strong><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">` +
        `P = ${heightFt.toFixed(1)} ft &nbsp;·&nbsp; L = ${lengthFt.toFixed(0)} ft &nbsp;·&nbsp; ` +
        `Tailwater: NWM SRC (reach ${comidStr}) &nbsp;·&nbsp; Q<sub>max</sub> = ${(qMaxCms * CMS_TO_CFS).toFixed(0)} cfs` +
        `</span>`;

    const htmlLegendPlugin = {
        id: 'htmlLegend',
        afterUpdate(chart) {
            const container = document.getElementById('ratingCurvesLegend');
            if (!container) return;
            
            container.innerHTML = '';
            const datasets = chart.data.datasets;
            
            // Track if we've already generated our consolidated danger icon to prevent duplicates
            let addedDangerLegend = false;
            
            datasets.forEach((dataset, index) => {
                const meta = chart.getDatasetMeta(index);
                
                // Skip tracking datasets we don't want to show
                if (dataset.label.includes('Intersections') || dataset.label === 'Danger Zone Conj') return;
                
                let labelTextStr = dataset.label;
                let isDangerZoneItem = dataset.label === 'Danger Zone';
                
                // If it's a danger zone dataset, consolidate its style and name
                if (isDangerZoneItem) {
                    if (addedDangerLegend) return; // Prevent duplicate entries
                    labelTextStr = 'Dangerous Flow Range';
                    addedDangerLegend = true;
                }
                
                const isHidden = meta.hidden === true || (meta.hidden === null && dataset.hidden === true);
                
                const legendItem = document.createElement('div');
                legendItem.style.display = 'flex';
                legendItem.style.alignItems = 'center';
                legendItem.style.cursor = 'pointer';
                legendItem.style.userSelect = 'none';
                legendItem.style.opacity = isHidden ? '0.5' : '1';
                legendItem.style.marginRight = '12px'; // Adds spacing between inline legends
                
                // Create the custom icon block
                const colorIcon = document.createElement('div');
                colorIcon.style.marginRight = '6px';
                
                if (isDangerZoneItem) {
                    // Custom aesthetic block icon for the shaded rectangle region
                    colorIcon.style.width = '18px';
                    colorIcon.style.height = '12px';
                    colorIcon.style.backgroundColor = 'rgba(231, 76, 60, 0.25)';
                    colorIcon.style.border = '1px solid #e74c3c';
                    colorIcon.style.borderRadius = '2px';
                } else {
                    // Regular line icon for rating trends
                    colorIcon.style.width = '24px';
                    colorIcon.style.height = '0px';
                    colorIcon.style.borderTop = `2px ${dataset.borderDash ? 'dashed' : 'solid'} ${dataset.borderColor}`;
                }
                
                const labelText = document.createElement('span');
                labelText.innerHTML = labelTextStr; // Handles HTML sub tags safely
                labelText.style.textDecoration = isHidden ? 'line-through' : 'none';
                
                legendItem.appendChild(colorIcon);
                legendItem.appendChild(labelText);
                
                legendItem.addEventListener('click', () => {
                    if (isDangerZoneItem) {
                        // Toggle both helper components of the danger zone fill simultaneously
                        const conjMeta = chart.getDatasetMeta(datasets.findIndex(d => d.label === 'Danger Zone Conj'));
                        const flipMeta = chart.getDatasetMeta(datasets.findIndex(d => d.label === 'Danger Zone'));
                        
                        const targetVisibility = !isHidden;
                        if (conjMeta) conjMeta.hidden = targetVisibility;
                        if (flipMeta) flipMeta.hidden = targetVisibility;
                    } else {
                        meta.hidden = !isHidden;
                    }
                    chart.update();
                });
                
                container.appendChild(legendItem);
            });
        }
    };

    const ctx = document.getElementById('ratingCurvesChart').getContext('2d');
    if (ratingCurvesChart) ratingCurvesChart.destroy();
    ratingCurvesChart = new Chart(ctx, {
        type: 'line',
        data: {
            datasets: [
                { label: 'Tailwater (yₜ)',  data: tailwater,
                  order: 1, borderColor: '#3498db', backgroundColor: '#3498db',
                  pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true,
                  pointStyle: 'line' },
                { label: 'Conjugate depth (y₂)', data: conjugate,
                  order: 1, borderColor: '#27ae60', backgroundColor: '#27ae60',
                  pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true,
                  pointStyle: 'line' },
                { label: 'Flip depth (y<sub>flip</sub>)', data: flip,
                  order: 1, borderColor: '#e74c3c', backgroundColor: '#e74c3c',
                  pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true,
                  borderDash: [6, 4], pointStyle: 'line' },
                { label: 'Danger Zone Conj', data: dangerConj,
                  order: 1, borderColor: 'transparent', backgroundColor: 'transparent',
                  pointRadius: 0, borderWidth: 0, tension: 0.2, spanGaps: false },
                { label: 'Danger Zone', data: dangerFlip,
                  order: 1, borderColor: 'transparent', backgroundColor: 'rgba(231, 76, 60, 0.25)',
                  pointRadius: 0, borderWidth: 0, tension: 0.2, spanGaps: false, fill: '-1' },
                { label: 'Intersections', data: intersections,
                  order: 0, type: 'scatter', borderColor: '#2c3e50', backgroundColor: '#2c3e50',
                  pointRadius: 5, pointHoverRadius: 7, pointHitRadius: 15, showLine: false },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            parsing: false,
            onClick: (event, elements, chart) => {
                if (elements.length > 0) {
                    const element = elements[0];
                    const dataset = chart.data.datasets[element.datasetIndex];
                    if (dataset.label === 'Intersections') {
                        const pt = dataset.data[element.index];
                        alert(`${pt.label}\n\nCoordinates:\nx: ${pt.x.toFixed(2)} cfs\ny: ${pt.y.toFixed(2)} ft`);
                    }
                }
            },
            plugins: {
                legend: { 
                    display: false // Disable the default canvas legend
                },
                tooltip: {
                    filter: (tooltipItem) => !tooltipItem.dataset.label.includes('Danger Zone'),
                    callbacks: {
                        title: (items) => {
                            if (items[0].dataset.label === 'Intersections') return items[0].raw.label;
                            return `Q = ${items[0].parsed.x.toFixed(0)} cfs`;
                        },
                        label: (item) => {
                            if (item.dataset.label === 'Intersections') {
                                return `x: ${item.parsed.x.toFixed(2)} cfs, y: ${item.parsed.y.toFixed(2)} ft`;
                            }
                            const cleanLabel = item.dataset.label.replace(/<[^>]*>?/gm, ''); // Strip HTML tags for canvas tooltip
                            return `${cleanLabel}: ${item.parsed.y.toFixed(2)} ft`;
                        },
                    },
                },
                zoom: {
                    limits: {
                        x: {min: 'original', max: 'original'},
                        y: {min: 'original', max: 'original'}
                    },
                    pan: {
                        enabled: true,
                        mode: 'xy',
                        // Let a single-finger touch drag scroll the page instead of panning the chart.
                        onPanStart: ({ event }) => event.pointerType === 'touch' ? false : undefined,
                    },
                    zoom: {
                        wheel: {
                            enabled: true,
                            // Require Ctrl so a normal scroll over the chart still scrolls the page.
                            modifierKey: 'ctrl',
                        },
                        pinch: {
                            enabled: true
                        },
                        mode: 'xy',
                    }
                }
            },
            scales: {
                x: {
                    type: 'linear',
                    position: 'bottom',
                    title: { display: true, text: 'Discharge Q (cfs)' },
                },
                y: {
                    title: { display: true, text: 'Depth (ft)' },
                    beginAtZero: true,
                },
            },
        },
        plugins: [htmlLegendPlugin]
    });
}
window.showRatingCurves = showRatingCurves;

// 5b. Synthetic Rating Curve (SRC): Manning's-equation stage-discharge curve for the
// dam's NHDPlus V2 reach, derived offline from hydrofabric bankfull channel geometry
// (see backend/build_synthetic_rating_curves.py). In-channel only.

// Extract the 10-day forecast envelope (min/max across all members) from the cache.
// Returns { currentFlow, forecastMin, forecastMax } in cfs, or null if unavailable.
async function _getForecastRange(comid) {
    const cached = _forecastCache.get(String(comid).replace(/\.0+$/, ''));
    if (!cached) return null;
    try {
        const mrData = await cached;
        if (!mrData) return null;
        const mrMean = mrData.mediumRange?.mean?.data ?? [];
        if (!mrMean.length) return null;
        const mrMembers = ['member1','member2','member3','member4','member5','member6']
            .map(k => mrData.mediumRange?.[k]?.data ?? []);
        let forecastMin = Infinity, forecastMax = -Infinity;
        mrMean.forEach((p, i) => {
            const upper = Math.max(...mrMembers.map(m => m[i]?.flow ?? p.flow));
            const lower = Math.min(...mrMembers.map(m => m[i]?.flow ?? p.flow));
            forecastMax = Math.max(forecastMax, upper);
            forecastMin = Math.min(forecastMin, lower);
        });
        if (!isFinite(forecastMin) || !isFinite(forecastMax)) return null;
        const nowMs = Date.now();
        const pts = mrMean.map(p => ({ ms: new Date(p.validTime).getTime(), flow: p.flow }));
        const ai = pts.findIndex(p => p.ms > nowMs);
        let currentFlow = pts[0]?.flow ?? null;
        if (ai > 0) {
            const t = (nowMs - pts[ai-1].ms) / (pts[ai].ms - pts[ai-1].ms);
            currentFlow = pts[ai-1].flow + t * (pts[ai].flow - pts[ai-1].flow);
        }
        return { currentFlow, forecastMin, forecastMax };
    } catch { return null; }
}

function _loadSrcData(comid) {
    if (!_srcCache.has(comid)) {
        _srcCache.set(comid, fetch(`data/src/${comid}.json?v=${window.DATA_VERSION || 'dev'}`).then(r => r.ok ? r.json() : null).catch(() => null));
    }
    return _srcCache.get(comid);
}

async function showSyntheticRatingCurve(comid, damName, heightFt = null, lengthFt = null) {
    comid = String(comid).replace(/\.0+$/, '');
    const container = document.getElementById('srcContainer');
    const header = document.getElementById('srcHeader');
    container.style.display = 'flex';
    header.innerHTML = `<strong>${damName} Synthetic Rating Curve</strong><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">Loading...</span>`;

    const CMS_TO_CFS = window.LHDHydraulics.constants.CMS_TO_CFS;
    const M_TO_FT = window.LHDHydraulics.constants.M_TO_FT;

    const curve = await _loadSrcData(comid);

    if (srcChart) { srcChart.destroy(); srcChart = null; }

    if (!curve) {
        header.innerHTML = `<strong>${damName} Synthetic Rating Curve</strong><br>` +
            `<span style="color:#7f8c8d; font-size: 12px;">No synthetic rating curve available for reach ${comid}.</span>`;
        return;
    }

    const srcQs = curve.discharge_cms;
    const points = curve.stage_m.map((s, i) => ({
        x: srcQs[i] * CMS_TO_CFS,
        y: s * M_TO_FT,
    }));
    const bankfullFt = curve.bankfull_stage_m * M_TO_FT;

    const methodLabel = curve.method === 'ahg'
        ? `AHG-calibrated (NWM Retro v3.0) &nbsp;·&nbsp; y = ${curve.y_coef}·Q<sup>${curve.y_exp}</sup>`
        : `Manning's trapezoid &nbsp;·&nbsp; n = ${curve.manning_n} &nbsp;·&nbsp; S = ${curve.slope} &nbsp;·&nbsp; in-channel only`;
    header.innerHTML =
        `<strong>${damName} Synthetic Rating Curve</strong><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">` +
        `Reach ${comid} &nbsp;·&nbsp; ${methodLabel} &nbsp;·&nbsp; ` +
        `bankfull stage = ${bankfullFt.toFixed(1)} ft` +
        `</span>`;

    const datasets = [
        { label: 'Synthetic rating curve', data: points,
          order: 1, borderColor: '#8e44ad', backgroundColor: '#8e44ad',
          pointRadius: 0, borderWidth: 2, tension: 0.2, pointStyle: 'line' },
    ];

    const hasGeom = isFinite(heightFt) && heightFt > 0 && isFinite(lengthFt) && lengthFt > 0;
    if (hasGeom) {
        const P = heightFt / M_TO_FT;
        const L = lengthFt / M_TO_FT;
        const conjugatePoints = [];
        const flipPoints = [];
        for (const Q of srcQs) {
            const Qcfs = Q * CMS_TO_CFS;
            const H  = window.LHDHydraulics.weirHSimp(Q, L);
            const y2 = window.LHDHydraulics.calcY2Simp(H, P);
            const yf = window.LHDHydraulics.computeYFlipAdv(Q, L, P);
            conjugatePoints.push({ x: Qcfs, y: y2 !== null ? y2 * M_TO_FT : null });
            flipPoints.push(     { x: Qcfs, y: yf !== null ? yf * M_TO_FT : null });
        }
        datasets.push(
            { label: 'Conjugate depth (y₂)', data: conjugatePoints,
              order: 1, borderColor: '#27ae60', backgroundColor: '#27ae60',
              pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true, pointStyle: 'line' },
            { label: 'Flip depth (y_flip)', data: flipPoints,
              order: 1, borderColor: '#e74c3c', backgroundColor: '#e74c3c',
              pointRadius: 0, borderWidth: 2, tension: 0.2, spanGaps: true,
              borderDash: [6, 4], pointStyle: 'line' },
        );
    }

    const ctx = document.getElementById('srcChart').getContext('2d');
    srcChart = new Chart(ctx, {
        type: 'line',
        data: { datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { usePointStyle: true } },
                tooltip: {
                    callbacks: {
                        label: (item) => `Q = ${item.parsed.x.toFixed(0)} cfs, stage = ${item.parsed.y.toFixed(2)} ft`,
                    },
                },
                zoom: {
                    limits: { x: { min: 'original', max: 'original' }, y: { min: 'original', max: 'original' } },
                    pan: {
                        enabled: true,
                        mode: 'xy',
                        // Let a single-finger touch drag scroll the page instead of panning the chart.
                        onPanStart: ({ event }) => event.pointerType === 'touch' ? false : undefined,
                    },
                    // Require Ctrl so a normal scroll over the chart still scrolls the page.
                    zoom: { wheel: { enabled: true, modifierKey: 'ctrl' }, pinch: { enabled: true }, mode: 'xy' },
                },
            },
            scales: {
                x: { type: 'linear', position: 'bottom', title: { display: true, text: 'Discharge Q (cfs)' } },
                y: { title: { display: true, text: 'Stage (ft)' }, beginAtZero: true },
            },
        },
    });
}
window.showSyntheticRatingCurve = showSyntheticRatingCurve;

// =========================================================================
// 5b-ii. Flow Duration Curve (FDC) Cache & Loader
// =========================================================================
const _fdcCache = new Map();
const _FDC_PERCENTILES = [0, 2, 5, 10, 20, 25, 30, 50, 75, 90, 95, 99, 100];
const _FDC_LINE_TENSION = 0.3; // Shared with findXAtFlowOnCurve so the drawn curve and the computed intersections always match
let fdcChart = null;

function _loadFdcData(comid) {
    if (!_fdcCache.has(comid)) {
        _fdcCache.set(
            comid,
            fetch(`data/fdc/${comid}.json?v=${window.DATA_VERSION || 'dev'}`)
                .then(r => r.ok ? r.json() : null)
                .catch(() => null)
        );
    }
    return _fdcCache.get(comid);
}

// The FDC line renders with tension (a curved cardinal spline through the data points), not straight
// segments — so finding where a target flow crosses the *rendered* curve means replicating Chart.js's own
// neighbor-based spline control-point formula (its internal `splineCurve` helper) and solving that cubic
// Bezier for y = target, rather than doing straight-line interpolation between the raw data points.
function _splineControlPoints(prev, curr, next, tension) {
    const d01 = Math.hypot(curr.x - prev.x, curr.y - prev.y);
    const d12 = Math.hypot(next.x - curr.x, next.y - curr.y);
    let s01 = d01 / (d01 + d12);
    let s12 = d12 / (d01 + d12);
    s01 = isNaN(s01) ? 0 : s01;
    s12 = isNaN(s12) ? 0 : s12;
    const fa = tension * s01;
    const fb = tension * s12;
    return {
        previous: { x: curr.x - fa * (next.x - prev.x), y: curr.y - fa * (next.y - prev.y) },
        next:     { x: curr.x + fb * (next.x - prev.x), y: curr.y + fb * (next.y - prev.y) },
    };
}

function _bezierAt(p0, p1, p2, p3, t) {
    const mt = 1 - t;
    const a = mt * mt * mt, b = 3 * mt * mt * t, c = 3 * mt * t * t, d = t * t * t;
    return {
        x: a * p0.x + b * p1.x + c * p2.x + d * p3.x,
        y: a * p0.y + b * p1.y + c * p2.y + d * p3.y,
    };
}

// Bisects for the Bezier parameter t in [0,1] where the curve's y equals targetY. Assumes y(t) is
// monotonic across the segment, which holds for this mild tension (0.3) on largely-monotonic FDC data.
function _solveBezierT(p0, p1, p2, p3, targetY) {
    let lo = 0, hi = 1;
    const signLo = Math.sign(_bezierAt(p0, p1, p2, p3, lo).y - targetY);
    const signHi = Math.sign(_bezierAt(p0, p1, p2, p3, hi).y - targetY);
    if (signLo === 0) return lo;
    if (signHi === 0) return hi;
    if (signLo === signHi) return null;
    for (let i = 0; i < 40; i++) {
        const mid = (lo + hi) / 2;
        const signMid = Math.sign(_bezierAt(p0, p1, p2, p3, mid).y - targetY);
        if (signMid === signLo) { lo = mid; } else { hi = mid; }
    }
    return (lo + hi) / 2;
}

// Finds the x (data-space) where the rendered, curved FDC line crosses y = targetY. Rebuilds the exact
// Chart.js spline segment that brackets targetY in pixel space (via the chart's own scales, so this stays
// correct across the log y-axis and any resize) and solves it directly, instead of assuming a straight line.
function findXAtFlowOnCurve(chart, points, targetY, tension) {
    const xScale = chart.scales.x;
    const yScale = chart.scales.y;
    const px = points.map(p => ({ x: xScale.getPixelForValue(p.x), y: yScale.getPixelForValue(p.y) }));
    const targetPxY = yScale.getPixelForValue(targetY);
    const n = px.length;

    for (let i = 0; i < points.length - 1; i++) {
        const p1 = points[i];
        const p2 = points[i + 1];
        const bracketed = (p1.y >= targetY && p2.y <= targetY) || (p1.y <= targetY && p2.y >= targetY);
        if (!bracketed) continue;

        const outgoing = _splineControlPoints(px[Math.max(0, i - 1)], px[i], px[Math.min(n - 1, i + 1)], tension).next;
        const incoming = _splineControlPoints(px[i], px[i + 1], px[Math.min(n - 1, i + 2)], tension).previous;

        const t = _solveBezierT(px[i], outgoing, incoming, px[i + 1], targetPxY);
        if (t === null) continue;

        const xPx = _bezierAt(px[i], outgoing, incoming, px[i + 1], t).x;
        return xScale.getValueForPixel(xPx);
    }
    return null;
}

// =========================================================================
// Main Display Function
// =========================================================================
async function showFlowDurationCurve(comid, damName, qMin = null, qMax = null) {
    comid = String(comid).replace(/\.0+$/, '');
    const hasDangerRange = qMin !== null && !isNaN(qMin) && qMax !== null && !isNaN(qMax);
    const container = document.getElementById('fdcContainer');
    const header    = document.getElementById('fdcHeader');
    
    container.style.display = 'flex';
    header.innerHTML = `<strong>${damName} Flow Duration Curve</strong><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">Loading…</span>`;

    const CMS_TO_CFS = window.LHDHydraulics.constants.CMS_TO_CFS;

    const flows = await _loadFdcData(comid);
    const pcts  = _FDC_PERCENTILES;

    if (fdcChart) { 
        fdcChart.destroy(); 
        fdcChart = null; 
    }

    if (!flows) {
        header.innerHTML = `<strong>${damName} Flow Duration Curve</strong><br>` +
            `<span style="color:#7f8c8d; font-size: 12px;">No FDC available for reach ${comid}.</span>`;
        return;
    }

    // Convert to (exceedance %, flow cfs) — sort left=high flow, right=low flow
    const exceedance = pcts.map(p => 100 - p).reverse();
    const flows_cfs  = [...flows].reverse().map(q => q !== null ? q * CMS_TO_CFS : null);

    const points = exceedance.map((e, i) => ({
        x: e,
        y: flows_cfs[i],
    })).filter(pt => pt.y !== null && pt.y > 0);

    const q50_cfs = (() => {
        const i = pcts.indexOf(50);
        return i >= 0 && flows[i] != null ? (flows[i] * CMS_TO_CFS).toFixed(0) : null;
    })();

    header.innerHTML =
        `<strong>${damName} Flow Duration Curve</strong><br>` +
        `<span style="color:#7f8c8d; font-size: 12px;">` +
        `Reach ${comid} &nbsp;·&nbsp; NWM Retrospective v3.0` +
        (q50_cfs ? ` &nbsp;·&nbsp; Q50 = ${q50_cfs} cfs` : '') +
        `</span>`;

    // Lowest valid non-zero flow rate on the chart to anchor logarithmic vertical lines
    const minYValue = points.length > 0 ? Math.min(...points.map(pt => pt.y)) : 1;

    // NWM FDC Line — built and charted first (below) so its actual rendered curve (with tension) exists
    // in the chart's scales before we solve for where it crosses qMax/qMin.
    const fdcLineDataset = {
        type: 'line',
        label: 'NWM FDC',
        data: points,
        order: 1,
        borderColor: '#2471a3',
        backgroundColor: 'rgba(36,113,163,0.12)',
        fill: false,
        pointRadius: 0,
        pointHitRadius: 10,
        borderWidth: 2,
        tension: _FDC_LINE_TENSION,
        pointStyle: 'circle',
    };

    const ctx = document.getElementById('fdcChart').getContext('2d');
    fdcChart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [fdcLineDataset] },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { 
                    labels: { 
                        usePointStyle: true, 
                        filter: (item) => !item.text.startsWith('_') && item.text !== 'Dangerous Flow Range Thresholds'
                    } 
                },
                tooltip: {
                    callbacks: {
                        label: (item) => {
                            if (item.dataset.label === 'Dangerous Flow Range') return null;
                            return `Q = ${item.parsed.y.toFixed(0)} cfs  (exceeded ${item.parsed.x.toFixed(0)}% of time)`;
                        }
                    },
                },
                zoom: {
                    limits: { x: { min: 'original', max: 'original' }, y: { min: 'original', max: 'original' } },
                    pan: {
                        enabled: true,
                        mode: 'xy',
                        // Let a single-finger touch drag scroll the page instead of panning the chart.
                        onPanStart: ({ event }) => event.pointerType === 'touch' ? false : undefined,
                    },
                    // Require Ctrl so a normal scroll over the chart still scrolls the page.
                    zoom: { wheel: { enabled: true, modifierKey: 'ctrl' }, pinch: { enabled: true }, mode: 'xy' },
                },
            },
            scales: {
                x: {
                    type: 'linear',
                    min: 0,
                    max: 100,
                    title: { display: true, text: 'Exceedance probability (%)' },
                    reverse: true,
                },
                y: {
                    type: 'logarithmic',
                    min: minYValue,
                    title: { display: true, text: 'Discharge Q (cfs)' },
                },
            },
        },
    });

    if (hasDangerRange) {
        // Now that the chart's scales exist, find where the *rendered* (curved) FDC line crosses qMax/qMin.
        const exactXMax = findXAtFlowOnCurve(fdcChart, points, qMax, _FDC_LINE_TENSION);
        const exactXMin = findXAtFlowOnCurve(fdcChart, points, qMin, _FDC_LINE_TENSION);

        // Fallbacks to chart edges if line doesn't cross FDC
        const xIntersectMax = exactXMax !== null ? exactXMax : 0;
        const xIntersectMin = exactXMin !== null ? exactXMin : 100;

        const dangerDatasets = [];

        // 1. Ceiling Horizontal Line (qMax) — spans only the rectangle's boundary, between the FDC intersections
        dangerDatasets.push({
            type: 'line',
            label: 'Dangerous Flow Range Thresholds',
            data: [{ x: xIntersectMax, y: qMax }, { x: xIntersectMin, y: qMax }],
            order: 0,
            borderColor: 'rgba(192,57,43,0.5)',
            borderWidth: 2,
            pointRadius: 0,
            fill: false,
        });

        // 2. Floor Horizontal Line (qMin) — spans only the rectangle's boundary, between the FDC intersections
        dangerDatasets.push({
            type: 'line',
            label: '_qMin',
            data: [{ x: xIntersectMax, y: qMin }, { x: xIntersectMin, y: qMin }],
            order: 0,
            borderColor: 'rgba(192,57,43,0.5)',
            borderWidth: 2,
            pointRadius: 0,
            fill: false,
            pointStyle: false,
        });

        // 3. Vertical Line at Maximum Flow Intersection (qMax)
        dangerDatasets.push({
            type: 'line',
            label: '_vLineMax',
            data: [{ x: xIntersectMax, y: minYValue }, { x: xIntersectMax, y: qMax }],
            order: 0,
            borderColor: exactXMax !== null ? 'rgba(192,57,43,0.5)' : 'rgba(0,0,0,0)',
            borderWidth: exactXMax !== null ? 1.5 : 0,
            pointRadius: 0,
            fill: false,
            showLine: true
        });

        // 4. Vertical Line at Minimum Flow Intersection (qMin)
        dangerDatasets.push({
            type: 'line',
            label: '_vLineMin',
            data: [{ x: xIntersectMin, y: minYValue }, { x: xIntersectMin, y: qMin }],
            order: 0,
            borderColor: exactXMin !== null ? 'rgba(192,57,43,0.5)' : 'rgba(0,0,0,0)',
            borderWidth: exactXMin !== null ? 1.5 : 0,
            pointRadius: 0,
            fill: false,
            showLine: true
        });

        // 5. BOUNDED RECTANGLE: Explicit box between qMax, qMin, and both vertical intersection lines
        const dangerAreaPoints = [
            { x: xIntersectMax, y: qMin }, // Bottom-Left: Intersection of qMin & Max Vertical Line
            { x: xIntersectMax, y: qMax }, // Top-Left: Intersection of qMax & Max Vertical Line
            { x: xIntersectMin, y: qMax }, // Top-Right: Intersection of qMax & Min Vertical Line
            { x: xIntersectMin, y: qMin }  // Bottom-Right: Intersection of qMin & Min Vertical Line
        ];

        dangerDatasets.push({
            type: 'line',
            label: 'Dangerous Flow Range',
            data: dangerAreaPoints,
            order: 2,
            borderColor: 'rgba(0,0,0,0)',
            backgroundColor: 'rgba(231,76,60,0.25)',
            fill: 'origin',
            pointRadius: 0,
            tension: 0, // Sharp 90-degree corners
        });

        // Insert ahead of the FDC line so legend/order match the original layout.
        fdcChart.data.datasets.unshift(...dangerDatasets);
        fdcChart.update('none');
    }
}

window.showFlowDurationCurve = showFlowDurationCurve;

// 5. Legend and Filter Integration
const legend = L.control({ position: 'bottomright' });
legend.onAdd = function (map) {
    const div = L.DomUtil.create('div', 'info legend');
    div.innerHTML = `
        <div class="legend-header"><strong>Dam Status</strong><span class="legend-toggle">&#9662;</span></div>
        <div class="legend-body">
            <div class="legend-row"><i style="background: #e67e22"></i><span>Dangerous Range Calculated</span></div>
            <div class="legend-row"><i style="background: #2980b9"></i><span>Live Forecast Available</span></div>
            <div class="legend-row"><i style="background: #95a5a6"></i><span>Location Info Only</span></div>
        </div>
    `;
    div.title = 'Click to hide/show the dam status legend';

    const isMobile = () => window.matchMedia('(max-width: 768px)').matches;

    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.on(div, 'click', () => {
        if (isMobile()) div.classList.toggle('legend-collapsed');
    });

    return div;
};
legend.addTo(map);

window.addEventListener('resize', () => { map.invalidateSize(); });

// --- Draggable + resizable floating panels (forecast + rating curves) ---
function closeCombinedPanel() {
    document.getElementById('combinedModal').classList.remove('is-open');
    document.getElementById('forecastSliderWrap').style.display = 'none';
}
window.closeCombinedPanel = closeCombinedPanel;

function enablePanelDragResize(panelId, headerId, onResize) {
    const panel  = document.getElementById(panelId);
    const handle = document.getElementById(headerId);
    if (!panel || !handle) return;

    let dragging = false;
    let startX = 0, startY = 0, startLeft = 0, startTop = 0;

    function pinAbsolute() {
        const r = panel.getBoundingClientRect();
        panel.style.left = r.left + 'px';
        panel.style.top  = r.top  + 'px';
        panel.style.transform = 'none';
        panel.style.margin = '0';
    }

    handle.addEventListener('mousedown', (e) => {
        if (e.target.closest('.close')) return;
        if (window.innerWidth <= 768) return;
        e.preventDefault();
        pinAbsolute();
        dragging = true;
        startX = e.clientX; startY = e.clientY;
        const r = panel.getBoundingClientRect();
        startLeft = r.left; startTop = r.top;
        document.body.style.userSelect = 'none';
    });

    window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        const r = panel.getBoundingClientRect();
        const minLeft = -r.width + 80;
        const maxLeft = window.innerWidth - 80;
        const minTop  = 0;
        const maxTop  = window.innerHeight - 30;
        panel.style.left = Math.max(minLeft, Math.min(maxLeft, startLeft + dx)) + 'px';
        panel.style.top  = Math.max(minTop,  Math.min(maxTop,  startTop  + dy)) + 'px';
    });

    window.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        document.body.style.userSelect = '';
    });

    if (typeof ResizeObserver !== 'undefined' && typeof onResize === 'function') {
        const ro = new ResizeObserver(() => onResize());
        ro.observe(panel);
    }

    const MIN_W = 340, MIN_H = 260;
    let resizing = null;

    panel.querySelectorAll('.resize-handle').forEach((h) => {
        h.addEventListener('mousedown', (e) => {
            if (window.innerWidth <= 768) return;
            e.preventDefault();
            e.stopPropagation();
            pinAbsolute();
            const r = panel.getBoundingClientRect();
            resizing = {
                dir: h.dataset.dir,
                startX: e.clientX, startY: e.clientY,
                startLeft: r.left, startTop: r.top,
                startW: r.width,  startH: r.height,
            };
            document.body.style.userSelect = 'none';
        });
    });

    window.addEventListener('mousemove', (e) => {
        if (!resizing) return;
        const dx = e.clientX - resizing.startX;
        const dy = e.clientY - resizing.startY;
        let { startLeft: L, startTop: T, startW: W, startH: H, dir } = resizing;

        if (dir.includes('e')) W = Math.max(MIN_W, resizing.startW + dx);
        if (dir.includes('s')) H = Math.max(MIN_H, resizing.startH + dy);
        if (dir.includes('w')) {
            const newW = Math.max(MIN_W, resizing.startW - dx);
            L = resizing.startLeft + (resizing.startW - newW);
            W = newW;
        }
        if (dir.includes('n')) {
            const newH = Math.max(MIN_H, resizing.startH - dy);
            T = resizing.startTop + (resizing.startH - newH);
            H = newH;
        }
        L = Math.max(0, Math.min(window.innerWidth  - W, L));
        T = Math.max(0, Math.min(window.innerHeight - H, T));

        panel.style.left   = L + 'px';
        panel.style.top    = T + 'px';
        panel.style.width  = W + 'px';
        panel.style.height = H + 'px';
    });

    window.addEventListener('mouseup', () => {
        if (resizing) {
            resizing = null;
            document.body.style.userSelect = '';
        }
    });
}

enablePanelDragResize('combinedModal', 'combinedModalHeader',
    () => { 
        if (forecastChart) forecastChart.resize(); 
        if (ratingCurvesChart) ratingCurvesChart.resize(); 
    });

document.getElementById('forecastSlider').addEventListener('input', function () {
    const days = parseInt(this.value);
if (_forecastState) {
        const { allPoints, hasSafetyRange, qMin, qMax, damName } = _forecastState;
        _renderForecastChart(allPoints, hasSafetyRange, qMin, qMax, damName, days);
    }
});

loadDams();

// --- Tab navigation ---
(function setupTabs() {
    const buttons = document.querySelectorAll('.nav-button[data-tab]');
    const panels = document.querySelectorAll('.tab-panel');
    const hamburger = document.getElementById('hamburger-toggle');
    const navMenu = document.getElementById('nav-menu');

    function activate(tabName) {
        buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tabName));
        panels.forEach(p => p.classList.toggle('active', p.id === `tab-${tabName}`));
        if (tabName === 'forecasts') {
            setTimeout(() => map.invalidateSize(), 0);
        }
    }

    buttons.forEach(btn => {
        btn.addEventListener('click', () => {
            activate(btn.dataset.tab);
            if (navMenu && navMenu.classList.contains('open')) {
                navMenu.classList.remove('open');
                if (hamburger) hamburger.setAttribute('aria-expanded', 'false');
            }
        });
    });

    if (hamburger && navMenu) {
        hamburger.addEventListener('click', () => {
            const open = navMenu.classList.toggle('open');
            hamburger.setAttribute('aria-expanded', String(open));
        });
    }
})();

// --- CFD Toolbox Sidebar Scroll-Spy Logic ---
(function setupCFDScrollSpy() {
    const tabContainer = document.getElementById('tab-cfd-toolbox');
    if (!tabContainer) return;

    const isMobile = () => window.matchMedia('(max-width: 768px)').matches;

    // --- Mobile-only side-tab TOC: a slim sticky tab that slides out a drawer
    // over the content, closed by default (no class = closed, per the CSS). ---
    const sidebar = document.querySelector('.cfd-sidebar');
    const tocToggle = document.getElementById('cfd-toc-toggle');
    function setTocOpen(open) {
        if (!sidebar || !tocToggle) return;
        sidebar.classList.toggle('cfd-toc-open', open);
        tocToggle.setAttribute('aria-expanded', String(open));
    }
    if (tocToggle) {
        tocToggle.addEventListener('click', (e) => {
            if (!isMobile() || !sidebar) return;
            e.stopPropagation(); // don't let the click-outside listener below immediately re-close it
            setTocOpen(!sidebar.classList.contains('cfd-toc-open'));
        });
    }
    // Tapping anywhere outside the drawer closes it (stands in for a dimmed
    // backdrop without the extra DOM element/z-index layering that would need).
    document.addEventListener('click', (e) => {
        if (!isMobile() || !sidebar) return;
        if (sidebar.classList.contains('cfd-toc-open') && !sidebar.contains(e.target)) {
            setTocOpen(false);
        }
    });

    // Listen for clicks on the sidebar to smooth scroll to the section
    const navLinks = document.querySelectorAll('.cfd-sidebar a');
    navLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            const targetId = this.getAttribute('href').substring(1);
            const targetElement = document.getElementById(targetId);

            if (targetElement) {
                // Scroll the tab container to the element's offset
                tabContainer.scrollTo({
                    top: targetElement.offsetTop - 20,
                    behavior: 'smooth'
                });
            }
            // Close the drawer back down after picking a section, so it doesn't
            // stay covering the content the user just scrolled to.
            if (isMobile()) setTocOpen(false);
        });
    });

    // Use Intersection Observer to highlight the active link as the user scrolls
    const headings = document.querySelectorAll('.cfd-content h2');
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                // Remove active class from all links
                navLinks.forEach(link => link.classList.remove('active'));
                
                // Add active class to the link matching this heading
                const id = entry.target.getAttribute('id');
                const activeLink = document.querySelector(`.cfd-sidebar a[href="#${id}"]`);
                if (activeLink) activeLink.classList.add('active');
            }
        });
    }, {
        root: tabContainer,
        rootMargin: '0px 0px -80% 0px', // Triggers when heading hits the top 20% of the screen
        threshold: 0
    });

    headings.forEach(heading => observer.observe(heading));
})();

// --- Load State Boundaries JSON ---
async function loadStateBoundaries(url) {
    try {
        let selectedStateLayer = null;
        let stateLayersMap = {};

        function getDynamicStyles() {
            const zoom = map.getZoom();
            let baseWeight = 1; // Thinner border at national scale
            if (zoom > 4 && zoom <= 6) baseWeight = 2;
            if (zoom > 6 && zoom <= 8) baseWeight = 3;
            if (zoom > 8) baseWeight = 4;
            
            return {
                defaultStyle: { color: 'black', weight: baseWeight, fillOpacity: 0.05 },
                hoverStyle: { color: 'black', weight: baseWeight + 1, fillOpacity: 0.2 },
                selectedStyle: { color: '#3498db', weight: baseWeight + 2, fillOpacity: 0.2 }
            };
        }

        window.updateBoundaryZOrder = () => {
            if (window.stateBoundaryLayer) {
                if (map.getZoom() <= 4) {
                    window.stateBoundaryLayer.bringToFront();
                } else {
                    window.stateBoundaryLayer.bringToBack();
                }
                
                // Update dynamic stroke width on zoom
                const styles = getDynamicStyles();
                window.stateBoundaryLayer.eachLayer(layer => {
                    if (layer === selectedStateLayer) {
                        layer.setStyle(styles.selectedStyle);
                    } else {
                        layer.setStyle(styles.defaultStyle);
                    }
                });
            }
        };
        map.on('zoomend', window.updateBoundaryZOrder);

        const response = await fetch(url);
        const data = await response.json();
        

        window.clearStateHighlight = () => {
            if (selectedStateLayer) {
                selectedStateLayer.setStyle(getDynamicStyles().defaultStyle);
                selectedStateLayer = null;
            }
        };

        window.highlightStateBoundary = (stateAbbr) => {
            window.clearStateHighlight();
            if (stateAbbr && stateLayersMap[stateAbbr]) {
                const layer = stateLayersMap[stateAbbr];
                layer.setStyle(getDynamicStyles().selectedStyle);
                selectedStateLayer = layer;
            }
        };

        const stateBoundaryLayer = L.geoJSON(data, {
            style: function() { return getDynamicStyles().defaultStyle; },
            onEachFeature: function(feature, layer) {
                const stateAbbr = feature.properties.stusps || feature.properties.STUSPS || feature.properties.STUSAB || feature.properties.STATE;
                if (stateAbbr) {
                    stateLayersMap[stateAbbr.toUpperCase()] = layer;
                }

                // Visual feedback on hover
                layer.on('mouseover', function() {
                    if (selectedStateLayer !== layer) {
                        layer.setStyle(getDynamicStyles().hoverStyle);
                    }
                });
                layer.on('mouseout', function() {
                    if (selectedStateLayer !== layer) {
                        layer.setStyle(getDynamicStyles().defaultStyle);
                    }
                });
                
                // Double click to zoom back out (deselect)
                layer.on('dblclick', function(e) {
                    // Prevent the default map double-click zoom from interfering on canvas
                    map.doubleClickZoom.disable();
                    setTimeout(() => map.doubleClickZoom.enable(), 500);

                    // Prevent the default map double-click zoom
                    if (e.originalEvent) {
                        L.DomEvent.stopPropagation(e.originalEvent);
                    }
                    
                    if (selectedStateLayer !== layer) {
                        return; // Only act if it's the currently selected state
                    }

                    if (typeof window.clearStateFilterChip === 'function') {
                        window.clearStateFilterChip();
                    }

                    map.flyTo([39.82, -98.57], 4, { duration: 1.5 });
                    if (typeof window.closeSearchPanel === 'function') {
                        window.closeSearchPanel();
                    }
                });

                // Click to filter by state
                layer.on('click', function(e) {
                    // Extract state abbreviation (Census shapefiles usually use STUSPS)
                    const stateAbbr = feature.properties.stusps || feature.properties.STUSPS || feature.properties.STUSAB || feature.properties.STATE;
                    if (stateAbbr && typeof window.setStateFilter === 'function') {
                        const upperAbbr = stateAbbr.toUpperCase();
                        // Only apply if it is not already the selected state
                        if (window.getStateFilter() !== upperAbbr) {
                            window.setStateFilter(upperAbbr);
                            if (selectedStateLayer) {
                                selectedStateLayer.setStyle(getDynamicStyles().defaultStyle);
                            }
                            layer.setStyle(getDynamicStyles().selectedStyle);
                            selectedStateLayer = layer;

                            // Adjust zoom to state boundary
                            setTimeout(() => { map.flyToBounds(layer.getBounds(), { duration: 1.5, padding: [50, 50] }); }, 50);
                            if (typeof window.openSearchPanel === 'function') {
                                window.openSearchPanel();
                            }
                        }
                    }
                });
            }
        });
        
        window.stateBoundaryLayer = stateBoundaryLayer;

        // Add it to the top-right layer control menu
        layerControl.addOverlay(stateBoundaryLayer, "State Boundaries");
        
        // If you want the boundaries to be visible immediately on load, uncomment the next line:
        stateBoundaryLayer.addTo(map);
        window.updateBoundaryZOrder();
    } catch (error) {
        console.error(`Failed to load JSON from ${url}:`, error);
    }
}

loadStateBoundaries('boundaries/cb_2025_us_state_20m_conus.json');

// --- Global Escape Key Handler ---
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        // Clear all filters if they are applied
        if (typeof window.clearAllFilters === 'function') {
            window.clearAllFilters();
        }
        if (typeof window.clearStateHighlight === 'function') {
            window.clearStateHighlight();
        }

        // Zoom out to national view
        map.flyTo([39.82, -98.57], 4, { duration: 1.5   });
        
        // Close any open panels or popups
        if (typeof window.closeSearchPanel === 'function') {
            window.closeSearchPanel();
        }
        if (typeof window.closeCombinedPanel === 'function') {
            window.closeCombinedPanel();
        }
        map.closePopup();
    }
});