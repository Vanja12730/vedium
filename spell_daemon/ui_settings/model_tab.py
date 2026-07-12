"""Model tab: backend choice and the local / server / cloud / CLI panels."""

import os
import threading
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

from .. import llm
from .. import diagnostics
from ..llm import fetch_cloud_models, CLOUD_PROVIDERS, connect_server, detect_local_server, connect_cloud
from ..cli_models import detect_clis, EFFORT_LEVELS
from ..config import (
    credential_store_available,
    get_stored_cloud_api_key,
    store_cloud_api_key,
    delete_stored_cloud_api_key,
)
from ..constants import (
    BG, SURFACE, SURF2, TEXT, MUTED, ACCENT, ACCENT2,
    WARN, ERR, SUCCESS, FONT_B,
    FORMATS, GPU_OPTIONS, N_CTX_OPTIONS, _detect_format_from_path,
)
from ..ui_thread import _ui_call
from ..ui_widgets import _add_tooltip, _make_toggle, lbl_row, _TGL_INDENT


def _build_model_tab(ui):
    """Build the Model tab: backend choice, local/server/cloud panels,
    prompt format, GPU mode, and context size."""
    win, cfg = ui.win, ui.cfg
    frm_model = ui.frm_model
    _bind_scroll = ui.bind_scroll

    # Determine initial mode from config. Prefer the explicit backend_mode; fall
    # back to inferring from server_url for configs saved before it existed.
    _init_mode = cfg.get("backend_mode", "")
    if _init_mode not in ("local", "server", "cloud", "cli"):
        _init_mode = "server" if cfg.get("server_url", "").strip() else "local"
    backend_var = tk.StringVar(master=win, value=_init_mode)

    lbl_row("Backend", 0, frm_model)
    mf = tk.Frame(frm_model, bg=BG)
    mf.grid(row=0, column=1, sticky="ew", pady=7)
    mf.columnconfigure(0, weight=1)

    # Radio buttons
    rb_frame = tk.Frame(mf, bg=BG)
    rb_frame.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
    tk.Radiobutton(rb_frame, text="Local model (.gguf)", variable=backend_var,
                   value="local", font=FONT_B, bg=BG, fg=TEXT,
                   activebackground=BG, selectcolor=SURFACE,
                   command=lambda: _on_backend_switch()).pack(side="left", padx=(0, 16))
    tk.Radiobutton(rb_frame, text="llama-server", variable=backend_var,
                   value="server", font=FONT_B, bg=BG, fg=TEXT,
                   activebackground=BG, selectcolor=SURFACE,
                   command=lambda: _on_backend_switch()).pack(side="left", padx=(0, 16))
    tk.Radiobutton(rb_frame, text="Cloud API", variable=backend_var,
                   value="cloud", font=FONT_B, bg=BG, fg=TEXT,
                   activebackground=BG, selectcolor=SURFACE,
                   command=lambda: _on_backend_switch()).pack(side="left", padx=(0, 16))
    tk.Radiobutton(rb_frame, text="AI CLI", variable=backend_var,
                   value="cli", font=FONT_B, bg=BG, fg=TEXT,
                   activebackground=BG, selectcolor=SURFACE,
                   command=lambda: _on_backend_switch()).pack(side="left")

    # ── Local model sub-panel ─────────────────────────────────────────────────
    local_frame = tk.Frame(mf, bg=BG)
    local_frame.columnconfigure(0, weight=1)

    model_var = tk.StringVar(master=win, value=cfg.get("model_path", ""))
    local_status_var = tk.StringVar(master=win, value="")
    tk.Entry(local_frame, textvariable=model_var, font=FONT_B,
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2,
             relief="flat").grid(row=0, column=0, sticky="ew", ipady=4, padx=(0, 6))

    def _check_local_model():
        path = model_var.get().strip()
        if not path:
            local_status_var.set("Choose a GGUF model file first.")
            local_status_lbl.config(fg=WARN)
            diagnostics.record("backend", "warning", "Local model path is empty.")
            return
        if not os.path.isfile(path):
            local_status_var.set("Model file was not found.")
            local_status_lbl.config(fg=ERR)
            diagnostics.record("backend", "error", "Local model file was not found.", path)
            return
        detected = _detect_format_from_path(path)
        if detected and detected in FORMATS:
            fmt_var.set(detected)
            fmt_status_var.set(f"  ✓ Auto-detected: {detected}")
            fmt_status_lbl.config(fg=SUCCESS)
            local_status_var.set(f"File found. Prompt format: {detected}.")
        else:
            local_status_var.set("File found. Pick the prompt format manually if needed.")
        local_status_lbl.config(fg=SUCCESS)
        diagnostics.record("backend", "info", "Local model file found.")

    def browse():
        p = filedialog.askopenfilename(parent=win, title="Select GGUF model",
                                       filetypes=[("GGUF", "*.gguf"), ("All", "*.*")])
        if p:
            model_var.set(p)
            detected = _detect_format_from_path(p)
            if detected and detected in FORMATS:
                fmt_var.set(detected)
                fmt_status_var.set(f"  ✓ Auto-detected: {detected}")
                fmt_status_lbl.config(fg=SUCCESS)
            else:
                fmt_status_var.set("  ✗ Could not detect — please select manually")
                fmt_status_lbl.config(fg=WARN)
            _check_local_model()

    tk.Button(local_frame, text="Browse", font=FONT_B, bg=ACCENT, fg="white",
              relief="flat", padx=8, cursor="hand2",
              command=browse).grid(row=0, column=1)
    tk.Button(local_frame, text="Check", font=FONT_B, bg=SURF2, fg=TEXT,
              relief="flat", padx=8, cursor="hand2",
              command=_check_local_model).grid(row=0, column=2, padx=(6, 0))
    local_status_lbl = tk.Label(local_frame, textvariable=local_status_var,
                                font=("Segoe UI", 8), bg=BG, fg=MUTED)
    local_status_lbl.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

    # ── llama-server sub-panel ────────────────────────────────────────────────
    server_frame = tk.Frame(mf, bg=BG)
    server_frame.columnconfigure(0, weight=1)

    server_url_var    = tk.StringVar(master=win, value=cfg.get("server_url", ""))
    server_status_var = tk.StringVar(master=win, value="")

    sf_inner = tk.Frame(server_frame, bg=BG)
    sf_inner.grid(row=0, column=0, columnspan=2, sticky="ew")
    sf_inner.columnconfigure(0, weight=1)
    tk.Entry(sf_inner, textvariable=server_url_var, font=FONT_B,
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2,
             relief="flat").grid(row=0, column=0, sticky="ew", ipady=4, padx=(0, 6))

    server_status_lbl = tk.Label(server_frame, textvariable=server_status_var,
                                  font=("Segoe UI", 8), bg=BG, fg=MUTED)
    server_status_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

    autodetect_var = tk.BooleanVar(master=win, value=cfg.get("auto_detect_server", True))
    autodetect_frame = tk.Frame(server_frame, bg=BG)
    autodetect_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
    _make_toggle(autodetect_frame, autodetect_var,
                 "Auto-detect a running local server on startup").pack(anchor="w")
    tk.Label(autodetect_frame,
             text="Scans common ports (8080, 8000, 8033, 8081, 5000, …) and connects "
                  "to the first llama-server found — no URL needed",
             font=("Segoe UI", 8), bg=BG, fg=MUTED
             ).pack(anchor="w", padx=(_TGL_INDENT, 0))

    nothink_var = tk.BooleanVar(master=win, value=cfg.get("server_disable_thinking", True))
    nothink_frame = tk.Frame(server_frame, bg=BG)
    nothink_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
    _make_toggle(nothink_frame, nothink_var,
                 "Disable thinking on reasoning models").pack(anchor="w")
    tk.Label(nothink_frame,
             text="Recommended ON: reasoning models (Gemma, Qwen3, …) answer directly "
                  "instead of spending the token budget on hidden chain-of-thought. "
                  "Turn OFF for higher-quality answers on complex chat questions (slower).",
             font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=380, justify="left"
             ).pack(anchor="w", padx=(_TGL_INDENT, 0))

    def _test_server():
        url = server_url_var.get().strip()
        # No URL entered → scan common local ports and connect to whatever is found.
        if not url:
            server_status_var.set("Searching for a local server…")
            server_status_lbl.config(fg=MUTED)
            def _do_detect():
                found = detect_local_server()
                if not found:
                    _ui_call(lambda: (
                        server_status_var.set("✗ No local server detected on common ports"),
                        server_status_lbl.config(fg=ERR),
                    ), wait=False)
                    return
                # Reflect the discovered URL in the field, then connect to it.
                _ui_call(lambda u=found: server_url_var.set(u), wait=False)
                try:
                    connect_server(found, cfg_n_ctx=ctx_var.get())
                    n = llm._llm._n_ctx if hasattr(llm._llm, "_n_ctx") else "?"
                    _ui_call(lambda u=found, n=n: (
                        server_status_var.set(f"✓ Detected & connected: {u}  (n_ctx={n})"),
                        server_status_lbl.config(fg=SUCCESS),
                    ), wait=False)
                except Exception as exc:
                    msg = str(exc)
                    diagnostics.record("backend", "error", "Server auto-detect connection failed.", msg)
                    _ui_call(lambda m=msg: (
                        server_status_var.set(f"✗ {m}"),
                        server_status_lbl.config(fg=ERR),
                    ), wait=False)
            threading.Thread(target=_do_detect, daemon=True).start()
            return
        server_status_var.set("Testing…")
        server_status_lbl.config(fg=MUTED)
        def _do_test():
            try:
                connect_server(url, cfg_n_ctx=ctx_var.get())
                n = llm._llm._n_ctx if hasattr(llm._llm, "_n_ctx") else "?"
                _ui_call(lambda n=n: (
                    server_status_var.set(f"✓ Connected  (n_ctx={n})"),
                    server_status_lbl.config(fg=SUCCESS),
                ), wait=False)
            except Exception as exc:
                msg = str(exc)
                diagnostics.record("backend", "error", "Server connection test failed.", msg)
                _ui_call(lambda m=msg: (
                    server_status_var.set(f"✗ {m}"),
                    server_status_lbl.config(fg=ERR),
                ), wait=False)
        threading.Thread(target=_do_test, daemon=True).start()

    tk.Button(sf_inner, text="Test / Auto-Detect", font=FONT_B, bg=ACCENT2, fg="white",
              relief="flat", padx=8, cursor="hand2",
              command=_test_server).grid(row=0, column=1)

    # ── Cloud API sub-panel ───────────────────────────────────────────────────
    cloud_frame = tk.Frame(mf, bg=BG)
    cloud_frame.columnconfigure(1, weight=1)

    cloud_provider_var = tk.StringVar(master=win, value=cfg.get("cloud_provider") or "Google Gemini")
    cloud_url_var      = tk.StringVar(master=win, value=cfg.get("cloud_base_url", ""))
    cloud_key_var      = tk.StringVar(master=win, value=cfg.get("cloud_api_key", ""))
    cloud_model_var    = tk.StringVar(master=win, value=cfg.get("cloud_model", ""))
    cloud_status_var   = tk.StringVar(master=win, value="")
    cred_available = credential_store_available()
    remember_key_var   = tk.BooleanVar(master=win, value=bool(cfg.get("remember_cloud_api_key", True) and cred_available))
    # Per-provider key memory for this run + favorites (quick-switch list).
    _cloud_keys    = dict(cfg.get("cloud_api_keys", {}))     # provider -> api key, stripped before save
    _prev_provider = [cloud_provider_var.get()]
    _favorites     = [dict(f) for f in cfg.get("cloud_favorites", []) if isinstance(f, dict)]

    def _cloud_lbl(text, r):
        tk.Label(cloud_frame, text=text, font=("Segoe UI", 9), bg=BG, fg=MUTED,
                 anchor="w").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)

    _cloud_lbl("Provider", 0)
    prov_menu = tk.OptionMenu(cloud_frame, cloud_provider_var, *CLOUD_PROVIDERS.keys())
    prov_menu.config(font=FONT_B, bg=SURFACE, fg=TEXT, activebackground=SURF2,
                     highlightthickness=0, relief="flat", anchor="w")
    prov_menu["menu"].config(bg=SURFACE, fg=TEXT)
    prov_menu.grid(row=0, column=1, sticky="ew", pady=3)

    _cloud_lbl("Base URL", 1)
    tk.Entry(cloud_frame, textvariable=cloud_url_var, font=FONT_B,
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2, relief="flat"
             ).grid(row=1, column=1, sticky="ew", ipady=3, pady=3)

    _cloud_lbl("API key", 2)
    keyrow = tk.Frame(cloud_frame, bg=BG)
    keyrow.grid(row=2, column=1, sticky="ew", pady=3)
    keyrow.columnconfigure(0, weight=1)
    cloud_key_entry = tk.Entry(keyrow, textvariable=cloud_key_var, font=FONT_B, show="•",
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2, relief="flat")
    cloud_key_entry.grid(row=0, column=0, sticky="ew", ipady=3, padx=(0, 6))
    def _toggle_key():
        cloud_key_entry.config(show="" if cloud_key_entry.cget("show") else "•")
    tk.Button(keyrow, text="👁", font=FONT_B, bg=SURF2, fg=TEXT, relief="flat",
              cursor="hand2", command=_toggle_key).grid(row=0, column=1)
    remember_state = "normal" if cred_available else "disabled"
    remember_toggle = _make_toggle(
        keyrow,
        remember_key_var,
        "Remember securely",
        state=remember_state,
    )
    remember_toggle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
    _add_tooltip(
        remember_toggle,
        "Stores the API key in Windows Credential Manager for your Windows user. "
        "The key is still stripped from vedium_config.json.",
    )

    # Model: an editable dropdown populated from the provider's /models endpoint.
    _cloud_lbl("Model", 3)
    modelrow = tk.Frame(cloud_frame, bg=BG)
    modelrow.grid(row=3, column=1, sticky="ew", pady=3)
    modelrow.columnconfigure(0, weight=1)
    _cb_style = ttk.Style()
    _cb_style.configure("Cloud.TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=TEXT, relief="flat")
    win.option_add("*TCombobox*Listbox.background", SURFACE)
    win.option_add("*TCombobox*Listbox.foreground", TEXT)
    win.option_add("*TCombobox*Listbox.selectBackground", ACCENT2)
    win.option_add("*TCombobox*Listbox.font", FONT_B)
    cloud_model_cb = ttk.Combobox(modelrow, textvariable=cloud_model_var,
                                  font=FONT_B, style="Cloud.TCombobox", values=[])
    cloud_model_cb.grid(row=0, column=0, sticky="ew", ipady=2, padx=(0, 6))
    fetch_btn = tk.Button(modelrow, text="↻ Fetch", font=FONT_B, bg=SURF2, fg=TEXT,
                          relief="flat", cursor="hand2")
    fetch_btn.grid(row=0, column=1, padx=(0, 6))
    fav_add_btn = tk.Button(modelrow, text="★", font=FONT_B, bg=SURF2, fg=ACCENT2,
                            relief="flat", cursor="hand2")
    fav_add_btn.grid(row=0, column=2)
    _add_tooltip(fav_add_btn, "Save the current provider + model to Favorites")

    # Favorites: quick-switch list of saved provider+model combos.
    _cloud_lbl("Favorites", 4)
    favrow = tk.Frame(cloud_frame, bg=BG)
    favrow.grid(row=4, column=1, sticky="ew", pady=3)
    favrow.columnconfigure(0, weight=1)
    fav_var = tk.StringVar(master=win, value="")
    fav_cb = ttk.Combobox(favrow, textvariable=fav_var, font=FONT_B,
                          style="Cloud.TCombobox", values=[], state="readonly")
    fav_cb.grid(row=0, column=0, sticky="ew", ipady=2, padx=(0, 6))
    fav_use_btn = tk.Button(favrow, text="Use", font=FONT_B, bg=ACCENT2, fg="white",
                            relief="flat", cursor="hand2")
    fav_use_btn.grid(row=0, column=1, padx=(0, 6))
    fav_del_btn = tk.Button(favrow, text="✕", font=FONT_B, bg=SURF2, fg=ERR,
                            relief="flat", cursor="hand2")
    fav_del_btn.grid(row=0, column=2)
    _add_tooltip(fav_del_btn, "Remove the selected favorite")

    cloud_hint = tk.Label(cloud_frame, font=("Segoe UI", 8), bg=BG, fg=MUTED,
                          justify="left", wraplength=380, text="")
    cloud_hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))

    cloud_status_lbl = tk.Label(cloud_frame, textvariable=cloud_status_var,
                                font=("Segoe UI", 8), bg=BG, fg=MUTED)
    cloud_status_lbl.grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))

    def _fetch_models(auto: bool = False):
        url, key = cloud_url_var.get().strip(), cloud_key_var.get().strip()
        if not url or not key:
            if not auto:
                cloud_status_var.set("Enter the base URL and API key first, then Fetch.")
                cloud_status_lbl.config(fg=WARN)
            return
        cloud_status_var.set("Fetching models…")
        cloud_status_lbl.config(fg=MUTED)
        def _do():
            try:
                models   = fetch_cloud_models(url, key)
                ids      = [mid for mid, _ in models]
                free_ids = [mid for mid, fr in models if fr]
                def _apply():
                    cloud_model_cb["values"] = ids
                    if not cloud_model_var.get().strip() and ids:
                        cloud_model_var.set(ids[0])
                    msg = f"✓ {len(ids)} models"
                    if free_ids:
                        msg += f" — {len(free_ids)} free (listed first)"
                    cloud_status_var.set(msg)
                    cloud_status_lbl.config(fg=SUCCESS)
                _ui_call(_apply, wait=False)
            except Exception as exc:
                m = str(exc)
                diagnostics.record("backend", "error", "Cloud model list failed.", m)
                _ui_call(lambda m=m: (cloud_status_var.set(f"✗ Could not list models: {m}"),
                                      cloud_status_lbl.config(fg=ERR)), wait=False)
        threading.Thread(target=_do, daemon=True).start()
    fetch_btn.config(command=_fetch_models)

    # ── Favorites logic ───────────────────────────────────────────────────────
    def _fav_display(f):
        return f"{f.get('provider', '?')} · {f.get('model', '?')}"
    def _refresh_favorites():
        fav_cb["values"] = [_fav_display(f) for f in _favorites]
    def _add_favorite():
        model = cloud_model_var.get().strip()
        if not model:
            cloud_status_var.set("Pick a model first, then click ★.")
            cloud_status_lbl.config(fg=WARN)
            return
        entry = {"provider": cloud_provider_var.get(),
                 "base_url": cloud_url_var.get().strip(), "model": model}
        if entry in _favorites:
            cloud_status_var.set("Already in favorites.")
            cloud_status_lbl.config(fg=MUTED)
            return
        _favorites.append(entry)
        _refresh_favorites()
        fav_var.set(_fav_display(entry))
        cloud_status_var.set(f"★ Added: {_fav_display(entry)}")
        cloud_status_lbl.config(fg=SUCCESS)
    def _use_favorite():
        match = next((f for f in _favorites if _fav_display(f) == fav_var.get()), None)
        if not match:
            return
        # Setting the provider fires _on_provider_change (URL + per-provider key +
        # model refresh); apply the saved URL/model afterwards so they win.
        cloud_provider_var.set(match.get("provider", "Custom"))
        if match.get("base_url"):
            cloud_url_var.set(match["base_url"])
        cloud_model_var.set(match.get("model", ""))
        cloud_status_var.set(f"Loaded favorite: {_fav_display(match)}")
        cloud_status_lbl.config(fg=SUCCESS)
    def _del_favorite():
        idx = next((i for i, f in enumerate(_favorites)
                    if _fav_display(f) == fav_var.get()), None)
        if idx is None:
            return
        removed = _favorites.pop(idx)
        _refresh_favorites()
        fav_var.set("")
        cloud_status_var.set(f"Removed: {_fav_display(removed)}")
        cloud_status_lbl.config(fg=MUTED)
    fav_add_btn.config(command=_add_favorite)
    fav_use_btn.config(command=_use_favorite)
    fav_del_btn.config(command=_del_favorite)
    _refresh_favorites()

    def _remember_provider_key(provider: str, key: str):
        provider = (provider or "").strip()
        key = (key or "").strip()
        if not provider:
            return
        if key:
            _cloud_keys[provider] = key
        if remember_key_var.get() and key:
            if not store_cloud_api_key(provider, key):
                cloud_status_var.set("Could not save key to Windows Credential Manager.")
                cloud_status_lbl.config(fg=WARN)
        elif cred_available:
            delete_stored_cloud_api_key(provider)

    _MODEL_HINTS = {
        "Google Gemini": "Free key at aistudio.google.com → 'Get API key'. All Gemini models are usable on the free tier (rate-limited).",
        "Grok (xAI)":    "Key from console.x.ai. Paid — no free models.",
        "OpenAI":        "Key from platform.openai.com. Paid — no free models.",
        "Groq":          "Free key from console.groq.com. Generous free tier on the listed open models.",
        "OpenRouter":    "Key from openrouter.ai. Models ending in ':free' cost nothing — those are listed first.",
        "DeepSeek":      "Key from platform.deepseek.com. Paid (low cost).",
        "Custom":        "Any OpenAI-compatible base URL (usually ends in /v1). Click Fetch to list its models.",
    }
    def _on_provider_change(*_):
        prov = cloud_provider_var.get()
        # Remember the key typed for the previous provider; load this one's key.
        if _prev_provider[0] and _prev_provider[0] != prov:
            _remember_provider_key(_prev_provider[0], cloud_key_var.get().strip())
            key = _cloud_keys.get(prov, "")
            if remember_key_var.get() and not key:
                key = get_stored_cloud_api_key(prov)
                if key:
                    _cloud_keys[prov] = key
            cloud_key_var.set(key)
        _prev_provider[0] = prov
        if prov != "Custom":
            cloud_url_var.set(CLOUD_PROVIDERS.get(prov, ""))
        cloud_hint.config(text=_MODEL_HINTS.get(prov, ""))
        cloud_model_cb["values"] = []          # clear stale list from the old provider
        if cloud_key_var.get().strip():
            _fetch_models(auto=True)            # auto-refresh when a key is already present
    # Fill the URL for a fresh config, but keep a saved/custom URL on first open.
    if not cloud_url_var.get().strip() and cloud_provider_var.get() != "Custom":
        cloud_url_var.set(CLOUD_PROVIDERS.get(cloud_provider_var.get(), ""))
    cloud_hint.config(text=_MODEL_HINTS.get(cloud_provider_var.get(), ""))
    cloud_provider_var.trace_add("write", _on_provider_change)
    # If a key is available in this session/environment, populate the dropdown on open.
    if cloud_key_var.get().strip() and cloud_url_var.get().strip():
        _fetch_models(auto=True)

    def _test_cloud():
        cloud_status_var.set("Connecting…")
        cloud_status_lbl.config(fg=MUTED)
        url = cloud_url_var.get().strip()
        key = cloud_key_var.get().strip()
        model = cloud_model_var.get().strip()
        def _do():
            try:
                connect_cloud(url, key, model, cfg_n_ctx=max(ctx_var.get(), 8192))
                def _connected():
                    cfg["backend_mode"] = "cloud"
                    cfg["cloud_provider"] = cloud_provider_var.get()
                    cfg["cloud_base_url"] = url
                    cfg["cloud_api_key"] = key
                    cfg["cloud_model"] = model
                    cfg["remember_cloud_api_key"] = bool(remember_key_var.get())
                    _remember_provider_key(cloud_provider_var.get(), key)
                    cfg["cloud_api_keys"] = dict(_cloud_keys)
                    diagnostics.set_status(backend_mode="cloud")
                    if remember_key_var.get() and cred_available:
                        cloud_status_var.set(f"Connected to {model}. API key saved in Windows Credential Manager.")
                    else:
                        cloud_status_var.set(f"Connected to {model} for this run. API keys are not saved to disk.")
                    cloud_status_lbl.config(fg=SUCCESS)
                    ui.refresh_setup_tab()
                _ui_call(_connected, wait=False)
            except Exception as exc:
                m = str(exc)
                diagnostics.record("backend", "error", "Cloud connection test failed.", m)
                _ui_call(lambda m=m: (cloud_status_var.set(f"✗ {m}"),
                                      cloud_status_lbl.config(fg=ERR)), wait=False)
        threading.Thread(target=_do, daemon=True).start()

    tk.Button(cloud_frame, text="Test & Connect", font=FONT_B, bg=ACCENT2, fg="white",
              relief="flat", padx=8, cursor="hand2", command=_test_cloud
              ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

    tk.Label(
        cloud_frame, font=("Segoe UI", 8), bg=BG, fg=MUTED,
        justify="left", wraplength=380,
        text="API keys are stripped from the settings file. With Remember securely on, "
             "the key is saved in Windows Credential Manager for your Windows user.",
    ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ── Show/hide sub-panels based on radio selection ─────────────────────────
    # Command-line AI sub-panel. Detection is local/free and reruns whenever a
    # new Settings window is constructed.
    cli_frame = tk.Frame(mf, bg=BG)
    cli_frame.columnconfigure(1, weight=1)
    cli_enabled_var = tk.BooleanVar(master=win, value=cfg.get("cli_models_enabled", False))
    cli_model_var = tk.StringVar(master=win, value=cfg.get("cli_model", ""))
    cli_effort_var = tk.StringVar(
        master=win, value=cfg.get("cli_reasoning_effort", "") or "Default"
    )
    cli_spell_var = tk.BooleanVar(
        master=win,
        value=bool(
            cfg.get("cli_models_enabled", False)
            and cfg.get("cli_spell_check_enabled", False)
        ),
    )
    cli_status_var = tk.StringVar(master=win, value="")

    try:
        cli_detection = detect_clis()
    except Exception as exc:
        diagnostics.record("backend", "error", "AI CLI detection failed.", str(exc))
        cli_detection = {
            "claude": {"path": None, "models": []},
            "codex": {"path": None, "models": [], "default_model": ""},
            "antigravity": {"path": None, "models": []},
        }
    cli_models = (
        cli_detection["claude"]["models"]
        + cli_detection["codex"]["models"]
        + cli_detection["antigravity"]["models"]
    )
    cli_model_ids = [entry["id"] for entry in cli_models]

    cli_main_toggle = None
    cli_spell_toggle = None
    cli_model_cb = None
    cli_effort_cb = None
    cli_status_lbl = None

    def _refresh_cli_controls():
        enabled = bool(cli_enabled_var.get())
        if not enabled and cli_spell_var.get():
            cli_spell_var.set(False)
        cli_model_cb.config(
            values=cli_model_ids if enabled else [],
            state="readonly" if enabled and cli_model_ids else "disabled",
        )
        cli_effort_cb.config(state="readonly" if enabled else "disabled")
        cli_spell_toggle.set_state("normal" if enabled else "disabled")

        selected = cli_model_var.get().strip()
        if not enabled:
            cli_status_var.set("Command-line models are switched off.")
            cli_status_lbl.config(fg=MUTED)
        elif not cli_model_ids:
            cli_status_var.set("No supported AI CLI was found on PATH.")
            cli_status_lbl.config(fg=ERR)
        elif not selected:
            cli_status_var.set("Choose a detected command-line model.")
            cli_status_lbl.config(fg=WARN)
        elif selected not in cli_model_ids:
            cli_status_var.set(f"Saved model is not currently available: {selected}")
            cli_status_lbl.config(fg=ERR)
        else:
            cli_status_var.set(f"Ready to select {selected} on Save & Connect.")
            cli_status_lbl.config(fg=SUCCESS)

    def _on_cli_enabled_toggle():
        if cli_enabled_var.get():
            allowed = messagebox.askokcancel(
                "Enable AI CLI models?",
                "Selected text and chat prompts will leave this computer through "
                "your signed-in Claude, Codex, or Antigravity CLI. Every request "
                "spends that subscription's usage. Vedium never reads or stores "
                "the CLI's credentials.\n\nContinue?",
                parent=win,
                icon="warning",
            )
            if not allowed:
                cli_enabled_var.set(False)
            else:
                diagnostics.record("config", "info", "AI CLI models enabled with consent.")
        else:
            cli_spell_var.set(False)
        _refresh_cli_controls()

    def _on_cli_spell_toggle():
        if cli_spell_var.get():
            allowed = messagebox.askokcancel(
                "Enable automatic CLI spell-check?",
                "Vedium will send an AI CLI subscription request whenever "
                "you complete a sentence with configured punctuation. This can "
                "spend usage repeatedly while you type, and each result may take "
                "many seconds. Slow results are shown for review instead of being "
                "replaced automatically.\n\nEnable automatic CLI spell-check?",
                parent=win,
                icon="warning",
            )
            if not allowed:
                cli_spell_var.set(False)
            else:
                diagnostics.record(
                    "config", "warning", "Automatic CLI spell-check enabled with consent."
                )
        _refresh_cli_controls()

    cli_main_toggle = _make_toggle(
        cli_frame, cli_enabled_var, "Use installed AI CLIs as models",
        command=_on_cli_enabled_toggle,
    )
    cli_main_toggle.grid(row=0, column=0, columnspan=2, sticky="w")
    tk.Label(
        cli_frame,
        text="Prompts leave this computer and each request spends your CLI subscription usage.",
        font=("Segoe UI", 8), bg=BG, fg=WARN, wraplength=500, justify="left",
    ).grid(row=1, column=0, columnspan=2, sticky="w", padx=(_TGL_INDENT, 0), pady=(2, 7))

    for row, (key, title) in enumerate((
        ("claude", "Claude Code"),
        ("codex", "Codex CLI"),
        ("antigravity", "Antigravity CLI"),
    ), start=2):
        info = cli_detection[key]
        path = info.get("path")
        count = len(info.get("models", []))
        if path:
            suffix = f"found - {count} model{'s' if count != 1 else ''}"
            if key == "codex" and info.get("default_model"):
                suffix += f"; default {info['default_model']}"
            colour = SUCCESS
        else:
            suffix = "not found on PATH - install the CLI and sign in once in your terminal"
            colour = MUTED
        detected_lbl = tk.Label(
            cli_frame, text=f"{title}: {suffix}", font=("Segoe UI", 8),
            bg=BG, fg=colour, anchor="w", justify="left", wraplength=500,
        )
        detected_lbl.grid(row=row, column=0, columnspan=2, sticky="w", pady=1)
        _add_tooltip(
            detected_lbl,
            str(path) if path else "Install the CLI and sign in once in your own terminal.",
        )

    tk.Label(cli_frame, text="Model", font=("Segoe UI", 9), bg=BG, fg=MUTED).grid(
        row=5, column=0, sticky="w", padx=(0, 8), pady=(8, 3)
    )
    cli_model_cb = ttk.Combobox(
        cli_frame, textvariable=cli_model_var, values=[], state="disabled", font=FONT_B,
    )
    cli_model_cb.grid(row=5, column=1, sticky="ew", pady=(8, 3))
    _bind_scroll(cli_model_cb)

    tk.Label(cli_frame, text="Reasoning effort", font=("Segoe UI", 9), bg=BG, fg=MUTED).grid(
        row=6, column=0, sticky="w", padx=(0, 8), pady=3
    )
    cli_effort_cb = ttk.Combobox(
        cli_frame, textvariable=cli_effort_var,
        values=("Default", *EFFORT_LEVELS[1:]), state="disabled", font=FONT_B,
    )
    cli_effort_cb.grid(row=6, column=1, sticky="w", pady=3)
    _bind_scroll(cli_effort_cb)

    cli_spell_toggle = _make_toggle(
        cli_frame, cli_spell_var, "Allow automatic spell-check with AI CLI",
        state="disabled", command=_on_cli_spell_toggle,
    )
    cli_spell_toggle.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
    tk.Label(
        cli_frame,
        text="Default OFF. Manual Understand, Improve Writing, follow-ups, and Chat still work.",
        font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=500, justify="left",
    ).grid(row=8, column=0, columnspan=2, sticky="w", padx=(_TGL_INDENT, 0), pady=(2, 0))

    cli_status_lbl = tk.Label(
        cli_frame, textvariable=cli_status_var, font=("Segoe UI", 8), bg=BG, fg=MUTED,
        wraplength=500, justify="left",
    )
    cli_status_lbl.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 0))
    _refresh_cli_controls()

    def _on_backend_switch():
        for f in (local_frame, server_frame, cloud_frame, cli_frame):
            f.grid_remove()
        mode = backend_var.get()
        panel = {
            "local": local_frame, "server": server_frame,
            "cloud": cloud_frame, "cli": cli_frame,
        }.get(mode, local_frame)
        panel.grid(row=1, column=0, columnspan=2, sticky="ew")

    # Apply initial visibility
    _on_backend_switch()

    # ── Prompt format ─────────────────────────────────────────────────────────
    fmt_var = tk.StringVar(master=win, value=cfg.get("prompt_format", FORMATS[0]))
    fmt_status_var = tk.StringVar(master=win, value="")
    lbl_row("Prompt format", 1, frm_model)
    fmt_frame = tk.Frame(frm_model, bg=BG)
    fmt_frame.grid(row=1, column=1, sticky="ew", pady=7)
    fmt_frame.columnconfigure(0, weight=1)
    fmt_cb = ttk.Combobox(fmt_frame, textvariable=fmt_var, values=FORMATS,
                          state="readonly", font=FONT_B)
    fmt_cb.grid(row=0, column=0, sticky="ew")
    _bind_scroll(fmt_cb)
    fmt_status_lbl = tk.Label(fmt_frame, textvariable=fmt_status_var,
                               font=FONT_B, bg=BG, fg=SUCCESS)
    fmt_status_lbl.grid(row=1, column=0, sticky="w")

    # If a model is already configured, show detection result immediately
    existing_path = cfg.get("model_path", "")
    if existing_path:
        detected = _detect_format_from_path(existing_path)
        if detected and detected in FORMATS:
            # Only show the label — don't override a manually-saved format choice
            fmt_status_var.set(f"  ✓ Detected from filename: {detected}")
            fmt_status_lbl.config(fg=MUTED)
        # No warning on startup — the user may have set the format manually before

    # ── GPU acceleration ──────────────────────────────────────────────────────
    gpu_var = tk.StringVar(master=win, value=cfg.get("gpu_acceleration", "Auto"))
    lbl_row("GPU acceleration", 2, frm_model)
    gf = tk.Frame(frm_model, bg=BG)
    gf.grid(row=2, column=1, sticky="w", pady=7)
    gpu_cb = ttk.Combobox(gf, textvariable=gpu_var, values=GPU_OPTIONS,
                          state="readonly", font=FONT_B, width=8)
    gpu_cb.pack(side="left")
    _bind_scroll(gpu_cb)
    # Show the actual backend that loaded (GPU / CPU / CPU (GPU fallback) etc.)
    # so the user can immediately see whether GPU is really being used.
    backend_now = "not loaded"
    if "GPU" in backend_now and "fallback" not in backend_now and "failed" not in backend_now:
        backend_colour = SUCCESS
    elif "CPU" in backend_now:
        backend_colour = WARN
    else:
        backend_colour = MUTED
    backend_label_var = tk.StringVar(master=win, value=f"  Active: {backend_now}")
    backend_lbl = tk.Label(gf, textvariable=backend_label_var,
             font=FONT_B, bg=BG, fg=backend_colour)
    backend_lbl.pack(side="left")
    win.backend_lbl = backend_lbl
    win.backend_label_var = backend_label_var

    # ── Context size ──────────────────────────────────────────────────────────
    ctx_var = tk.IntVar(master=win, value=cfg.get("n_ctx", 768))
    lbl_row("Context size", 3, frm_model)
    cf = tk.Frame(frm_model, bg=BG)
    cf.grid(row=3, column=1, sticky="w", pady=7)
    ctx_top = tk.Frame(cf, bg=BG)
    ctx_top.pack(anchor="w")
    ctx_cb = ttk.Combobox(ctx_top, textvariable=ctx_var, values=N_CTX_OPTIONS,
                          state="readonly", font=FONT_B, width=6)
    ctx_cb.pack(side="left")
    _bind_scroll(ctx_cb)
    tk.Label(ctx_top, text=" tokens", font=FONT_B, bg=BG, fg=TEXT).pack(side="left")
    tk.Label(cf, text="Takes effect on Save & Load Model  ·  larger = more VRAM / RAM  ·  increase if rephrasing large selections",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).pack(anchor="w")
    _add_tooltip(ctx_cb,
        "Controls how much text the model can read at once (prompt + output combined).\n\n"
        "Think of it as the model's working memory:\n"
        "  • 768–1024    Normal use — sentence-by-sentence correction\n"
        "  • 2048–4096   A few paragraphs selected for rephrasing\n"
        "  • 8192+        Half a page or more selected at once\n"
        "  • 32768+       Several pages of selected text\n\n"
        "If you select a large block of text and the result looks cut off or garbled,\n"
        "increase this value, save, and reload the model.\n\n"
        "The trade-off: larger values use more RAM (CPU) or VRAM (GPU).\n"
        "Takes effect only after clicking Save & Load Model."
    )

    # ── publish shared state ──────────────────────────────────────────────────
    ui.backend_var = backend_var
    ui.model_var, ui.server_url_var = model_var, server_url_var
    ui.autodetect_var, ui.nothink_var = autodetect_var, nothink_var
    ui.cloud_provider_var, ui.cloud_url_var = cloud_provider_var, cloud_url_var
    ui.cloud_key_var, ui.cloud_model_var = cloud_key_var, cloud_model_var
    ui.remember_key_var = remember_key_var
    ui.cloud_keys, ui.favorites = _cloud_keys, _favorites
    ui.remember_provider_key = _remember_provider_key
    ui.cli_enabled_var, ui.cli_model_var = cli_enabled_var, cli_model_var
    ui.cli_effort_var, ui.cli_spell_var = cli_effort_var, cli_spell_var
    ui.cli_model_ids = list(cli_model_ids)
    ui.cli_effort_value = lambda: "" if cli_effort_var.get() == "Default" else cli_effort_var.get()
    ui.fmt_var, ui.gpu_var, ui.ctx_var = fmt_var, gpu_var, ctx_var
