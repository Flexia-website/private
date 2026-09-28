document.addEventListener('DOMContentLoaded', () => {
  const sidebar = document.querySelector('.admin-sidebar');
  const burger = document.querySelector('.admin-hamburger');
  if (!sidebar || !burger) return;

  const backdrop = document.createElement('div');
  backdrop.className = 'admin-backdrop';
  document.body.appendChild(backdrop);

  function openDrawer() {
    sidebar.classList.add('open');
    backdrop.classList.add('show');
  }
  function closeDrawer() {
    sidebar.classList.remove('open');
    backdrop.classList.remove('show');
  }
  burger.addEventListener('click', () => {
    sidebar.classList.contains('open') ? closeDrawer() : openDrawer();
  });
  backdrop.addEventListener('click', closeDrawer);
  sidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', closeDrawer));
});

document.addEventListener('click', e => {
  if (e.target.closest('.modal')) {
    if (e.target.classList.contains('modal')) e.target.classList.add('hidden');
  }
});
