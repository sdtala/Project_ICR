(() => {
  const container = document.querySelector('#site-map');
  const source = document.querySelector('#map-data');
  if (!container || !source) return;
  if (!window.L) {
    container.innerHTML = '<div class="map-error">The local map library could not load. All ranking outputs remain available below.</div>';
    return;
  }
  const renderStarted = performance.now();
  const performanceChip = document.querySelector('#map-performance');
  const data = JSON.parse(source.textContent);
  container.innerHTML = '';
  const canvasRenderer = L.canvas({ padding: 0.5 });
  const map = L.map(container, { preferCanvas: true, renderer: canvasRenderer, zoomControl: true });
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);
  const styles = {
    'Operator B': { color: '#172033', fillColor: '#172033', radius: 4 },
    'Selected X': { color: '#9a6700', fillColor: '#d6a526', radius: 7 },
    'Redundant X': { color: '#a93a36', fillColor: '#e8a19d', radius: 5 },
    'Eligible X': { color: '#2563a6', fillColor: '#8fb9dd', radius: 5 },
    'Filtered X': { color: '#8d99a8', fillColor: '#d5dce4', radius: 4 }
  };
  const layers = Object.fromEntries(Object.keys(styles).map(name => [name, L.layerGroup().addTo(map)]));
  const bounds = data.sites.map(site => [site.latitude, site.longitude]);
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[character]);
  const number = value => new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value);
  const sitePriority = { 'Operator B': 0, 'Filtered X': 1, 'Eligible X': 2, 'Redundant X': 3, 'Selected X': 4 };
  const sites = [...data.sites].sort((a, b) => sitePriority[a.status] - sitePriority[b.status]);
  const statusCounts = sites.reduce((counts, site) => {
    counts[site.status] = (counts[site.status] || 0) + 1;
    return counts;
  }, {});
  const addSite = site => {
    const selectedSpacing = site.nearest_selected_x_km == null ? 'First portfolio site' : `${number(site.nearest_selected_x_km)} km`;
    const marker = L.circleMarker([site.latitude, site.longitude], {
      ...styles[site.status], renderer: canvasRenderer,
      fillOpacity: site.status === 'Selected X' ? 0.96 : (site.status === 'Filtered X' ? 0.45 : 0.82),
      weight: site.status === 'Selected X' ? 2 : 1.2
    });
    marker.on('click', () => {
      if (!marker.getPopup()) {
        const detail = site.gap_km == null ? '' : `<br>Nearest B: ${escapeHtml(site.nearest_b)}<br>Gap: ${number(site.gap_km)} km<br>Density ratio: ${number(site.density_ratio)}<br>Local X spacing: ${number(site.local_x_spacing_km)} km<br>Nearest selected X: ${selectedSpacing}<br>Marginal score: ${number(site.marginal_score)}`;
        marker.bindPopup(`<strong>${escapeHtml(site.siteid)}</strong><br>${escapeHtml(site.status)}${detail}`);
      }
      marker.openPopup();
    });
    layers[site.status].addLayer(marker);
  };
  const labelledLayers = Object.fromEntries(
    Object.entries(layers).map(([name, layer]) => [`${name} (${statusCounts[name] || 0})`, layer])
  );
  L.control.layers(null, labelledLayers, { collapsed: false }).addTo(map);
  if (bounds.length) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 14 });
  let rendered = 0;
  const batchSize = 750;
  const renderBatch = () => {
    const end = Math.min(rendered + batchSize, sites.length);
    for (; rendered < end; rendered += 1) addSite(sites[rendered]);
    if (performanceChip) performanceChip.textContent = `${rendered.toLocaleString()} / ${sites.length.toLocaleString()} sites`;
    if (rendered < sites.length) {
      requestAnimationFrame(renderBatch);
    } else {
      requestAnimationFrame(() => {
        if (performanceChip) performanceChip.textContent = `${sites.length.toLocaleString()} sites · ${((performance.now() - renderStarted) / 1000).toFixed(2)}s`;
      });
    }
  };
  tiles.on('tileerror', () => container.classList.add('tiles-unavailable'));
  requestAnimationFrame(renderBatch);
})();
