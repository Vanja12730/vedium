"""Model-output post-processing.

Reasoning / chain-of-thought stripping (think-tags and Harmony channels),
BHS diacritics restoration, and the structural sanity checks that stop a
bad completion from replacing the user's text.
"""

import re
import difflib
import unicodedata

from vedium.constants import _CJK_RE, _word_count
from vedium import diagnostics
from vedium.llm_state import _backend



# Reasoning-tag variants emitted by different model families, matched
# case-insensitively and tolerant of surrounding whitespace.  DeepSeek-R1 /
# QwQ use <think>; some Qwen3 and other builds use <thinking>; a handful use
# <reason>/<reasoning> or <thought>...</thought>.
_THINK_OPEN_RE  = re.compile(r"<\s*(?:think|thinking|thought|reason|reasoning)\s*>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"<\s*/\s*(?:think|thinking|thought|reason|reasoning)\s*>", re.IGNORECASE)

# --- Harmony / channel-format scaffolding (gpt-oss and similar) ---------------
# These models wrap output in <|channel|>NAME<|message|>...<|end|> segments.
# The user-facing answer lives in the "final" channel; "analysis"/"commentary"/
# "reasoning" channels are chain-of-thought and must NEVER reach the document.
# Some local templates emit malformed single-pipe variants such as
# "<|channel>thought<channel|>", so every token pattern tolerates a missing pipe
# on either side.  The harmony-token pattern deliberately requires at least one
# pipe so it never eats generic angle-bracket content like List<String> or <br>.
_HARMONY_TOK_RE = re.compile(r"<\|[A-Za-z_]+\|?>|<[A-Za-z_]+\|>")
_CHANNEL_HDR_RE = re.compile(r"<\|?channel\|?>\s*([A-Za-z]+)", re.IGNORECASE)
_FINAL_HDR_RE   = re.compile(r"<\|?channel\|?>\s*final\b", re.IGNORECASE)
_COT_CHANNELS   = {"analysis", "commentary", "reasoning"}
_LEAD_NOISE_RE  = re.compile(
    r"^\s*(?:assistant|developer|system|user|final|analysis|commentary|reasoning|thought|thinking)\b[:\s]*",
    re.IGNORECASE,
)

# Some local templates (gpt-oss-style, observed with Gemma builds) delimit the
# end of the reasoning block with a literal "<thought_end>" marker instead of a
# closing </think> tag, e.g.:
#   <|channel>...reasoning...<thought_end><channel|>the actual answer
# The answer is everything after the LAST such marker. Tolerates pipe and
# separator variants: <|thought_end|>, <thought-end>, <thought end>, etc.
_THOUGHT_END_RE = re.compile(r"<\s*\|?\s*thought[_\-\s]*end\s*\|?\s*>", re.IGNORECASE)
# The matching opener, used by the streaming filter to know reasoning has begun.
_THOUGHT_OPEN_RE = re.compile(r"<\s*\|?\s*(?:thought[_\-\s]*(?:start|begin)?|channel)\s*\|?\s*>", re.IGNORECASE)

# Detokenised Harmony leak (gpt-oss run through the in-process llama-cpp path,
# e.g. with the wrong prompt format selected): llama-cpp renders special tokens
# (<|channel|>, <|message|>, <|start|>, <|end|>) as empty strings, so the raw
# output has NO <|...|> markers at all — the channel names survive as bare
# lowercase words glued straight onto the surrounding text:
#     "analysisWe need to fix…assistantfinalCorrected text."
# The answer follows the glued "assistantfinal" marker; a leading bare CoT
# channel name with no such marker means the answer never made it out.
# Both patterns are deliberately strict (case-sensitive, glued to the next
# character) so ordinary prose starting with "Analysis …" is never touched.
_PLAIN_FINAL_MARK   = "assistantfinal"
_PLAIN_COT_OPEN_RE  = re.compile(r"^\s*(?:analysis|commentary)\S")


def _strip_channels(text: str) -> str:
    """Remove harmony channel scaffolding, keeping only the answer text.

    Handles three shapes:
      1. final channel present  – keep only what follows the LAST "final"
         channel header (canonical gpt-oss answer location; drops any preceding
         analysis/commentary reasoning).
      2. only CoT channels      – no "final" channel and every channel seen is a
         known chain-of-thought one (analysis/commentary/reasoning): the answer
         never made it out (e.g. truncated mid-reasoning) -> return "" so the
         safety layer falls back to the original text instead of pasting CoT.
      3. other / malformed      – strip the scaffolding tokens and keep the
         prose.  Covers models that route the actual answer through a
         non-standard channel name (the observed case used a channel literally
         called "thought").
    """
    if "<|" not in text and "|>" not in text:
        # No markers left — either a clean answer, or a detokenised Harmony
        # leak (see _PLAIN_FINAL_MARK above). Keep what follows the LAST glued
        # "assistantfinal"; a bare CoT channel opener with no final marker
        # means the answer was truncated mid-reasoning -> return "" so the
        # safety layer falls back to the original text.
        if _PLAIN_FINAL_MARK in text:
            return text.rsplit(_PLAIN_FINAL_MARK, 1)[1].strip()
        if _PLAIN_COT_OPEN_RE.match(text):
            return ""
        return text

    finals = list(_FINAL_HDR_RE.finditer(text))
    if finals:
        text = text[finals[-1].end():]
    else:
        names = [m.group(1).lower() for m in _CHANNEL_HDR_RE.finditer(text)]
        if names and all(n in _COT_CHANNELS for n in names):
            return ""

    text = _HARMONY_TOK_RE.sub("", text)
    # Drop a leading channel-name or role word left behind by the token removal
    # (e.g. "thought\n...", "assistant: ...").  Loop a few times to peel a short
    # "assistant final" style run.
    for _ in range(4):
        peeled = _LEAD_NOISE_RE.sub("", text)
        if peeled == text:
            break
        text = peeled
    return text.strip()


def _strip_reasoning(text: str) -> str:
    """Remove a reasoning model's chain-of-thought from raw output.

    Handles three output shapes, not just the well-formed one:

      1. Balanced       "<think>...</think>answer"        -> "answer"
      2. Close-only     "...reasoning...</think>answer"   -> "answer"
         DeepSeek-R1 and similar models inject the opening <think> into the
         PROMPT via their chat template, so generation starts *inside* the
         block and the raw output contains only the closing tag.  The old
         "<think>.*?</think>" regex never matched this, so the entire
         chain-of-thought was returned as the "correction" and silently
         pasted into the user's document.
      3. Open, no close "<think>...(truncated)"           -> ""
         max_tokens was hit before the model finished reasoning; no answer
         survived, so return empty and let the safety layer fall back to the
         original text rather than leaking partial reasoning.
    """
    if not text:
        return text

    # Channel/thought-style reasoning that opened but never closed (no
    # <thought_end>, no closing </think>, no 'final' channel) means the model
    # ran out of budget or looped while still reasoning — no answer survived.
    # Return "" so the caller falls back to the original text instead of pasting
    # raw chain-of-thought. The <|message|> guard preserves well-formed harmony
    # output (handled by _strip_channels below), which always carries a message
    # token even when its answer rides a non-standard channel name.
    if (_THOUGHT_OPEN_RE.search(text)
            and not _THOUGHT_END_RE.search(text)
            and not _THINK_CLOSE_RE.search(text)
            and not _FINAL_HDR_RE.search(text)
            and not re.search(r"<\|?message\|?>", text, re.IGNORECASE)):
        return ""

    # Some templates close reasoning with a literal "<thought_end>" marker rather
    # than </think>; the answer is whatever follows the last one. Cut here first
    # so the channel/think handling below only sees the answer portion.
    thought_ends = list(_THOUGHT_END_RE.finditer(text))
    if thought_ends:
        text = text[thought_ends[-1].end():]

    # Harmony / channel-format models wrap the answer in <|channel|> segments;
    # strip that scaffolding before the <think>-tag handling below.
    text = _strip_channels(text)

    # 1. Strip well-formed blocks (also covers multiple blocks).
    text = re.sub(
        r"<\s*(think|thinking|thought|reason|reasoning)\s*>.*?<\s*/\s*\1\s*>",
        "", text, flags=re.DOTALL | re.IGNORECASE,
    )
    # 2. Close-only remnant: keep only what follows the LAST closing tag.
    closes = list(_THINK_CLOSE_RE.finditer(text))
    if closes:
        text = text[closes[-1].end():]
    # 3. Unterminated opening tag -> truncated reasoning, nothing to keep.
    if _THINK_OPEN_RE.search(text):
        return ""

    return text.strip()


def _maybe_flag_reasoning_model(raw: str) -> None:
    """Set _backend.reasoning_detected=True the first time a reasoning block is seen.

    Called immediately after every raw LLM completion so the flag is set before
    the *next* call computes its token budget — meaning the very first response
    from a reasoning model still runs short, but every subsequent call gets the
    correct budget.  In practice this is fine: the first trigger is almost always
    a warm-up or short sentence whose truncated <think> output still resolves to
    the right correction; the improvement is visible from the second sentence on.
    """
    if not _backend.reasoning_detected and (
        _THINK_OPEN_RE.search(raw) or _THINK_CLOSE_RE.search(raw)
        or _THOUGHT_END_RE.search(raw)
        # Detokenised Harmony leak: gpt-oss reasoning with the special tokens
        # stripped still burns output tokens, so the budget must activate.
        or _PLAIN_FINAL_MARK in raw or _PLAIN_COT_OPEN_RE.match(raw)
    ):
        # A bare closing tag (no opening one) is the DeepSeek-R1 signature:
        # the open tag lives in the prompt, so we must detect on </think> too,
        # otherwise the thinking budget never activates for these models.
        # <thought_end> covers gpt-oss-style templates that use that delimiter.
        _backend.reasoning_detected = True


def _streaming_answer(raw: str) -> tuple:
    """Given the raw stream accumulated so far, return (visible_text, thinking).

    Used to drive a live UI: while the model is still inside its reasoning
    block, `thinking` is True and `visible_text` is "" (so the UI shows a
    "Thinking…" placeholder); once the answer begins to emerge, `thinking`
    flips to False and `visible_text` holds the cleaned answer-so-far.

    Reuses the same delimiters as _strip_reasoning, evaluated incrementally:
      • answer is whatever follows the LAST reasoning boundary
        (<thought_end>, </think>, or a 'final' channel header);
      • before any boundary, if a reasoning opener is actually present we are
        still thinking;
      • a model with no reasoning markers at all streams straight through.
    """
    if not raw:
        return "", False

    boundary = None
    for m in _THOUGHT_END_RE.finditer(raw):
        boundary = m.end()
    for m in _THINK_CLOSE_RE.finditer(raw):
        boundary = max(boundary or 0, m.end())
    finals = list(_FINAL_HDR_RE.finditer(raw))
    if finals:
        boundary = max(boundary or 0, finals[-1].end())
    # Detokenised Harmony leak: the glued "assistantfinal" marker is the
    # reasoning/answer boundary when special tokens were dropped.
    plain_final = raw.rfind(_PLAIN_FINAL_MARK)
    if plain_final != -1:
        boundary = max(boundary or 0, plain_final + len(_PLAIN_FINAL_MARK))

    if boundary is not None:
        answer = _strip_reasoning(raw[boundary:])
        return answer, False

    # No boundary yet. If we can SEE a reasoning opener, we're mid-reasoning.
    if (_THINK_OPEN_RE.search(raw) or _THOUGHT_OPEN_RE.search(raw)
            or _CHANNEL_HDR_RE.search(raw) or _PLAIN_COT_OPEN_RE.match(raw)):
        return "", True

    # No markers at all. Only assume "still reasoning" when a reasoning model was
    # detected AND thinking is actually active — i.e. a model (like DeepSeek-R1)
    # whose opening tag lives in the prompt, so its raw output starts mid-thought
    # with no visible opener. When thinking is disabled (the default for the
    # llama-server path) or on cloud backends, the output IS the answer and must
    # stream live — otherwise the chat just shows "Thinking…" the whole time.
    if _backend.reasoning_detected and not _backend.server_disable_thinking:
        return "", True

    # Plain answer (or thinking-disabled output): show it as it streams.
    return raw.strip(), False


# Tokens reserved for <think> blocks when a reasoning model is auto-detected.
# correction task uses 200-600 thinking tokens on small models; larger reasoning
# models (14B+) routinely use 1000-3000+ tokens. 2048 is a safer default.
# Users can override this via cfg["thinking_budget"].
_THINKING_BUDGET_AUTO = 2048


def _finish_is_length(reason) -> bool:
    """True when a finish_reason means the token budget was exhausted.
    OpenAI-compatible providers report "length"; some pass through native
    variants like Gemini's MAX_TOKENS."""
    return str(reason or "").strip().lower() in {"length", "max_tokens", "max_output_tokens"}


def _reasoning_starved_output(text: str) -> bool:
    """True when a length-capped completion has no dependable answer.

    Two shapes: nothing visible at all (server-side thinkers burned the budget
    before emitting content), or reasoning markers in the content (inline
    thinkers like gemma-4's <thought> blocks) — hitting the cap with a thought
    block present means the thought or the answer was cut short, so the visible
    text can't be trusted even when it is non-empty."""
    if not (text or "").strip():
        return True
    return bool(_THINK_OPEN_RE.search(text) or _THINK_CLOSE_RE.search(text)
                or _THOUGHT_OPEN_RE.search(text) or _THOUGHT_END_RE.search(text))


def _flag_hidden_reasoning(model: str, max_tokens: int) -> None:
    """Mark the backend as a hidden-reasoning model (cloud models whose
    thinking counts against max_tokens but never appears in the response, so
    _maybe_flag_reasoning_model's <think>-tag detection can't catch them).
    Subsequent calls then reserve _THINKING_BUDGET_AUTO via _get_thinking_budget."""
    diagnostics.record_throttled(
        "backend", "warning",
        f"{model} ran out of its {max_tokens}-token budget while reasoning; "
        f"reserving a {_THINKING_BUDGET_AUTO}-token thinking budget and retrying.",
        cooldown_sec=30.0,
    )
    _backend.reasoning_detected = True


def _get_thinking_budget(cfg: dict | None = None) -> int:
    """Return the number of tokens to reserve for <think> reasoning output.

    Priority:
      1. cfg["thinking_budget"] if > 0  — explicit manual override.
      2. _THINKING_BUDGET_AUTO           — when a reasoning model has been
                                           auto-detected (first <think> seen).
      3. 0                               — non-reasoning model; no budget needed.
    """
    if cfg is not None:
        manual = int(cfg.get("thinking_budget", 0))
        if manual > 0:
            return manual
    return _THINKING_BUDGET_AUTO if _backend.reasoning_detected else 0


_BHS_ALLOWED = set("šŠčČćĆžŽđĐ")

# Standard Serbian Cyrillic → Gaj's Latin transliteration. Small models
# sometimes emit Cyrillic homoglyphs (e.g. Cyrillic 'о' U+043E that looks
# identical to Latin 'o') in otherwise-Latin BHS text. We transliterate any
# stray Cyrillic back to Latin so the output is consistently Latin script.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e",
    "ж": "ž", "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj",
    "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "ћ": "ć", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "č",
    "џ": "dž", "ш": "š",
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E",
    "Ж": "Ž", "З": "Z", "И": "I", "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj",
    "М": "M", "Н": "N", "Њ": "Nj", "О": "O", "П": "P", "Р": "R", "С": "S",
    "Т": "T", "Ћ": "Ć", "У": "U", "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č",
    "Џ": "Dž", "Ш": "Š",
}


def _sanitize_bhs_diacritics(text: str) -> str:
    """Normalise model output to clean BHS Latin script.

    BHS only uses š/č/ć/ž/đ. Models sometimes hallucinate other accented
    characters (ǒ, ù, ó, …) or emit Cyrillic homoglyphs. This:
      1. transliterates any stray Cyrillic letter to its Latin equivalent, and
      2. strips combining marks from any non-BHS accented character, keeping
         only the base letter.
    """
    result = []
    for ch in text:
        if ch in _CYRILLIC_TO_LATIN:
            result.append(_CYRILLIC_TO_LATIN[ch])
        elif ch in _BHS_ALLOWED or ord(ch) < 128:
            result.append(ch)
        else:
            # Decompose to base + combining marks, keep only the base
            decomposed = unicodedata.normalize("NFD", ch)
            base = decomposed[0]
            recomposed = unicodedata.normalize("NFC", base)
            result.append(recomposed)
    return "".join(result)


# Common BHS words that lose their diacritics when typed on a plain keyboard.
# Each key (de-diacriticised form) maps unambiguously to one correct spelling —
# i.e. the key is NOT itself a valid different BHS word. Deliberately excludes
# traps like 'posao' (job), 'zao' (evil), 'sto' (hundred/table) where the
# bare-Latin form is a real word. This catches the high-frequency cases small
# models miss; it is not a full dictionary.
_BHS_DIACRITIC_WORDS = {
    # verbs: ići / doći / naći / proći / izaći (l-participles)
    "isao": "išao", "dosao": "došao", "nasao": "našao", "prosao": "prošao",
    "otisao": "otišao", "izasao": "izašao", "usao": "ušao",
    "dosli": "došli", "nasli": "našli", "prosli": "prošli", "otisli": "otišli",
    "izasli": "izašli", "dosla": "došla", "nasla": "našla", "prosla": "prošla",
    # moći / htjeti / željeti / kazati present tense
    "moze": "može", "mozes": "možeš", "mozemo": "možemo", "mozete": "možete",
    "hoce": "hoće", "hoces": "hoćeš", "hocemo": "hoćemo", "hocete": "hoćete",
    "nece": "neće", "neces": "nećeš", "necemo": "nećemo", "necete": "nećete",
    "zelim": "želim", "zelis": "želiš", "zeli": "želi", "zelimo": "želimo",
    "kaze": "kaže", "kazes": "kažeš", "kazem": "kažem",
    "ides": "ideš", "imas": "imaš", "znas": "znaš", "gledas": "gledaš",
    "radis": "radiš", "vidis": "vidiš", "jedes": "jedeš",
    # common nouns / adverbs / interjections
    "cao": "ćao", "sta": "šta", "nesto": "nešto", "nista": "ništa",
    "svasta": "svašta", "vise": "više", "jos": "još", "vec": "već",
    "covek": "čovek", "covjek": "čovjek", "zena": "žena", "zene": "žene",
    "muz": "muž", "noc": "noć", "noci": "noći", "moc": "moć",
    "pomoc": "pomoć", "caj": "čaj", "cesto": "često", "veceras": "večeras",
    "vecera": "večera",
}

_BHS_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_BHS_DIACRITIC_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _match_case(original: str, replacement: str) -> str:
    """Apply the casing of `original` to `replacement`."""
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _restore_common_bhs(text: str) -> str:
    """Restore diacritics on common BHS words the model left bare.

    Whole-word, case-preserving. Only touches words in the curated
    unambiguous list, so correctly-spelled text passes through untouched.
    """
    def repl(m: "re.Match") -> str:
        word = m.group(0)
        corrected = _BHS_DIACRITIC_WORDS[word.lower()]
        return _match_case(word, corrected)
    return _BHS_WORD_RE.sub(repl, text)


def _capitalize_sentences(text: str) -> str:
    """Capitalize the first letter of the text and of each sentence.

    A sentence boundary is sentence-ending punctuation (. ! ?) followed by
    whitespace. Only the first alphabetic character after the boundary is
    upper-cased; everything else is left untouched so mid-sentence words and
    intentional casing are preserved. Unicode-aware, so BHS letters
    (š/č/ć/ž/đ → Š/Č/Ć/Ž/Đ) capitalize correctly.
    """
    chars = list(text)
    n = len(chars)
    capitalize_next = True
    for i, ch in enumerate(chars):
        if capitalize_next:
            if ch.isspace():
                continue
            if ch.isalpha():
                # Extract word to check for camelCase (brand names / code symbols)
                j = i
                while j < n and (chars[j].isalnum() or chars[j] in "'_-"):
                    j += 1
                word = "".join(chars[i:j])
                is_camel = any(c1.islower() and c2.isupper() for c1, c2 in zip(word, word[1:]))
                if not is_camel:
                    chars[i] = ch.upper()
            capitalize_next = False
        elif ch in ".!?":
            # Only a real sentence boundary if the mark is followed by whitespace
            # or ends the text. This prevents capitalizing the character after a
            # period inside URLs/emails/decimals/abbreviations — e.g.
            # "example.com" must NOT become "example.Com", and "3.14" stays "3.14".
            nxt = chars[i + 1] if i + 1 < n else None
            if nxt is None or nxt.isspace():
                capitalize_next = True
    return "".join(chars)


# --- LLM output cleanup / safety ---
def _strip_echo_prefix(original: str, corrected: str) -> str:
    """
    Some models echo the source text before the translation, producing output like:
        "Na primjer, ako pišeš something...\nFor example, if you write something..."
    This function detects that pattern and returns only the intended output part.

    Strategy: split on newlines; if ANY leading chunk is very similar to the
    original (>=65% token overlap after Unicode normalisation), discard everything
    up to and including that chunk and return the rest.  Falls back to `corrected`
    unchanged if nothing looks like an echo.
    Unicode normalisation handles diacritics: "pises" ≈ "pišeš".
    """
    import unicodedata

    def _normalise(s: str) -> str:
        """Fold diacritics so 'pises' and 'pišeš' share tokens."""
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()

    lines = [l.strip() for l in corrected.split("\n") if l.strip()]
    if len(lines) < 2:
        return corrected          # single line — nothing to strip

    orig_tokens = set(re.findall(r"\w+", _normalise(original).lower()))
    if not orig_tokens:
        return corrected

    # Walk forward through lines; once we find one that doesn't look like the
    # original, everything from that line onward is the real output.
    for i, line in enumerate(lines):
        line_tokens = set(re.findall(r"\w+", _normalise(line).lower()))
        if not line_tokens:
            continue
        overlap = len(orig_tokens & line_tokens) / len(orig_tokens)
        if overlap >= 0.65:
            # This line looks like the original — skip it and return the rest.
            remainder = "\n".join(lines[i + 1:])
            if remainder:
                return remainder
            # Echo was the last line; stripping it leaves nothing useful.
            # Return an empty string so _safe_model_correction falls back to
            # the original rather than keeping the echoed source text.
            return ""

    return corrected


def _clean_llm_output(text: str) -> str:
    """Normalize harmless formatting problems in model output."""
    text = text.strip()
    text = _strip_reasoning(text).strip()
    text = text.strip("\"'")
    # Add missing spaces after punctuation without disturbing decimals like 3.14.
    text = re.sub(r"([,;:])(?=\S)", r"\1 ", text)
    text = re.sub(r"([.!?])(?=[A-Za-zÀ-ž0-9])", r"\1 ", text)
    text = re.sub(r"(?<=\d)\. (?=\d)", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _punct_counts(text: str) -> dict[str, int]:
    """Count punctuation marks that are usually unsafe to silently delete."""
    # Intentionally excludes " and ' — models routinely normalise straight quotes
    # to curly quotes (or vice versa), which would cause a spurious count mismatch
    # and make _safe_model_correction discard an otherwise valid correction.
    return {ch: text.count(ch) for ch in ",;:()[]{}"}


# URLs (http/https/www) and email addresses — must be preserved verbatim.
_URLISH_RE = re.compile(
    r"(?:https?://|www\.)\S+|[^\s@]+@[^\s@]+\.[^\s@]+",
    re.IGNORECASE,
)


def _looks_structurally_bad(original: str, corrected: str, is_translation: bool = False) -> bool:
    """Return True if model output appears to have damaged punctuation/spacing."""
    if not corrected:
        return True

    # CamelCase parity — checked in both directions:
    #
    #   corr_camel - orig_camel  (new transitions introduced by the model)
    #     Catches word-gluing artifacts: "itMy", "wordAnother".  Only flags
    #     transitions that are genuinely NEW so that brand names / acronyms
    #     already present in the original (iPhone, macOS, etc.) never cause a
    #     valid correction to be discarded.
    #
    #   orig_camel - corr_camel  (existing transitions removed by the model)
    #     Catches the mirror failure: the model silently flattening a
    #     programmatic identifier to lowercase.
    #     e.g. getUserByIndx -> getuserbyindx, isReady -> isready.
    #     Both the new-camelCase and removed-camelCase checks must apply
    #     regardless of is_translation: code identifiers must be preserved
    #     verbatim in the target language as well as the source.
    orig_camel = set(re.findall(r"[a-z][A-Z]", original))
    corr_camel = set(re.findall(r"[a-z][A-Z]", corrected))
    if corr_camel - orig_camel:          # new camelCase -> word-gluing artifact
        return True
    if orig_camel - corr_camel:          # removed camelCase -> identifier flattened
        return True

    # For every camelCase word in the original text, it must appear exactly case-sensitively in the corrected text
    # (if the word's base form is still there).
    for word in original.split():
        clean_word = word.strip(".,;:!?()\"'")
        if any(c1.islower() and c2.isupper() for c1, c2 in zip(clean_word, clean_word[1:])):
            if clean_word.lower() in corrected.lower() and clean_word not in corrected:
                return True

    # Bracket-content preservation: every [...] expression that appears in the
    # original must appear verbatim in the corrected output.
    # A grammar or translation model has no business renaming index variables
    # or key literals — arr[userIndex] -> arr[user index] is destructive even
    # though the bracket count stays at 1/1 and would pass all other checks.
    # Applied regardless of is_translation for the same reason as camelCase.
    for bracket_expr in re.findall(r"\[[^\]]*\]", original):
        if bracket_expr not in corrected:
            return True

    # URL / email preservation: any link or address in the original must survive
    # verbatim.  Models sometimes "helpfully" rewrite a URL — e.g. replacing the
    # scheme (https://example.com -> <link>://example.com), shortening it, or
    # turning it into markdown.  Trailing sentence punctuation is stripped from
    # the token before the check so a model legitimately adding a period after a
    # link doesn't trip it.  Applied regardless of is_translation: a URL or email
    # is identical in every language.
    for token in _URLISH_RE.findall(original):
        token = token.rstrip(".,;:!?)\"'")
        if token and token not in corrected:
            return True

    # Punctuation parity: do not silently delete existing internal punctuation
    # during grammar correction.  Skip this check for translation — the target
    # language legitimately uses different comma/semicolon structure, so a
    # missing comma is not evidence of model damage.
    if not is_translation:
        original_counts  = _punct_counts(original)
        corrected_counts = _punct_counts(corrected)
        for ch, count in original_counts.items():
            if corrected_counts.get(ch, 0) < count:
                return True

    return False


def _similarity(a: str, b: str) -> float:
    """Return a 0-1 similarity ratio between two strings (case-folded)."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# _CJK_RE and _word_count live in constants.py (shared with buffer's
# min-word gating); both are imported at the top of this module.


def _word_count_ok(original: str, corrected: str, is_translation: bool = False) -> bool:
    """Return False if the model silently dropped or added words beyond a safe margin.

    Translations legitimately change word count (articles, prepositions, idioms),
    so the check is skipped for those.  For grammar/diacritics correction the output
    should be close to the same length as the input — a difference of more than one
    word is a strong signal that the model hallucinated or truncated.

    CJK text uses character counts rather than word counts (no spaces between words).
    Common grammatical corrections in Japanese/Chinese can add 1-2 characters (e.g.
    adding a polite です ending), so a tolerance of ±2 characters is used there.
    """
    if is_translation:
        return True
    tolerance = 2 if _CJK_RE.search(original) else 1
    return abs(_word_count(original) - _word_count(corrected)) <= tolerance


# Assistant boilerplate / refusals / chat acknowledgements a model emits instead
# of correcting the text (e.g. "Please provide the text you would like me to
# correct.", "I cannot fulfil this request", "As an AI...", "Okay, let's do it!").
# These must never replace the user's text — even in translation mode, where the
# normal low-similarity floor is intentionally skipped.
_META_RESPONSE_RE = re.compile(
    # — refusals / boilerplate —
    r"(please provide|provide the (text|sentence)"
    r"|you('| woul)d? ?(like|want) me to"
    r"|the (text|sentence) you('| woul)d?"
    r"|i (cannot|can ?not|can'?t|am unable|'m unable|am not able|'m not able)"
    r"|i'?m sorry|i am sorry|as an ai|i'?d be happy to|i would be happy to"
    r"|there (is|'s) no (text|sentence|content)|no text (was )?provided"
    r"|could you (please )?(provide|clarify)|feel free to|happy to help"
    # — conversational acknowledgements (model treated input as a chat) —
    # Kept deliberately specific: phrases that are almost never a legitimate
    # translation/correction of user text, so genuine short translations
    # (e.g. "Got it", "No problem", "I'll help you") are not falsely rejected.
    r"|let'?s do it|let'?s get started|sure thing|here (you go|'?s (the|your))"
    r"|i'?ll help you with|how can i (help|assist))",
    re.IGNORECASE,
)


def _looks_like_meta_response(original: str, corrected: str) -> bool:
    """True if the model answered ABOUT the task instead of doing it.

    Only flags boilerplate the model INTRODUCED — if the same phrase is in the
    original (e.g. the user actually wrote "please provide the report"), it is
    not treated as a meta-response, avoiding false positives on genuine text.
    """
    return bool(_META_RESPONSE_RE.search(corrected)
                and not _META_RESPONSE_RE.search(original))


def _safe_model_correction(
    original: str,
    corrected: str,
    is_translation: bool = False,
) -> str:
    """Clean model output and fall back to original if it looks unsafe."""
    corrected = _clean_llm_output(corrected)
    corrected = _strip_echo_prefix(original, corrected)
    # Reject assistant boilerplate / refusals regardless of mode. This runs even
    # for translations, where the low-similarity floor below is intentionally
    # skipped and would otherwise let a meta-response through and overwrite the
    # user's text.
    if _looks_like_meta_response(original, corrected):
        return original
    if _looks_structurally_bad(original, corrected, is_translation):
        return original
    if not _word_count_ok(original, corrected, is_translation):
        return original
    # Similarity floor: if the corrected text is less than 40% similar to the
    # original, the model has likely hallucinated or rewritten the sentence
    # rather than correcting it.  Skipped for translations, where a low
    # character-level similarity between source and target is expected and normal.
    if not is_translation and _similarity(original, corrected) < 0.40:
        return original
    return corrected
