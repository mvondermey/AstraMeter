import pytest

from .balancer import apply_priority_load


def settle(pv: float, house: float, car: float, pool: float = 0.0) -> tuple:
    """Idealised closed loop: the pool steps to ``pool + control_grid`` until
    the control grid is 0. Returns (pool_output, metered_grid)."""
    for _ in range(50):
        grid = house + car - pv - pool
        control = apply_priority_load(grid, pool, car)
        if abs(control) < 1e-6:
            break
        pool += control
    return pool, house + car - pv - pool


def test_no_load_is_identity():
    assert apply_priority_load(500.0, 200.0, 0.0) == 500.0
    assert apply_priority_load(-500.0, -200.0, 0.0) == -500.0


def test_surplus_after_car_unchanged():
    # Batteries charging 1 kW with the grid at 0: nothing to change.
    assert apply_priority_load(0.0, -1000.0, 1400.0) == 0.0
    assert apply_priority_load(-300.0, -1000.0, 1400.0) == -300.0


@pytest.mark.parametrize(
    ("pv", "house", "car", "start_pool", "want_pool", "want_grid"),
    [
        # Big surplus: car 1.4 kW from PV, batteries charge the rest (-2.1 kW).
        (4000, 500, 1400, 0, -2100, 0),
        # Surplus 800 W < car: batteries 0, car gets 800 PV + 600 grid.
        (1300, 500, 1400, 600, 0, 600),
        # Night: batteries cover the house only, car fully from grid.
        (0, 500, 1400, 1900, 500, 1400),
        # Night starting idle: batteries ramp to the house load only.
        (0, 500, 1400, 0, 500, 1400),
    ],
)
def test_closed_loop_car_first(pv, house, car, start_pool, want_pool, want_grid):
    pool, grid = settle(pv, house, car, start_pool)
    assert pool == pytest.approx(want_pool)
    assert grid == pytest.approx(want_grid)


def test_never_asks_pool_below_zero_for_discharge_case():
    # Pool discharging 300 W, grid +200, car 1400: demand 500 > 0 but the car
    # share exceeds it -> steer the pool to exactly 0, not into charging.
    assert apply_priority_load(200.0, 300.0, 1400.0) == -300.0
