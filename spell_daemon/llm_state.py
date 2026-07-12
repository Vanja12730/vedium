"""Shared mutable state for the LLM layer.

Lives in its own module so llm_backends and llm_postprocess can both use
it without importing each other. ``_backend`` is shared everywhere via
from-imports; it is never rebound, only mutated, so every module sees the
same live state.
"""

import threading


class _ActiveBackend:
    """The currently connected backend: client, identity, and its guard lock.

    ``client`` is whatever object answers the llama-style call interface
    (llama_cpp.Llama, _LlamaServerClient, _CloudClient, or CLIClient);
    ``path`` identifies the model file / server URL / cloud model; ``label``
    is the human-readable name shown in the UI.  Mutate only while holding
    ``lock`` -- generation paths hold it for entire calls so two pipelines
    can never share one model instance.

    ``reasoning_detected`` flips True the first time the connected model
    emits reasoning markers (resets on connect).  ``server_disable_thinking``
    mirrors the "disable thinking" setting; config.load_cfg and the Settings
    bottom bar apply it live.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.path = ""
        self.label = "None"
        self.reasoning_detected = False
        self.server_disable_thinking = True


_backend = _ActiveBackend()

# Set by the UI to abort an in-flight streaming generation.
_stream_cancel = threading.Event()
