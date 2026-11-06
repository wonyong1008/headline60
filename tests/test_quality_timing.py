"""Measured audio timing and user-selected pacing, without AI inference."""
import array
import math
import wave

import pytest

from app.renderer.storyboard import natural_timeline, pause_points, scene_captions
from app.script.models import CaptionEdit, Scene


def test_user_speed_changes_visual_timeline_without_forcing_target_seconds():
    frames, speed = natural_timeline([2.36, 3.54], 1.18)
    assert frames == [60, 90] and speed == 1.18


@pytest.mark.parametrize("speed", [.8, float('nan'), 1.5])
def test_unbounded_or_slowed_down_pacing_is_rejected(speed):
    with pytest.raises(ValueError):
        natural_timeline([2], speed)


def test_edited_captions_scale_from_the_speed_they_were_reviewed_at():
    scene = Scene(headline='자막 속도 검증', narration='설정한 속도와 자막 시간을 함께 맞춥니다.',
                  caption_reference_speed=1, caption_overrides=[CaptionEdit(text='검토한 자막', start=0, end=2.36)])
    cues = scene_captions(scene, 2, speed=1.18)
    assert cues[0]['end'] == pytest.approx(2)


def test_real_quiet_interval_can_anchor_a_caption_boundary(tmp_path):
    rate = 48000
    samples = array.array('h')
    for duration, active in [(.25, True), (.20, False), (.25, True)]:
        samples.extend(round(5000 * math.sin(2 * math.pi * 400 * i / rate)) if active else 0
                       for i in range(round(duration * rate)))
    audio = tmp_path / 'speech.wav'
    with wave.open(str(audio), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    assert any(abs(point - .35) < .03 for point in pause_points(audio))
    scene = Scene(headline='호흡과 구절 경계', narration='앞 문장을 확인합니다. 뒤 문장을 확인합니다.')
    cues = scene_captions(scene, .7, audio)
    assert len(cues) == 2 and abs(cues[0]['end'] - .35) < .03
    assert cues[-1]['end'] == .7
