# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Identify the language of a selected passage.

Only used for answer modes on "Auto". Those prompts otherwise tell the model to
"answer in the same language as the text", which makes the model identify the
language itself — and small models get it wrong on dense, Latinate prose. A
gemma-3-4b run over a Hegel passage answered in French 5 times out of 5, and
other models drifted to Spanish and German on the same input.

Naming the language explicitly removes the guess. Detection deliberately
returns None whenever it is not confident, so an uncertain answer falls back to
the previous behaviour rather than confidently naming the wrong language.

Spell-check uses the same scorer only as a target-specific hint: one distinctive
function word may be enough to prove that a short sentence is already in the
selected language, but never enough to choose a reading-mode answer language.
"""

import re

# Names must match WORKING_LANGUAGE_OPTIONS exactly — the result is fed straight
# into the prompt as the language to answer in.
_STOPWORDS = {
    "English":    "the of and to in is that it for as with this be are was not but by from have has",
    "German":     "der die das und ist nicht sich den von zu mit dem ein eine auch als auf für wird es",
    "French":     "le la les de des et est un une dans que qui pour par sur avec ce cette il elle au du ne pas",
    "Spanish":    "el la los las de que y en es un una por con para se del al lo como más pero su",
    "Italian":    "il lo la gli le di che e è un una per con non si del della come più sono ma nel questo anche alla",
    "Portuguese": "o a os as de que e é do da em no na um uma para com não se por mais como mas ao dos",
    "Dutch":      "de het een en van is dat in te niet op zijn met voor ook aan door maar worden",
    "BHS":        "i je se na da za su od koji ali kao sa ne što ili biti bio ovo taj",
    "Polish":     "i w na z do że nie to się jest jak ale po od za przez który oraz tego",
    "Czech":      "a v na se je že to s z do který ale jako pro po od být tento",
    "Swedish":    "och att det som en på är för med av den till inte om har de ett",
    "Danish":     "og at det en den til er som på de med af for ikke der han",
    "Norwegian":  "og i det er en som på til av for med den ikke har de et",
    "Finnish":    "ja on ei se että ovat hän mutta kuin myös voi kun niin ole",
    "Romanian":   "și de la în cu care este un o pentru nu se din pe al au",
    "Hungarian":  "a az és hogy nem is de egy van meg csak még ez azt vagy",
    "Turkish":    "ve bir bu için ile da de olarak olan daha çok gibi kadar ise",
}
_STOPWORDS = {lang: set(words.split()) for lang, words in _STOPWORDS.items()}

# The three Scandinavian stopword sets necessarily overlap. These exact forms
# are both common and distinctive, so a small boost separates real paragraphs
# while samples without such evidence still abstain at the normal ratio gate.
_SCANDINAVIAN_HINTS = {
    "Swedish": set("och är att för ett".split()),
    "Danish": set("af hvad bliver".split()),
    "Norwegian": set("av hva blir".split()),
}
_HINT_WEIGHT = 3

# Scripts settle the question outright — no Latin-alphabet scoring can compete.
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
# Letters unique to Ukrainian vs Russian, used only to split the two.
_UKRAINIAN_ONLY = set("їієґ")
_RUSSIAN_ONLY = set("ыэъё")

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Below this many recognised function words the sample is too short to trust.
_MIN_HITS = 4
# A script is strong evidence, but a lone letter/name is not a passage.
_MIN_SCRIPT_WORDS = 3
# The winner must beat the runner-up by this ratio, or the passage is ambiguous
# (English and Dutch, or Danish and Norwegian, share a lot of short words).
_MIN_RATIO = 1.4


def _script_language(text: str):
    """Language implied by the writing system, or None for Latin script."""
    if _GREEK.search(text):
        return "Greek"
    if _CYRILLIC.search(text):
        letters = set(text.lower())
        if letters & _UKRAINIAN_ONLY:
            return "Ukrainian"
        if letters & _RUSSIAN_ONLY:
            return "Russian"
        return "Russian"     # the far more common default for unmarked Cyrillic
    return None


def detect_language(text: str, *, min_hits: int = _MIN_HITS):
    """Best-guess language name, or None when the passage is too short or unclear.

    None is a normal, expected result — callers must fall back rather than
    treat it as an error.
    """
    text = (text or "").strip()
    if not text:
        return None

    words = [w.lower() for w in _WORD_RE.findall(text)]
    if not words:
        return None
    script = _script_language(text)
    if script:
        return script if len(words) >= _MIN_SCRIPT_WORDS else None
    # Accents alone are a strong hint but a weak discriminator, so they only
    # feed the scoring below via the stopword lists themselves.
    raw_counts = {
        lang: sum(1 for w in words if w in stop)
        for lang, stop in _STOPWORDS.items()
    }
    counts = dict(raw_counts)
    for lang, hints in _SCANDINAVIAN_HINTS.items():
        counts[lang] += _HINT_WEIGHT * sum(1 for word in words if word in hints)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    best, best_hits = ranked[0]
    if raw_counts[best] < min_hits:
        return None
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if runner_up and best_hits < runner_up * _MIN_RATIO:
        return None
    return best


def is_same_language(text: str, working_language: str) -> bool:
    """True when `text` is already written in the working language.

    Decides whether spell-check should CORRECT the text or TRANSLATE it into the
    working language, and correspondingly whether the auto-replace safety checks
    should expect similar output (correction) or allow a rewrite (translation).
    Both callers must use this one predicate: if they disagree, the text gets
    corrected while the safety net is relaxed as though it had been translated.

    Auto counts as "same" — there is no target language to translate into.

    Detection failure normally counts as DIFFERENT, i.e. translate. For this
    targeted question the minimum is lowered to one recognised word, but the
    normal winner/runner-up ratio still applies: "ein sehr schoner Tag" is
    positively German, while an input with no German evidence still translates
    to German. The BHS ambiguity exception is documented below.
    """
    lang = (working_language or "Auto").strip()
    if lang == "Auto":
        return True
    from vedium.constants import _NATIVE_TYPING_LANGS
    detected = detect_language(text, min_hits=1)
    if lang in _NATIVE_TYPING_LANGS:
        # BHS typed without diacritics is often too short or ambiguous for the
        # detector, so uncertainty still means "correct as BHS". Positive
        # evidence that the sentence is another language, however, must honour
        # the selected working language and translate it to BHS just like every
        # other named language.
        return detected in (None, lang)
    return detected == lang
