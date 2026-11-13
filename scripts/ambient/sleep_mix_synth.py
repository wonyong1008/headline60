"""신생아 수면용 빗소리 믹스 합성기 — 빗소리 바탕 + 빗방울 + 시냇물 + 빗길 차 지나가는 소리.

외부 음원 없이 numpy 난수로 만든다. 아기가 깨지 않게 모든 변화는 수 초~수 분에 걸쳐 천천히 일어나고,
차 소리도 멀리서 다가와 지나가는 모양(수 초에 걸쳐 커졌다 작아짐)으로만 낸다.
30초 단위로 이어 만들며 경계는 꼬리를 넘겨 끊김이 없다. 출력: WAV 또는 표준출력(s16le, ffmpeg 파이프).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import wave
from pathlib import Path

import numpy as np

SR = 48_000
CHUNK_S = 30


def db(x: float) -> float:
    return 10 ** (x / 20)


def fir_from_response(gain_fn, taps: int = 4095) -> np.ndarray:
    nfft = 1 << 16
    f = np.maximum(np.fft.rfftfreq(nfft, 1 / SR), 1.0)
    h = np.roll(np.fft.irfft(gain_fn(f), nfft), taps // 2)[:taps] * np.blackman(taps)
    return h / math.sqrt(float(np.sum(h**2)))  # 백색잡음 입력 → RMS 1


def hp(f, fc, order=2):
    return 1 / np.sqrt(1 + (fc / f) ** (2 * order))


def lp(f, fc, order=2):
    return 1 / np.sqrt(1 + (f / fc) ** (2 * order))


def bump(f, center, octaves):
    return np.exp(-0.5 * (np.log2(f / center) / octaves) ** 2)


class FilteredNoise:
    """구간을 이어 붙여도 끊기지 않는 필터 잡음(overlap-add)."""

    def __init__(self, rng, h, channels=2):
        self.rng, self.h, self.ch = rng, h, channels
        self.tail = np.zeros((channels, len(h) - 1))

    def next(self, n: int) -> np.ndarray:
        x = self.rng.standard_normal((self.ch, n))
        m = n + len(self.h) - 1
        nfft = 1 << (m - 1).bit_length()
        y = np.fft.irfft(np.fft.rfft(x, nfft, axis=1) * np.fft.rfft(self.h, nfft), nfft, axis=1)[:, :m]
        y[:, : len(self.h) - 1] += self.tail
        self.tail = y[:, n:].copy()
        return y[:, :n]


def shaped_burst(rng, length, tau_s, center, width, lo=300.0, hi=9000.0):
    n = np.arange(length)
    x = rng.standard_normal(length) * np.exp(-n / (tau_s * SR))
    nfft = 1 << (2 * length).bit_length()
    f = np.maximum(np.fft.rfftfreq(nfft, 1 / SR), 1.0)
    g = (0.2 + 0.8 * bump(f, center, width)) * hp(f, lo) * lp(f, hi)
    y = np.fft.irfft(np.fft.rfft(x, nfft) * g, nfft)[:length]
    y[-max(8, length // 8):] *= np.linspace(1, 0, max(8, length // 8))
    return y


def chirp_drop(rng, fmin, fmax, tau_range, xi_range, click=0.25):
    """물방울/거품 소리(감쇠하며 음이 살짝 올라가는 사인파)."""
    f0 = math.exp(rng.uniform(math.log(fmin), math.log(fmax)))
    tau = rng.uniform(*tau_range) * SR
    xi = rng.uniform(*xi_range)
    t = np.arange(int(5 * tau))
    phase = 2 * np.pi * np.cumsum(f0 * (1 + xi * t / tau)) / SR + rng.uniform(0, 2 * np.pi)
    env = np.exp(-t / tau)
    a = max(2, int(0.0006 * SR))
    env[:a] *= np.linspace(0, 1, a)
    k = env * np.sin(phase)
    if click:
        c = min(len(k), int(0.0004 * SR))
        k[:c] += click * rng.standard_normal(c) * np.linspace(1, 0, c)
    return k


def normalize(k):
    return (k / (math.sqrt(float(np.sum(k**2))) + 1e-12)).astype(np.float32)


def drop_bank(rng, count=256):
    bank = []
    for _ in range(count):
        if rng.random() < 0.6:
            k = shaped_burst(rng, int(rng.integers(96, 300)), rng.uniform(0.4e-3, 1.5e-3),
                             math.exp(rng.uniform(math.log(2000), math.log(6000))), rng.uniform(0.7, 1.2))
            bank.append(normalize(k))
        else:
            bank.append(normalize(chirp_drop(rng, 900, 3500, (2.5e-3, 10e-3), (0.05, 0.25))) * 0.7)
    return bank


def bubble_bank(rng, count=192):
    return [normalize(chirp_drop(rng, 250, 1400, (6e-3, 30e-3), (0.05, 0.3), click=0.1)) for _ in range(count)]


def smooth_control(rng, total_s, step_s, sigma_steps, grid_s):
    """느리게 변하는 무작위 곡선(평균0, 표준편차≈1)을 grid_s 간격으로 반환."""
    pts = rng.standard_normal(int(total_s / step_s) + 3)
    xs = np.arange(len(pts)) * step_s
    grid = np.arange(0, total_s + grid_s, grid_s)
    v = np.interp(grid, xs, pts)
    w = max(1, int(sigma_steps * step_s / grid_s))
    k = np.hanning(2 * w + 1)
    v = np.convolve(np.pad(v, w, mode="edge"), k / k.sum(), mode="valid")
    return grid, v / (np.std(v) + 1e-9)


def render_events(rng, out, rate_env, rate_max, bank, amp_alpha, amp_cap, amp_scale, pan_fn):
    """rate_env(0~1, 샘플 단위)에 비례하는 빈도로 커널을 흩뿌린다. out은 (2, n+maxlen)."""
    n = len(rate_env)
    cand = rng.poisson(rate_max * n / SR)
    pos = rng.integers(0, n, cand)
    pos = pos[rng.random(cand) < rate_env[pos]]
    k = len(pos)
    amps = np.minimum(rng.pareto(amp_alpha, k) + 1, amp_cap) * amp_scale
    theta = (pan_fn(k) + 1) * (math.pi / 4)
    gl, gr = (amps * np.cos(theta)).astype(np.float32), (amps * np.sin(theta)).astype(np.float32)
    idx = rng.integers(0, len(bank), k)
    for j in range(k):
        ker = bank[idx[j]]
        s = pos[j]
        out[0, s : s + len(ker)] += gl[j] * ker
        out[1, s : s + len(ker)] += gr[j] * ker
    return k


def car_schedule(rng, total_s, mean_gap, forced):
    times = list(forced) if forced else []
    if not forced:
        t = 6.0
        while t < total_s - 8:
            t += max(12.0, rng.exponential(mean_gap))
            times.append(t)
    cars = []
    for tc in times:
        big = rng.random() < 0.15
        v = rng.uniform(7, 11) if big else rng.uniform(9, 17)
        cars.append({"tc": tc, "v": v, "d": rng.uniform(8, 22), "dir": rng.choice((-1, 1)),
                     "gain": rng.uniform(0.6, 1.0), "rumble": 1.0 if big else 0.55, "half": 55.0 / v})
    return cars


def soft_limit(x, knee=0.25, ceiling=0.63):
    y = x.copy()
    ax = np.abs(y)
    over = ax > knee
    span = ceiling - knee
    y[over] = np.sign(y[over]) * (knee + span * np.tanh((ax[over] - knee) / span))
    return y


def synthesize(out: str, seconds: float, seed: int, gain_db: float, forced_cars: list[float], mean_gap: float) -> dict:
    started = time.monotonic()
    rng = np.random.default_rng(seed)
    bed = FilteredNoise(rng, fir_from_response(
        lambda f: 10 ** ((-1 * np.log2(f / 1000) + 4 * bump(f, 2500, 1.0)) / 20) * hp(f, 150) * lp(f, 7000)))
    flow = FilteredNoise(rng, fir_from_response(lambda f: bump(f, 500, 1.2) * hp(f, 120) * lp(f, 2500)))
    drops, bubbles = drop_bank(rng), bubble_bank(rng)
    maxlen = max(len(k) for k in drops + bubbles) + 1
    buf_n = 60 * SR  # 차 소리용 잡음 버퍼(원형 필터 → 이어 붙여도 매끈)

    def loop_noise(gain_fn):
        x = rng.standard_normal(buf_n)
        f = np.maximum(np.fft.rfftfreq(buf_n, 1 / SR), 1.0)
        y = np.fft.irfft(np.fft.rfft(x) * gain_fn(f), buf_n)
        return (y / np.sqrt(np.mean(y**2))).astype(np.float32)

    tire_far = loop_noise(lambda f: bump(f, 600, 1.0) * hp(f, 150))
    tire_near = loop_noise(lambda f: bump(f, 2000, 1.0) * hp(f, 300) * lp(f, 7000))
    rumble = loop_noise(lambda f: bump(f, 90, 0.8) * hp(f, 30))
    cars = car_schedule(rng, seconds, mean_gap, forced_cars)
    for c in cars:
        c["off"] = int(rng.integers(0, buf_n - int(2 * c["half"] * SR) - 1))

    grid_rain, z_slow = smooth_control(rng, seconds, 30, 4, 2.0)
    _, z_gust = smooth_control(rng, seconds, 8, 1.5, 2.0)
    rain_db = np.clip(1.5 * z_slow + 0.8 * z_gust, -3, 3)
    grid_s, z_stream = smooth_control(rng, seconds, 60, 5, 5.0)
    stream_lv = 0.775 + 0.225 * np.tanh(z_stream)
    grid_am, z_am = smooth_control(rng, seconds, 0.125, 1.5, 0.125)
    flow_am = np.clip(1 + 0.3 * z_am, 0.5, 1.6)

    total = int(round(seconds * SR))
    carry = np.zeros((2, maxlen), np.float32)
    gains = None
    stats = {"drops": 0, "bubbles": 0, "cars": len(cars)}
    peak, sumsq = 0.0, 0.0
    if out == "-":
        sink, write = None, sys.stdout.buffer.write
    else:
        sink = wave.open(out, "wb")
        sink.setnchannels(2), sink.setsampwidth(2), sink.setframerate(SR)
        write = sink.writeframes
    for c0 in range(0, total, CHUNK_S * SR):
        n = min(CHUNK_S * SR, total - c0)
        t = (c0 + np.arange(n)) / SR
        rain = 10 ** (np.interp(t, grid_rain, rain_db) / 20)
        slv = np.interp(t, grid_s, stream_lv)
        ev = np.zeros((2, n + maxlen), np.float32)
        stats["drops"] += render_events(rng, ev, rain / 1.42, 180 * 1.42, drops, 2.6, 4.0, 0.5,
                                        lambda k: rng.uniform(-0.9, 0.9, k))
        drop_part = ev.copy()
        ev[:] = 0
        stats["bubbles"] += render_events(rng, ev, slv, 60, bubbles, 3.0, 3.0, 0.5,
                                          lambda k: np.clip(rng.normal(-0.35, 0.3, k), -0.95, 0.6))
        bub_part = ev
        b = bed.next(n) * rain
        fl = flow.next(n) * (slv * np.interp(t, grid_am, flow_am))
        fl[0] *= 1.25
        fl[1] *= 0.7
        car = np.zeros((2, n))
        for c in cars:
            s, e = c["tc"] - c["half"], c["tc"] + c["half"]
            a, z = max(s, c0 / SR), min(e, (c0 + n) / SR)
            if a >= z:
                continue
            i0, i1 = int(round(a * SR)) - c0, int(round(z * SR)) - c0
            tt = (c0 + np.arange(i0, i1)) / SR
            x = c["v"] * (tt - c["tc"]) * c["dir"]
            p = np.minimum(1.0, 18.0 / np.sqrt(c["d"] ** 2 + x**2))
            u = (tt - s) / (e - s)
            taper = np.minimum(1, np.minimum(u, 1 - u) / 0.25) ** 2
            j = c["off"] + ((tt - s) * SR).astype(np.int64)
            sig = (p * taper * c["gain"]) * ((1 - p**2) * tire_far[j] + p**2 * 0.8 * tire_near[j]
                                             + p**1.5 * 0.6 * c["rumble"] * rumble[j])
            th = (np.tanh(x / 15) * 0.8 + 1) * (math.pi / 4)
            car[0, i0:i1] += sig * np.cos(th)
            car[1, i0:i1] += sig * np.sin(th)
        if gains is None:
            gd = db(-8) / (math.sqrt(float(np.mean(drop_part[:, :n] ** 2))) + 1e-12)
            gb = db(-14) / (math.sqrt(float(np.mean(bub_part[:, :n] ** 2))) + 1e-12)
            gains = {"drop": gd, "bub": gb, "flow": db(-13), "car": db(5)}
            base = b + gd * drop_part[:, :n] + gb * bub_part[:, :n] + gains["flow"] * fl
            gains["master"] = db(-26 + gain_db) / math.sqrt(float(np.mean(base**2)))
        events = gains["drop"] * drop_part + gains["bub"] * bub_part
        if c0:
            events[:, :maxlen] += carry
        carry = events[:, n : n + maxlen].copy()
        mix = (b + events[:, :n] + gains["flow"] * fl + gains["car"] * car) * gains["master"]
        y = soft_limit(mix)
        peak = max(peak, float(np.max(np.abs(y))))
        sumsq += float(np.sum(y**2))
        write((np.clip(y.T, -1, 1) * 32767).astype("<i2").tobytes())
    if sink:
        sink.close()
    stats.update(seconds=seconds, seed=seed, gain_db=gain_db, peak_dbfs=round(20 * math.log10(peak), 2),
                 rms_dbfs=round(10 * math.log10(sumsq / (2 * total)), 2), elapsed_s=round(time.monotonic() - started, 1))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="WAV 경로 또는 - (표준출력 s16le)")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=10)
    ap.add_argument("--gain-db", type=float, default=0.0)
    ap.add_argument("--cars", default="", help="미리듣기용 차 통과 시각(초), 쉼표 구분")
    ap.add_argument("--mean-gap", type=float, default=45.0)
    a = ap.parse_args()
    forced = [float(x) for x in a.cars.split(",") if x]
    stats = synthesize(a.out, a.seconds, a.seed, a.gain_db, forced, a.mean_gap)
    print(json.dumps(stats, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
