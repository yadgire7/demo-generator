// Larkspur Market works without this script. With it: the product picture follows the chosen
// colour, and checkout's new-address fields show only when a new address is chosen.

document.addEventListener('DOMContentLoaded', () => {
  const image = document.getElementById('product-image');
  const colourName = document.getElementById('colour-name');
  if (image) {
    const name = image.alt.replace(/ in .*$/, '');
    document.querySelectorAll('input[name="colour"]').forEach((radio) => {
      radio.addEventListener('change', () => {
        const slug = radio.value.toLowerCase().replace(/ /g, '-');
        image.src = `/img/${image.dataset.slug}/${slug}.svg`;
        image.alt = `${name} in ${radio.value}`;
        if (colourName) colourName.textContent = radio.value;
      });
    });
  }

  const fields = document.querySelector('.new-address');
  const choices = document.querySelectorAll('input[name="address"][type="radio"]');
  if (fields && choices.length) {
    const update = () => {
      const chosen = document.querySelector('input[name="address"]:checked');
      fields.hidden = Boolean(chosen && chosen.value !== 'new');
    };
    choices.forEach((radio) => radio.addEventListener('change', update));
    update();
  }
});
