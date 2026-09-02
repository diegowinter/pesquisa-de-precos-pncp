// Submenus do cabeçalho (tela estreita). O acordeão — abrir um fecha os outros — é nativo,
// via `name="menu"` nos <details>; aqui fica só o que o HTML não faz: fechar ao clicar fora
// e ao apertar Esc, senão o submenu fica pendurado sobre a página depois de navegar.
(() => {
  const grupos = () => document.querySelectorAll("header.topo details.menu-grupo[open]");
  const fechar = (exceto) => grupos().forEach((d) => { if (d !== exceto) d.open = false; });

  document.addEventListener("click", (evento) => {
    fechar(evento.target.closest("details.menu-grupo"));
  });
  document.addEventListener("keydown", (evento) => {
    if (evento.key === "Escape") fechar(null);
  });
})();
