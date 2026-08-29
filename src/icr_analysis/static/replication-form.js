(() => {
  const overlapMode = document.querySelector('#overlap-mode');
  const ratioField = document.querySelector('#overlap-ratio-field');
  const kilometreField = document.querySelector('#overlap-km-field');
  const updateOverlapFields = () => {
    if (!overlapMode || !ratioField || !kilometreField) return;
    const fixed = overlapMode.value === 'fixed';
    ratioField.hidden = fixed;
    kilometreField.hidden = !fixed;
  };
  overlapMode?.addEventListener('change', updateOverlapFields);
  updateOverlapFields();
  document.querySelectorAll('[data-replication-form]').forEach(form => {
    form.addEventListener('submit', () => {
      const processing = document.querySelector('#processing');
      if (processing) processing.hidden = false;
    });
  });
})();
