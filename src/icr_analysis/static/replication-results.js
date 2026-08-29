(() => {
  const container = document.querySelector('#replication-map');
  const source = document.querySelector('#replication-map-data');
  if (!container || !source) return;
  if (!window.L) {
    container.innerHTML = '<div class="map-error">The local map library could not load. Replication CSV outputs remain available below.</div>';
    return;
  }
  const started = performance.now();
  const performanceChip = document.querySelector('#replication-map-performance');
  const data = JSON.parse(source.textContent);
  container.innerHTML = '';
  const renderer = L.canvas({ padding: 0.5 });
  const map = L.map(container, { preferCanvas: true, renderer, zoomControl: true });
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);
  const styles = {
    'Operator B': { color: '#172033', fillColor: '#172033', radius: 4, fillOpacity: 0.80, weight: 1.2 },
    'Selected X footprint': { color: '#7a5b12', fillColor: '#d6a526', radius: 7, fillOpacity: 0.96, weight: 2 },
    'B overlap removed': { color: '#a93a36', fillColor: '#f0a6a1', radius: 6, fillOpacity: 0.88, weight: 1.8 },
    'X not retained': { color: '#708090', fillColor: '#cbd5df', radius: 4, fillOpacity: 0.42, weight: 1 }
  };
  const layers = Object.fromEntries(Object.keys(styles).map(name => [name, L.layerGroup().addTo(map)]));
  const priority = { 'Operator B': 0, 'X not retained': 1, 'B overlap removed': 2, 'Selected X footprint': 3 };
  const sites = [...data.sites].sort((a, b) => priority[a.status] - priority[b.status]);
  const counts = sites.reduce((result, site) => {
    result[site.status] = (result[site.status] || 0) + 1;
    return result;
  }, {});
  const labelledLayers = Object.fromEntries(
    Object.entries(layers).map(([name, layer]) => [`${name} (${counts[name] || 0})`, layer])
  );
  L.control.layers(null, labelledLayers, { collapsed: false }).addTo(map);
  const bounds = sites.map(site => [site.latitude, site.longitude]);
  if (bounds.length) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 14 });
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[character]);
  const number = value => new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value);
  const addSite = site => {
    const marker = L.circleMarker([site.latitude, site.longitude], { ...styles[site.status], renderer });
    marker.on('click', () => {
      if (!marker.getPopup()) {
        const detail = site.status === 'Operator B' ? '' : `<br>Zone: ${escapeHtml(site.zone_id)}<br>Nearest B: ${escapeHtml(site.nearest_b)}<br>Distance to B: ${number(site.nearest_b_distance_km)} km<br>Overlap distance: ${number(site.overlap_threshold_km)} km<br>Local X spacing: ${number(site.local_x_spacing_km)} km<br>${escapeHtml(site.selection_reason)}`;
        marker.bindPopup(`<strong>${escapeHtml(site.siteid)}</strong><br>${escapeHtml(site.status)}${detail}`);
      }
      marker.openPopup();
    });
    layers[site.status].addLayer(marker);
  };
  let rendered = 0;
  const batchSize = 750;
  const renderBatch = () => {
    const end = Math.min(rendered + batchSize, sites.length);
    for (; rendered < end; rendered += 1) addSite(sites[rendered]);
    if (performanceChip) performanceChip.textContent = `${rendered.toLocaleString()} / ${sites.length.toLocaleString()} sites`;
    if (rendered < sites.length) requestAnimationFrame(renderBatch);
    else requestAnimationFrame(() => {
      if (performanceChip) performanceChip.textContent = `${sites.length.toLocaleString()} sites · ${((performance.now() - started) / 1000).toFixed(2)}s`;
    });
  };
  tiles.on('tileerror', () => container.classList.add('tiles-unavailable'));
  requestAnimationFrame(renderBatch);
})();
