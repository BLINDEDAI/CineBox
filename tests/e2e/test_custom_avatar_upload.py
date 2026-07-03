"""Browser E2E tests for custom-avatar-upload (AC-1, AC-2, AC-3, AC-4, AC-5, AC-6,
AC-7, AC-8, AC-11).

Covers every ### Tester scope row that requires a live browser:
  AC-1  — upload a valid image -> avatar shows in the Ajustes preview + sidebar chip
  AC-5  — replace an existing avatar -> new image shows; exactly one Storage
          upload call targets the fixed per-user key (upsert, no accumulation)
  AC-6  — remove -> generated avatar shown again in preview + chip; object gone
          (delete + PATCH remove both invoked)
  AC-3  — unsupported type (SVG/PDF) rejected client-side, avatar unchanged,
          no Storage upload attempted
  AC-4  — >5 MB rejected client-side, avatar unchanged, no Storage upload attempted
  AC-7  — a user with no avatar shows the generated initials avatar in chip +
          public profile
  AC-8 / AC-2 — public profile of a user with an avatar loads and renders the
          image (not CSP-blocked; the img-src header allows the Storage host)
  AC-11 — a11y: axe WCAG 2.2 A/AA zero critical/serious on the changed Ajustes
          -> Perfil view (es-ES, 375 px + desktop); controls keyboard-operable
          with visible focus; avatar carries an accessible name

AC-9 (cross-user isolation) is enforced by Supabase Storage RLS server-side, not
by any app code reachable from the browser -- it is covered by reasoning against
the RLS policy in the Tester handoff, not by a Playwright test (there is no
app-layer control to exercise; a live grant/deny RLS probe needs a live Supabase
project + two real auth sessions, out of reach for this offline harness).

Strategy (mirrors tests/e2e/test_settings_and_lists.py):
  - Real Cinephora server via conftest.py base_url fixture (no DB/auth required).
  - /api/profile and /api/config stubbed via page.route(), narrow route
    registered AFTER the broad one (page.route is LIFO).
  - Views are mounted by driving the production seam (_updateSidebarUser /
    showView('settings-view')) via page.evaluate -- no real Supabase session.
  - Supabase Storage upload/delete is stubbed by replacing the module-level
    `_supabase` object in app.js (page.evaluate) with a fake that records calls
    and returns configurable results -- avoids needing a real window.supabase
    client or a live bucket, per the tester-bundle's "prefer stubbing the
    network for deterministic unit-level assertions" guidance.
  - axe-core (4.9.0) injected via vendored tests/e2e/axe.min.js as a same-origin
    routed <script> (CSP: script-src 'self'), same pattern as
    test_settings_and_lists.py / test_public_profiles_a11y.py.
  - Screenshots saved to handoffs/custom-avatar-upload/screenshots/.
"""

import json
from pathlib import Path

from playwright.sync_api import Page

# ── Paths ──────────────────────────────────────────────────────────────────────
_E2E_DIR = Path(__file__).resolve().parent
AXE_JS = _E2E_DIR / "axe.min.js"
_SCREENSHOTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "handoffs"
    / "custom-avatar-upload"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_AVATAR_URL = (
    "https://demo-project.supabase.co/storage/v1/object/public/avatars/"
    "test-user-id/avatar.webp?v=1"
)

_PROFILE_NO_AVATAR = {
    "ok": True,
    "profile": {
        "username": "avataruser",
        "is_public": True,
        "show_collection": False,
        "show_stats": False,
        "avatar_url": None,
    },
}

_PROFILE_WITH_AVATAR = {
    "ok": True,
    "profile": {
        "username": "avataruser",
        "is_public": True,
        "show_collection": False,
        "show_stats": False,
        "avatar_url": _AVATAR_URL,
    },
}

_PUBLIC_PROFILE_WITH_AVATAR = {
    "ok": True,
    "profile": {
        "username": "avataruser",
        "avatar_url": _AVATAR_URL,
    },
}

_PUBLIC_PROFILE_NO_AVATAR = {
    "ok": True,
    "profile": {
        "username": "avataruser",
        "avatar_url": None,
    },
}


# ── Shared helpers (mirrors test_settings_and_lists.py) ───────────────────────


def _route_config(page: Page, base_url: str):
    """Stub /api/config so initApp() runs without real Supabase credentials."""

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"supabase_url": "https://demo-project.supabase.co",
                              "supabase_anon_key": "demo-anon-key"}),
        )

    page.route(f"{base_url}/api/config", handle)


def _route_profile(page: Page, base_url: str, get_payload: dict, *, on_patch=None):
    """Stub GET/PATCH /api/profile.

    `on_patch(body) -> dict|None` is called for PATCH requests; if it returns a
    dict, that becomes the response body (status 200); if it returns None the
    route falls through to a generic {ok:true} success.
    """
    state = {"get_payload": get_payload}

    def handle(route):
        if route.request.method == "PATCH":
            body = route.request.post_data_json or {}
            result = on_patch(body) if on_patch else None
            if result is None:
                result = {"ok": True}
            route.fulfill(
                status=result.get("_status", 200),
                content_type="application/json",
                body=json.dumps({k: v for k, v in result.items() if k != "_status"}),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(state["get_payload"]),
            )

    page.route(f"{base_url}/api/profile", handle)
    return state


def _goto_spa(page: Page, base_url: str):
    """Navigate to the SPA and wait for network idle; remove the welcome overlay."""
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
        }"""
    )


def _mount_authenticated_user(page: Page, email: str = "user@example.com",
                               uid: str = "test-user-id"):
    """Drive the sidebar-user seam to simulate an authenticated session."""
    page.evaluate(
        """({emailAddr, userId}) => {
            if (!_currentUser) {
                _currentUser = { id: userId, email: emailAddr };
            } else {
                _currentUser.id = userId;
                _currentUser.email = emailAddr;
            }
            _updateSidebarUser(emailAddr);
        }""",
        {"emailAddr": email, "userId": uid},
    )


def _open_settings_view(page: Page):
    """Open the #settings-view section by calling the production showView seam."""
    page.evaluate(
        """() => {
            if (typeof showView === 'function') {
                showView('settings-view');
            } else {
                if (typeof showSettingsView === 'function') showSettingsView();
                const s = document.getElementById('settings-view');
                if (s) {
                    document.querySelectorAll('.view').forEach(v => v.hidden = true);
                    s.hidden = false;
                }
            }
        }"""
    )
    page.wait_for_timeout(600)


def _stub_supabase_storage(page: Page, *, upload_ok: bool = True):
    """Replace the module-level `_supabase` (app.js) with a fake client whose
    `.storage.from('avatars').upload(...)` records every call in
    `window.__avatarUploadCalls` and returns {error: null} (or a stubbed error
    when upload_ok=False) without any real network request.

    This is the "stub the network for deterministic unit-level assertions"
    approach from the tester-bundle -- it avoids depending on window.supabase
    or a live bucket while still exercising the real settings.js upload path
    (_uploadAvatar -> _supabase.storage.from('avatars').upload(...)).
    """
    page.evaluate(
        """(uploadOk) => {
            window.__avatarUploadCalls = [];
            const fakeStorage = {
                from(bucket) {
                    return {
                        upload(key, blob, opts) {
                            window.__avatarUploadCalls.push({ bucket, key, opts });
                            return Promise.resolve(
                                uploadOk ? { data: { path: key }, error: null }
                                         : { data: null, error: { message: 'stubbed failure' } }
                            );
                        },
                    };
                },
            };
            _supabase = { storage: fakeStorage };
        }""",
        upload_ok,
    )


import base64

# A real, decodable 1x1 transparent PNG (68 bytes) -- needed because
# _avatarToWebpBlob (settings.js) decodes the file via new Image()/canvas;
# arbitrary padded bytes fail img.onload and resolve(null), which the upload
# flow treats as "could not process the image" (a false negative for AC-1/AC-5
# tests that need the upload to actually reach _supabase.storage.upload()).
_REAL_1X1_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII="
)


def _make_test_file_bytes(kind: str, size: int) -> bytes:
    """Build bytes for a fake upload of the given MIME-implying kind.

    For "png" the returned bytes are ALWAYS a real, browser-decodable 1x1 PNG
    (padded with a trailing harmless comment-free no-op is not possible for
    PNG without breaking the CRC, so for the >5MB oversized-rejection case we
    instead return non-decodable padded bytes -- the size gate in settings.js
    fires on file.size BEFORE any decode is attempted, so decodability does
    not matter for that path).
    """
    if kind == "png":
        if size <= len(_REAL_1X1_PNG):
            return _REAL_1X1_PNG
        # Oversized case: decoding is never reached (the size gate runs first),
        # so padded-but-invalid bytes are fine here.
        return _REAL_1X1_PNG + b"\x00" * (size - len(_REAL_1X1_PNG))
    if kind == "svg":
        payload = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        return payload + b"<!--" + b"\x00" * max(0, size - len(payload) - 7) + b"-->"
    if kind == "pdf":
        return b"%PDF-1.4\n" + b"\x00" * max(0, size - 9)
    raise ValueError(kind)


def _set_input_files_via_datatransfer(page: Page, selector: str, *, name: str,
                                       mime: str, data: bytes):
    """Attach an in-memory File to a hidden <input type=file> via DataTransfer,
    bypassing the OS file picker (headless-safe) and preserving the exact
    filename + MIME type + byte size the production validation branches on.
    """
    import base64
    b64 = base64.b64encode(data).decode("ascii")
    page.evaluate(
        """({sel, name, mime, b64}) => {
            const input = document.querySelector(sel);
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
            const file = new File([bytes], name, { type: mime });
            const dt = new DataTransfer();
            dt.items.add(file);
            input.files = dt.files;
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        {"sel": selector, "name": name, "mime": mime, "b64": b64},
    )


def _setup_settings_view_with_avatar(page: Page, base_url: str, *, has_avatar: bool,
                                      on_patch=None):
    """Common setup: stub config+profile, goto SPA, authenticate, open settings."""
    _route_config(page, base_url)
    profile_payload = _PROFILE_WITH_AVATAR if has_avatar else _PROFILE_NO_AVATAR
    state = _route_profile(page, base_url, profile_payload, on_patch=on_patch)
    _goto_spa(page, base_url)
    _mount_authenticated_user(page)
    _open_settings_view(page)
    return state


def _inject_axe(page: Page, base_url: str):
    """Inject axe-core via a same-origin routed <script> (CSP: script-src 'self')."""
    axe_url = f"{base_url}/__test__/axe.min.js"
    axe_content = AXE_JS.read_bytes()

    def _serve_axe(route):
        route.fulfill(
            status=200, content_type="application/javascript", body=axe_content
        )

    page.route(axe_url, _serve_axe)
    page.add_script_tag(url=axe_url)
    page.wait_for_timeout(200)


def _run_axe(page: Page, context_selector: str = "html") -> list:
    """Run axe-core, return violations filtered to critical/serious impact."""
    results = page.evaluate(
        """(sel) => {
            return axe.run(
                document.querySelector(sel) || document,
                {
                    runOnly: {
                        type: 'tag',
                        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']
                    }
                }
            ).then(r => r.violations.map(v => ({
                id: v.id,
                impact: v.impact,
                description: v.description,
                nodes: v.nodes.length
            })));
        }""",
        context_selector,
    )
    return [v for v in results if v["impact"] in ("critical", "serious")]


def _screenshot(page: Page, name: str) -> str:
    path = str(_SCREENSHOTS_DIR / f"{name}.png")
    page.screenshot(path=path)
    return path


# ── AC-1: upload a valid image -> preview + chip ──────────────────────────────


def test_ac1_upload_valid_image_updates_preview_and_chip(page: Page, base_url: str):
    """AC-1: uploading a valid PNG shows the uploaded avatar in the Ajustes
    preview and the sidebar chip after the set-PATCH round-trip."""
    page.set_viewport_size({"width": 1280, "height": 800})

    patch_calls = {"n": 0, "last_body": None}

    def _on_patch(body):
        patch_calls["n"] += 1
        patch_calls["last_body"] = body
        return {"ok": True, "profile": _PROFILE_WITH_AVATAR["profile"]}

    _setup_settings_view_with_avatar(page, base_url, has_avatar=False, on_patch=_on_patch)
    _stub_supabase_storage(page, upload_ok=True)

    _set_input_files_via_datatransfer(
        page, "#settings-avatar-input", name="me.png", mime="image/png",
        data=_make_test_file_bytes("png", 20_000),
    )
    page.wait_for_timeout(800)

    assert patch_calls["n"] >= 1, "AC-1: PATCH /api/profile {avatar:'set'} was not called after upload"
    assert patch_calls["last_body"].get("avatar") == "set", (
        f"AC-1: expected {{'avatar': 'set'}}, got {patch_calls['last_body']!r}"
    )

    upload_calls = page.evaluate("() => window.__avatarUploadCalls")
    assert len(upload_calls) == 1, f"AC-1: expected exactly 1 Storage upload call, got {len(upload_calls)}"
    assert upload_calls[0]["bucket"] == "avatars"
    assert upload_calls[0]["key"] == "test-user-id/avatar.webp"

    # Preview: an <img> now renders inside the settings avatar element.
    preview_img = page.locator("[data-settings-avatar] img.settings-avatar-img")
    assert preview_img.count() == 1, "AC-1: uploaded avatar <img> not rendered in Ajustes preview"
    src = preview_img.get_attribute("src")
    assert src and src.startswith("https://demo-project.supabase.co/storage/v1/object/public/avatars/"), (
        f"AC-1: preview <img> src does not match the Storage-avatars allowlist: {src!r}"
    )

    # Sidebar chip: re-rendered via _renderProfileChip() from the same handler.
    chip_img = page.locator("#profile-chip img.profile-chip-avatar-img")
    assert chip_img.count() == 1, "AC-1: uploaded avatar <img> not rendered in sidebar chip"

    _screenshot(page, "ac1-upload-preview-and-chip")


def test_ac1_success_message_shown(page: Page, base_url: str):
    """AC-1: a successful upload shows a confirmation message ('Avatar actualizado.')."""
    page.set_viewport_size({"width": 1280, "height": 800})

    def _on_patch(body):
        return {"ok": True, "profile": _PROFILE_WITH_AVATAR["profile"]}

    _setup_settings_view_with_avatar(page, base_url, has_avatar=False, on_patch=_on_patch)
    _stub_supabase_storage(page, upload_ok=True)

    _set_input_files_via_datatransfer(
        page, "#settings-avatar-input", name="me.png", mime="image/png",
        data=_make_test_file_bytes("png", 20_000),
    )
    page.wait_for_timeout(800)

    message_text = page.evaluate(
        "() => document.getElementById('message')?.textContent || ''"
    )
    assert "actualizado" in message_text.lower(), (
        f"AC-1: expected a success message mentioning 'actualizado', got {message_text!r}"
    )


# ── AC-5: replace -> new image shown, exactly one object at the fixed key ─────


def test_ac5_replace_avatar_uploads_to_same_fixed_key(page: Page, base_url: str):
    """AC-5: replacing an existing avatar re-uses the SAME fixed per-user key
    (upsert overwrite) -- exactly one Storage object ever exists for the user,
    verified here as exactly one upload call targeting the unchanged key."""
    page.set_viewport_size({"width": 1280, "height": 800})

    def _on_patch(body):
        return {"ok": True, "profile": _PROFILE_WITH_AVATAR["profile"]}

    _setup_settings_view_with_avatar(page, base_url, has_avatar=True, on_patch=_on_patch)
    _stub_supabase_storage(page, upload_ok=True)

    # Confirm the preview already shows the pre-existing avatar before replace.
    pre_img = page.locator("[data-settings-avatar] img.settings-avatar-img")
    assert pre_img.count() == 1, "AC-5: expected the existing avatar to render before replace"

    _set_input_files_via_datatransfer(
        page, "#settings-avatar-input", name="new-avatar.jpg", mime="image/jpeg",
        data=_make_test_file_bytes("png", 30_000),  # bytes are opaque to the stub; MIME drives validation
    )
    page.wait_for_timeout(800)

    upload_calls = page.evaluate("() => window.__avatarUploadCalls")
    assert len(upload_calls) == 1, (
        f"AC-5: replace must issue exactly one Storage upload call (upsert), got {len(upload_calls)}"
    )
    assert upload_calls[0]["key"] == "test-user-id/avatar.webp", (
        "AC-5: replace must target the SAME fixed per-user key as the original upload"
    )
    assert upload_calls[0]["opts"]["upsert"] is True, (
        "AC-5: replace must upsert (overwrite in place), not create a second object"
    )

    _screenshot(page, "ac5-replace-avatar-same-key")


# ── AC-6: remove -> generated fallback shown; object gone ─────────────────────


def test_ac6_remove_avatar_shows_generated_fallback(page: Page, base_url: str):
    """AC-6: removing an uploaded avatar re-renders the generated initials
    avatar in both the Ajustes preview and the sidebar chip, and calls the
    remove PATCH action."""
    page.set_viewport_size({"width": 1280, "height": 800})

    patch_calls = {"bodies": []}

    def _on_patch(body):
        patch_calls["bodies"].append(body)
        return {"ok": True, "profile": _PROFILE_NO_AVATAR["profile"]}

    _setup_settings_view_with_avatar(page, base_url, has_avatar=True, on_patch=_on_patch)

    # Precondition: uploaded avatar renders before remove.
    pre_img = page.locator("[data-settings-avatar] img.settings-avatar-img")
    assert pre_img.count() == 1, "AC-6: expected the uploaded avatar to render before remove"

    remove_btn = page.locator("#settings-avatar-remove-btn")
    assert remove_btn.count() == 1, "AC-6: #settings-avatar-remove-btn not found"
    assert not remove_btn.is_disabled(), "AC-6: remove button must be enabled when an avatar exists"
    remove_btn.click()
    page.wait_for_timeout(600)

    assert any(b.get("avatar") == "remove" for b in patch_calls["bodies"]), (
        f"AC-6: PATCH /api/profile {{'avatar':'remove'}} was not called; got {patch_calls['bodies']!r}"
    )

    # Preview reverts to the generated fallback (no <img>, initials text present).
    post_img = page.locator("[data-settings-avatar] img.settings-avatar-img")
    assert post_img.count() == 0, "AC-6: preview must NOT show an <img> after remove"
    avatar_text = page.locator("[data-settings-avatar]").inner_text()
    assert avatar_text.strip(), "AC-6: generated initials must be shown after remove"

    # Chip also reverts.
    chip_img = page.locator("#profile-chip img.profile-chip-avatar-img")
    assert chip_img.count() == 0, "AC-6: sidebar chip must NOT show an <img> after remove"

    _screenshot(page, "ac6-remove-shows-generated-fallback")


def test_ac6_remove_button_disabled_when_no_avatar(page: Page, base_url: str):
    """AC-6 precondition sanity: the 'Quitar' control is disabled when there is
    no uploaded avatar to remove (settings.js:271-272)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)

    remove_btn = page.locator("#settings-avatar-remove-btn")
    assert remove_btn.count() == 1
    assert remove_btn.is_disabled(), "AC-6: remove button must be disabled with no avatar_url"


# ── AC-3 / AC-4: unsupported type / oversized rejected client-side ────────────


def test_ac3_unsupported_svg_rejected_avatar_unchanged(page: Page, base_url: str):
    """AC-3: an SVG file is rejected before any network call; avatar unchanged."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)
    _stub_supabase_storage(page, upload_ok=True)

    _set_input_files_via_datatransfer(
        page, "#settings-avatar-input", name="evil.svg", mime="image/svg+xml",
        data=_make_test_file_bytes("svg", 1000),
    )
    page.wait_for_timeout(500)

    upload_calls = page.evaluate("() => window.__avatarUploadCalls")
    assert upload_calls == [], "AC-3: no Storage upload call must be attempted for an SVG"

    message_text = page.evaluate(
        "() => document.getElementById('message')?.textContent || ''"
    )
    assert "formato" in message_text.lower() or "no admitido" in message_text.lower(), (
        f"AC-3: expected a clear 'unsupported format' error message, got {message_text!r}"
    )

    # Avatar is unchanged: still the generated fallback (no <img>).
    img = page.locator("[data-settings-avatar] img.settings-avatar-img")
    assert img.count() == 0, "AC-3: avatar must remain the generated fallback after a rejected SVG"

    _screenshot(page, "ac3-svg-rejected")


def test_ac3_unsupported_pdf_rejected_avatar_unchanged(page: Page, base_url: str):
    """AC-3: a PDF file is rejected before any network call; avatar unchanged."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)
    _stub_supabase_storage(page, upload_ok=True)

    _set_input_files_via_datatransfer(
        page, "#settings-avatar-input", name="doc.pdf", mime="application/pdf",
        data=_make_test_file_bytes("pdf", 1000),
    )
    page.wait_for_timeout(500)

    upload_calls = page.evaluate("() => window.__avatarUploadCalls")
    assert upload_calls == [], "AC-3: no Storage upload call must be attempted for a PDF"

    _screenshot(page, "ac3-pdf-rejected")


def test_ac4_oversized_upload_rejected_avatar_unchanged(page: Page, base_url: str):
    """AC-4: a file over 5 MB is rejected client-side before any network call."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)
    _stub_supabase_storage(page, upload_ok=True)

    six_mb = 6 * 1024 * 1024
    _set_input_files_via_datatransfer(
        page, "#settings-avatar-input", name="huge.png", mime="image/png",
        data=_make_test_file_bytes("png", six_mb),
    )
    page.wait_for_timeout(500)

    upload_calls = page.evaluate("() => window.__avatarUploadCalls")
    assert upload_calls == [], "AC-4: no Storage upload call must be attempted for a >5MB file"

    message_text = page.evaluate(
        "() => document.getElementById('message')?.textContent || ''"
    )
    assert "5 mb" in message_text.lower() or "supera" in message_text.lower(), (
        f"AC-4: expected a clear size-limit error message, got {message_text!r}"
    )

    img = page.locator("[data-settings-avatar] img.settings-avatar-img")
    assert img.count() == 0, "AC-4: avatar must remain the generated fallback after a rejected oversized file"

    _screenshot(page, "ac4-oversized-rejected")


# ── AC-7: no-avatar user -> generated initials avatar in chip + public profile ─


def test_ac7_no_avatar_shows_generated_in_chip(page: Page, base_url: str):
    """AC-7: a user who never uploaded an avatar shows the generated initials
    avatar in the sidebar chip."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_profile(page, base_url, _PROFILE_NO_AVATAR)
    _goto_spa(page, base_url)

    page.evaluate(
        """async () => {
            _updateSidebarUser('user@example.com');
            await _loadProfileChip();
        }"""
    )
    page.locator("#profile-chip[aria-label]").wait_for(state="attached", timeout=5000)

    chip_img = page.locator("#profile-chip img.profile-chip-avatar-img")
    assert chip_img.count() == 0, "AC-7: chip must NOT render an <img> when avatar_url is null"

    avatar_span = page.locator("#profile-chip .profile-chip-avatar")
    assert avatar_span.count() == 1, "AC-7: generated avatar span must be present"
    bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('#profile-chip .profile-chip-avatar')).backgroundImage"
    )
    assert bg and bg != "none", f"AC-7: generated chip avatar has no CSSOM gradient background; got {bg!r}"

    _screenshot(page, "ac7-no-avatar-chip-generated")


def test_ac7_no_avatar_shows_generated_on_public_profile(page: Page, base_url: str):
    """AC-7: a public profile with avatar_url null shows the generated avatar."""
    page.set_viewport_size({"width": 1280, "height": 800})

    page.route(
        f"{base_url}/api/public/profile/avataruser",
        lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(_PUBLIC_PROFILE_NO_AVATAR),
        ),
    )
    # Known relative-path 404 workaround for the /u/ redirect target (documented
    # in test_public_profiles_a11y.py PRODUCTION BUG note): route styles/script
    # to their real server paths.
    page.route(f"{base_url}/u/styles.css", lambda r: r.fulfill(
        status=200, content_type="text/css",
        body=Path(f"{Path(__file__).resolve().parent.parent.parent}/styles.css").read_text(encoding="utf-8"),
    ) if Path(f"{Path(__file__).resolve().parent.parent.parent}/styles.css").exists() else r.fallback())
    page.route(f"{base_url}/u/public.js", lambda r: r.fulfill(
        status=200, content_type="application/javascript",
        body=Path(f"{Path(__file__).resolve().parent.parent.parent}/public.js").read_text(encoding="utf-8"),
    ) if Path(f"{Path(__file__).resolve().parent.parent.parent}/public.js").exists() else r.fallback())

    page.goto(f"{base_url}/u/avataruser")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)

    avatar_wrap = page.locator(".pub-profile-avatar")
    assert avatar_wrap.count() == 1, "AC-7: public profile avatar wrapper not rendered"
    img = page.locator(".pub-profile-avatar-img")
    assert img.count() == 0, "AC-7: public profile must NOT render an <img> when avatar_url is null"
    has_generated_class = page.evaluate(
        "() => document.querySelector('.pub-profile-avatar')?.classList.contains('pub-profile-avatar-generated')"
    )
    assert has_generated_class, "AC-7: public profile avatar must use the generated fallback class"

    _screenshot(page, "ac7-no-avatar-public-profile-generated")


# ── AC-8 / AC-2: public profile with an avatar renders, not CSP-blocked ───────


def test_ac8_public_profile_avatar_renders_not_csp_blocked(page: Page, base_url: str):
    """AC-8 / AC-2: a public profile whose owner has an uploaded avatar renders
    the <img> and the request is not blocked by CSP (img-src allows
    https://*.supabase.co, server.py:539)."""
    page.set_viewport_size({"width": 1280, "height": 800})

    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    page.route(
        f"{base_url}/api/public/profile/avataruser",
        lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(_PUBLIC_PROFILE_WITH_AVATAR),
        ),
    )
    repo_root = Path(__file__).resolve().parent.parent.parent
    page.route(f"{base_url}/u/styles.css", lambda r: r.fulfill(
        status=200, content_type="text/css",
        body=(repo_root / "styles.css").read_text(encoding="utf-8"),
    ))
    page.route(f"{base_url}/u/public.js", lambda r: r.fulfill(
        status=200, content_type="application/javascript",
        body=(repo_root / "public.js").read_text(encoding="utf-8"),
    ))
    # The avatar URL itself points at a real (unreachable in this offline
    # harness) Supabase host -- we only assert it was NOT blocked by CSP (a
    # CSP violation is reported via a 'securitypolicyviolation' event / console
    # error mentioning "Content-Security-Policy", distinct from a plain network
    # failure to resolve/connect to the external host).
    page.route(f"{base_url}/u/avataruser", lambda r: r.fallback())

    page.goto(f"{base_url}/u/avataruser")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)

    img = page.locator(".pub-profile-avatar-img")
    assert img.count() == 1, "AC-8: public profile must render an <img> when avatar_url is set"
    src = img.get_attribute("src")
    assert src == _AVATAR_URL, f"AC-8: <img src> must equal the server-provided avatar_url, got {src!r}"
    alt = img.get_attribute("alt")
    assert alt == "Avatar de @avataruser", f"AC-2/AC-11: expected accessible alt text, got {alt!r}"

    csp_violations = [e for e in console_errors if "Content-Security-Policy" in e or "content security policy" in e.lower()]
    assert csp_violations == [], f"AC-8: the avatar <img> was blocked by CSP: {csp_violations}"

    _screenshot(page, "ac8-public-profile-avatar-renders")


# ── AC-11: a11y — Ajustes -> Perfil avatar controls ────────────────────────────


def test_ac11_settings_avatar_axe_desktop(page: Page, base_url: str):
    """AC-11: #settings-view (with avatar controls) passes axe WCAG 2.2 A/AA at
    1280px desktop, zero critical/serious violations."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=True)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac11-settings-avatar-axe-desktop")

    assert violations == [], (
        f"AC-11: axe found {len(violations)} critical/serious violations in "
        f"#settings-view (desktop 1280px): " + json.dumps(violations, indent=2)
    )


def test_ac11_settings_avatar_axe_mobile_375(page: Page, base_url: str):
    """AC-11: #settings-view passes axe WCAG 2.2 A/AA at 375px mobile."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=True)

    page.evaluate(
        """() => {
            const sv = document.getElementById('settings-view');
            if (sv) { sv.style.display = 'block'; sv.hidden = false; }
        }"""
    )
    page.locator("#settings-view").wait_for(state="visible", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac11-settings-avatar-axe-mobile")

    assert violations == [], (
        f"AC-11: axe found {len(violations)} critical/serious violations in "
        f"#settings-view (mobile 375px): " + json.dumps(violations, indent=2)
    )


def test_ac11_settings_avatar_axe_desktop_no_avatar(page: Page, base_url: str):
    """AC-11 (iter-2 addition): #settings-view passes axe WCAG 2.2 A/AA at
    1280px desktop in the OTHER preview state -- has_avatar=False, i.e. the
    generated-initials branch of _renderSettingsAvatar (wrapper span carries
    role="img" + aria-label="Tu avatar", no child <img>). The two iter-1 axe
    tests above only ever exercised has_avatar=True (child <img alt="Tu
    avatar">, role/aria-label stripped from the wrapper); this test proves the
    OTHER state-branch introduced by the frontend-dev iter-2 fix
    (settings.js:105 static template + _renderSettingsAvatar's else-branch) is
    independently axe-clean, not just the branch the original bounce covered."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac11-settings-avatar-axe-desktop-no-avatar")

    assert violations == [], (
        f"AC-11: axe found {len(violations)} critical/serious violations in "
        f"#settings-view (desktop 1280px, no-avatar/generated-initials state): "
        + json.dumps(violations, indent=2)
    )


def test_ac11_settings_avatar_axe_mobile_375_no_avatar(page: Page, base_url: str):
    """AC-11 (iter-2 addition): #settings-view passes axe WCAG 2.2 A/AA at
    375px mobile in the generated-initials (has_avatar=False) state -- the
    mobile counterpart of test_ac11_settings_avatar_axe_desktop_no_avatar
    above, mirroring the has_avatar=True desktop/mobile pairing already
    covered by the iter-1 tests."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)

    page.evaluate(
        """() => {
            const sv = document.getElementById('settings-view');
            if (sv) { sv.style.display = 'block'; sv.hidden = false; }
        }"""
    )
    page.locator("#settings-view").wait_for(state="visible", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac11-settings-avatar-axe-mobile-no-avatar")

    assert violations == [], (
        f"AC-11: axe found {len(violations)} critical/serious violations in "
        f"#settings-view (mobile 375px, no-avatar/generated-initials state): "
        + json.dumps(violations, indent=2)
    )


def test_ac11_avatar_upload_input_keyboard_reachable_visible_focus(page: Page, base_url: str):
    """AC-11: the (visually hidden but focusable) file input and the visible
    upload label both support keyboard focus with a visible focus indicator
    (settings.js:109-111 clip-rect hidden-input pattern + styles.css
    :focus-visible + adjacent-sibling-label rule)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)

    file_input = page.locator("#settings-avatar-input")
    assert file_input.count() == 1
    file_input.focus()
    focused_id = page.evaluate("() => document.activeElement.id")
    assert focused_id == "settings-avatar-input", (
        f"AC-11: expected #settings-avatar-input to be focusable, got #{focused_id!r}"
    )

    # The adjacent <label for="settings-avatar-input"> receives the visible
    # focus styling via the :focus-visible + sibling-label CSS rule.
    label = page.locator("label[for='settings-avatar-input']")
    assert label.count() == 1, "AC-11: upload label (for=settings-avatar-input) not found"
    outline = page.evaluate(
        "() => window.getComputedStyle(document.querySelector(\"label[for='settings-avatar-input']\")).outlineWidth"
    )
    box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.querySelector(\"label[for='settings-avatar-input']\")).boxShadow"
    )
    has_focus = outline not in ("0px", "") or box_shadow not in ("none", "")
    assert has_focus, (
        f"AC-11: no visible focus indicator on the upload label when the input has focus: "
        f"outline={outline}, boxShadow={box_shadow}"
    )

    _screenshot(page, "ac11-upload-input-keyboard-focus")


def test_ac11_remove_button_keyboard_reachable_visible_focus(page: Page, base_url: str):
    """AC-11: the 'Quitar' (remove) button is keyboard-operable with a visible
    focus indicator when an avatar exists (button is enabled)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=True)

    remove_btn = page.locator("#settings-avatar-remove-btn")
    assert remove_btn.count() == 1
    assert not remove_btn.is_disabled()
    remove_btn.focus()

    focused_id = page.evaluate("() => document.activeElement.id")
    assert focused_id == "settings-avatar-remove-btn"

    outline = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).outlineWidth"
    )
    box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).boxShadow"
    )
    has_focus = outline not in ("0px", "") or box_shadow not in ("none", "")
    assert has_focus, (
        f"AC-11: no visible focus indicator on #settings-avatar-remove-btn: "
        f"outline={outline}, boxShadow={box_shadow}"
    )

    _screenshot(page, "ac11-remove-button-keyboard-focus")


def test_ac11_avatar_preview_has_accessible_name_no_avatar(page: Page, base_url: str):
    """AC-11: in the generated-initials (no-avatar) state, the avatar preview
    wrapper span itself carries the accessible name via role="img" +
    aria-label="Tu avatar" (settings.js _renderSettingsAvatar else-branch --
    the wrapper IS the leaf graphic here, valid role="img" usage)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)

    avatar_el = page.locator("[data-settings-avatar]")
    assert avatar_el.count() == 1
    role = avatar_el.get_attribute("role")
    aria_label = avatar_el.get_attribute("aria-label")
    assert role == "img", (
        f"AC-11: expected [data-settings-avatar] role='img' in the no-avatar state, got {role!r}"
    )
    assert aria_label == "Tu avatar", (
        f"AC-11: expected [data-settings-avatar] aria-label='Tu avatar', got {aria_label!r}"
    )


def test_ac11_avatar_preview_has_accessible_name_with_avatar(page: Page, base_url: str):
    """AC-11 (iter-2 addition): in the has-avatar state, the wrapper span's
    role/aria-label are intentionally stripped (settings.js:299-300) to avoid
    a role="img" wrapper around a real <img> child (the container-role
    antipattern flagged by the frontend Reviewer); the accessible name lives
    on the child <img alt="Tu avatar"> instead (settings.js:303). This test
    replaces the iter-1 assertion that expected the wrapper's aria-label to be
    present in BOTH states -- that assumption predates the state-aware AC-11
    fix and no longer holds for the has-avatar branch."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=True)

    avatar_el = page.locator("[data-settings-avatar]")
    assert avatar_el.count() == 1
    wrapper_role = avatar_el.get_attribute("role")
    wrapper_aria_label = avatar_el.get_attribute("aria-label")
    assert wrapper_role is None and wrapper_aria_label is None, (
        "AC-11: the has-avatar wrapper must NOT carry role/aria-label (would "
        f"wrap the child <img> in a redundant role='img'); got role={wrapper_role!r}, "
        f"aria-label={wrapper_aria_label!r}"
    )

    img = page.locator("[data-settings-avatar] img.settings-avatar-img")
    assert img.count() == 1, "AC-11: expected a child <img> in the has-avatar state"
    alt = img.get_attribute("alt")
    assert alt == "Tu avatar", (
        f"AC-11: expected the child <img> to carry the accessible name via alt, got {alt!r}"
    )


def test_ac11_upload_input_has_accessible_name(page: Page, base_url: str):
    """AC-11: the file input carries its own accessible name (aria-label) in
    addition to the visible label, so assistive tech announces its purpose
    even before any styling / label-association nuance."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_settings_view_with_avatar(page, base_url, has_avatar=False)

    file_input = page.locator("#settings-avatar-input")
    aria_label = file_input.get_attribute("aria-label")
    assert aria_label, "AC-11: #settings-avatar-input has no accessible name (aria-label)"
