"""Checks the cross-section functions against textbook shapes. Run with: pytest"""
import numpy as np
import pandas as pd
import pytest

from hydrogeomkit import hydraulic_radius, wetted_perimeter, xsec_area

W = np.array([10.0, 50.0, 120.0])
D = np.array([2.0, 3.0, 4.5])


def test_triangle_both_shapes():
    tri_A, tri_P = W * D / 2, 2 * np.sqrt(D**2 + (W / 2) ** 2)
    for shape, kw in [("r", {"r": 1}), ("a", {"a": 0})]:
        assert np.allclose(xsec_area(W, D, shape, **kw), tri_A)
        assert np.allclose(wetted_perimeter(W, D, shape, **kw), tri_P)
        assert np.allclose(hydraulic_radius(W, D, shape, **kw), tri_A / tri_P)


def test_rectangle():
    assert np.allclose(xsec_area(W, D, "a", a=1), W * D)
    assert np.allclose(wetted_perimeter(W, D, "a", a=1), W + 2 * D)


def test_parabola_matches_closed_form():
    L = W / 2
    k = D / L**2
    exact = 2 * ((L / 2) * np.sqrt(1 + 4 * k * k * L * L) + np.arcsinh(2 * k * L) / (4 * k))
    assert np.allclose(xsec_area(W, D, "r", r=2), 2 / 3 * W * D)
    assert np.allclose(wetted_perimeter(W, D, "r", r=2), exact, rtol=1e-10)


def test_single_numbers_and_missing_values():
    A = xsec_area(width=10.0, depth=2.0, shape="a", a=1)
    assert isinstance(A, float) and A == 20.0
    assert np.isnan(wetted_perimeter(width=0.0, depth=2.0, shape="r", r=2))
    R = hydraulic_radius([10.0, np.nan], [2.0, 1.0], "a", a=1)
    assert np.isclose(R[0], 20.0 / 14.0) and np.isnan(R[1])


def test_r_and_a_can_change_per_row():
    r = np.array([1.0, 2.0, 3.5])
    together = wetted_perimeter(W, D, "r", r=r)
    one_by_one = [wetted_perimeter(w, d, "r", r=x) for w, d, x in zip(W, D, r)]
    assert np.allclose(together, one_by_one)
    assert np.isnan(xsec_area(W, D, "r", r=[1.0, np.nan, 2.0])[1])


def test_pandas_columns_keep_their_index():
    df = pd.DataFrame({"w": W, "d": D, "r": [1.0, 2.0, 3.0]}, index=[10, 20, 30])
    A = xsec_area(df["w"], df["d"], "r", r=df["r"])
    assert isinstance(A, pd.Series) and list(A.index) == [10, 20, 30]


def test_side_slope_z():
    assert np.allclose(xsec_area(W, D, "z", z=0), W * D)                 # rectangle
    assert np.allclose(wetted_perimeter(W, D, "z", z=0), W + 2 * D)
    zt = W / (2 * D)                                                     # triangle
    assert np.allclose(wetted_perimeter(W, D, "z", z=zt), wetted_perimeter(W, D, "r", r=1))
    z = 1.5                                                              # same as a = b / W
    a = (W - 2 * z * D) / W
    assert np.allclose(xsec_area(W, D, "z", z=z), xsec_area(W, D, "a", a=a))
    assert np.allclose(wetted_perimeter(W, D, "z", z=z), wetted_perimeter(W, D, "a", a=a))


def test_side_slope_too_steep_warns():
    with pytest.warns(UserWarning):
        A = xsec_area([10.0, 4.0], [2.0, 2.0], "z", z=2)
    assert A[0] == 12.0 and np.isnan(A[1])


@pytest.mark.parametrize("kw", [{"shape": "r"}, {"shape": "r", "r": 0.5},
                                {"shape": "a", "a": 1.5}, {"shape": "z", "z": -1},
                                {"shape": "z"}, {"shape": "x", "r": 2}])
def test_bad_input_raises(kw):
    with pytest.raises(ValueError):
        xsec_area(W, D, **kw)
