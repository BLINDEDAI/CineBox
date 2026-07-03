// Marca pre-render anti-flash: la bienvenida es ahora una LANDING de marketing que
// se muestra SIEMPRE que no haya sesión (no solo la primera visita). El estado de
// sesión solo se conoce de forma asíncrona (Supabase getSession), así que aquí, de
// forma síncrona en <head> y antes de pintar, usamos un proxy: si existe en
// localStorage el token de sesión de Supabase (clave `sb-<ref>-auth-token`) el
// usuario está (probablemente) logueado → ocultamos la landing pre-paint para evitar
// el flash landing→app. Si no hay token, la landing se muestra de inmediato.
// app.js corrige el caso borde (token presente pero caducado) mostrando la landing.
try {
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.startsWith("sb-") && k.endsWith("-auth-token")) {
      document.documentElement.classList.add("cinephora-authed");
      break;
    }
  }
} catch (e) { /* localStorage inaccesible → landing visible (fail-open, sin flash grave) */ }
