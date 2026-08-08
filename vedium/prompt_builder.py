# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

# ── 3. PROMPT BUILDER ─────────────────────────────────────────────────────────
# Converts (format, system_msg, user_msg) into a raw prompt string + stop tokens.
# Add a new elif branch here to support additional model families.

def _build_prompt(fmt: str, system: str, user: str):
    """Return (prompt_str, stop_token_list) for the given format name."""
    if fmt.startswith("ChatML"):
        p = (f"<|im_start|>system\n{system}\n<|im_end|>\n"
             f"<|im_start|>user\n{user}\n<|im_end|>\n"
             "<|im_start|>assistant\n")
        s = ["<|im_end|>", "<|im_start|>"]

    elif fmt.startswith("Llama 3"):
        p = (f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n"
             f"{system}<|eot_id|>"
             f"<|start_header_id|>user<|end_header_id|>\n\n{user}<|eot_id|>"
             "<|start_header_id|>assistant<|end_header_id|>\n\n")
        s = ["<|eot_id|>"]

    elif fmt.startswith("Mistral"):
        p = f"[INST] {system}\n\n{user} [/INST]"
        s = ["[INST]", "</s>"]

    elif fmt.startswith("Gemma"):
        p = (f"<start_of_turn>user\n{system}\n\n{user}<end_of_turn>\n"
             "<start_of_turn>model\n")
        s = ["<end_of_turn>"]

    elif fmt.startswith("Zephyr"):
        p = (f"<|system|>\n{system}</s>\n"
             f"<|user|>\n{user}</s>\n<|assistant|>\n")
        s = ["</s>", "<|user|>"]

    elif fmt.startswith("Alpaca"):
        p = (f"### System:\n{system}\n\n"
             f"### Instruction:\n{user}\n\n### Response:\n")
        s = ["### Instruction:"]

    elif fmt.startswith("DeepSeek"):
        p = (f"<|begin_of_sentence|>System: {system}\n\n"
             f"User: {user}\n\nAssistant:")
        s = ["User:", "<|end_of_sentence|>"]

    elif fmt.startswith("Command-R"):
        p = (f"<BOS_TOKEN><|START_OF_TURN_TOKEN|><|SYSTEM_TOKEN|>{system}"
             f"<|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|USER_TOKEN|>{user}"
             "<|END_OF_TURN_TOKEN|><|START_OF_TURN_TOKEN|><|CHATBOT_TOKEN|>")
        s = ["<|END_OF_TURN_TOKEN|>"]

    elif fmt.startswith("Harmony"):
        # gpt-oss (OpenAI Harmony format).  The assistant turn is primed with
        # the "final" channel header so the model answers directly instead of
        # first emitting an "analysis" (chain-of-thought) message.
        # "Reasoning: low" reinforces the same intent; app instructions go in
        # the developer role per the spec.
        #
        # The priming and the stop list below are LOAD-BEARING TOGETHER — do not
        # change one without the other.  Measured against gpt-oss-20b-MXFP4:
        #   * <|end|> is NOT an end-of-generation token in this GGUF (only
        #     <|return|>, <|call|> and <|endoftext|> are), so nothing stops the
        #     model at a message boundary except the "<|end|>" *string* stop.
        #   * <|start|>, <|end|>, <|channel|> and <|message|> are user-defined
        #     rather than control tokens, so llama-cpp detokenises them to their
        #     literal text instead of dropping them.
        # Consequence: drop the "final" priming and the model opens an analysis
        # message, whose closing <|end|> then trips the string stop — so the call
        # returns pure chain-of-thought and the answer never arrives.  Removing
        # "<|end|>" from the stop list instead lets generation run on into a
        # second assistant turn.  Any change here needs both sides reworked plus
        # a re-test of _strip_channels().
        p = ("<|start|>system<|message|>Reasoning: low\n\n"
             "# Valid channels: analysis, commentary, final. "
             "Channel must be included for every message.<|end|>"
             f"<|start|>developer<|message|># Instructions\n\n{system}<|end|>"
             f"<|start|>user<|message|>{user}<|end|>"
             "<|start|>assistant<|channel|>final<|message|>")
        # <|return|> is a real EOG token; <|end|> and <|start|> are not, so on
        # BOTH the in-process and llama-server paths they only take effect as
        # string stops (see the note above).
        s = ["<|return|>", "<|end|>", "<|start|>"]

    else:  # Plain / fallback
        p = f"System: {system}\n\nUser: {user}\n\nAssistant:"
        s = ["\nUser:", "\nSystem:"]

    return p, s


def _build_chat_prompt(fmt: str, system: str, history: list) -> tuple:
    """
    Build a multi-turn chat prompt from a conversation history.

    `history` is a list of dicts: [{"role": "user"|"assistant", "content": "..."}, ...]
    The last entry must be a user message; the prompt ends with the assistant turn open.
    Returns (prompt_str, stop_token_list).
    """
    if fmt.startswith("ChatML"):
        p = f"<|im_start|>system\n{system}\n<|im_end|>\n"
        for turn in history:
            role = "user" if turn["role"] == "user" else "assistant"
            p += f"<|im_start|>{role}\n{turn['content']}\n<|im_end|>\n"
        p += "<|im_start|>assistant\n"
        s = ["<|im_end|>", "<|im_start|>"]

    elif fmt.startswith("Llama 3"):
        p = (f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n"
             f"{system}<|eot_id|>")
        for turn in history:
            role = "user" if turn["role"] == "user" else "assistant"
            p += f"<|start_header_id|>{role}<|end_header_id|>\n\n{turn['content']}<|eot_id|>"
        p += "<|start_header_id|>assistant<|end_header_id|>\n\n"
        s = ["<|eot_id|>"]

    elif fmt.startswith("Mistral"):
        # Mistral interleaves [INST]...[/INST]...
        p = ""
        for i, turn in enumerate(history):
            if turn["role"] == "user":
                if i == 0:
                    p += f"[INST] {system}\n\n{turn['content']} [/INST]"
                else:
                    p += f"[INST] {turn['content']} [/INST]"
            else:
                p += f" {turn['content']} </s>"
        s = ["[INST]", "</s>"]

    elif fmt.startswith("Gemma"):
        p = ""
        for i, turn in enumerate(history):
            if turn["role"] == "user":
                content = (f"{system}\n\n{turn['content']}" if i == 0
                           else turn["content"])
                p += f"<start_of_turn>user\n{content}<end_of_turn>\n"
            else:
                p += f"<start_of_turn>model\n{turn['content']}<end_of_turn>\n"
        p += "<start_of_turn>model\n"
        s = ["<end_of_turn>"]

    elif fmt.startswith("Zephyr"):
        p = f"<|system|>\n{system}</s>\n"
        for turn in history:
            if turn["role"] == "user":
                p += f"<|user|>\n{turn['content']}</s>\n"
            else:
                p += f"<|assistant|>\n{turn['content']}</s>\n"
        p += "<|assistant|>\n"
        s = ["</s>", "<|user|>"]

    elif fmt.startswith("DeepSeek"):
        p = f"<|begin_of_sentence|>System: {system}\n\n"
        for turn in history:
            if turn["role"] == "user":
                p += f"User: {turn['content']}\n\n"
            else:
                p += f"Assistant: {turn['content']}\n\n"
        p += "Assistant:"
        s = ["User:", "<|end_of_sentence|>"]

    elif fmt.startswith("Command-R"):
        p = f"<BOS_TOKEN><|START_OF_TURN_TOKEN|><|SYSTEM_TOKEN|>{system}<|END_OF_TURN_TOKEN|>"
        for turn in history:
            if turn["role"] == "user":
                p += f"<|START_OF_TURN_TOKEN|><|USER_TOKEN|>{turn['content']}<|END_OF_TURN_TOKEN|>"
            else:
                p += f"<|START_OF_TURN_TOKEN|><|CHATBOT_TOKEN|>{turn['content']}<|END_OF_TURN_TOKEN|>"
        p += "<|START_OF_TURN_TOKEN|><|CHATBOT_TOKEN|>"
        s = ["<|END_OF_TURN_TOKEN|>"]

    elif fmt.startswith("Harmony"):
        # See _build_prompt: the trailing "final" channel header suppresses
        # the chain-of-thought message; past assistant turns are replayed as
        # final-channel messages.
        p = ("<|start|>system<|message|>Reasoning: low\n\n"
             "# Valid channels: analysis, commentary, final. "
             "Channel must be included for every message.<|end|>"
             f"<|start|>developer<|message|># Instructions\n\n{system}<|end|>")
        for turn in history:
            if turn["role"] == "user":
                p += f"<|start|>user<|message|>{turn['content']}<|end|>"
            else:
                p += f"<|start|>assistant<|channel|>final<|message|>{turn['content']}<|end|>"
        p += "<|start|>assistant<|channel|>final<|message|>"
        s = ["<|return|>", "<|end|>", "<|start|>"]

    else:  # Plain / fallback
        p = f"System: {system}\n\n"
        for turn in history:
            role = "User" if turn["role"] == "user" else "Assistant"
            p += f"{role}: {turn['content']}\n\n"
        p += "Assistant:"
        s = ["\nUser:", "\nSystem:"]

    return p, s
