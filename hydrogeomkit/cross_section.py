"""
Cross-section geometry from channel top width and depth.

Three functions, each for single values or whole columns:

- ``xsec_area``: cross-sectional area [m²]
- ``wetted_perimeter``: wetted perimeter [m]
- ``hydraulic_radius``: area / wetted perimeter [m]

using one of three symmetric channel shapes (both banks identical, with the deepest
point at the channel center):

**Dingman's r** (``shape="r"``; Dingman & Afshari, 2018): a symmetric power-law bank,
``z = D (2x/W)^r``, with ``r >= 1`` (1 = triangle, 2 = parabola, large r -> rectangle).

- Area (exact): ``A = r / (r + 1) * W * D``
- Wetted perimeter (exact arc length):
  ``P = W * integral_0^1 sqrt(1 + (r*beta)^2 * u^(2r-2)) du``, with ``beta = 2D / W``,
  evaluated with adaptive, error-controlled integration (``scipy.integrate.quad_vec``).

**Trapezoidal** (``shape="a"``): bottom width ``b = a * W``, with ``0 <= a <= 1``
(0 = triangle, 1 = rectangle), top width ``W``, depth ``D``.

- Area (exact): ``A = (b + W) / 2 * D``
- Wetted perimeter (exact): ``P = b + 2 * sqrt(D^2 + ((W - b) / 2)^2)``

**Side slope** (``shape="z"``): banks with slope ``z:1`` (z horizontal to 1 vertical),
``z >= 0`` (0 = rectangle); bottom width ``b = W - 2zD``, which must be >= 0.

- Area (exact): ``A = (b + zD) * D = (W - zD) * D``
- Wetted perimeter (exact): ``P = b + 2D * sqrt(1 + z^2)``

``width``, ``depth``, and ``r``, ``a``, or ``z`` can each be a single number, a list, an array,
or a pandas column, so r or a can differ from row to row.

    >>> from hydrogeomkit import get_channel_geometry, xsec_area, wetted_perimeter, hydraulic_radius
    >>> df = get_channel_geometry(huc8="03160112")
    >>> df["bnk_xsce_A"] = xsec_area(df["bnk_width"], df["bnk_depth"], shape="r", r=2)
    >>> df["bnk_wet_p"] = wetted_perimeter(df["bnk_width"], df["bnk_depth"], shape="r", r=2)
    >>> df["bnk_hyd_R"] = hydraulic_radius(df["bnk_width"], df["bnk_depth"], shape="r", r=2)
"""
from __future__ import annotations

import warnings
from typing import Iterable, Union

import numpy as np
import pandas as pd

Number = Union[int, float]
Param = Union[Number, str, Iterable[Number], None]

_SHAPES = {"r": "r", "dingman": "r", "dingmanr": "r", "power": "r",
           "z": "z", "sideslope": "z", "side_slope": "z", "slope": "z",
           "a": "a", "trapezoid": "a", "trapezoidal": "a"}


# ---------------------------------------------------------------- the three quantities
def xsec_area(width, depth, shape: str = "r", r: Param = None, a: Param = None,
              z: Param = None):
    """Cross-sectional area [m²].

    ``width`` (top width) and ``depth`` in meters. ``width``, ``depth``, and ``r`` or
    ``a`` can each be a single number, a list, an array, or a pandas column (so r or a
    can differ per row). A pandas column in gives a pandas column out.
    ``shape="r"``: Dingman's r, ``A = r / (r + 1) * W * D``.
    ``shape="a"``: trapezoid with bottom width ``a * W``, ``A = (a*W + W) / 2 * D``.
    ``shape="z"``: trapezoid with side slope ``z:1`` (horizontal:vertical),
    ``A = (W - z*D) * D``.
    Missing, zero, or negative width or depth give NaN.

    >>> xsec_area(width=18.19, depth=1.23, shape="r", r=2)
    """
    s, w, d, p, ok = _prepare(width, depth, shape, r, a, z)
    A = np.full(w.shape, np.nan)
    if ok.any():
        A[ok] = _area(s, w[ok], d[ok], p[ok])
    return _out(A, width, depth)


def wetted_perimeter(width, depth, shape: str = "r", r: Param = None, a: Param = None,
                     z: Param = None):
    """Wetted perimeter [m]. Takes the same inputs as ``xsec_area``.

    ``shape="r"``: Dingman's r, the exact arc length of the bed
    ``P = W * integral_0^1 sqrt(1 + (r*beta)^2 * u^(2r-2)) du``, ``beta = 2D / W``
    (adaptive integration). ``shape="a"``: trapezoid,
    ``P = b + 2 * sqrt(D^2 + ((W - b) / 2)^2)`` with ``b = a * W``. ``shape="z"``:
    trapezoid with side slope z:1, ``P = b + 2 * D * sqrt(1 + z^2)`` with
    ``b = W - 2 * z * D``.

    >>> wetted_perimeter(width=18.19, depth=1.23, shape="r", r=2)
    """
    s, w, d, p, ok = _prepare(width, depth, shape, r, a, z)
    P = np.full(w.shape, np.nan)
    if ok.any():
        P[ok] = _perimeter(s, w[ok], d[ok], p[ok])
    return _out(P, width, depth)


def hydraulic_radius(width, depth, shape: str = "r", r: Param = None, a: Param = None,
                     z: Param = None):
    """Hydraulic radius [m]: cross-sectional area divided by wetted perimeter.
    Takes the same inputs as ``xsec_area``.

    >>> hydraulic_radius(width=18.19, depth=1.23, shape="r", r=2)
    """
    s, w, d, p, ok = _prepare(width, depth, shape, r, a, z)
    R = np.full(w.shape, np.nan)
    if ok.any():
        R[ok] = _area(s, w[ok], d[ok], p[ok]) / _perimeter(s, w[ok], d[ok], p[ok])
    return _out(R, width, depth)


# ---------------------------------------------------------------- shape formulas
def _area(s, w, d, p):
    if s == "r":                                   # Dingman's r
        return p / (p + 1.0) * w * d
    if s == "z":                                   # trapezoid, side slope z:1
        return (w - p * d) * d
    return 0.5 * (p * w + w) * d                   # trapezoid, bottom width b = a*W


def _perimeter(s, w, d, p):
    if s == "r":                                   # Dingman's r: exact arc length
        from scipy.integrate import quad_vec
        k2 = (p * 2.0 * d / w) ** 2
        val, _ = quad_vec(lambda u: np.sqrt(1.0 + k2 * np.power(u, 2.0 * p - 2.0)),
                          0.0, 1.0, epsabs=1e-13, epsrel=1e-13)
        return w * val
    if s == "z":                                   # trapezoid, side slope z:1
        return (w - 2.0 * p * d) + 2.0 * d * np.sqrt(1.0 + p ** 2)
    b = p * w                                      # trapezoid, bottom width b = a*W
    return b + 2.0 * np.sqrt(d ** 2 + ((w - b) / 2.0) ** 2)


def _prepare(width, depth, shape, r, a, z):
    """Check the inputs; return shape code, arrays, shape parameter, and valid rows."""
    s = _shape(shape)
    param = {"r": r, "a": a, "z": z}[s]
    if param is None:
        raise ValueError(f'{s} is required when shape="{s}"')
    w, d, p = np.broadcast_arrays(np.atleast_1d(np.asarray(width, float)),
                                  np.atleast_1d(np.asarray(depth, float)),
                                  np.atleast_1d(np.asarray(param, float)))
    fin = np.isfinite(p)
    if s == "r" and np.any(fin & (p < 1)):
        raise ValueError("Dingman's r must be >= 1")
    if s == "a" and np.any(fin & ((p < 0) | (p > 1))):
        raise ValueError("a (bottom width / top width) must be between 0 and 1")
    if s == "z" and np.any(fin & (p < 0)):
        raise ValueError("z (side slope, horizontal:vertical) must be >= 0")
    ok = np.isfinite(w) & np.isfinite(d) & fin & (w > 0) & (d > 0)
    if s == "z":
        # the side slopes must fit inside the top width: bottom width W - 2zD >= 0
        too_steep = ok & (w - 2.0 * p * d < -1e-12)
        if too_steep.any():
            warnings.warn(f"{int(too_steep.sum())} row(s) have a top width smaller than "
                          "2 * z * depth, so side slope z cannot fit; their results are NaN",
                          stacklevel=3)
            ok &= ~too_steep
    return s, w, d, p, ok


def _out(values, width, depth):
    """Plain number in -> plain number out; pandas column in -> pandas column out."""
    if np.ndim(width) == 0 and np.ndim(depth) == 0:
        return float(values[0])
    for x in (width, depth):
        if isinstance(x, pd.Series):
            return pd.Series(values, index=x.index)
    return values


# ---------------------------------------------------------------- helpers
def _shape(shape: str) -> str:
    key = str(shape).strip().lower().replace("'", "").replace(" ", "")
    if key not in _SHAPES:
        raise ValueError('shape must be "r" (Dingman\'s r), "a" (bottom/top width ratio), '
                         'or "z" (side slope z:1)')
    return _SHAPES[key]
