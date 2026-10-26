from localization.contracts import LocalizationInput


def make_input(aweme_id: str = "7001", generation: int = 1) -> LocalizationInput:
    return LocalizationInput.model_validate(
        {
            "aweme_id": aweme_id,
            "channel_id": "chan_1",
            "generation": generation,
            "video_key": f"source/{aweme_id}.mp4",
            "target_lang": "vi",
            "voice": {"provider": "voicestudio", "voice_id": "voice-a", "settings": {}},
        }
    )
