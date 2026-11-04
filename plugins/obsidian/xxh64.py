"""xxHash64 (seed 0), pure Python. LiveSync names chunks h:<base36 of xxh64(piece + "-" + length)> by default
(livesync-commonlib src/managers/HashManager/XXHashHashManager.ts, class XXHash64HashManager)."""

from __future__ import annotations

import struct

_M = (1 << 64) - 1
P1, P2, P3, P4, P5 = 11400714785074694791, 14029467366897019727, 1609587929392839161, 9650029242287828579, 2870177450012600261


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & _M


def _round(acc: int, inp: int) -> int:
    return (_rotl((acc + inp * P2) & _M, 31) * P1) & _M


def _merge(h: int, v: int) -> int:
    return ((h ^ _round(0, v)) * P1 + P4) & _M


def xxh64(data: bytes, seed: int = 0) -> int:
    n, i = len(data), 0
    if n >= 32:
        v = [(seed + P1 + P2) & _M, (seed + P2) & _M, seed & _M, (seed - P1) & _M]
        while i <= n - 32:
            for k in range(4):
                v[k] = _round(v[k], struct.unpack_from("<Q", data, i + 8 * k)[0])
            i += 32
        h = (_rotl(v[0], 1) + _rotl(v[1], 7) + _rotl(v[2], 12) + _rotl(v[3], 18)) & _M
        for x in v:
            h = _merge(h, x)
    else:
        h = (seed + P5) & _M
    h = (h + n) & _M
    while i <= n - 8:
        h = (_rotl(h ^ _round(0, struct.unpack_from("<Q", data, i)[0]), 27) * P1 + P4) & _M
        i += 8
    if i <= n - 4:
        h = (_rotl(h ^ (struct.unpack_from("<I", data, i)[0] * P1 & _M), 23) * P2 + P3) & _M
        i += 4
    while i < n:
        h = (_rotl(h ^ (data[i] * P5 & _M), 11) * P1) & _M
        i += 1
    h ^= h >> 33
    h = (h * P2) & _M
    h ^= h >> 29
    h = (h * P3) & _M
    h ^= h >> 32
    return h
