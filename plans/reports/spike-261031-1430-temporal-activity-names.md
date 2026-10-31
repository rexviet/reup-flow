# Spike: tên activity thật trong lịch sử Temporal

Nguồn: lịch sử của 12 workflow `VideoLocalizationWorkflow` đã hoàn tất, đọc bằng `temporal workflow show` trong image admin-tools.
Workflow id có dạng `video-{aweme_id}-g{generation}`.

| Activity | Queue | Agent trên giao diện |
|---|---|---|
| ingest, assemble_audio, layout_captions | media | media |
| render | render | render |
| publish | io | publish |
| transcribe | stt | stt |
| voice_rate, voice_profile, plan_synthesis, synthesize_segments | tts / io | tts |
| check_voice | voice_check | voice_check |
| classify_video | llm | ad_classifier |
| detect_subtitles | io | subtitle_detector |
| localizer_context, assemble_script, `agent__localizer_single__model_request` | io / llm | localizer |
| dubber_setup, measure_fit, segment_room, write_dubbed_script, commit_dub, `agent__dubber_code__model_request`, `agent__dubber_code__toolset__<agent>__call_tool` | io / llm / tts | dubber |
| review_before_render, review_render, review_dub | llm | reviewer |

Kết luận:

- Activity của agent LLM **có** tên riêng dạng `agent__<agent>__model_request` và `agent__<agent>__toolset__<agent>__call_tool`, nên đoán trước bằng tiền tố `agent__<agent>` là được; không cần đọc payload.
- Queue không đủ để suy ra agent (activity của Dubber nằm trên cả `io`, `llm`, `tts`), nên catalog dựa vào tên.
- Cột `stage` của `localization.llm_calls` dùng cùng bộ tên (`classify_video`, `agent__dubber_code__model_request`, `review_render`, `detect_subtitles`...), nên một catalog phục vụ cả hai nguồn.
- Role Postgres của downloader là `reup` (superuser), đọc được `localization.llm_calls` và `localization.runs` mà không cần grant mới; migration cấp quyền trong kế hoạch **không cần**.
- Trạng thái `localization.runs.status` hiện có: `published`, `skipped_ad`, `skipped_no_video`, `published_original`, `needs_human`, `failed`.
