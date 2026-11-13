"""부스럭(비닐 구김) 소리 합성기 — 외부 음원 없이 numpy 난수로 새로 만든다.

구성
- 손 동작(0.35~0.95초) 단위로 소리 밀도가 오르내린다. 동작 사이 0.04~0.32초 쉼.
- 동작 안에서 2~10ms짜리 짧은 '딱' 소리가 초당 수백~천여 번 겹친다(세기는 꼬리가 긴 분포).
- 비닐끼리 스치는 소리(대역 잡음)와 아주 작은 바닥 잡음을 깔아 완전 무음을 없앤다.
- 30초 단위로 이어 만들어 1시간도 반복 없이 생성한다. 경계는 꼬리를 넘겨 끊김이 없다.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import wave
from pathlib import Path

import numpy as np

SR = 48_000
CHUNK_S = 30
FIR_TAPS = 511


def build_kernels(rng: np.random.Generator, count: int = 192) -> list[np.ndarray]:
    kernels: list[np.ndarray] = []
    centers = np.empty(count)
    for i in range(count):
        length = int(rng.integers(96, 481))
        n = np.arange(length)
        tau = rng.uniform(0.25e-3, 2.2e-3) * SR
        burst = rng.standard_normal(length) * np.exp(-n / tau)
        attack = max(2, int(rng.uniform(0.05e-3, 0.3e-3) * SR))
        burst[:attack] *= np.linspace(0.0, 1.0, attack)
        center = math.exp(rng.uniform(math.log(1200.0), math.log(9000.0)))
        width = rng.uniform(0.5, 1.2)
        nfft = 1 << (2 * length).bit_length()
        freqs = np.maximum(np.fft.rfftfreq(nfft, 1.0 / SR), 1.0)
        gain = 0.25 + 0.75 * np.exp(-0.5 * (np.log2(freqs / center) / width) ** 2)
        gain /= 1.0 + (200.0 / freqs) ** 4
        gain /= 1.0 + (freqs / 11000.0) ** 4  # 11kHz 위는 줄여 날카로움·true peak 완화
        shaped = np.fft.irfft(np.fft.rfft(burst, nfft) * gain, nfft)[:length]
        fade = max(8, length // 8)
        shaped[-fade:] *= np.linspace(1.0, 0.0, fade)
        shaped /= math.sqrt(float(np.sum(shaped**2))) + 1e-12
        kernels.append(shaped.astype(np.float32))
        centers[i] = center
    order = np.argsort(centers)  # 낮은 음색 → 높은 음색
    return [kernels[i] for i in order]


def gesture_table(rng: np.random.Generator, seconds: float) -> np.ndarray:
    rows = []
    t = float(rng.uniform(0.0, 0.2))
    while t < seconds:
        dur = float(rng.uniform(0.35, 0.95))
        rows.append((t, dur, rng.uniform(500, 1800), rng.uniform(0.55, 1.0), rng.uniform(-0.35, 0.35)))
        t += dur + float(rng.uniform(0.04, 0.32))
    return np.array(rows)  # start, dur, peak_rate, loud, pan


def event_table(rng: np.random.Generator, gestures: np.ndarray, seconds: float):
    times, amps, pans = [], [], []
    for start, dur, peak, loud, pan in gestures:
        count = rng.poisson(peak * dur)
        x = rng.random(count)
        env = np.sin(np.pi * x) ** 0.8
        keep = rng.random(count) < env
        x, env = x[keep], env[keep]
        k = len(x)
        times.append(start + x * dur)
        amps.append(loud * np.sqrt(env) * np.minimum(rng.pareto(2.3, k) + 1.0, 6.0))
        pans.append(np.clip(pan + rng.normal(0.0, 0.18, k), -0.9, 0.9))
    count = rng.poisson(15 * seconds)  # 동작 사이 비닐이 펴지며 나는 작은 소리
    times.append(rng.random(count) * seconds)
    amps.append(rng.uniform(0.08, 0.25, count))
    pans.append(rng.uniform(-0.8, 0.8, count))
    t = np.concatenate(times)
    order = np.argsort(t)
    return t[order], np.concatenate(amps)[order], np.concatenate(pans)[order]


def fir(lo_hz: float, hi_hz: float, taps: int = FIR_TAPS) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2.0

    def lowpass(fc: float) -> np.ndarray:
        return 2.0 * fc / SR * np.sinc(2.0 * fc / SR * n)

    h = lowpass(hi_hz) - (lowpass(lo_hz) if lo_hz > 0 else 0.0)
    return h * np.blackman(taps)


class FilteredNoise:
    """구간을 이어 붙여도 끊기지 않는 필터 잡음(overlap-add)."""

    def __init__(self, rng: np.random.Generator, h: np.ndarray) -> None:
        self.rng = rng
        self.h = h
        self.tail = np.zeros((2, len(h) - 1))

    def next(self, n: int) -> np.ndarray:
        x = self.rng.standard_normal((2, n))
        m = n + len(self.h) - 1
        nfft = 1 << (m - 1).bit_length()
        y = np.fft.irfft(np.fft.rfft(x, nfft, axis=1) * np.fft.rfft(self.h, nfft), nfft, axis=1)[:, :m]
        y[:, : len(self.h) - 1] += self.tail
        self.tail = y[:, n:].copy()
        return y[:, :n]


def envelope(gestures: np.ndarray, t0: float, n: int) -> np.ndarray:
    env = np.zeros(n)
    t1 = t0 + n / SR
    active = gestures[(gestures[:, 0] < t1) & (gestures[:, 0] + gestures[:, 1] > t0)]
    for start, dur, _peak, loud, _pan in active:
        s = max(0, int(math.floor((start - t0) * SR)))
        e = min(n, int(math.ceil((start + dur - t0) * SR)))
        x = np.clip((np.arange(s, e) / SR + t0 - start) / dur, 0.0, 1.0)
        env[s:e] += loud * np.sin(np.pi * x) ** 0.8
    return env


def rms(x: np.ndarray) -> float:
    return math.sqrt(float(np.mean(np.square(x, dtype=np.float64))))


def soft_limit(x: np.ndarray, knee: float = 0.32, ceiling: float = 0.68) -> np.ndarray:
    """knee 아래는 그대로, 위는 ceiling(-3.35dBFS)으로 부드럽게 눌러 갑작스러운 큰 소리를 줄인다."""
    y = x.astype(np.float64)
    ax = np.abs(y)
    over = ax > knee
    span = ceiling - knee
    y[over] = np.sign(y[over]) * (knee + span * np.tanh((ax[over] - knee) / span))
    return y


def synthesize(out: Path, seconds: float, seed: int, gain_db: float) -> dict:
    started = time.monotonic()
    rng = np.random.default_rng(seed)
    kernels = build_kernels(rng)
    maxlen = max(len(k) for k in kernels)
    gestures = gesture_table(rng, seconds)
    t, a, p = event_table(rng, gestures, seconds)
    idx = rng.integers(0, len(kernels), len(t))
    big = a > 2.5  # 큰 소리는 낮고 두꺼운 음색
    idx[big] = rng.integers(0, int(len(kernels) * 0.4), int(big.sum()))
    friction = FilteredNoise(rng, fir(1500.0, 7000.0, taps=127))
    floor = FilteredNoise(rng, fir(0.0, 3000.0, taps=63))
    carry = np.zeros((2, maxlen), np.float32)
    total = int(round(seconds * SR))
    gains = None
    peak = 0.0
    sumsq = 0.0
    limited = 0
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        for c0 in range(0, total, CHUNK_S * SR):
            n = min(CHUNK_S * SR, total - c0)
            t0, t1 = c0 / SR, (c0 + n) / SR
            lo, hi = np.searchsorted(t, [t0, t1])
            crinkle = np.zeros((2, n + maxlen + 1), np.float32)
            starts = np.round((t[lo:hi] - t0) * SR).astype(np.int64)
            theta = (p[lo:hi] + 1.0) * (math.pi / 4.0)
            gl = (a[lo:hi] * np.cos(theta)).astype(np.float32)
            gr = (a[lo:hi] * np.sin(theta)).astype(np.float32)
            for j in range(hi - lo):
                k = kernels[idx[lo + j]]
                s = starts[j]
                crinkle[0, s : s + len(k)] += gl[j] * k
                crinkle[1, s : s + len(k)] += gr[j] * k
            crinkle[:, :maxlen] += carry
            carry = crinkle[:, n : n + maxlen].copy()
            crinkle = crinkle[:, :n]
            fr = friction.next(n) * envelope(gestures, t0, n)
            fl = floor.next(n)
            if gains is None:
                rc = rms(crinkle)
                gf = rc * 10 ** (-14 / 20) / rms(fr)
                gfl = rc * 10 ** (-30 / 20) / rms(fl)
                gm = 10 ** ((-23 + gain_db) / 20) / rms(crinkle + gf * fr + gfl * fl)
                gains = (gf, gfl, gm)
            gf, gfl, gm = gains
            mix = (crinkle + gf * fr + gfl * fl) * gm
            limited += int(np.count_nonzero(np.abs(mix) > 0.32))
            y = soft_limit(mix)
            peak = max(peak, float(np.max(np.abs(y))))
            sumsq += float(np.sum(np.square(y)))
            w.writeframes((np.clip(y.T, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return {
        "seconds": seconds,
        "seed": seed,
        "gain_db": gain_db,
        "sample_rate": SR,
        "gestures": int(len(gestures)),
        "events": int(len(t)),
        "events_per_second": round(len(t) / seconds, 1),
        "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 2),
        "rms_dbfs": round(10 * math.log10(max(sumsq / (2 * total), 1e-18)), 2),
        "limited_sample_ratio": round(limited / (2 * total), 6),
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--seed", type=int, default=60)
    ap.add_argument("--gain-db", type=float, default=0.0)
    args = ap.parse_args()
    stats = synthesize(args.out, args.seconds, args.seed, args.gain_db)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
