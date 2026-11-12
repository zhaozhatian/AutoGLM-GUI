"""Text normalization helpers for model-produced action parameters."""

_WRAPPING_QUOTES = {'"', "'"}


def strip_wrapping_quotes(text: str) -> str:
    """Remove one matching pair of quotes that wraps the whole string.

    Some models answer with ``do(action="Type", text="\\"hello\\"")``. The
    escaped quotes survive parsing as literal characters, so the device would
    receive ``"hello"`` including the quotes. Strip exactly one wrapping pair
    and leave everything else untouched.
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _WRAPPING_QUOTES:
        return text[1:-1]
    return text
