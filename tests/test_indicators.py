import pytest

from app.indicators.core import bollinger, ema, macd, rsi, sma


def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_seeded_with_sma_and_converges():
    out = ema([1, 2, 3, 4, 5, 6], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0
    assert out[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5
    assert out[-1] == pytest.approx(5.0)  # 6*0.5 + 4*0.5


def test_rsi_all_gains_is_100_and_flat_is_none_until_warm():
    out = rsi([float(i) for i in range(1, 20)], 14)
    assert out[13] is None
    assert out[14] == 100.0
    assert out[-1] == 100.0


def test_rsi_reference_values():
    # Classic textbook series (Wilder). Cross-checked with an independent pandas implementation.
    closes = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
        46.03,
        46.41,
        46.22,
        45.64,
    ]
    out = rsi(closes, 14)
    assert out[14] == pytest.approx(70.46, abs=0.05)
    assert out[-1] == pytest.approx(57.92, abs=0.05)


def test_macd_shapes_and_alignment():
    closes = [100 + (i % 7) * 0.5 + i * 0.1 for i in range(60)]
    line, sig, hist = macd(closes, 12, 26, 9)
    assert len(line) == len(sig) == len(hist) == 60
    assert line[24] is None and line[25] is not None
    assert sig[25 + 8 - 1] is None and sig[25 + 8] is not None
    assert hist[-1] == pytest.approx(line[-1] - sig[-1])


def test_macd_rejects_bad_periods():
    with pytest.raises(ValueError):
        macd([1.0, 2.0], 26, 12)


def test_bollinger_bands_symmetric():
    closes = [10, 11, 12, 11, 10, 11, 12, 11, 10, 11]
    up, mid, low = bollinger(closes, 5, 2.0)
    assert mid[4] == pytest.approx(10.8)
    assert up[4] - mid[4] == pytest.approx(mid[4] - low[4])
    assert up[3] is None
