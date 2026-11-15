#!/usr/bin/env python3
"""Offline preview in the configured worker: chunked multilingual STT, translation, TTS, render."""
import asyncio
import hashlib
import io
import json
import wave
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from localization.activities.assemble_audio import render as mix_audio
from localization.activities.ffmpeg import probe_duration_ms, run_ffmpeg
from localization.activities.layout_captions import FontMeter, build_captions
from localization.activities.render import render_file
from localization.activities.transcribe import regroup
from localization.config import load_settings
from localization.contracts import EditSpec
from localization.contracts.voice import VoiceRef
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts
from localization.storage.object_store import ObjectStore

ROOT = Path('/tmp/chunked-multilingual-video')
PREFIX = 'work/7689288513854595771/r0/'


def save(name, body):
    (ROOT / name).write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n')


def load(name):
    return json.loads((ROOT / name).read_text())


def chunks_from_vad(samples):
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    tensor = torch.frombuffer(bytearray(samples), dtype=torch.int16).float() / 32768
    speech = get_speech_timestamps(tensor, load_silero_vad(), sampling_rate=16000,
                                  max_speech_duration_s=10, min_silence_duration_ms=350)
    chunks = []
    for span in speech:
        a, b = max(0, span['start'] - 3200), min(len(tensor), span['end'] + 3200)
        if chunks and b - chunks[-1][0] <= 12 * 16000 and a - chunks[-1][1] <= 16000:
            chunks[-1][1] = b
        else:
            if chunks and a < chunks[-1][1]:
                midpoint = (a + chunks[-1][1]) // 2
                chunks[-1][1], a = midpoint, midpoint
            chunks.append([a, b])
    return chunks


async def main():
    ROOT.mkdir(exist_ok=True)
    settings = load_settings()
    store = ObjectStore.from_settings(settings)
    for name in ['source.mp4', 'vocals.wav', 'bgm.wav', 'edit-spec.json']:
        if not (ROOT / name).exists():
            store.download_file(PREFIX + name, ROOT / name, produced_by='existing-run')
    if not (ROOT / 'vocals-16k.wav').exists():
        run_ffmpeg(['-i', str(ROOT / 'vocals.wav'), '-ar', '16000', '-ac', '1',
                    '-c:a', 'pcm_s16le', str(ROOT / 'vocals-16k.wav')])
    with wave.open(str(ROOT / 'vocals-16k.wav')) as wav:
        pcm = wav.readframes(wav.getnframes())
    if not (ROOT / 'chunks.json').exists():
        save('chunks.json', chunks_from_vad(pcm))
    chunks = load('chunks.json')
    key = settings.voicestudio_api_key
    headers = {'Authorization': 'Bearer ' + key.get_secret_value()} if key else {}
    segments, languages = [], []
    async with httpx.AsyncClient(timeout=600, headers=headers) as http:
        for index, (a, b) in enumerate(chunks):
            name = f'stt-{index:03d}.json'
            if not (ROOT / name).exists():
                audio = io.BytesIO()
                with wave.open(audio, 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                    wav.writeframes(pcm[a * 2:b * 2])
                response = await http.post(settings.voicestudio_url.rstrip('/') + '/v1/audio/transcriptions',
                    data={'model': settings.stt_model, 'multilingual': 'true', 'temperature': '0',
                          'response_format': 'verbose_json', 'timestamp_granularities[]': ['word', 'segment']},
                    files={'file': ('chunk.wav', audio.getvalue(), 'audio/wav')})
                response.raise_for_status()
                save(name, response.json())
            raw = load(name)
            local = regroup(OpenAICompatStt._parse(raw))
            offset, limit = round(a / 16), round(b / 16)
            for segment in local.segments:
                start, end = offset + segment.start_ms, min(limit, offset + segment.end_ms)
                if end > start and segment.text.strip():
                    segments.append({'id': len(segments), 'start_ms': start, 'end_ms': end,
                                     'text': segment.text, 'language': raw.get('language'), 'chunk': index})
            languages.append(raw.get('language'))
            print(f'STT {index + 1}/{len(chunks)} {offset/1000:.1f}-{limit/1000:.1f}s {raw.get("language")}', flush=True)
    save('transcript.json', {'segments': segments, 'chunk_languages': languages})
    llm = AsyncOpenAI(base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key.get_secret_value(), timeout=180, max_retries=0)
    async def translate(items, instruction):
        response = await llm.chat.completions.create(model=settings.localizer_model.removeprefix('openai:'),
            messages=[{'role': 'system', 'content': instruction},
                      {'role': 'user', 'content': json.dumps(items, ensure_ascii=False)}],
            response_format={'type': 'json_object'})
        save('llm-usage-' + str(len(list(ROOT.glob('llm-usage-*')))) + '.json',
             response.usage.model_dump() if response.usage else {})
        return json.loads(response.choices[0].message.content)['lines']
    if not (ROOT / 'translation.json').exists():
        lines = await translate(segments,
            'Translate mixed Chinese/Korean dialogue into natural concise Vietnamese. Each item has its detected language and timestamps. '
            'Use context for names consistently. Do not transliterate unintelligible sounds or invent meaning. '
            'If a segment is clearly repetition/noise without recoverable meaning, return empty text_vi and an explanation in note. '
            'Keep spoken lines concise enough for the source time window (about 5 Vietnamese syllables/sec). '
            'Return JSON {"lines":[{"id":0,"text_vi":"...","note":""}]} with exactly every input id.')
        assert sorted(x['id'] for x in lines) == list(range(len(segments)))
        save('translation.json', lines)
    by_id = {x['id']: x for x in load('translation.json')}
    voice = VoiceRef(provider=settings.voice_provider, voice_id='c4bbe9c2', settings={})
    tts = OpenAICompatTts.from_settings(settings)
    placements, clips, captions, fit_report = [], [], [], []
    for i, segment in enumerate(segments):
        line = by_id[i]['text_vi'].strip()
        if not line:
            continue
        start = segment['start_ms']
        next_start = segments[i + 1]['start_ms'] if i + 1 < len(segments) else 189634
        slot = max(200, min(next_start - start, segment['end_ms'] - start + 450))
        path = ROOT / f'tts-{i:03d}.wav'
        if not path.exists():
            path.write_bytes(await tts.synthesize(line, voice))
        duration = probe_duration_ms(path)
        if duration / slot > 1.35:
            short_name = f'short-{i:03d}.json'
            if not (ROOT / short_name).exists():
                shortened = await translate([{'id': i, 'text_vi': line, 'target_seconds': slot / 1000}],
                    'Shorten this Vietnamese spoken line to fit target_seconds at about 5 syllables/sec. Preserve its meaning and names. '
                    'Return JSON {"lines":[{"id":0,"text_vi":"..."}]}, preserving the supplied id.')
                assert len(shortened) == 1 and shortened[0]['id'] == i and shortened[0]['text_vi'].strip()
                save(short_name, shortened[0])
            line = load(short_name)['text_vi'].strip()
            path = ROOT / f'tts-{i:03d}-short.wav'
            if not path.exists():
                path.write_bytes(await tts.synthesize(line, voice))
            duration = probe_duration_ms(path)
        ratio = max(1.0, duration / slot)
        if ratio > 2:
            raise RuntimeError(f'Line {i} still needs excessive speed: {ratio:.2f}')
        clips.append(path); placements.append((start, ratio))
        end = min(next_start, start + round(duration / ratio))
        captions.append((i, start, end, line))
        fit_report.append({'id': i, 'text_vi': line, 'start_ms': start, 'end_ms': end, 'speed': ratio})
        print(f'TTS {i + 1}/{len(segments)} speed={ratio:.2f}', flush=True)
    save('dub-lines.json', fit_report)
    base = EditSpec.model_validate_json((ROOT / 'edit-spec.json').read_bytes())
    audio = ROOT / 'dubbed.wav'
    if not audio.exists():
        mix_dir = ROOT / 'audio-work'; mix_dir.mkdir(exist_ok=True)
        mix_audio(bgm=ROOT / 'bgm.wav', clips=clips, placements=placements,
                  duration_ms=base.duration_ms, out=audio, work=mix_dir,
                  target=settings.loudness_lufs, peak=settings.true_peak_dbtp)
    meter = FontMeter(settings.caption_font_path)
    spec = base.model_copy(update={'captions': build_captions(captions, meter, base.caption_box),
        'cover': base.cover.model_copy(update={'only_with_captions': True}) if base.cover else None,
        'audio_sha256': hashlib.sha256(audio.read_bytes()).hexdigest()})
    (ROOT / 'preview-spec.json').write_text(spec.model_dump_json(indent=2))
    out = ROOT / 'chunked-multilingual-ngoc-huyen.mp4'
    if not out.exists():
        work = ROOT / 'render-work'; work.mkdir(exist_ok=True)
        render_file(spec, source=ROOT / 'source.mp4', audio=audio,
                    font_path=settings.caption_font_path, out=out, work_dir=work)
    print('DONE', out, out.stat().st_size, flush=True)
    await llm.close()


if __name__ == '__main__':
    asyncio.run(main())
