"""Import only literal, stable-ID visual properties from Studio's source projection."""

import html
import re
from html.parser import HTMLParser

_RGBA = r"rgba\([0-9]{1,3}, [0-9]{1,3}, [0-9]{1,3}, (?:0(?:\.[0-9]+)?|1(?:\.0+)?)\)"

STYLE = {
    "background-image": (
        rf"linear-gradient\(-?[0-9]{{1,3}}deg, {_RGBA} [0-9]{{1,3}}%, "
        rf"{_RGBA} [0-9]{{1,3}}%\)"
    ),
    "color": r"#[0-9a-fA-F]{6}",
    "font-size": r"[1-9][0-9]{0,2}px",
    "font-family": r"[a-zA-Z0-9 ,_-]{1,100}",
    "font-weight": r"[1-9]00",
    "opacity": r"(?:0(?:\.[0-9]+)?|1(?:\.0+)?)",
    "-webkit-text-stroke": r"[0-9]{1,2}px #[0-9a-fA-F]{6}",
    "text-align": r"left|center|right",
    "bottom": r"[0-9]{1,3}(?:\.[0-9]+)?(?:px|%)",
}


def capture_visual(source: bytes, cues) -> dict:
    ids = {f"cue-{c.id}" for c in cues}
    styles = {}

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            identity = attrs.get("id")
            if identity not in ids:
                return
            if identity in styles:
                raise ValueError("duplicate stable cue ID")
            style = {}
            for declaration in attrs.get("style", "").split(";"):
                key, sep, value = declaration.partition(":")
                key, value = key.strip(), value.strip()
                if sep and key in STYLE:
                    if re.fullmatch(STYLE[key], value) is None:
                        raise ValueError("unsupported visual value")
                    style[key] = value
            styles[identity] = style

    Parser().feed(source.decode())
    return styles


def apply_visual(source: bytes, visual: dict) -> bytes:
    text = source.decode()
    for identity, style in visual.items():
        if style:
            value = html.escape(";".join(f"{k}:{v}" for k, v in style.items()), quote=True)
            text = text.replace(
                f'id="{identity}" class=', f'id="{identity}" style="{value}" class='
            )
    return text.encode()


def require_visual_only(source: bytes, baseline: bytes, cues):
    """Never discard source edits that the stable-ID importer does not understand."""
    identities = {f"cue-{cue.id}" for cue in cues}

    def normalized(document):
        tokens = []

        class Parser(HTMLParser):
            def handle_starttag(self, tag, attributes):
                attrs = dict(attributes)
                # Native Studio stamps editor IDs and serializes boolean attributes.
                # Neither changes source behavior; other attributes remain exact.
                editor_id = attrs.pop("data-hf-id", None)
                if (
                    editor_id is not None
                    and re.fullmatch(r"hf-[a-zA-Z0-9_-]{1,100}", editor_id) is None
                ):
                    raise ValueError("invalid Studio editor identity")
                for key in ("data-no-timeline", "muted", "playsinline", "controls"):
                    if key in attrs and attrs[key] in (None, ""):
                        attrs[key] = None
                if attrs.get("id") in identities:
                    remaining = []
                    for declaration in attrs.get("style", "").split(";"):
                        key, separator, value = declaration.partition(":")
                        if separator and key.strip() not in STYLE:
                            remaining.append((key.strip(), value.strip()))
                    attrs.pop("style", None)
                    if remaining:
                        attrs["style"] = str(sorted(remaining))
                tokens.append((tag, sorted(attrs.items())))

            def handle_endtag(self, tag):
                tokens.append(("end", tag))

            def handle_data(self, text):
                if text.strip():
                    tokens.append(("text", text))

        Parser().feed(document.decode())
        return tokens

    if normalized(source) != normalized(baseline):
        raise ValueError("Studio source contains edits that require an explicit import")
