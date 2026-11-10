"""SRT is always derived from the saved caption data, never active spoken text."""


def timestamp(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def srt_document(cues) -> bytes:
    blocks = []
    for cue in sorted(cues, key=lambda c: (c.start_ms, c.id)):
        if cue.text_vi.strip():
            text = cue.text_vi.replace("\r", "").replace("\n\n", "\n")
            blocks.append(
                f"{len(blocks) + 1}\n{timestamp(cue.start_ms)} --> "
                f"{timestamp(cue.end_ms)}\n{text}\n"
            )
    return "\n".join(blocks).encode("utf-8")
