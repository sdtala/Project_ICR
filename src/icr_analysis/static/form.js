(() => {
  const mode = document.querySelector('#mode');
  if (!mode) return;
  const criterion = document.querySelector('#criterion');
  const prefilter = document.querySelector('#prefilter');
  const countField = document.querySelector('#count-field');
  const prefilterField = document.querySelector('#prefilter-field');
  const thresholdField = document.querySelector('#threshold-field');
  const thresholdUnit = document.querySelector('#threshold-unit');
  const thresholdPortfolioField = document.querySelector('#threshold-portfolio-field');
  const thresholdPortfolio = document.querySelector('#threshold-portfolio');
  const minimumSeparation = document.querySelector('#min-separation-ratio');

  function updateForm() {
    const fixed = mode.value === 'fixed_count';
    countField.hidden = !fixed;
    countField.querySelector('input').disabled = !fixed;
    prefilterField.hidden = !fixed;
    prefilter.disabled = !fixed;
    const thresholdType = fixed ? prefilter.value : criterion.value;
    const needsThreshold = !fixed || thresholdType !== 'none';
    thresholdField.hidden = !needsThreshold;
    const thresholdInput = thresholdField.querySelector('input');
    thresholdInput.disabled = !needsThreshold;
    thresholdInput.required = needsThreshold;
    thresholdUnit.textContent = thresholdType === 'absolute' ? '(km)' : '(ratio)';
    thresholdPortfolioField.hidden = fixed;
    thresholdPortfolio.disabled = fixed;
  }
  [mode, criterion, prefilter].forEach(control => control.addEventListener('change', updateForm));
  thresholdPortfolio.addEventListener('change', () => {
    if (thresholdPortfolio.value === 'declustered' && Number(minimumSeparation.value) <= 0) minimumSeparation.value = '0.5';
    updateForm();
  });
  updateForm();

  document.querySelectorAll('[data-analysis-form]').forEach(form => form.addEventListener('submit', () => {
    const overlay = document.querySelector('#processing');
    if (overlay) overlay.hidden = false;
  }));
})();
