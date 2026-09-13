# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import collections
import os
import re
import sys
from pynput.keyboard import Key

# On macOS the clipboard shortcuts use the Command key; everywhere else Ctrl.
_CMD_KEY = Key.cmd if sys.platform == "darwin" else Key.ctrl

_CTRL_KEYS = frozenset({Key.ctrl, Key.ctrl_l, Key.ctrl_r}) | (
    frozenset({Key.cmd, Key.cmd_l, Key.cmd_r}) if sys.platform == "darwin" else frozenset()
)

_OS_RESERVED_HOTKEYS = frozenset({"f6", "f7", "f10"})
_OS_RESERVED_HOTKEY_REASONS = {
    "f6":  "F6 focuses the browser address bar — the selection is lost before the copy fires.",
    "f7":  "F7 toggles caret browsing in browsers — a dialog appears and steals focus before the copy fires.",
    "f10": "F10 activates the Windows menu bar, which drops your text selection before it can be copied.",
}

DEFAULT_TRIGGER_CHARS = ".!?,"

# _HERE points to the refactored directory (where the launcher is)
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_HERE, "vedium_config.json")
# Pre-rename config file (the app used to be SpellDaemon/GrammarAnywhere);
# load_cfg() adopts it as CONFIG_FILE on the first run after the upgrade.
LEGACY_CONFIG_FILE = os.path.join(_HERE, "spell_daemon_config.json")

_rephrase_history = collections.deque(maxlen=20)

DEFAULT_CFG = {
    "model_path":             "",
    "server_url":             "",
    "auto_detect_server":     True,
    "server_disable_thinking": True,
    # The installer places the local-GGUF engine under {app}/llama-server. An
    # explicit path is mainly for development and portable/manual installations.
    "managed_server_path":    "",
    "backend_mode":           "local",
    "cloud_provider":         "",
    "cloud_base_url":         "",
    "cloud_api_key":          "",
    "cloud_model":            "",
    "cloud_api_keys":         {},
    "cloud_favorites":        [],
    "remember_cloud_api_key": True,
    "cli_models_enabled":     False,
    "cli_model":              "",
    "cli_reasoning_effort":   "",
    "prompt_format":          "Auto  (detect from model)",
    "gpu_acceleration":       "Auto",
    "working_language":       "Auto",
    "trigger_chars":          DEFAULT_TRIGGER_CHARS,
    # Spell-check is opt-in on a fresh install. The assistant hotkeys remain
    # available, while background sentence capture/correction stays dormant
    # until the user explicitly chooses Auto or Toast in Settings.
    "spell_check_mode":       "Off",
    "toast_accept_hotkey":    "ctrl+enter",
    "enabled":                True,
    "min_word_count":         2,
    "max_wait_sec":           12,
    "understand_hotkey":      "f8",
    "writing_hotkey":         "f9",
    "rephrase_reading_mode":  "What's This About?",
    "rephrase_writing_mode":  "Polish My Writing",
    "reflection_source":      "",
    "reflection_journal_path": "",
    "reflection_window_position": None,
    "vocabulary_journal_path": "",
    "vocabulary_window_position": None,
    "rephrase_creativity":    0.35,
    "idle_reset_sec":         30.0,
    "n_ctx":                  4096,
    "diacritics_only_auto":    False,
    "colour_scheme":          "Parchment",
    "custom_rephrase_modes":    {},
    "builtin_prompt_overrides": {},
    "mode_triggers":            {},
    "mode_hotkeys":             {},
    "thinking_budget":          0,
    "font_size_offset":         0,
}

BG      = "#fbf8f2"
SURFACE = "#fffdf8"
SURF2   = "#ece5da"
ACCENT  = "#2f6f55"
ACCENT2 = "#3f8266"
TEXT    = "#25221d"
MUTED   = "#746c62"
SUCCESS = "#2f7a4f"
WARN    = "#8a5a1f"
ERR     = "#9a3030"

SCHEMES = {
    "Forest": dict(
        BG="#f4f7f0", SURFACE="#fffef9", SURF2="#dfe8d8",
        ACCENT="#376b50", ACCENT2="#4f8366", TEXT="#1f2b22",
        MUTED="#687767", SUCCESS="#2f7a4f", WARN="#8a5a1f", ERR="#9a3030",
    ),
    "Parchment": dict(
        BG="#fbf8f2", SURFACE="#fffdf8", SURF2="#ece5da",
        ACCENT="#2f6f55", ACCENT2="#3f8266", TEXT="#25221d",
        MUTED="#746c62", SUCCESS="#2f7a4f", WARN="#8a5a1f", ERR="#a03030",
    ),
    "Slate": dict(
        BG="#f6f7f8", SURFACE="#ffffff", SURF2="#dde2e8",
        ACCENT="#355f8f", ACCENT2="#4f7cae", TEXT="#202733",
        MUTED="#6b7481", SUCCESS="#2d7a52", WARN="#8a5a1f", ERR="#a13333",
    ),
    "Charcoal": dict(
        BG="#202225", SURFACE="#292c30", SURF2="#373b40",
        ACCENT="#7fb58f", ACCENT2="#9ac8a7", TEXT="#f2f0ea",
        MUTED="#a5a096", SUCCESS="#8fca97", WARN="#d9a64a", ERR="#e07171",
    ),
    "Ink": dict(
        BG="#fbfbfa", SURFACE="#ffffff", SURF2="#e5e3df",
        ACCENT="#24283b", ACCENT2="#48506f", TEXT="#151515",
        MUTED="#67645f", SUCCESS="#2e6b3e", WARN="#835f22", ERR="#962020",
    ),
    "Rose dust": dict(
        BG="#fbf6f5", SURFACE="#fffdfb", SURF2="#eadedc",
        ACCENT="#8f5965", ACCENT2="#a96f7a", TEXT="#2a1f20",
        MUTED="#796968", SUCCESS="#547a50", WARN="#8f6228", ERR="#a03030",
    ),
}

def apply_scheme(name: str):
    global BG, SURFACE, SURF2, ACCENT, ACCENT2, TEXT, MUTED, SUCCESS, WARN, ERR
    s = SCHEMES.get(name, SCHEMES["Parchment"])
    BG      = s["BG"]
    SURFACE = s["SURFACE"]
    SURF2   = s["SURF2"]
    ACCENT  = s["ACCENT"]
    ACCENT2 = s["ACCENT2"]
    TEXT    = s["TEXT"]
    MUTED   = s["MUTED"]
    SUCCESS = s["SUCCESS"]
    WARN    = s["WARN"]
    ERR     = s["ERR"]

    import sys
    for mod_name, mod in list(sys.modules.items()):
        if mod is not None and ("vedium" in mod_name or mod_name in ["ui_settings", "ui_toasts", "daemon", "replacement"]):
            for key in ["BG", "SURFACE", "SURF2", "ACCENT", "ACCENT2", "TEXT", "MUTED", "SUCCESS", "WARN", "ERR"]:
                if hasattr(mod, key):
                    setattr(mod, key, s[key])

FONT_H = ("Georgia", 13, "bold")
FONT_B = ("Segoe UI", 9)
FONT_SIZE_OFFSET = 0

def apply_font_size(offset: int):
    global FONT_B, FONT_H, FONT_SIZE_OFFSET
    FONT_B = ("Segoe UI", 9 + offset)
    FONT_H = ("Georgia", 13 + offset, "bold")
    FONT_SIZE_OFFSET = offset

    import sys
    for mod_name, mod in list(sys.modules.items()):
        if mod is not None and ("vedium" in mod_name or mod_name in ["ui_settings", "ui_toasts", "daemon", "replacement"]):
            if hasattr(mod, "FONT_B"):
                setattr(mod, "FONT_B", FONT_B)
            if hasattr(mod, "FONT_H"):
                setattr(mod, "FONT_H", FONT_H)
            if hasattr(mod, "FONT_SIZE_OFFSET"):
                setattr(mod, "FONT_SIZE_OFFSET", FONT_SIZE_OFFSET)

AUTO_FORMAT = "Auto  (detect from model)"

FORMATS = [
    AUTO_FORMAT,
    "ChatML  (Qwen / Phi-3 / Yi)",
    "Llama 3  (Meta)",
    "Mistral / Mixtral",
    "Gemma  (Google)",
    "Zephyr / HF Chat",
    "Alpaca / Vicuna",
    "DeepSeek",
    "Command-R  (Cohere)",
    "Harmony  (gpt-oss)",
    "Plain  (no template)",
]

GPU_OPTIONS = ["Auto", "Off", "Full"]

# Auto, English and BHS stay pinned at the top as the common choices; the rest
# are alphabetical so a longer list stays scannable. Every name here is
# interpolated straight into the prompt, so it must read as the language's
# ordinary English name. Names must also match vedium/language.py's detector,
# which returns one of these for answer modes on Auto.
WORKING_LANGUAGE_OPTIONS = [
    "Auto",
    "English",
    "BHS",
    "Czech",
    "Danish",
    "Dutch",
    "Finnish",
    "French",
    "German",
    "Greek",
    "Hungarian",
    "Italian",
    "Norwegian",
    "Polish",
    "Portuguese",
    "Romanian",
    "Russian",
    "Spanish",
    "Swedish",
    "Turkish",
    "Ukrainian",
]

# Working languages whose ambiguous short input is assumed to be native typing
# rather than foreign text. BHS users routinely type without diacritics, which
# the language detector cannot reliably identify from one short sentence. Clear
# evidence of another language still triggers translation into BHS.
# Any other named language qualifies dynamically — see language.is_same_language.
#
# Formerly _SLAVIC_LANGS, which described the members rather than the purpose
# and implied that adding Polish or Czech would be correct; it would not, since
# the old prompt behind it was hardcoded with BHS diacritics.
_NATIVE_TYPING_LANGS = {"BHS"}

def _is_header(mode: str) -> bool:
    return mode.startswith("──")

def _modes_with_headers(flat_modes: list[str]) -> list[str]:
    # Two coarse groups mirroring the Reading/Writing panels in Settings.
    # Custom modes (not in either set) are appended after the Writing group,
    # translations get their own trailing group.
    _GROUPS = [
        ("── Reading (answers) ──",
         {"What's This About?", "Define This", "Key Takeaways",
          "Explain It", "Explain It (Simply)", "Explain with Quotes",
          "Extract Writing Style"}),
        ("── Writing (replaces) ──",
         {"Polish My Writing", "More Casual", "More Formal (Academic)",
          "Diplomatically Worded", "More Concise", "More Detailed",
          "Simpler Language", "Sharpen the Thesis", "Draft a Counterargument",
          "Diacritics Only", "Format as Bullet Points", "Format as Numbered List",
          "Format as Checklist", "Format as Table", "Add Headings"}),
    ]
    result: list[str] = []
    seen:   set[str]  = set()

    for header, members in _GROUPS:
        group = [m for m in flat_modes if m in members]
        if group:
            result.append(header)
            result.extend(group)
            seen.update(group)

    custom = [m for m in flat_modes if m not in seen and not m.startswith("Translate ")]
    if custom:
        result.extend(custom)
        seen.update(custom)

    translations = [m for m in flat_modes if m.startswith("Translate ")]
    if translations:
        result.append("── Translation ──")
        result.extend(translations)

    return result

REPHRASE_MODES = [
    "What's This About?",
    "Define This",
    "Key Takeaways",
    "Explain It",
    "Explain It (Simply)",
    "Explain with Quotes",
    "Extract Writing Style",
    "Polish My Writing",
    "More Casual",
    "More Formal (Academic)",
    "Diplomatically Worded",
    "More Concise",
    "More Detailed",
    "Simpler Language",
    "Sharpen the Thesis",
    "Draft a Counterargument",
    "Format as Bullet Points",
    "Format as Numbered List",
    "Format as Checklist",
    "Format as Table",
    "Add Headings",
    "Diacritics Only",
    "Translate BHS → English",
    "Translate English → BHS",
    "Translate Chinese → English",
    "Translate English → Chinese",
    "Translate Chinese → BHS",
]
REPHRASE_MODES = _modes_with_headers(REPHRASE_MODES)

_BUILTIN_MODES = [m for m in REPHRASE_MODES if not _is_header(m)]

# Answer modes produce an answer ABOUT the text (a summary, a definition, an
# explanation) rather than a rewritten version of it. Their output is shown in
# a read-only style card and NEVER replaces the source text — the source is
# usually something the user is reading (a web page, a PDF), not writing.
ANSWER_MODES = frozenset({
    "What's This About?",
    "Define This",
    "Key Takeaways",
    "Explain It",
    "Explain It (Simply)",
    "Explain with Quotes",
    "Extract Writing Style",
})

def _is_answer_mode(mode: str) -> bool:
    """True for modes whose output is shown to the reader, never pasted back."""
    return (mode or "") in ANSWER_MODES

HOTKEY_OPTIONS = ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12"]

N_CTX_OPTIONS = [512, 768, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072]

_REPHRASE_PROMPTS = {
    "More Casual": (
        "Rewrite the following text in a relaxed, conversational tone. "
        "Keep the same core meaning. "
        "Preserve every claim, example, qualification, technical term, name, and concrete detail. "
        "Do NOT add facts, opinions, jokes, or new content, and do NOT drop points to sound breezier. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "Polish My Writing": (
        "Polish the following writing while keeping the writer's meaning, voice, tone, and level of "
        "formality recognisably their own. Make only changes that improve clarity, flow, grammar, "
        "punctuation, or concision; leave wording that already works alone. "
        "Preserve every argument, example, qualification, technical term, and concrete detail. "
        "Do NOT add facts, claims, interpretations, examples, or conclusions. Do NOT summarise. "
        "You may complete an obvious fragment, remove repetition, or reorder nearby sentences when the "
        "intended relationship is clear, but do not guess at ambiguous meaning. "
        "Do not inflate the vocabulary, make the text more formal, add headings or lists, or impose a "
        "generic AI style. Avoid filler such as 'Moreover', 'Furthermore', 'It is important to note', "
        "and 'In conclusion'. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "More Concise": (
        "Rewrite the following text to be more concise. "
        "Remove filler words and redundancy while keeping the full meaning. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "More Detailed": (
        "Make the following text more detailed using ONLY information, implications, examples, and "
        "relationships already present in the text. Clarify compressed reasoning and make implicit "
        "connections explicit where they are unambiguous. "
        "Do NOT invent facts, evidence, quotations, sources, examples, motives, or claims. "
        "If the text does not contain enough information to expand a point safely, leave that point concise. "
        "Preserve the writer's meaning, tone, and level of certainty. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "Simpler Language": (
        "Rewrite the following text using simpler, everyday language "
        "that anyone can understand. Keep the same meaning. "
        "Simplify the wording, not the content: preserve every claim, example, "
        "and qualification, even if that keeps a sentence longer. "
        "Do NOT add facts, examples, or explanations that are not in the text. "
        "If a technical term or name has no everyday equivalent, keep it as written. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "What's This About?": (
        "The reader has selected a passage they are reading and pressed a key to find out what it is about. "
        "In one to three short sentences, tell them what the passage is about and what its main point, "
        "claim, or purpose is. "
        "Be concrete and specific — name the actual subject and the actual point, not vague categories. "
        "Do not rewrite or paraphrase the passage sentence by sentence; describe it from above. "
        "Do not evaluate it or add opinions of your own. "
        "Return ONLY the answer. No preamble, no labels, no extra lines."
    ),
    "Define This": (
        "The reader has selected a word, phrase, or short expression they did not understand while reading. "
        "Explain what it means in plain language, in one to three short sentences. "
        "If it is a technical term, explain it the way you would to an intelligent newcomer and mention "
        "what field it belongs to. "
        "If it has several plausible meanings, give the most likely one first, then briefly note the others. "
        "If it is the name of a person, place, organisation, or work, say briefly who or what that is. "
        "Return ONLY the explanation. No preamble, no labels, no extra lines."
    ),
    "Key Takeaways": (
        "The reader has selected a longer passage and wants its key points without re-reading it. "
        "List the main points as short bullet lines, each starting with '• '. "
        "Use at most five bullets; fewer is better. Each bullet is one plain sentence capturing one "
        "distinct point. "
        "Preserve the author's actual claims — do not add interpretations or advice of your own. "
        "Return ONLY the bullet list. No heading, no preamble, no extra lines."
    ),
    "Explain It": (
        "Act as a conservative reading-aid paraphraser. Restate the difficult passage in clear, modern language without interpreting, expanding, or commenting on it. "
        "Follow the passage's order and logical structure. Preserve every explicit claim, list item, contrast, qualification, attribution, and evaluation. Do not omit or merge them. "
        "Copy every distinct evaluative term from the source into the answer; never replace several evaluations with one umbrella word. If the source uses 'superfluous,' 'inappropriate,' and 'counterproductive,' the answer must retain all three. "
        "Preserve scope and time distinctions such as 'previous or contemporary' rather than shortening them to 'existing.' "
        "Use only information stated in the passage. Add no background, definitions, examples, reasons, descriptions, implications, stakes, or conclusions. "
        "Keep vague or abstract wording equally general: never make it more specific. If the passage says only 'given the nature of X,' keep that phrase equally general; do not describe the nature of X or use it to invent a reason. "
        "Keep incomplete reasoning incomplete. If the author gives a judgment without fully explaining it, attribute the judgment to the author and stop there. Preserve uncertainty exactly: 'seems' must remain 'seems'. "
        "Write one natural paragraph of one to three concise sentences, no longer than the passage. Do not quote, cite, label, use bullets, or mention the passage or these instructions. Return ONLY the paraphrase."
    ),
    "Explain It (Simply)": (
        "Rewrite the passage in plain, everyday language. Preserve who says what, the order of the ideas, every contrast, qualification, list item, and distinct judgment. "
        "Never reverse what is usual and what is unusual, what the author accepts and rejects, or what is certain and what only seems true. "
        "Do not add reasons, background, definitions, examples, analogies, implications, or conclusions. If the source leaves something unexplained or vague, leave it unexplained or vague. "
        "Keep every separate judgment separate rather than replacing several judgments with one broad word. "
        "Write one or two short sentences, clearly shorter than the source. Prefer simple words, but keep an abstract term if simplifying it would invent meaning. "
        "Return ONLY the explanation. No preamble, labels, bullets, or extra lines."
    ),
    "Explain with Quotes": (
        "The reader wants a dependable explanation grounded only in the selected passage. "
        "First write 'Meaning:' followed by one or two short sentences explaining the passage's central idea. "
        "Then write 'Quotes:' followed by at most two bullet lines. "
        "Each bullet must begin with a short EXACT quote from the passage in quotation marks "
        "(at most 12 words), then a dash and a brief explanation of what that quote supports. "
        "Every claim in Meaning must be supported by at least one quoted phrase in Quotes. "
        "Do not add facts, motives, context, or interpretations not supported by the passage. "
        "If the passage does not establish something, say so rather than guessing. "
        "Keep the answer compact, use the same language as the passage, and return ONLY this card."
    ),
    "Extract Writing Style": (
        "The user selected a representative writing sample and wants to turn its style into reusable "
        "writing instructions. Analyse HOW the passage is written, not what it is about. "
        "Infer only characteristics supported by the sample: voice and register, sentence length and "
        "rhythm, diction, paragraph structure, rhetorical habits, imagery, punctuation, and notable "
        "tendencies to use or avoid. Do not reproduce phrases from the sample and do not identify or "
        "guess the author, book, genre, or subject. "
        "Create a short descriptive name of two to five words that describes the writing mechanics, "
        "not the topic. "
        "Return EXACTLY this format:\n"
        "STYLE NAME: <name>\n"
        "STYLE PROFILE:\n"
        "<a compact, concrete set of reusable writing guidelines>"
    ),
    "More Formal (Academic)": (
        "Rewrite the following text in a formal academic style. "
        "Use precise vocabulary and avoid contractions. Keep the same meaning. "
        "Preserve every argument, example, qualification, technical term, and concrete detail. "
        "Do NOT add claims, citations, references, evidence, or hedging that is not in the original. "
        "Do NOT pad the text with filler such as 'Moreover', 'Furthermore', or 'It is important to note'. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "Diacritics Only": (
        "You are a diacritics restoration tool for BHS (Bosnian/Croatian/Serbian). "
        "Your ONLY task is to restore missing diacritical characters: "
        "replace 'c' with 'č' or 'ć', 's' with 'š', 'z' with 'ž', 'd' with 'đ' — "
        "but ONLY where the correct diacritic is unambiguous given the word's meaning and context. "
        "If a word is ambiguous (e.g. 'kao' — keep as-is), leave it unchanged. "
        "Do NOT fix grammar. Do NOT change sentence structure. Do NOT rephrase. "
        "Do NOT change capitalization, punctuation, spacing, or word order. "
        "Do NOT add or remove any words. "
        "Return ONLY the text with diacritics restored. No explanation. No extra lines. "
        "If no diacritics need restoring, return the text exactly as given."
    ),
    "Translate BHS → English": (
        "Translate the following Bosnian, Croatian, or Serbian (BHS) text into natural, fluent English. "
        "The input may be written without diacritics (š→s, č/ć→c, ž→z, đ→d) — interpret it correctly. "
        "Do not translate word-for-word, but stay close to the original meaning. "
        "Use idiomatic, native-sounding phrasing where appropriate. "
        "Avoid adding new interpretations or changing the intent. "
        "Preserve tone, emotion, nuance, names, paragraph breaks, and formatting. "
        "Return ONLY the final English text. No explanation. No extra lines."
    ),
    "Translate English → BHS": (
        "Translate the following English text into natural, fluent Bosnian/Croatian/Serbian (ijekavica standard). "
        "Do not translate word-for-word, but stay close to the original meaning. "
        "Use idiomatic, native-sounding phrasing where appropriate. "
        "Always use correct diacritics (š, č, ć, ž, đ) in the output. "
        "Avoid adding new interpretations or changing the intent. "
        "Preserve tone, emotion, nuance, names, paragraph breaks, and formatting. "
        "Return ONLY the final BHS text. No explanation. No extra lines."
    ),
    "Translate Chinese → English": (
        "Translate the following Chinese text into natural, fluent English. "
        "The input may be in Simplified or Traditional Chinese — handle both correctly. "
        "Do not translate word-for-word; find the natural, idiomatic English equivalent. "
        "Preserve the original tone, register, and intent — do not soften, embellish, or interpret. "
        "Preserve names, proper nouns, paragraph breaks, and formatting. "
        "Return ONLY the final English text. No explanation. No extra lines."
    ),
    "Translate English → Chinese": (
        "Translate the following English text into natural, fluent Simplified Chinese (Mandarin). "
        "Do not translate word-for-word; find the natural, idiomatic Chinese equivalent. "
        "Use standard Mainland Chinese conventions (Simplified characters, Mandarin vocabulary). "
        "Preserve the original tone, register, and intent — do not soften, embellish, or interpret. "
        "Preserve names, proper nouns, paragraph breaks, and formatting. "
        "Return ONLY the final Chinese text. No explanation. No extra lines."
    ),
    "Translate Chinese → BHS": (
        "Translate the following Chinese text into natural, fluent Bosnian (ijekavica). "
        "The input may be in Simplified or Traditional Chinese — handle both correctly. "
        "Do not translate word-for-word; find the natural, idiomatic Bosnian equivalent. "
        "Use standard Bosnian (ijekavica), not Croatian or Serbian variants where they differ. "
        "Always use correct Bosnian diacritics (š, č, ć, ž, đ) in the output. "
        "Preserve the original tone, register, and intent — do not soften, embellish, or interpret. "
        "Preserve names, proper nouns, paragraph breaks, and formatting. "
        "Return ONLY the final Bosnian text. No explanation. No extra lines."
    ),
    "Diplomatically Worded": (
        "Rewrite the following text to be more tactful and diplomatic, without losing any of the substance. "
        "Soften blunt or harsh phrasing, but do not water down the core message or omit uncomfortable truths. "
        "If the original is critical or negative, keep the critique — just deliver it constructively. "
        "Avoid corporate filler and empty softeners ('perhaps you might consider'). Be direct but civil. "
        "Preserve the author's meaning, intent, and all specific details. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "Draft a Counterargument": (
        "Draft the strongest reasonable counterargument to the position in the following text. "
        "Address the original argument's actual claims, assumptions, and qualifications; do not strawman or mock it. "
        "Use only issues that can be identified from the text and general reasoning. Do NOT invent evidence, "
        "statistics, quotations, sources, events, or attributed views. "
        "Keep the same register and approximate length as the original, and state the counterargument directly "
        "without a preamble such as 'On the other hand'. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
    "Sharpen the Thesis": (
        "Restructure the following text so the central claim or conclusion leads the paragraph. "
        "Identify the main point and state it in the first sentence, clearly and specifically — no wind-up, no hedging. "
        "All supporting points should follow and serve the lead claim. Remove any sentences that do not directly support it. "
        "Do not add new arguments or facts not already present in the original. "
        "Preserve the author's voice and vocabulary as much as possible. "
        "Return ONLY the rewritten text. No explanation. No extra lines."
    ),
}

_FORMATTING_PROMPTS = {
    "Format as Bullet Points": (
        "Arrange the text as a bullet list, each item beginning with '• '. "
        "Use one item per distinct point and indented sub-bullets for its supporting details. "
        "Keep the source order where it matters. Do not turn it into a short summary."
    ),
    "Format as Numbered List": (
        "Arrange the text as a numbered list using '1. ', '2. ', and so on. "
        "Keep the source order and keep supporting details with their point. "
        "Number existing points or steps without inventing a sequence, priority, or missing steps."
    ),
    "Format as Checklist": (
        "Arrange explicit tasks or requirements as a checklist with '- [ ] ' before each item. "
        "Use '- [x] ' only for tasks explicitly stated to be completed. "
        "Keep deadlines, owners, conditions, and supporting details with their task. "
        "Keep non-actionable information in a short Notes section instead of inventing tasks from it. "
        "If there are no tasks or requirements, preserve the text without adding checkboxes."
    ),
    "Format as Table": (
        "Arrange the information as a Markdown pipe table with a header and separator row. "
        "Choose useful columns from the information present, such as Item and Details. "
        "Use as many rows as needed to retain all information. Leave cells blank for missing "
        "values; never guess them. Escape literal pipe characters within cells. "
        "Keep cell contents on a single line, separating multiple details with semicolons."
    ),
    "Add Headings": (
        "Organise the text into readable paragraphs under short, descriptive headings. "
        "Put each heading on its own line, with a blank line before the paragraph below. "
        "Use plain-text headings without Markdown markers. Derive headings only from the "
        "content; do not add an introduction, conclusion, or empty sections. "
        "Keep the original sentences and their order wherever possible."
    ),
}
for _format_mode, _format_instruction in _FORMATTING_PROMPTS.items():
    _REPHRASE_PROMPTS[_format_mode] = (
        "Format the supplied text into the requested structure. "
        "Preserve every fact, claim, qualification, name, number, attribution, and concrete detail. "
        "Preserve the original meaning, uncertainty, and language. Do not summarise or add new content. "
        "Make only wording changes necessary to fit the structure. "
        + _format_instruction +
        " Return ONLY the formatted text, without a preamble, explanation, or enclosing code fence."
    )

_FACTORY_PROMPTS = {}
_FACTORY_PROMPTS.update(_REPHRASE_PROMPTS)

def _resolve_trigger_mode(text: str, triggers: dict) -> tuple[str | None, str]:
    stripped = text.lstrip()
    best_mode: str | None = None
    best_len:  int        = 0

    for mode, trigger in triggers.items():
        t = trigger.strip()
        if not t:
            continue
        if stripped.lower().startswith(t.lower()):
            if len(t) > best_len:
                best_mode = mode
                best_len  = len(t)

    if best_mode is None:
        return None, text

    leading_ws = text[: len(text) - len(stripped)]
    clean = leading_ws + stripped[best_len:].lstrip()
    return best_mode, clean

def _detect_format_from_path(path: str) -> str | None:
    name = os.path.basename(path).lower()

    rules = [
        (r"gpt[-_]?oss",                    "Harmony  (gpt-oss)"),
        (r"deepseek",                       "DeepSeek"),
        (r"llama[-_]?3",                    "Llama 3  (Meta)"),
        (r"meta[-_]?llama[-_]?3",           "Llama 3  (Meta)"),
        (r"mi(x|s)tral",                    "Mistral / Mixtral"),
        (r"gemma",                          "Gemma  (Google)"),
        (r"zephyr",                         "Zephyr / HF Chat"),
        (r"command[-_]?r",                  "Command-R  (Cohere)"),
        (r"cohere",                         "Command-R  (Cohere)"),
        (r"(alpaca|vicuna)",                "Alpaca / Vicuna"),
        (r"qwen",                           "ChatML  (Qwen / Phi-3 / Yi)"),
        (r"phi[-_]?(3|4)",                  "ChatML  (Qwen / Phi-3 / Yi)"),
        (r"\byi[-_]?\d",                    "ChatML  (Qwen / Phi-3 / Yi)"),
        (r"internlm",                       "ChatML  (Qwen / Phi-3 / Yi)"),
        (r"minicpm",                        "ChatML  (Qwen / Phi-3 / Yi)"),
        (r"llama",                          "Alpaca / Vicuna"),
    ]

    for pattern, fmt in rules:
        if re.search(pattern, name):
            return fmt

    return None

# Matches any CJK character (Hiragana/Katakana, CJK ideographs, Hangul).
_CJK_RE = re.compile(
    r"[぀-ヿ"   # Hiragana / Katakana
    r"㐀-䶿"    # CJK Extension A
    r"一-鿿"    # CJK Unified Ideographs (core block)
    r"豈-﫿"    # CJK Compatibility Ideographs
    r"ｦ-ﾟ"    # Halfwidth Katakana
    r"가-힯]"   # Hangul syllables
)

def _word_count(text: str) -> int:
    """Return a word count suitable for both gating and token-budget estimates.

    Space-separated languages: len(text.split()) as usual.
    CJK languages (Chinese, Japanese, Korean, ...): count non-whitespace
    characters, since each character is roughly one morpheme and words are
    not (reliably) space-separated.  This prevents CJK text from being
    treated as a single 'word' and failing the min_word_count gate.
    """
    if _CJK_RE.search(text):
        return sum(1 for c in text if not c.isspace())
    return len(text.split())

def _is_mid_token(sentence: str, punct: str) -> bool:
    if sentence.count("`") % 2 == 1:
        return True

    tokens = sentence.split()
    if not tokens:
        return False
    last = tokens[-1]

    if "://" in last:
        return True

    if last.lower().startswith("www."):
        return True

    if re.search(r"\w{4,}\.\w{2,6}(?:[/?#]|$)", last):
        if re.search(r"\w\.\w", last):
            return True

    if re.search(r"(?:[A-Za-z]:\\|\\\\)", last):
        return True

    if "\\" in last:
        return True

    if last.count("/") >= 2:
        return True

    if re.match(r"^/[^/].*/", last):
        return True

    if len(re.findall(r"[a-z][A-Z]", last)) >= 2:
        return True

    if "[" in last:
        return True

    if "_" in last:
        return True

    return False
