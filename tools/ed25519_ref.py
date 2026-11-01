# -*- coding: utf-8 -*-
"""Ed25519 参考实现（纯标准库，RFC 8032）。

只给「开发者工具」用（生成密钥对、签发激活码、给更新包签名）。
软件端只内置 verify（更小，且不需要私钥）。

自测：python tools/ed25519_ref.py
"""
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(D * y * y + 1)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = (x * I) % P
    if x % 2 != 0:
        x = P - x
    return x


By = 4 * _inv(5) % P
Bx = _xrecover(By)
B = (Bx % P, By % P, 1, (Bx * By) % P)          # 扩展坐标 (X, Y, Z, T)


def _add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = t1 * 2 * D * t2 % P
    dd = z1 * 2 * z2 % P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(p, e: int):
    q = (0, 1, 1, 0)
    while e > 0:
        if e & 1:
            q = _add(q, p)
        p = _add(p, p)
        e >>= 1
    return q


def _encode_point(p) -> bytes:
    x, y, z, _t = p
    zi = _inv(z)
    x, y = x * zi % P, y * zi % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode_point(s: bytes):
    if len(s) != 32:
        raise ValueError("point must be 32 bytes")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _xrecover(y)
    if (x & 1) != sign:
        x = P - x
    p = (x, y, 1, x * y % P)
    if not _on_curve(p):
        raise ValueError("point not on curve")
    return p


def _on_curve(p) -> bool:
    x, y, z, t = p
    zi = _inv(z)
    x, y = x * zi % P, y * zi % P
    return (-x * x + y * y - 1 - D * x * x * y * y) % P == 0


def _hint(*parts) -> int:
    h = _sha512(b"".join(parts))
    return int.from_bytes(h, "little") % L


def public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _encode_point(_mul(B, a))


def sign(seed: bytes, msg: bytes) -> bytes:
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:]
    A = _encode_point(_mul(B, a))
    r = _hint(prefix, msg)
    R = _encode_point(_mul(B, r))
    k = _hint(R, A, msg)
    S = (r + k * a) % L
    return R + int.to_bytes(S, 32, "little")


def verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pk) != 32 or len(sig) != 64:
        return False
    try:
        A = _decode_point(pk)
        R = _decode_point(sig[:32])
        S = int.from_bytes(sig[32:], "little")
        if S >= L:
            return False
        k = _hint(sig[:32], pk, msg)
        left = _mul(B, S)
        right = _add(R, _mul(A, k))
        return _encode_point(left) == _encode_point(right)
    except Exception:
        return False


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    # RFC 8032 §7.1 TEST 1 (空消息)
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    want_pk = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    want_sig = ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    pk = public_key(seed)
    sig = sign(seed, b"")
    ok1 = pk.hex() == want_pk
    ok2 = sig.hex() == want_sig
    ok3 = verify(pk, b"", sig)
    ok4 = not verify(pk, b"tampered", sig)
    print("公钥匹配:", ok1)
    print("签名匹配:", ok2)
    print("验签通过:", ok3)
    print("篡改拒绝:", ok4)
    raise SystemExit(0 if all([ok1, ok2, ok3, ok4]) else 1)
