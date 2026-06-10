// Marca pre-render: añade la clase antes de pintar para evitar el flash de la
// pantalla de bienvenida en visitantes recurrentes. Debe ejecutarse de forma
// síncrona en <head> (sin defer), igual que el script inline original.
if (localStorage.getItem("cinebox_visited")) document.documentElement.classList.add("cinebox-visited");
