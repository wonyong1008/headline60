import array
import math
import wave
from itertools import pairwise
from pathlib import Path


def inspect_audio(path: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1:
            raise ValueError("음성 검사는 모노 PCM 16bit WAV가 필요합니다.")
        rate = audio.getframerate()
        samples = array.array("h", audio.readframes(audio.getnframes()))
    if not samples:
        raise ValueError("빈 음성이 생성되었습니다.")
    peak = max(abs(x) for x in samples) / 32768
    rms = math.sqrt(sum((x / 32768)**2 for x in samples) / len(samples))
    window = max(1, rate // 50)
    levels = [math.sqrt(sum((x / 32768)**2 for x in samples[i:i + window]) / len(samples[i:i + window]))
              for i in range(0, len(samples), window)]
    voiced = [i for i, level in enumerate(levels) if level > 0.008]
    duration = len(samples) / rate
    if not voiced or rms < 0.002:
        raise ValueError("무음 또는 지나치게 작은 음성입니다. 다른 프로필을 직접 선택하세요.")
    gap = longest = 0
    for level in levels[voiced[0]:voiced[-1] + 1]:
        gap = gap + 0.02 if level <= 0.008 else 0
        longest = max(longest, gap)
    speech_fraction = len(voiced) / len(levels)
    if longest > 3 or speech_fraction < 0.12 or duration > 35:
        raise ValueError(f"비정상 음성 길이/침묵 감지: {duration:.2f}초, 발성 비율 {speech_fraction:.2f}")
    clipping = sum(abs(x) >= 32760 for x in samples) / len(samples)
    zcr = sum((a >= 0) != (b >= 0) for a, b in pairwise(samples)) / max(1, len(samples) - 1)
    if zcr > .42 and speech_fraction > .9 and duration > 1:
        raise ValueError("고주파 잡음으로 의심되는 음성이 감지되었습니다. 직접 확인한 뒤 다시 생성하세요.")
    if clipping > 0.02:
        raise ValueError("음성 클리핑이 과도합니다. 해당 출력을 사용하지 않습니다.")
    return {"duration": duration, "peak_dbfs": 20 * math.log10(max(peak, 1e-9)),
            "rms_dbfs": 20 * math.log10(max(rms, 1e-9)), "speech_fraction": speech_fraction,
            "longest_internal_silence": longest, "clipping_ratio": clipping,
            "noise_indicator_zcr": zcr,
            "trim_start": max(0, voiced[0] * .02 - .10),
            "trim_end": min(duration, (voiced[-1] + 1) * .02 + .16)}
